# custom_components/aruba_central/device_tracker.py
from __future__ import annotations

import logging, time
from datetime import timedelta
from typing import Any, Dict, List, Optional

import aiohttp, voluptuous as vol
from homeassistant.components.device_tracker import PLATFORM_SCHEMA as BASE_PLATFORM_SCHEMA
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.components.device_tracker.const import SourceType
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

_LOGGER = logging.getLogger(__name__)
_LOGGER.warning("aruba_central: module import OK")  # <-- hoor je altijd bij succesvol laden

CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_CUSTOMER_ID = "customer_id"
CONF_API_BASE = "api_base"
CONF_OAUTH_BASE = "oauth_base"  # optioneel; default = api_base
CONF_GROUP = "group"
CONF_SITE = "site"
CONF_CLIENT_TYPE = "client_type"
DEFAULT_SCAN_INTERVAL_S = 60
DEFAULT_CLIENT_TYPE = "WIRELESS"

PLATFORM_SCHEMA = BASE_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_CLIENT_ID): cv.string,
        vol.Required(CONF_CLIENT_SECRET): cv.string,
        vol.Required(CONF_REFRESH_TOKEN): cv.string,
        vol.Required(CONF_API_BASE): cv.url,
        vol.Optional(CONF_OAUTH_BASE): cv.url,
        vol.Optional(CONF_CUSTOMER_ID): cv.string,
        vol.Optional(CONF_GROUP): cv.string,
        vol.Optional(CONF_SITE): cv.string,
        vol.Optional(CONF_CLIENT_TYPE, default=DEFAULT_CLIENT_TYPE): vol.In(["WIRELESS","WIRED","ALL"]),
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL_S): vol.Any(
            cv.positive_int, cv.time_period, cv.time_period_str
        ),
    }
)

async def async_setup_platform(hass: HomeAssistant, config: dict, async_add_entities, discovery_info=None):
    _LOGGER.warning("aruba_central: async_setup_platform START")  # <-- moet je zien bij opstart
    session = async_get_clientsession(hass)
    api_base = config[CONF_API_BASE].rstrip("/")
    oauth_base = (config.get(CONF_OAUTH_BASE) or api_base).rstrip("/")
    api = _CentralAPI(
        session, api_base, oauth_base,
        config[CONF_CLIENT_ID], config[CONF_CLIENT_SECRET],
        config[CONF_REFRESH_TOKEN], config.get(CONF_CUSTOMER_ID)
    )

    interval_cfg = config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_S)
    if isinstance(interval_cfg, int):
        interval_td = timedelta(seconds=interval_cfg)
    elif isinstance(interval_cfg, str):
        interval_td = cv.time_period_str(interval_cfg)
    else:
        interval_td = interval_cfg

    poller = _Poller(
        hass=hass, api=api,
        group=config.get(CONF_GROUP), site=config.get(CONF_SITE),
        client_type=config.get(CONF_CLIENT_TYPE, DEFAULT_CLIENT_TYPE),
        interval=interval_td, async_add_entities=async_add_entities
    )
    await poller.start()
    _LOGGER.warning("aruba_central: async_setup_platform DONE")   # <-- en deze ook

