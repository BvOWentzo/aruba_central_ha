from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import aiohttp
import voluptuous as vol

from homeassistant.components.device_tracker import SOURCE_TYPE_ROUTER
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from datetime import timedelta

# -----------------------------
# YAML schema (platform style)
# -----------------------------
CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_CUSTOMER_ID = "customer_id"
CONF_API_BASE = "api_base"      # bv. https://apigw-eu3.central.arubanetworks.com
CONF_OAUTH_BASE = "oauth_base"  # bv. https://eu3.arubanetworks.com
CONF_SITE = "site"
CONF_GROUP = "group"
CONF_AP_SERIALS = "ap_serials"

DEFAULT_SCAN_SECONDS = 60

PLATFORM_SCHEMA = cv.PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_CLIENT_ID): cv.string,
        vol.Required(CONF_CLIENT_SECRET): cv.string,
        vol.Required(CONF_REFRESH_TOKEN): cv.string,
        vol.Optional(CONF_CUSTOMER_ID): cv.string,
        vol.Required(CONF_API_BASE): cv.string,
        vol.Required(CONF_OAUTH_BASE): cv.string,
        vol.Optional(CONF_SITE): cv.string,
        vol.Optional(CONF_GROUP): cv.string,
        vol.Optional(CONF_AP_SERIALS): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_SECONDS): cv.positive_int,
    }
)

# -----------------------------
# Mini Aruba Central client
# -----------------------------
class _Central:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_base: str,
        oauth_base: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        customer_id: Optional[str],
        log,
    ) -> None:
        self.s = session
        self.api_base = api_base.rstrip("/")
        self.oauth_base = oauth_base.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.customer_id = customer_id
        self.access_token: Optional[str] = None
        self.expiry = 0.0
        self.log = log

    async def _ensure_token(self):
        now = time.time()
        if self.access_token and now < (self.expiry - 60):
            return
        data = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
        }
        url = f"{self.oauth_base}/oauth2/token"
        async with self.s.post(url, data=data, timeout=30) as r:
            if r.status != 200:
                raise RuntimeError(f"Token refresh failed ({r.status}): {await r.text()}")
            j = await r.json()
        self.access_token = j.get("access_token")
        self.expiry = time.time() + float(j.get("expires_in", 3600))
        new_rt = j.get("refresh_token")
        if new_rt:
            self.refresh_token = new_rt
        self.log.debug("Aruba Central token refreshed")

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None):
        await self._ensure_token()
        headers = {"Authorization": f"Bearer {self.access_token}"}
        if self.customer_id:
            headers["X-Customer-ID"] = self.customer_id
        url = f"{self.api_base}{path}"
        async with self.s.get(url, headers=headers, params=params, timeout=30) as r:
            if r.status != 200:
                raise RuntimeError(f"GET {path} failed ({r.status}): {await r.text()}")
            return await r.json()

    async def list_clients(
        self,
        *,
        site: Optional[str],
        group: Optional[str],
        ap_serials: Optional[List[str]],
        limit: int = 1000,
        client_type: str = "wireless",
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": str(limit), "client_type": client_type.upper()}
        if site:
            params["site"] = site
        if group:
            params["group"] = group
        if ap_serials:
            params["access_points"] = ",".join(ap_serials)

        # try v2, fall back to v1
        try:
            j = await self._get("/monitoring/v2/clients", params=params)
            items = j.get("clients") or j.get("data") or j
            if isinstance(items, list):
                return items
        except Exception:
            pass
        j = await self._get("/monitoring/v1/clients", params=params)
        items = j.get("clients") or j.get("data") or j
        return items if isinstance(items, list) else []

# -----------------------------
# Platform setup
# -----------------------------
async def async_get_scanner(hass: HomeAssistant, config):
    # We implement entity-based tracker, not legacy DeviceScanner -> return None
    return None

async def async_setup_platform(hass: HomeAssistant, config, async_add_entities, discovery_info=None):
    import logging
    log = logging.getLogger("custom_components.aruba_central")

    scan_seconds = int(config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_SECONDS))
    session = async_get_clientsession(hass)

    api = _Central(
        session=session,
        api_base=config[CONF_API_BASE],
        oauth_base=config[CONF_OAUTH_BASE],
        client_id=config[CONF_CLIENT_ID],
        client_secret=config[CONF_CLIENT_SECRET],
        refresh_token=config[CONF_REFRESH_TOKEN],
        customer_id=config.get(CONF_CUSTOMER_ID),
        log=log,
    )

    state: Dict[str, Dict[str, Any]] = {}  # mac -> normalized client dict
    entities: Dict[str, ArubaCentralTracker] = {}  # mac -> entity

    async def _poll(now=None):
        try:
            raw = await api.list_clients(
                site=config.get(CONF_SITE),
                group=config.get(CONF_GROUP),
                ap_serials=config.get(CONF_AP_SERIALS),
                limit=1000,
                client_type="wireless",
            )
        except Exception as e:
            log.warning("Aruba Central poll failed: %s", e)
            # mark all as disconnected (optional: keep last seen)
            for ent in entities.values():
                ent.set_snapshot({"mac": ent.mac, "connected": False})
            return

        snapshot: Dict[str, Dict[str, Any]] = {}
        for c in raw:
            mac = (c.get("macaddr") or c.get("mac") or "").lower()
            if not mac:
                continue
            snapshot[mac] = {
                "mac": mac,
                "ip": c.get("ip_address") or c.get("ip") or None,
                "hostname": c.get("name") or c.get("hostname") or c.get("device_name") or mac,
                "ssid": c.get("essid") or c.get("ssid") or None,
                "ap_name": c.get("ap_name") or c.get("associated_device") or c.get("associated_ap") or None,
                "rssi": c.get("rssi") or c.get("signal") or None,
                "manufacturer": c.get("manufacturer") or None,
                "user_role": c.get("user_role") or c.get("role") or None,
                "vlan": c.get("vlan") or c.get("vlan_id") or None,
                "connected": True
                if c.get("connected") is None
                else bool(c.get("connected")),
            }

        # update/create entities
        for mac, data in snapshot.items():
            state[mac] = data
            if mac not in entities:
                ent = ArubaCentralTracker(mac, lambda m: state.get(m, {"mac": m, "connected": False}))
                entities[mac] = ent
                async_add_entities([ent], True)
            else:
                entities[mac].set_snapshot(data)

        # mark disappeared as disconnected
        vanished = set(state.keys()) - set(snapshot.keys())
        for mac in vanished:
            state[mac] = {"mac": mac, "connected": False}
            if mac in entities:
                entities[mac].set_snapshot(state[mac])

    # first poll, then schedule
    await _poll()
    async_track_time_interval(hass, _poll, timedelta(seconds=scan_seconds))

