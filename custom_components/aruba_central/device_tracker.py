# custom_components/aruba_central/device_tracker.py
from __future__ import annotations

import time
import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import voluptuous as vol

from homeassistant.components.device_tracker import PLATFORM_SCHEMA as BASE_PLATFORM_SCHEMA
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.components.device_tracker.const import SOURCE_TYPE_ROUTER
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

_LOGGER = logging.getLogger(__name__)

# ---- Config keys (minimaal) ----
CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_CUSTOMER_ID = "customer_id"      # optioneel (MSP) -> TenantID header
CONF_API_BASE = "api_base"            # bv. https://apigw-eu2.central.arubanetworks.com
CONF_OAUTH_BASE = "oauth_base"        # bv. https://eu2.arubanetworks.com

# Optioneel: basic filters om load te beperken (we houden het minimaal)
CONF_GROUP = "group"                  # naam of GUID (Central accepteert beide)
CONF_SITE = "site"                    # alternatief filter
CONF_CLIENT_TYPE = "client_type"      # WIRELESS | WIRED | ALL (default WIRELESS)

DEFAULT_SCAN_INTERVAL = 60
DEFAULT_CLIENT_TYPE = "WIRELESS"

PLATFORM_SCHEMA = BASE_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_CLIENT_ID): cv.string,
        vol.Required(CONF_CLIENT_SECRET): cv.string,
        vol.Required(CONF_REFRESH_TOKEN): cv.string,
        vol.Required(CONF_API_BASE): cv.url,
        vol.Required(CONF_OAUTH_BASE): cv.url,
        vol.Optional(CONF_CUSTOMER_ID): cv.string,
        vol.Optional(CONF_GROUP): cv.string,
        vol.Optional(CONF_SITE): cv.string,
        vol.Optional(CONF_CLIENT_TYPE, default=DEFAULT_CLIENT_TYPE): vol.In(["WIRELESS", "WIRED", "ALL"]),
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): cv.positive_int,
    }
)

