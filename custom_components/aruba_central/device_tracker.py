from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import voluptuous as vol

from homeassistant.components.device_tracker.const import SOURCE_TYPE_ROUTER
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

_LOGGER = logging.getLogger(__name__)

# ---- YAML options
CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_CUSTOMER_ID = "customer_id"     # optional, used as TenantID header
CONF_API_BASE = "api_base"           # e.g. https://apigw-eu2.central.arubanetworks.com
CONF_OAUTH_BASE = "oauth_base"       # e.g. https://eu2.arubanetworks.com
CONF_SITE = "site"                   # optional
CONF_GROUP = "group"                 # optional (name or GUID)
CONF_GROUP_ID = "group_id"           # optional (GUID; mapped to 'group' if provided)
CONF_AP_SERIALS = "ap_serials"       # optional list[str]
CONF_CLIENT_TYPE = "client_type"     # WIRELESS | WIRED | ALL
CONF_CLIENT_STATUS = "client_status" # CONNECTED | FAILED

DEFAULT_SCAN_INTERVAL = 60
DEFAULT_CLIENT_TYPE = "WIRELESS"
DEFAULT_CLIENT_STATUS = "CONNECTED"

PLATFORM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLIENT_ID): cv.string,
        vol.Required(CONF_CLIENT_SECRET): cv.string,
        vol.Required(CONF_REFRESH_TOKEN): cv.string,
        vol.Required(CONF_API_BASE): cv.url,
        vol.Required(CONF_OAUTH_BASE): cv.url,
        vol.Optional(CONF_CUSTOMER_ID): cv.string,
        vol.Optional(CONF_SITE): cv.string,
        vol.Optional(CONF_GROUP): cv.string,
        vol.Optional(CONF_GROUP_ID): cv.string,
        vol.Optional(CONF_AP_SERIALS): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(CONF_CLIENT_TYPE, default=DEFAULT_CLIENT_TYPE): vol.In(["WIRELESS", "WIRED", "ALL"]),
        vol.Optional(CONF_CLIENT_STATUS, default=DEFAULT_CLIENT_STATUS): vol.In(["CONNECTED", "FAILED"]),
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.Coerce(int),
    }
)

async def async_setup_platform(hass: HomeAssistant, config: dict, async_add_entities, discovery_info=None):
    session = async_get_clientsession(hass)
    api = ArubaCentralAPI(
        session=session,
        api_base=config[CONF_API_BASE].rstrip("/"),
        oauth_base=config[CONF_OAUTH_BASE].rstrip("/"),
        client_id=config[CONF_CLIENT_ID],
        client_secret=config[CONF_CLIENT_SECRET],
        refresh_token=config[CONF_REFRESH_TOKEN],
        customer_id=config.get(CONF_CUSTOMER_ID),
    )

    tracker = ArubaCentralTracker(
        hass=hass,
        api=api,
        site=config.get(CONF_SITE),
        group=config.get(CONF_GROUP),
        group_id=config.get(CONF_GROUP_ID),
        ap_serials=config.get(CONF_AP_SERIALS) or [],
        client_type=config.get(CONF_CLIENT_TYPE, DEFAULT_CLIENT_TYPE),
        client_status=config.get(CONF_CLIENT_STATUS, DEFAULT_CLIENT_STATUS),
        scan_interval=timedelta(seconds=config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
        async_add_entities=async_add_entities,
    )

    await tracker.async_start()


# ---------------- API client ----------------

class ArubaCentralAPI:
    def __init__(self, session: aiohttp.ClientSession, api_base: str, oauth_base: str,
                 client_id: str, client_secret: str, refresh_token: str, customer_id: Optional[str]):
        self._session = session
        self._api_base = api_base
        self._oauth_base = oauth_base
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: Optional[str] = None
        self._access_expiry: float = 0
        self._customer_id = customer_id

    async def _ensure_token(self):
        # Refresh if absent or expiring (<60s left)
        if self._access_token and time.time() < self._access_expiry - 60:
            return
        data = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
        }
        url = f"{self._oauth_base}/oauth2/token"
        async with self._session.post(url, data=data, timeout=30) as resp:
            txt = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Token refresh failed {resp.status}: {txt}")
            payload = await resp.json()
            self._access_token = payload.get("access_token")
            # sommige tenants geven ook een nieuwe refresh_token terug
            self._refresh_token = payload.get("refresh_token", self._refresh_token)
            # Central access tokens zijn meestal ~2 uur geldig; neem expires_in als aanwezig
            self._access_expiry = time.time() + int(payload.get("expires_in", 3600))
            _LOGGER.debug("Aruba Central token refreshed; expires_in=%s", payload.get("expires_in"))

    def _headers(self) -> Dict[str, str]:
        headers = {"Authorization": f"Bearer {self._access_token}"}
        if self._customer_id:
            headers["TenantID"] = self._customer_id
        return headers

    async def list_groups(self) -> List[str]:
        """Alle groepsnamen; handig om GUID->naam te mappen indien nodig."""
        await self._ensure_token()
        url = f"{self._api_base}/configuration/v2/groups"
        async with self._session.get(url, headers=self._headers(), timeout=30) as resp:
            if resp.status != 200:
                _LOGGER.debug("groups list failed: %s", await resp.text())
                return []
            data = await resp.json()
            # response: {"data":[{"group":"name1"}, ...]}
            return [g.get("group") for g in (data.get("data") or []) if "group" in g]

    async def get_clients(self, *,
                          site: Optional[str],
                          group: Optional[str],
                          ap_serials: List[str],
                          client_type: str,
                          client_status: str) -> List[Dict[str, Any]]:
        """Ophalen via /monitoring/v2/clients met filtering & paginatie."""
        await self._ensure_token()

        params: Dict[str, Any] = {
            "client_status": client_status,            # CONNECTED | FAILED
            "limit": 1000,
        }

        # Client type: ALL => 2 calls en samenvoegen
        if client_type in ("WIRELESS", "WIRED"):
            params["client_type"] = client_type

        # één van: group/site/label/network/cluster_id/swarm_id
        if group:
            params["group"] = group
        elif site:
            params["site"] = site

        if ap_serials:
            # v2 API accepteert 'serial' filter; meerdere waarden comma-separated
            params["serial"] = ",".join(ap_serials)

        url = f"{self._api_base}/monitoring/v2/clients"

        async def _fetch_one(ptype: Optional[str]) -> List[Dict[str, Any]]:
            p = dict(params)
            if ptype:
                p["client_type"] = ptype
            items: List[Dict[str, Any]] = []
            last = None
            for _ in range(10):  # safety: max 10 pagina’s
                if last:
                    p["last_client_mac"] = last
                async with self._session.get(url, headers=self._headers(), params=p, timeout=30) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        raise RuntimeError(f"clients fetch failed {resp.status}: {txt}")
                    data = await resp.json()
                    chunk = data.get("data") or data.get("clients") or []
                    items.extend(chunk)
                    last = data.get("last_client_mac")
                    if not last or not chunk:
                        break
            return items

        if client_type == "ALL":
            wl, wd = await asyncio.gather(_fetch_one("WIRELESS"), _fetch_one("WIRED"))
            return wl + wd
        else:
            return await _fetch_one(None)