# -----------------------------
# Tracker Entity
# -----------------------------
class ArubaCentralTracker(TrackerEntity):
    _attr_source_type = SOURCE_TYPE_ROUTER
    _attr_icon = "mdi:wifi"

    def __init__(self, mac: str, resolver):
        self.mac = mac
        self._resolver = resolver
        self._attr_unique_id = f"aruba_central_{mac.replace(':','')}"
        self._last: Dict[str, Any] = {"mac": mac, "connected": False}

    def set_snapshot(self, snap: Dict[str, Any]):
        self._last = snap
        self.async_write_ha_state()

    @property
    def name(self) -> str:
        h = self._client.get("hostname")
        return h or self.mac

    @property
    def is_connected(self) -> bool:
        return bool(self._client.get("connected"))

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        c = self._client
        attrs = {
            "mac": c.get("mac"),
            "ip": c.get("ip"),
            "hostname": c.get("hostname"),
            "ssid": c.get("ssid"),
            "ap_name": c.get("ap_name"),
            "rssi": c.get("rssi"),
            "manufacturer": c.get("manufacturer"),
            "user_role": c.get("user_role"),
            "vlan": c.get("vlan"),
        }
        return {k: v for k, v in attrs.items() if v is not None}

    @property
    def _client(self) -> Dict[str, Any]:
        # prefer live resolver; fall back to last snapshot
        return self._resolver(self.mac) or self._last

    async def async_update(self) -> None:
        # Coordinator-loos; updates komen via scheduler, hier niets te doen.
        return