class _CentralAPI:
    def __init__(self, session: aiohttp.ClientSession, api_base: str, oauth_base: str,
                 client_id: str, client_secret: str, refresh_token: str, customer_id: Optional[str]):
        self.s = session; self.api_base = api_base; self.oauth_base = oauth_base
        self.client_id = client_id; self.client_secret = client_secret
        self.refresh_token = refresh_token; self.customer_id = customer_id
        self.access_token: Optional[str] = None; self.expiry = 0.0

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
        _LOGGER.warning("aruba_central: POST %s (OAuth refresh)", url)  # <-- zichtbare log
        async with self.s.post(url, data=data, timeout=30) as r:
            txt = await r.text()
            if r.status != 200:
                _LOGGER.error("aruba_central: token refresh failed %s: %s", r.status, txt)
                raise RuntimeError(f"Token refresh failed {r.status}: {txt}")
            j = await r.json()
        self.access_token = j.get("access_token")
        self.refresh_token = j.get("refresh_token", self.refresh_token)
        self.expiry = time.time() + int(j.get("expires_in", 3600))
        _LOGGER.warning("aruba_central: token ok; expires_in=%s", j.get("expires_in"))

    def _headers(self) -> Dict[str, str]:
        h = {"Authorization": f"Bearer {self.access_token}"}
        if self.customer_id: h["TenantID"] = self.customer_id
        return h

    async def list_clients(self, *, group: Optional[str], site: Optional[str], client_type: str) -> List[Dict[str, Any]]:
        await self._ensure_token()
        url = f"{self.api_base}/monitoring/v2/clients"
        params: Dict[str, Any] = {"client_status": "CONNECTED", "limit": 1000}
        if client_type in ("WIRELESS","WIRED"): params["client_type"] = client_type
        if group: params["group"] = group
        elif site: params["site"] = site

        items: List[Dict[str, Any]] = []; last = None
        for _ in range(10):
            q = dict(params); 
            if last: q["last_client_mac"] = last
            _LOGGER.warning("aruba_central: GET %s params=%s", url, q)  # <-- zichtbare log
            async with self.s.get(url, headers=self._headers(), params=q, timeout=30) as r:
                txt = await r.text()
                if r.status != 200:
                    _LOGGER.error("aruba_central: clients fetch failed %s: %s", r.status, txt)
                    raise RuntimeError(f"clients fetch failed {r.status}: {txt}")
                data = await r.json()
            chunk = data.get("data") or data.get("clients") or []
            items.extend(chunk)
            last = data.get("last_client_mac")
            if not last or not chunk: break
        return items

class _Poller:
    def __init__(self, *, hass: HomeAssistant, api: _CentralAPI,
                 group: Optional[str], site: Optional[str], client_type: str,
                 interval: timedelta, async_add_entities):
        self.hass=hass; self.api=api; self.group=group; self.site=site
        self.client_type=client_type; self.interval=interval
        self.async_add_entities=async_add_entities; self.entities: dict[str,_ClientEntity]={}

    async def start(self):
        _LOGGER.warning("aruba_central: scheduler every %ss", int(self.interval.total_seconds()))
        await self._poll()
        async_track_time_interval(self.hass, self._poll, self.interval)

    async def _poll(self, *_):
        try:
            clients = await self.api.list_clients(group=self.group, site=self.site, client_type=self.client_type)
        except Exception as e:
            _LOGGER.error("aruba_central: poll failed: %s", e)
            for ent in self.entities.values(): ent.mark_seen(False)
            return
        seen: set[str] = set()
        for c in clients:
            mac = (c.get("macaddr") or c.get("mac") or "").lower()
            if not mac: continue
            seen.add(mac)
            ip = c.get("ipaddr") or c.get("ip_address")
            ent = self.entities.get(mac)
            if not ent:
                ent = _ClientEntity(mac=mac); self.entities[mac] = ent; self.async_add_entities([ent])
            ent.update_ip(ip); ent.mark_seen(True)
        for mac, ent in self.entities.items():
            if mac not in seen: ent.mark_seen(False)

class _ClientEntity(TrackerEntity):
    _attr_icon = "mdi:wifi"
    def __init__(self, mac: str):
        self._mac = mac; self._ip: Optional[str]=None; self._home=False
    @property
    def unique_id(self) -> str: return f"aruba_central_{self._mac.replace(':','')}"
    @property
    def name(self) -> str: return self._mac
    @property
    def source_type(self) -> SourceType: return SourceType.ROUTER
    @property
    def is_connected(self) -> bool: return self._home
    @property
    def mac_address(self) -> str: return self._mac
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        out={"mac": self._mac}; 
        if self._ip: out["ip"]=self._ip
        return out
    def update_ip(self, ip: Optional[str]): self._ip = ip
    def mark_seen(self, present: bool):
        self._home = present
        self.async_write_ha_state()