# ------------- Tracker --------------

class ArubaCentralTracker:
    def __init__(self, hass: HomeAssistant, api: ArubaCentralAPI, *,
                 site: Optional[str], group: Optional[str], group_id: Optional[str],
                 ap_serials: List[str], client_type: str, client_status: str,
                 scan_interval: timedelta, async_add_entities):
        self.hass = hass
        self.api = api
        self.site = site
        self.group = group
        self.group_id = group_id
        self.ap_serials = ap_serials
        self.client_type = client_type
        self.client_status = client_status
        self.scan_interval = scan_interval
        self.async_add_entities = async_add_entities
        self.entities: dict[str, ArubaClientEntity] = {}

    async def async_start(self):
        # Indien group_id is opgegeven maar geen group naam: probeer GUID direct,
        # lukt dat niet dan mappen we GUID->naam via /configuration/v2/groups (fallback).
        if self.group_id and not self.group:
            self.group = self.group_id  # veel tenants accepteren GUID in 'group'
            _LOGGER.debug("Using group_id as 'group' filter: %s", self.group_id)

        await self._poll_update()
        async_track_time_interval(self.hass, self._poll_update, self.scan_interval)

    async def _poll_update(self, *_):
        try:
            clients = await self.api.get_clients(
                site = self.site,
                group = self.group,
                ap_serials = self.ap_serials,
                client_type = self.client_type,
                client_status = self.client_status,
            )
            _LOGGER.debug("Fetched %d clients from Central", len(clients))
        except Exception as exc:
            _LOGGER.error("Aruba Central poll failed: %s", exc)
            return

        seen = set()

        for c in clients:
            mac = (c.get("macaddr") or c.get("mac") or "").lower()
            if not mac:
                continue
            seen.add(mac)
            name = c.get("name") or c.get("hostname") or c.get("username") or mac
            ip = c.get("ipaddr") or c.get("ip_address")
            ap_name = c.get("associated_device") or c.get("ap_name") or c.get("sw_name")
            manufacturer = c.get("manufacturer")
            os_type = c.get("os_type")
            rssi = c.get("signal_db") or c.get("rssi")

            if mac not in self.entities:
                ent = ArubaClientEntity(mac=mac, name=name)
                self.entities[mac] = ent
                self.async_add_entities([ent])

            self.entities[mac].update_from_api(
                ip=ip,
                ap_name=ap_name,
                manufacturer=manufacturer,
                os_type=os_type,
                rssi=rssi,
            )

        # markeer vermiste clients als 'weg'
        for mac, ent in list(self.entities.items()):
            ent.seen(now=(mac in seen))

class ArubaClientEntity(TrackerEntity):
    def __init__(self, mac: str, name: str):
        self._mac = mac
        self._name = name
        self._ip: Optional[str] = None
        self._ap_name: Optional[str] = None
        self._manufacturer: Optional[str] = None
        self._os_type: Optional[str] = None
        self._rssi: Optional[int] = None
        self._is_home: bool = True
        self._attrs: Dict[str, Any] = {}

    @property
    def unique_id(self) -> str:
        return f"aruba_central_{self._mac}"

    @property
    def name(self) -> str:
        return self._name

    @property
    def source_type(self) -> str:
        return SOURCE_TYPE_ROUTER

    @property
    def is_connected(self) -> bool:
        return self._is_home

    @property
    def mac_address(self) -> str:
        return self._mac

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        return self._attrs

    def update_from_api(self, *, ip: Optional[str], ap_name: Optional[str],
                        manufacturer: Optional[str], os_type: Optional[str], rssi: Optional[int]):
        self._ip = ip
        self._ap_name = ap_name
        self._manufacturer = manufacturer
        self._os_type = os_type
        self._rssi = rssi
        self._is_home = True
        self._attrs = {
            "ip": self._ip,
            "associated_device": self._ap_name,
            "manufacturer": self._manufacturer,
            "os_type": self._os_type,
            "rssi": self._rssi,
        }

    def seen(self, now: bool):
        self._is_home = now
