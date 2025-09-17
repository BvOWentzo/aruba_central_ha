from __future__ import annotations

import logging, time, aiohttp, voluptuous as vol
from datetime import timedelta
from typing import Any, Dict, Optional

from homeassistant.components.device_tracker import PLATFORM_SCHEMA as BASE_PLATFORM_SCHEMA
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.components.device_tracker.const import SourceType
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed, CoordinatorEntity

_LOGGER = logging.getLogger(__name__)

CONF_CLIENT_ID="client_id"; CONF_CLIENT_SECRET="client_secret"; CONF_REFRESH_TOKEN="refresh_token"
CONF_CUSTOMER_ID="customer_id"; CONF_API_BASE="api_base"; CONF_OAUTH_BASE="oauth_base"
CONF_GROUP="group"; CONF_SITE="site"; CONF_CLIENT_TYPE="client_type"

DEFAULT_CLIENT_TYPE="WIRELESS"; DEFAULT_SCAN_INTERVAL_S=60

PLATFORM_SCHEMA = BASE_PLATFORM_SCHEMA.extend({
    vol.Required(CONF_CLIENT_ID): cv.string,
    vol.Required(CONF_CLIENT_SECRET): cv.string,
    vol.Required(CONF_REFRESH_TOKEN): cv.string,
    vol.Required(CONF_API_BASE): cv.url,
    vol.Optional(CONF_OAUTH_BASE): cv.url,
    vol.Optional(CONF_CUSTOMER_ID): cv.string,
    vol.Optional(CONF_GROUP): cv.string,
    vol.Optional(CONF_SITE): cv.string,
    vol.Optional(CONF_CLIENT_TYPE, default=DEFAULT_CLIENT_TYPE): vol.In(["WIRELESS","WIRED","ALL"]),
    vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL_S): vol.Any(cv.positive_int, cv.time_period, cv.time_period_str),
})

async def async_setup_platform(hass: HomeAssistant, config: dict, async_add_entities, discovery_info=None):
    session = async_get_clientsession(hass)
    api_base = config[CONF_API_BASE].rstrip("/")
    oauth_base = (config.get(CONF_OAUTH_BASE) or api_base).rstrip("/")

    si = config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_S)
    if isinstance(si, int): update_interval = timedelta(seconds=si)
    elif isinstance(si, str): update_interval = cv.time_period_str(si)
    else: update_interval = si

    api = _CentralAPI(session, api_base, oauth_base,
                      config[CONF_CLIENT_ID], config[CONF_CLIENT_SECRET],
                      config[CONF_REFRESH_TOKEN], config.get(CONF_CUSTOMER_ID))

    coordinator = CentralCoordinator(
        hass=hass, api=api, update_interval=update_interval,
        group=config.get(CONF_GROUP), site=config.get(CONF_SITE),
        client_type=config.get(CONF_CLIENT_TYPE, DEFAULT_CLIENT_TYPE)
    )
    await coordinator.async_config_entry_first_refresh()

    entities: Dict[str, ArubaClientEntity] = {}
    for mac in coordinator.data.keys():
        ent = ArubaClientEntity(coordinator, mac); entities[mac]=ent
    if entities: async_add_entities(list(entities.values()))

    @callback
    def _on_update():
        # nieuwe macs → entiteit bijmaken
        new = [m for m in coordinator.data.keys() if m not in entities]
        if new:
            ents = []
            for mac in new:
                ent = ArubaClientEntity(coordinator, mac); entities[mac]=ent; ents.append(ent)
            async_add_entities(ents)
    coordinator.async_add_listener(_on_update)

class _CentralAPI:
    def __init__(self, session: aiohttp.ClientSession, api_base: str, oauth_base: str,
                 client_id: str, client_secret: str, refresh_token: str, customer_id: Optional[str]):
        self.s=session; self.api_base=api_base; self.oauth_base=oauth_base
        self.client_id=client_id; self.client_secret=client_secret
        self.refresh_token=refresh_token; self.customer_id=customer_id
        self.access_token: Optional[str]=None; self.expiry=0.0

    async def _ensure_token(self):
        if self.access_token and time.time() < self.expiry-60: return
        url=f"{self.oauth_base}/oauth2/token"
        data={"grant_type":"refresh_token","client_id":self.client_id,"client_secret":self.client_secret,"refresh_token":self.refresh_token}
        async with self.s.post(url, data=data, timeout=30) as r:
            txt=await r.text()
            if r.status!=200: raise UpdateFailed(f"Token refresh failed {r.status}: {txt}")
            j=await r.json()
        self.access_token=j.get("access_token")
        self.refresh_token=j.get("refresh_token", self.refresh_token)
        self.expiry=time.time()+int(j.get("expires_in",3600))

    def _headers(self)->Dict[str,str]:
        h={"Authorization":f"Bearer {self.access_token}"}
        if self.customer_id: h["TenantID"]=self.customer_id
        return h

    async def list_clients(self, *, group: Optional[str], site: Optional[str], client_type: str):
        await self._ensure_token()
        url=f"{self.api_base}/monitoring/v2/clients"
        params={"client_status":"CONNECTED","limit":1000}
        if client_type in ("WIRELESS","WIRED"): params["client_type"]=client_type
        if group: params["group"]=group
        elif site: params["site"]=site
        items=[]; last=None
        for _ in range(10):
            q=dict(params); 
            if last: q["last_client_mac"]=last
            async with self.s.get(url, headers=self._headers(), params=q, timeout=30) as r:
                txt=await r.text()
                if r.status!=200: raise UpdateFailed(f"clients fetch failed {r.status}: {txt}")
                data=await r.json()
            chunk=data.get("data") or data.get("clients") or []
            items.extend(chunk); last=data.get("last_client_mac")
            if not last or not chunk: break
        return items

class CentralCoordinator(DataUpdateCoordinator[Dict[str, Dict[str, Any]]]):
    def __init__(self, hass: HomeAssistant, api: _CentralAPI, update_interval: timedelta,
                 group: Optional[str], site: Optional[str], client_type: str):
        super().__init__(hass, _LOG, "aruba_central", update_interval)
        self._api=api; self._group=group; self._site=site; self._client_type=client_type
    async def _async_update_data(self):
        clients=await self._api.list_clients(group=self._group, site=self._site, client_type=self._client_type)
        out={}
        for c in clients:
            mac=(c.get("macaddr") or c.get("mac") or "").lower()
            if not mac: continue
            out[mac]={"ip":c.get("ipaddr") or c.get("ip_address"), "name":c.get("name") or c.get("hostname") or mac}
        return out

_LOG = logging.getLogger(__name__)

class ArubaClientEntity(CoordinatorEntity[CentralCoordinator], TrackerEntity):
    _attr_icon="mdi:wifi"
    def __init__(self, coordinator: CentralCoordinator, mac: str):
        super().__init__(coordinator); self._mac=mac
    @property
    def unique_id(self)->str: return f"aruba_central_{self._mac.replace(':','')}"
    @property
    def name(self)->str: return self._mac
    @property
    def source_type(self)->SourceType: return SourceType.ROUTER
    @property
    def is_connected(self)->bool: return self._mac in self.coordinator.data
    @property
    def mac_address(self)->str: return self._mac
    @property
    def extra_state_attributes(self)->Dict[str,Any]:
        info=self.coordinator.data.get(self._mac) or {}
        out={"mac":self._mac}
        if info.get("ip"): out["ip"]=info["ip"]
        return out
