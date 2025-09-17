# AOS10 via Aruba Central - AOS8-stijl DeviceScanner (presence op MAC)
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp
import voluptuous as vol

from homeassistant.components.device_tracker import PLATFORM_SCHEMA as BASE_PLATFORM_SCHEMA
from homeassistant.components.device_tracker import DeviceScanner
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
_LOGGER.warning("aruba_central(DeviceScanner): module import OK")  # hoor je altijd bij succesvol laden

# ---- Config ----
CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_CUSTOMER_ID = "customer_id"      # optioneel (MSP -> TenantID header)
CONF_API_BASE = "api_base"            # bv. https://eu-apigw.central.arubanetworks.com
CONF_OAUTH_BASE = "oauth_base"        # optioneel; default = api_base
CONF_GROUP = "group"                  # optioneel (naam of GUID)
CONF_SITE = "site"                    # optioneel
CONF_CLIENT_TYPE = "client_type"      # WIRELESS | WIRED | ALL

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
        vol.Optional(CONF_CLIENT_TYPE, default=DEFAULT_CLIENT_TYPE): vol.In(["WIRELESS", "WIRED", "ALL"]),
        # GEEN scan_interval hier: DeviceScanner timing regelt HA zelf, zoals bij AOS8.
    }
)

async def async_get_scanner(hass: HomeAssistant, config: dict) -> DeviceScanner:
    """Wordt door HA aangeroepen; geef een DeviceScanner terug."""
    _LOGGER.warning("aruba_central(DeviceScanner): async_get_scanner START")
    session = async_get_clientsession(hass)
    api_base = config[CONF_API_BASE].rstrip("/")
    oauth_base = (config.get(CONF_OAUTH_BASE) or api_base).rstrip("/")

    scanner = ArubaCentralScanner(
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
    )
    await scanner.async_init()
    _LOGGER.warning("aruba_central(DeviceScanner): async_get_scanner DONE")
    return scanner


# ---------- Central API helper ----------
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
        # vernieuw 60s vóór expiry
        if self.access_token and time.time() < self.expiry - 60:
            return
        url = f"{self.oauth_base}/oauth2/token"
        data = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
        }
        _LOGGER.warning("aruba_central(DeviceScanner): POST %s (OAuth refresh)", url)
        async with self.s.post(url, data=data, timeout=30) as r:
            txt = await r.text()
            if r.status != 200:
                _LOGGER.error("aruba_central(DeviceScanner): token refresh failed %s: %s", r.status, txt)
                raise RuntimeError(f"Token refresh failed {r.status}: {txt}")
            j = await r.json()
        self.access_token = j.get("access_token")
        self.refresh_token = j.get("refresh_token", self.refresh_token)  # soms nieuw
        self.expiry = time.time() + int(j.get("expires_in", 3600))
        _LOGGER.warning("aruba_central(DeviceScanner): token ok; expires_in=%s", j.get("expires_in"))

    def _headers(self) -> Dict[str, str]:
        h = {"Authorization": f"Bearer {self.access_token}"}
        if self.customer_id:
            h["TenantID"] = self.customer_id  # MSP header
        return h

    async def list_clients(self, *, group: Optional[str], site: Optional[str], client_type: str) -> List[Dict[str, Any]]:
        """GET /monitoring/v2/clients (CONNECTED) met eenvoudige paginatie."""
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
            _LOGGER.warning("aruba_central(DeviceScanner): GET %s params=%s", url, q)
            async with self.s.get(url, headers=self._headers(), params=q, timeout=30) as r:
                txt = await r.text()
                if r.status != 200:
                    _LOGGER.error("aruba_central(DeviceScanner): clients fetch failed %s: %s", r.status, txt)
                    raise RuntimeError(f"clients fetch failed {r.status}: {txt}")
                data = await r.json()
            chunk = data.get("data") or data.get("clients") or []
            items.extend(chunk)
            last = data.get("last_client_mac")
            if not last or not chunk:
                break
        return items


# ---------- De echte DeviceScanner ----------
class ArubaCentralScanner(DeviceScanner):
    """AOS8-achtige scanner: levert alleen MAC-adressen; HA beheert presence-entities."""

    def __init__(self, **kw):
        self._api = _CentralAPI(
            session=kw["session"],
            api_base=kw["api_base"],
            oauth_base=kw["oauth_base"],
            client_id=kw["client_id"],
            client_secret=kw["client_secret"],
            refresh_token=kw["refr]()_
