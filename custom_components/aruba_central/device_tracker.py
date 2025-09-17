# /config/custom_components/aruba_central/device_tracker.py
from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import voluptuous as vol

from homeassistant.components.device_tracker import TrackerEntity
from homeassistant.components.device_tracker.const import SOURCE_TYPE_ROUTER
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)
_LOGGER.warning("aruba_central(TrackerEntity): module import OK")

# Laat HA ons (eventueel) elke ~60s aanroepen, maar we gebruiken zelf een scheduler op jouw scan_interval
SCAN_INTERVAL = timedelta(seconds=60)

CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_CUSTOMER_ID = "customer_id"
CONF_API_BASE = "api_base"
CONF_OAUTH_BASE = "oauth_base"
CONF_GROUP = "group"
CONF_SITE = "site"
CONF_CLIENT_TYPE = "client_type"

DEFAULT_CLIENT_TYPE = "WIRELESS"
DEFAULT_SCAN_INTERVAL_S = 60

PLATFORM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLIENT_ID): cv.string,
        vol.Required(CONF_CLIENT_SECRET): cv.string,
        vol.Required(CONF_REFRESH_TOKEN): cv.string,
        vol.Required(CONF_API_BASE): cv.url,
        vol.Optional(CONF_OAUTH_BASE): cv.url,
        vol.Optional(CONF_CUSTOMER_ID): cv.string,
        vol.Optional(CONF_GROUP): cv.string,
        vol.Optional(CONF_SITE): cv.string,
        vol.Optional(CONF_CLIENT_TYPE, default=DEFAULT_CLIENT_TYPE): vol.In(["WIRELESS", "WIRED", "ALL"]),
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL_S): vol.Any(
            cv.positive_int, cv.time_period, cv.time_period_str
        ),
    }
)

async def async_setup_platform(hass: HomeAssistant, config: ConfigType, async_add_entities, discovery_info=None):
    """Set up Aruba Central device_tracker as immediate-flip TrackerEntity (no consider_home)."""
    _LOGGER.warning("aruba_central(TrackerEntity): async_setup_platform START")

    # Parse scan_interval from YAML (seconds or HH:MM:SS)
    si = config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_S)
    if isinstance(si, int):
        interval_s = si
    elif isinstance(si, str):
        interval_s = int(cv.time_period_str(si).total_seconds())
    else:
        interval_s = int(si.total_seconds())
    if interval_s < 5:
        interval_s = 5

    session = async_get_clientsession(hass)
    api_base = config[CONF_API_BASE].rstrip("/")
    oauth_base = (config.get(CONF_OAUTH_BASE) or api_base).rstrip("/")

    mgr = ArubaCentralManager(
        hass=hass,
        async_add_entities=async_add_entities,
        session=session,
        api_base=api_base,
        oauth_base=oauth_base,
        client_id=config[CONF_CLIENT_ID],
        client_secret=config[CONF_CLIENT_SECRET],
        refresh_token=config[CONF_REFRESH_TOKEN],
        customer_id=config.get(CONF_CUSTOMER_ID),
        group=config.get(CONF_GROUP),
        site=config.get(CONF_SITE),
        client_type=config.get(CONF_CLIENT_TYPE, DEFAULT_CLIENT_TYPE),
        interval_s=interval_s,
    )
    await mgr.async_start()
    _LOGGER.warning("aruba_central(TrackerEntity): async_setup_platform DONE (interval_s=%s)", interval_s)


class _CentralAPI:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_base: str,
        oauth_base: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        customer_id: Optional[str],
    ):
        self.s = session
        self.api_base = api_base
        self.oauth_base = oauth_base
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.customer_id = customer_id
        self.access_token: Optional[str] = None
        self.expiry = 0.0

    async def _ensure_token(self):
        if self.access_token and time.time() < self.expiry - 60:
            return
        url = f"{self.oauth_base}/oauth2/token"
        data = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
        }
        _LOGGER.warning("aruba_central(TrackerEntity): POST %s (OAuth refresh)", url)
        async with self.s.post(url, data=data, timeout=30) as r:
            body = await r.text()
            if r.status != 200:
                _LOGGER.error("aruba_central(TrackerEntity): token refresh failed %s: %s", r.status, body)
                raise RuntimeError(f"Token refresh failed {r.status}: {body}")
            try:
                j = json.loads(body)
            except Exception:
                _LOGGER.error("aruba_central(TrackerEntity): token refresh invalid JSON: %s", body)
                raise
        self.access_token = j.get("access_token")
        self.refresh_token = j.get("refresh_token", self.refresh_token)
        self.expiry = time.time() + int(j.get("expires_in", 3600))
        _LOGGER.warning("aruba_central(TrackerEntity): token ok; expires_in=%s", j.get("expires_in"))

    def _headers(self) -> Dict[str, str]:
        h = {"Authorization": f"Bearer {self.access_token}"}
        if self.customer_id:
            h["TenantID"] = self.customer_id
        return h

    async def list_clients(self, *, group: Optional[str], site: Optional[str], client_type: str) -> List[Dict[str, Any]]:
        await self._ensure_token()
        url = f"{self.api_base}/monitoring/v2/clients"
        params: Dict[str, Any] = {"client_status": "CONNECTED", "limit": 1000}
        if client_type in ("WIRELESS", "WIRED"):
            params["client_type"] = client_type
        if group:
            params["group"] = group
        elif site:
            params["site"] = site

        items: List[Dict[str, Any]] = []
        last: Optional[str] = None
        for _ in range(10):
            q = dict(params)
            if last:
                q["last_client_mac"] = last
            _LOGGER.warning("aruba_central(TrackerEntity): GET %s params=%s", url, q)
            async with self.s.get(url, headers=self._headers(), params=q, timeout=30) as r:
                body = await r.text()
                if r.status != 200:
                    _LOGGER.error("aruba_central(TrackerEntity): clients fetch failed %s: %s", r.status, body)
                    raise RuntimeError(f"clients fetch failed {r.status}: {body}")
                try:
                    data = json.loads(body)
                except Exception:
                    _LOGGER.error("aruba_central(TrackerEntity): clients fetch invalid JSON: %s", body)
                    raise
            chunk = data.get("data") or data.get("clients") or []
            items.extend(chunk)
            last = data.get("last_client_mac")
            if not last or not chunk:
                break
        _LOGGER.warning("aruba_central(TrackerEntity): total clients returned=%s", len(items))
        return items