# ---------------- Setup ----------------
async def async_setup_platform(hass: HomeAssistant, config: dict, async_add_entities, discovery_info=None):
    session = async_get_clientsession(hass)
    api = _CentralAPI(
        session=session,
        api_base=config[CONF_API_BASE].rstrip("/"),
        oauth_base=config[CONF_OAUTH_BASE].rstrip("/"),
        client_id=config[CONF_CLIENT_ID],
        client_secret=config[CONF_CLIENT_SECRET],
        refresh_token=config[CONF_REFRESH_TOKEN],
        customer_id=config.get(CONF_CUSTOMER_ID),
    )

    poller = _Poller(
        hass=hass,
        api=api,
        group=config.get(CONF_GROUP),
        site=config.get(CONF_SITE),
        client_type=config.get(CONF_CLIENT_TYPE, DEFAULT_CLIENT_TYPE),
        interval=timedelta(seconds=config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
        async_add_entities=async_add_entities,
    )
    await poller.start()

# -------------- Central API (minimal) --------------
class _CentralAPI:
    def __init__(self, session: aiohttp.ClientSession, api_base: str, oauth_base: str,
                 client_id: str, client_secret: str, refresh_token: str, customer_id: Optional[str]):
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
        _LOGGER.debug("Central OAuth refresh: POST %s", url)
        async with self.s.post(url, data=data, timeout=30) as r:
            txt = await r.text()
            if r.status != 200:
                _LOGGER.error("Token refresh failed %s: %s", r.status, txt)
                raise RuntimeError(f"Token refresh failed {r.status}: {txt}")
            j = await r.json()
        self.access_token = j.get("access_token")
        self.refresh_token = j.get("refresh_token", self.refresh_token)  # soms nieuw
        self.expiry = time.time() + int(j.get("expires_in", 3600))
        _LOGGER.debug("Central token refreshed; expires_in=%s", j.get("expires_in"))

    def _headers(self) -> Dict[str, str]:
        h = {"Authorization": f"Bearer {self.access_token}"}
        if self.customer_id:
            h["TenantID"] = self.customer_id  # MSP header
        return h

    async def list_clients(self, *, group: Optional[str], site: Optional[str], client_type: str) -> List[Dict[str, Any]]:
        """/monitoring/v2/clients — alleen wat nodig is om MAC aanwezigheid te bepalen."""
        await self._ensure_token()
        url = f"{self.api_base}/monitoring/v2/clients"
        params: Dict[str, Any] = {
            "client_status": "CONNECTED",
            "limit": 1000,
        }
        # typefilter (AOS8 deed alleen wifi; default hier WIRELESS)
        if client_type in ("WIRELESS", "WIRED"):
            params["client_type"] = client_type
        # EEN van group/site (optioneel)
        if group:
            params["group"] = group
        elif site:
            params["site"] = site

        items: List[Dict[str, Any]] = []
        last: Optional[str] = None
        for _ in range(10):  # simpele paginatie
            q = dict(params)
            if last:
                q["last_client_mac"] = last
            _LOGGER.debug("Central GET %s params=%s", url, q)
            async with self.s.get(url, headers=self._headers(), params=q, timeout=30) as r:
                txt = await r.text()
                if r.status != 200:
                    _LOGGER.error("Central clients fetch failed %s: %s", r.status, txt)
                    raise RuntimeError(f"clients fetch failed {r.status}: {txt}")
                data = await r.json()
            chunk = data.get("data") or data.get("clients") or []
            items.extend(chunk)
            last = data.get("last_client_mac")
            if not last or not chunk:
                break
        return items

# -------------- Poller en entity (presence-only) --------------
class _Poller:
    def __init__(self, *, hass: HomeAssistant, api: _CentralAPI,
                 group: Optional[str], site: Optional[str], client_type: str,
                 interval: timedelta, async_add_entities):
        self.hass = hass
        self.api = api
        self.group = group
        self.site = site
        self.client_type = client_type
        self.interval = interval
        self.async_add_entities = async_add_entities
        self.entities: dict[str, _ClientEntity] = {}

    async def start(self):
        await self._poll()
        async_track_time_interval(self.hass, self._poll, self.interval)

    async def _poll(self, *_):
        try:
            clients = await self.api.list_clients(group=self.group, site=self.site, client_type=self.client_type)
        except Exception as e:
            _LOGGER.error("Aruba Central poll failed: %s", e)
            # markeer alles not_home
            for ent in self.entities.values():
                ent.mark_seen(False)
            return

        seen: set[str] = set()
        for c in clients:
            mac = (c.get("macaddr") or c.get("mac") or "").lower()
            if not mac:
                continue
            seen.add(mac)
            ip = c.get("ipaddr") or c.get("ip_address")
            ent = self.entities.get(mac)
            if not ent:
                ent = _ClientEntity(mac=mac)
                self.entities[mac] = ent
                self.async_add_entities([ent])
            ent.update_ip(ip)
            ent.mark_seen(True)

        # verdwenen -> not_home
        for mac, ent in self.entities.items():
            if mac not in seen:
                ent.mark_seen(False)

class _ClientEntity(TrackerEntity):
    _attr_icon = "mdi:wifi"

    def __init__(self, mac: str):
        self._mac = mac
        self._ip: Optional[str] = None
        self._home: bool = False

    @property
    def unique_id(self) -> str:
        return f"aruba_central_{self._mac.replace(':','')}"

    @property
    def name(self) -> str:
        # AOS8-stijl: gebruik MAC als zichtbare naam
        return self._mac

    @property
    def source_type(self) -> str:
        return SOURCE_TYPE_ROUTER

    @property
    def is_connected(self) -> bool:
        return self._home

    @property
    def mac_address(self) -> str:
        return self._mac

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        # Alleen minimale info zoals AOS8: mac (+ ip indien bekend)
        out: Dict[str, Any] = {"mac": self._mac}
        if self._ip:
            out["ip"] = self._ip
        return out

    def update_ip(self, ip: Optional[str]):
        self._ip = ip

    def mark_seen(self, present: bool):
        self._home = present
        self.async_write_ha_state()
