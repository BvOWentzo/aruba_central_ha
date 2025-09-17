from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import voluptuous as vol

from homeassistant.components.device_tracker import PLATFORM_SCHEMA as BASE_PLATFORM_SCHEMA
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.components.device_tracker.const import SourceType
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed, CoordinatorEntity

_LOGGER = logging.getLogger(__name__)

CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_CUSTOMER_ID = "customer_id"      # optioneel (MSP TenantID header)
CONF_API_BASE = "api_base"            # bv. https://eu-apigw.central.arubanetworks.com
CONF_OAUTH_BASE = "oauth_base"        # optioneel; default = api_base
CONF_GROUP = "group"                  # optioneel
CONF_SITE = "site"                    # optioneel
CONF_CLIENT_TYPE = "client_type"      # WIRELESS | WIRED | ALL

DEFAULT_CLIENT_TYPE = "WIRELESS"
DEFAULT_SCAN_INTERVAL_S = 60

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
        vol.Optional(CONF_CLIENT_TYPE, default=DEFAULT_CLIENT_TYPE): vol.In(["WIRELESS", "WIRED", "ALL"]),
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL_S): vol.Any(
            cv.positive_int, cv.time_period, cv.time_period_str
        ),
    }
)


async def async_setup_platform(hass: HomeAssistant, config: dict, async_add_entities, discovery_info=None):
    session = async_get_clientsession(hass)
    api_base = config[CONF_API_BASE].rstrip("/")
    oauth_base = (config.get(CONF_OAUTH_BASE) or api_base).rstrip("/")

    scan_cfg = config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_S)
    if isinstance(scan_cfg, int):
        update_interval = timedelta(seconds=scan_cfg)
    elif isinstance(scan_cfg, str):
        update_interval = cv.time_period_str(scan_cfg)
    else:
        update_interval = scan_cfg

    api = _CentralAPI(
        session=session,
        api_base=api_base,
        oauth_base=oauth_base,
        client_id=config[CONF_CLIENT_ID],
        client_secret=config[CONF_CLIENT_SECRET],
        refresh_token=config[CONF_REFRESH_TOKEN],
        customer_id=config.get(CONF_CUSTOMER_ID),
    )

    group = config.get(CONF_GROUP)
    site = config.get(CONF_SITE)
    client_type = config.get(CONF_CLIENT_TYPE, DEFAULT_CLIENT_TYPE)

    coordinator = CentralCoordinator(
        hass=hass,
        api=api,
        update_interval=update_interval,
        group=group,
        site=site,
        client_type=client_type,
    )

    # Eerste refresh (start ook het periodieke schema)
    await coordinator.async_refresh_initial()

    entities: Dict[str, ArubaClientEntity] = {}

    # Maak entities voor bestaande clients
    for mac, info in coordinator.data.items():
        ent = ArubaClientEntity(coordinator, mac)
        entities[mac] = ent
        async_add_entities([ent])

    # Listener om nieuwe clients als entiteit toe te voegen
    @callback
    def _watch_new_clients():
        for mac in list(coordinator.data.keys()):
            if mac not in entities:
                ent = ArubaClientEntity(coordinator, mac)
                entities[mac] = ent
                async_add_entities([ent])

    coordinator.async_add_listener(_watch_new_clients)


# ---------------- Central API ----------------
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
        async with self.s.post(url, data=data, timeout=30) as r:
            txt = await r.text()
            if r.status != 200:
                raise UpdateFailed(f"Token refresh failed {r.status}: {txt}")
            j = await r.json()
        self.access_token = j.get("access_token")
        self.refresh_token = j.get("refresh_token", self.refresh_token)  # rotatie
        self.expiry = time.time() + int(j.get("expires_in", 3600))

    def _headers(self) -> Dict[str, str]:
        h = {"Authorization": f"Bearer {self.access_token}"}
        if self.customer_id:
            h["TenantID"] = self.customer_id
        return h

    async def list_clients(
        self, *, group: Optional[str], site: Optional[str], client_type: str
    ) -> List[Dict[str, Any]]:
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
            async with self.s.get(url, headers=self._headers(), params=q, timeout=30) as r:
                txt = await r.text()
                if r.status != 200:
                    raise UpdateFailed(f"clients fetch failed {r.status}: {txt}")
                data = await r.json()
            chunk = data.get("data") or data.get("clients") or []
            items.extend(chunk)
            last = data.get("last_client_mac")
            if not last or not chunk:
                break
        return items


# ---------------- Coordinator ----------------
class CentralCoordinator(DataUpdateCoordinator[Dict[str, Dict[str, Any]]]):
    def __init__(
        self,
        hass: HomeAssistant,
        api: _CentralAPI,
        update_interval: timedelta,
        group: Optional[str],
        site: Optional[str],
        client_type: str,
    ):
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name="aruba_central_coordinator",
            update_interval=update_interval,
        )
        self._api = api
        self._group = group
        self._site = site
        self._client_type = client_type
        self.data: Dict[str, Dict[str, Any]] = {}

    async def _async_update_data(self) -> Dict[str, Dict[str, Any]]:
        clients = await self._api.list_clients(
            group=self._group, site=self._site, client_type=self._client_type
        )
        out: Dict[str, Dict[str, Any]] = {}
        for c in clients:
            mac = (c.get("macaddr") or c.get("mac") or "").lower()
            if not mac:
                continue
            out[mac] = {
                "ip": c.get("ipaddr") or c.get("ip_address"),
                "name": c.get("name") or c.get("hostname") or mac,
            }
        return out

    async def async_refresh_initial(self):
        await self.async_refresh()
        # start periodiek updaten iets later zodat HA eerst entiteiten kan aanmaken
        async def _kick(_now):
            await self.async_request_refresh()
        async_call_later(self.hass, 0, _kick)


# ---------------- Entity ----------------
class ArubaClientEntity(CoordinatorEntity[CentralCoordinator], TrackerEntity):
    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator: CentralCoordinator, mac: str):
        super().__init__(coordinator)
        self._mac = mac

    @property
    def unique_id(self) -> str:
        return f"aruba_central_{self._mac.replace(':','')}"

    @property
    def name(self) -> str:
        return self._mac

    @property
    def source_type(self) -> SourceType:
        return SourceType.ROUTER

    @property
    def is_connected(self) -> bool:
        return self._mac in self.coordinator.data

    @property
    def mac_address(self) -> str:
        return self._mac

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        info = self.coordinator.data.get(self._mac) or {}
        out: Dict[str, Any] = {"mac": self._mac}
        if info.get("ip"):
            out["ip"] = info["ip"]
        return out