class ArubaCentralManager:
    """Maintains entities and performs scheduled Central fetches."""

    def __init__(
        self,
        hass: HomeAssistant,
        async_add_entities,
        session: aiohttp.ClientSession,
        api_base: str,
        oauth_base: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        customer_id: Optional[str],
        group: Optional[str],
        site: Optional[str],
        client_type: str,
        interval_s: int,
    ):
        self.hass = hass
        self.async_add_entities = async_add_entities
        self.interval_s = max(5, int(interval_s))
        self._api = _CentralAPI(session, api_base, oauth_base, client_id, client_secret, refresh_token, customer_id)
        self._group = group
        self._site = site
        self._client_type = client_type
        self._entities: Dict[str, ArubaCentralClientEntity] = {}
        self._cancel = None
        self._last_fetch_ts: float = 0.0
        self._next_due_ts: float = 0.0
        self._cache: Dict[str, Dict[str, Any]] = {}

    async def async_start(self):
        # Eerste fetch meteen; daarna interval
        await self._update()
        self._cancel = async_track_time_interval(self.hass, self._scheduled_update, timedelta(seconds=self.interval_s))
        _LOGGER.warning("aruba_central(TrackerEntity): scheduler started interval=%ss", self.interval_s)

    async def _scheduled_update(self, now):
        await self._update()

    async def _update(self):
        now = time.time()
        if now < self._next_due_ts:
            _LOGGER.warning(
                "aruba_central(TrackerEntity): skip API until next_due in %ss",
                int(self._next_due_ts - now),
            )
            return

        age = now - self._last_fetch_ts if self._last_fetch_ts else 1e9
        if self._last_fetch_ts and age < self.interval_s:
            _LOGGER.warning(
                "aruba_central(TrackerEntity): throttle (age=%ss < min=%ss)",
                int(age), self.interval_s
            )
            self._next_due_ts = now + (self.interval_s - age)
            return

        _LOGGER.warning("aruba_central(TrackerEntity): calling Central API (age=%ss, min=%ss)",
                        0 if age == 1e9 else int(age), self.interval_s)
        try:
            clients = await self._api.list_clients(group=self._group, site=self._site, client_type=self._client_type)
        except Exception as e:
            _LOGGER.error("aruba_central(TrackerEntity): API failure; cooldown %ss: %s", self.interval_s, e)
            self._next_due_ts = now + self.interval_s
            return

        mapping: Dict[str, Dict[str, Any]] = {}
        for c in clients:
            mac = (c.get("macaddr") or c.get("mac") or "").lower()
            if not mac:
                continue
            mapping[mac] = {
                "name": c.get("name") or c.get("hostname") or mac,
                "ip": c.get("ipaddr") or c.get("ip_address"),
            }

        # Create new entities
        new_macs = [m for m in mapping.keys() if m not in self._entities]
        if new_macs:
            ents = [ArubaCentralClientEntity(mac=m, name=mapping[m]["name"], ip=mapping[m].get("ip")) for m in new_macs]
            self.async_add_entities(ents)
            for e in ents:
                self._entities[e.mac] = e
            _LOGGER.warning("aruba_central(TrackerEntity): added %s new entities", len(ents))

        # Update all entities: set connected if present, else disconnected (IMMEDIATE FLIP)
        for mac, ent in self._entities.items():
            info = mapping.get(mac)
            ent.update_from(info)

        self._cache = mapping
        self._last_fetch_ts = now
        self._next_due_ts = now + self.interval_s
        _LOGGER.warning("aruba_central(TrackerEntity): update complete; entities=%s", len(self._entities))


class ArubaCentralClientEntity(TrackerEntity):
    """Immediate on/off presence per MAC without consider_home."""

    _attr_should_poll = False  # we push state on updates

    def __init__(self, mac: str, name: str, ip: Optional[str]):
        self.mac = mac
        self._name = name
        self._ip = ip
        self._connected = False
        self._available = True

    @property
    def name(self) -> str:
        return self._name

    @property
    def unique_id(self) -> str:
        return f"aruba_central_{self.mac}"

    @property
    def source_type(self) -> str:
        return SOURCE_TYPE_ROUTER

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def mac_address(self) -> str:
        return self.mac

    @property
    def ip_address(self) -> Optional[str]:
        return self._ip

    @property
    def available(self) -> bool:
        return self._available

    @callback
    def update_from(self, info: Optional[Dict[str, Any]]):
        """Flip state immediately based on presence in latest API result."""
        if info:
            self._connected = True
            self._ip = info.get("ip") or self._ip
            self._name = info.get("name") or self._name
        else:
            self._connected = False
        self.async_write_ha_state()
