from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import voluptuous as vol

from homeassistant.components.device_tracker import (
    PLATFORM_SCHEMA as BASE_PLATFORM_SCHEMA,
    DeviceScanner,
    DOMAIN as DEVICE_TRACKER_DOMAIN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

# Laat HA ons ~elke 60s aanroepen i.p.v. ~12s (legacy scheduler)
SCAN_INTERVAL = timedelta(seconds=60)

# Config keys
CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_CUSTOMER_ID = "customer_id"
CONF_API_BASE = "api_base"
CONF_OAUTH_BASE = "oauth_base"
CONF_GROUP = "group"
CONF_SITE = "site"
CONF_CLIENT_TYPE = "client_type"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_CLIENT_TYPE = "WIRELESS"
DEFAULT_SCAN_INTERVAL_S = 60


def _flatten_conf(config: dict) -> dict:
    """Ondersteun zowel {device_tracker:{...}} als platte dict."""
    if DEVICE_TRACKER_DOMAIN in config and isinstance(config[DEVICE_TRACKER_DOMAIN], dict):
        return config[DEVICE_TRACKER_DOMAIN]
    return config


def _parse_scan_interval(val) -> int:
    """Accepteer int seconden of 'HH:MM:SS' string. Fallback op default."""
    if val is None:
        return DEFAULT_SCAN_INTERVAL_S
    if isinstance(val, int):
        return max(5, int(val))
    if isinstance(val, str):
        parts = val.split(":")
        try:
            if len(parts) == 3:
                h, m, s = (int(p) for p in parts)
                return max(5, h * 3600 + m * 60 + s)
            if len(parts) == 2:
                m, s = (int(p) for p in parts)
                return max(5, m * 60 + s)
            return max(5, int(val))
        except Exception:
            _LOGGER.error(
                "aruba_central(DeviceScanner): invalid scan_interval '%s', using default %ss",
                val, DEFAULT_SCAN_INTERVAL_S
            )
            return DEFAULT_SCAN_INTERVAL_S
    try:
        return max(5, int(val.total_seconds()))  # type: ignore[attr-defined]
    except Exception:
        return DEFAULT_SCAN_INTERVAL_S


PLATFORM_SCHEMA = BASE_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_CLIENT_ID): cv.string,
        vol.Required(CONF_CLIENT_SECRET): cv.string,
        vol.Required(CONF_REFRESH_TOKEN): cv.string,
        vol.Required(CONF_API_BASE): cv.url,
        vol.Optional(CONF_OAUTH_BASE): cv.url,  # default = api_base
        vol.Optional(CONF_CUSTOMER_ID): cv.string,
        vol.Optional(CONF_GROUP): cv.string,
        vol.Optional(CONF_SITE): cv.string,
        vol.Optional(CONF_CLIENT_TYPE, default=DEFAULT_CLIENT_TYPE): vol.In(["WIRELESS", "WIRED", "ALL"]),
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL_S): vol.Any(cv.positive_int, cv.string),
    }
)


async def async_get_scanner(hass: HomeAssistant, config: dict) -> Optional[DeviceScanner]:
    _LOGGER.warning("aruba_central(DeviceScanner): async_get_scanner START")
    conf = _flatten_conf(config)

    for k in (CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_REFRESH_TOKEN, CONF_API_BASE):
        if k not in conf:
            _LOGGER.error("aruba_central(DeviceScanner): missing required option: %s", k)
            return None

    min_interval_s = _parse_scan_interval(conf.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_S))
    _LOGGER.warning("aruba_central(DeviceScanner): Central throttle set to %ss", min_interval_s)

    session = async_get_clientsession(hass)
    api_base = conf[CONF_API_BASE].rstrip("/")
    oauth_base = (conf.get(CONF_OAUTH_BASE) or api_base).rstrip("/")

    scanner = ArubaCentralScanner(
        hass=hass,
        session=session,
        api_base=api_base,
        oauth_base=oauth_base,
        client_id=conf[CONF_CLIENT_ID],
        client_secret=conf[CONF_CLIENT_SECRET],
        refresh_token=conf[CONF_REFRESH_TOKEN],
        customer_id=conf.get(CONF_CUSTOMER_ID),
        group=conf.get(CONF_GROUP),
        site=conf.get(CONF_SITE),
        client_type=conf.get(CONF_CLIENT_TYPE, DEFAULT_CLIENT_TYPE),
        min_interval_s=min_interval_s,
    )
    await scanner.async_init()
    _LOGGER.warning("aruba_central(DeviceScanner): async_get_scanner DONE")
    return scanner


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
            _LOGGER.debug(
                "aruba_central(DeviceScanner): using cached token (expires_in=%ss)",
                int(self.expiry - time.time()),
            )
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
        self.refresh_token = j.get("refresh_token", self.refresh_token)
        self.expiry = time.time() + int(j.get("expires_in", 3600))
        _LOGGER.warning("aruba_central(DeviceScanner): token ok; expires_in=%s", j.get("expires_in"))

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

        _LOGGER.warning("aruba_central(DeviceScanner): GET %s params=%s", url, params)
        async with self.s.get(url, headers=self._headers(), params=params, timeout=30) as r:
            txt = await r.text()
            if r.status != 200:
                _LOGGER.error("aruba_central(DeviceScanner): clients fetch failed %s: %s", r.status, txt)
                raise RuntimeError(f"clients fetch failed {r.status}: {txt}")
            data = await r.json()

        items = data.get("data") or data.get("clients") or []
        _LOGGER.warning("aruba_central(DeviceScanner): total clients returned=%s", len(items))
        return items


