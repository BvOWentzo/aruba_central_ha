import json
import time
import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.components.device_tracker import (
    PLATFORM_SCHEMA as BASE_PLATFORM_SCHEMA,
    DeviceScanner,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)
_LOGGER.warning("aruba_central(DeviceScanner): import OK")

CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_API_BASE = "api_base"
CONF_OAUTH_BASE = "oauth_base"
CONF_CUSTOMER_ID = "customer_id"
CONF_GROUP = "group"
CONF_SITE = "site"
CONF_CLIENT_TYPE = "client_type"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_CLIENT_TYPE = "WIRELESS"
DEFAULT_SCAN_INTERVAL = 60

SCAN_INTERVAL = timedelta(seconds=60)

PLATFORM_SCHEMA = BASE_PLATFORM_SCHEMA.extend({
    vol.Required(CONF_CLIENT_ID): cv.string,
    vol.Required(CONF_CLIENT_SECRET): cv.string,
    vol.Required(CONF_REFRESH_TOKEN): cv.string,
    vol.Required(CONF_API_BASE): cv.string,
    vol.Optional(CONF_OAUTH_BASE): cv.string,
    vol.Optional(CONF_CUSTOMER_ID): cv.string,
    vol.Optional(CONF_GROUP): cv.string,
    vol.Optional(CONF_SITE): cv.string,
    vol.Optional(CONF_CLIENT_TYPE, default=DEFAULT_CLIENT_TYPE): vol.In(["WIRELESS", "WIRED", "ALL"]),
    vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): cv.positive_int,
})


async def async_get_scanner(hass: HomeAssistant, config: dict) -> Optional[DeviceScanner]:
    session = async_get_clientsession(hass)
    return ArubaCentralScanner(
        session=session,
        api_base=config[CONF_API_BASE],
        oauth_base=config.get(CONF_OAUTH_BASE) or config[CONF_API_BASE],
        client_id=config[CONF_CLIENT_ID],
        client_secret=config[CONF_CLIENT_SECRET],
        refresh_token=config[CONF_REFRESH_TOKEN],
        customer_id=config.get(CONF_CUSTOMER_ID),
        group=config.get(CONF_GROUP),
        site=config.get(CONF_SITE),
        client_type=config.get(CONF_CLIENT_TYPE),
        min_interval=int(config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    )


class ArubaCentralScanner(DeviceScanner):
    def __init__(self, session, api_base, oauth_base, client_id, client_secret, refresh_token, customer_id,
                 group, site, client_type, min_interval):
        self._session = session
        self._api_base = api_base.rstrip("/")
        self._oauth_base = oauth_base.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._customer_id = customer_id
        self._group = group
        self._site = site
        self._client_type = client_type
        self._min_interval = max(10, int(min_interval))
        self._access_token = None
        self._token_expiry = 0
        self._last_fetch = 0
        self._cached_clients = []
        self._clients_by_mac = {}

    async def _ensure_token(self):
        now = time.time()
        if self._access_token and now < self._token_expiry - 60:
            _LOGGER.warning("aruba_central(DeviceScanner): using cached token")
            return

        url = f"{self._oauth_base}/oauth2/token"
        data = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token
        }

        _LOGGER.warning("aruba_central(DeviceScanner): requesting new token from %s", url)
        try:
            async with self._session.post(url, data=data) as resp:
                text = await resp.text()
                _LOGGER.warning("aruba_central(DeviceScanner): token POST status %s: %s", resp.status, text)
                if resp.status != 200:
                    raise Exception(f"Token refresh failed: {text}")
                result = json.loads(text)
                self._access_token = result["access_token"]
                self._refresh_token = result.get("refresh_token", self._refresh_token)
                self._token_expiry = time.time() + result.get("expires_in", 3600)
        except Exception as e:
            _LOGGER.error("aruba_central(DeviceScanner): token refresh EXCEPTION: %s", e)
            raise

    async def async_scan_devices(self) -> List[str]:
        now = time.time()
        age = now - self._last_fetch

        _LOGGER.warning("aruba_central(DeviceScanner): scan called at %.1f (age: %.1fs)", now, age)

        if self._last_fetch == 0:
            _LOGGER.warning("aruba_central(DeviceScanner): DECISION first run → call Central")
        elif age >= self._min_interval:
            _LOGGER.warning("aruba_central(DeviceScanner): DECISION age %ss >= min %ss → call Central", int(age), self._min_interval)
        else:
            _LOGGER.warning("aruba_central(DeviceScanner): DECISION age %ss < min %ss → use cache", int(age), self._min_interval)
            return [c.get("macaddr", "").lower() for c in self._cached_clients if "macaddr" in c]

        try:
            await self._ensure_token()
            headers = {"Authorization": f"Bearer {self._access_token}"}
            if self._customer_id:
                headers["TenantID"] = self._customer_id

            url = f"{self._api_base}/monitoring/v2/clients"
            params = {
                "client_status": "CONNECTED",
                "client_type": self._client_type,
                "limit": 1000
            }
            if self._group:
                params["group"] = self._group
            elif self._site:
                params["site"] = self._site

            _LOGGER.warning("aruba_central(DeviceScanner): GET %s with headers %s and params %s", url, headers, params)
            async with self._session.get(url, headers=headers, params=params) as resp:
                text = await resp.text()
                _LOGGER.warning("aruba_central(DeviceScanner): API status %s: %s", resp.status, text)
                if resp.status != 200:
                    raise Exception(f"Central fetch failed: {text}")
                data = json.loads(text)
                self._cached_clients = data.get("data", []) or data.get("clients", [])
                self._last_fetch = now
                self._clients_by_mac = {
                    c.get("macaddr", "").lower(): {
                        "ip": c.get("ipaddr") or c.get("ip_address"),
                        "name": c.get("name") or c.get("hostname") or c.get("macaddr")
                    }
                    for c in self._cached_clients if "macaddr" in c
                }
        except Exception as e:
            _LOGGER.error("aruba_central(DeviceScanner): scan error: %s", e)

        return list(self._clients_by_mac.keys())

    async def async_get_device_name(self, device: str) -> Optional[str]:
        return self._clients_by_mac.get(device.lower(), {}).get("name")

    async def async_get_extra_attributes(self, device: str) -> Dict[str, Any]:
        client = self._clients_by_mac.get(device.lower(), {})
        return {"ip": client.get("ip")} if "ip" in client else {}