class ArubaCentralScanner(DeviceScanner):
    def __init__(
        self,
        *,
        hass: HomeAssistant,
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
        min_interval_s: int,
    ):
        self._hass = hass
        self._api = _CentralAPI(
            session=session,
            api_base=api_base,
            oauth_base=oauth_base,
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            customer_id=customer_id,
        )
        self._group = group
        self._site = site
        self._client_type = client_type
        self._min_interval_s = max(5, int(min_interval_s))
        self._last_fetch_ts: float = 0.0
        self._cache_clients: List[Dict[str, Any]] = []
        self._last_by_mac: Dict[str, Dict[str, Any]] = {}

    async def async_init(self):
        _LOGGER.warning(
            "aruba_central(DeviceScanner): init api_base=%s oauth_base=%s group=%s site=%s type=%s min=%ss",
            self._api.api_base, self._api.oauth_base, self._group, self._site, self._client_type, self._min_interval_s
        )
        try:
            await self._api._ensure_token()
        except Exception as e:
            _LOGGER.error("aruba_central(DeviceScanner): initial token refresh failed: %s", e)

    async def _fetch_if_needed(self):
        now = time.time()
        age = now - self._last_fetch_ts
        if self._last_fetch_ts and age < self._min_interval_s:
            _LOGGER.warning(
                "aruba_central(DeviceScanner): HA poll; prev_age=%ss; throttle=%ss → cache only",
                int(age), self._min_interval_s
            )
            return
        _LOGGER.warning("aruba_central(DeviceScanner): DECISION first run or min age met → call Central")
        clients = await self._api.list_clients(group=self._group, site=self._site, client_type=self._client_type)
        self._cache_clients = clients
        self._last_fetch_ts = now
        _LOGGER.warning("aruba_central(DeviceScanner): fetched %s clients (API)", len(clients))

    async def async_scan_devices(self) -> List[str]:
        _LOGGER.warning("aruba_central(DeviceScanner): async_scan_devices() called by HA")
        try:
            await self._fetch_if_needed()
            clients = list(self._cache_clients)
        except Exception as e:
            _LOGGER.error("aruba_central(DeviceScanner): scan failed: %s", e)
            return list(self._last_by_mac.keys())

        macs: List[str] = []
        new_by_mac: Dict[str, Dict[str, Any]] = {}

        for c in clients:
            mac = (c.get("macaddr") or c.get("mac") or "").lower()
            if not mac:
                continue
            macs.append(mac)
            new_by_mac[mac] = {
                "ip": c.get("ipaddr") or c.get("ip_address"),
                "name": c.get("name") or c.get("hostname") or mac,
            }

        # Direct not_home voor verdwenen clients
        offline_macs = set(self._last_by_mac.keys()) - set(new_by_mac.keys())
        for mac in offline_macs:
            _LOGGER.debug("aruba_central(DeviceScanner): %s → not_home (missing in latest Central list)", mac)
            await self.async_see(mac, host_name=mac, location_name="not_home")

        self._last_by_mac = new_by_mac
        _LOGGER.warning("aruba_central(DeviceScanner): returning %s devices to HA", len(macs))
        return macs

    async def async_get_device_name(self, device: str) -> Optional[str]:
        info = self._last_by_mac.get(device.lower())
        return info.get("name") if info else None

    async def async_get_extra_attributes(self, device: str) -> Dict[str, Any]:
        info = self._last_by_mac.get(device.lower()) or {}
        out: Dict[str, Any] = {"mac": device.lower()}
        if "ip" in info and info["ip"]:
            out["ip"] = info["ip"]
        return out

    async def async_see(self, mac: str, host_name: Optional[str] = None, location_name: str = "home"):
        # Gebruik de service call (stabiel in huidige HA builds), en markeer als router bron
        payload = {
            "mac": mac,
            "host_name": host_name,
            "location_name": location_name,
            "source_type": "router",
        }
        await self._hass.services.async_call("device_tracker", "see", payload, blocking=False)
