import asyncio
import logging
from datetime import timedelta, datetime
import aiohttp
import async_timeout
import voluptuous as vol
from homeassistant.components.device_tracker import DOMAIN
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import slugify

_LOGGER = logging.getLogger(__name__)

DEFAULT_SCAN_INTERVAL_S = 60
SCAN_INTERVAL = timedelta(seconds=DEFAULT_SCAN_INTERVAL_S)

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required("client_id"): str,
        vol.Required("client_secret"): str,
        vol.Required("refresh_token"): str,
        vol.Required("customer_id"): str,
        vol.Required("group"): str,
        vol.Required("api_base"): str,
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL_S): vol.Any(int),
    })
}, extra=vol.ALLOW_EXTRA)

async def async_get_scanner(hass, config):
    conf = config[DOMAIN]
    scanner = ArubaCentralScanner(hass, conf)
    await scanner.async_initialize()
    return scanner

class ArubaCentralScanner:
    def __init__(self, hass, config):
        self._hass = hass
        self._client_id = config["client_id"]
        self._client_secret = config["client_secret"]
        self._refresh_token = config["refresh_token"]
        self._customer_id = config["customer_id"]
        self._group = config["group"]
        self._api_base = config["api_base"]
        self._scan_interval = timedelta(seconds=config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_S))

        self._access_token = None
        self._last_fetch = None
        self._last_by_mac = {}
        self._last_seen = {}

        self._session = async_get_clientsession(hass)

    async def async_initialize(self):
        await self._refresh_access_token()
        async_track_time_interval(
            self._hass, self._scheduled_update, self._scan_interval
        )

    async def _scheduled_update(self, now):
        try:
            await self._update_clients()
        except Exception as e:
            _LOGGER.error("Scheduled update failed: %s", e)

    async def async_scan_devices(self):
        _LOGGER.debug("HA requested a device scan (poll)")
        await self._maybe_update_clients()
        self._expire_old_devices()
        return list(self._last_by_mac.keys())

    async def async_get_device_name(self, device):
        client = self._last_by_mac.get(device.upper())
        if client:
            return client.get("hostname") or client.get("ip_address")
        return None

    async def _maybe_update_clients(self):
        if not self._last_fetch or (datetime.utcnow() - self._last_fetch) > self._scan_interval:
            await self._update_clients()
        else:
            _LOGGER.debug("Using cached client data")

    async def _update_clients(self):
        await self._refresh_access_token()

        url = f"{self._api_base}/monitoring/v2/clients"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        params = {
            "client_status": "CONNECTED",
            "client_type": "WIRELESS",
            "group": self._group,
            "limit": 1000,
        }

        try:
            async with async_timeout.timeout(10):
                async with self._session.get(url, headers=headers, params=params) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"Client fetch failed {resp.status}: {await resp.text()}")
                    data = await resp.json()
                    self._process_client_list(data.get("clients", []))
                    self._last_fetch = datetime.utcnow()
                    _LOGGER.warning("Fetched %d clients", len(self._last_by_mac))
        except Exception as e:
            _LOGGER.error("Failed to fetch clients: %s", e)

    def _process_client_list(self, clients):
        new_clients = {}
        now = datetime.utcnow()
        for client in clients:
            mac = client.get("mac_address", "").upper()
            if mac:
                new_clients[mac] = client
                self._last_seen[mac] = now
        self._last_by_mac = new_clients

    def _expire_old_devices(self):
        now = datetime.utcnow()
        timeout = self._scan_interval.total_seconds() + 30
        expired = [mac for mac, last in self._last_seen.items() if (now - last).total_seconds() > timeout]
        for mac in expired:
            self._last_by_mac.pop(mac, None)
            _LOGGER.debug("Device expired due to timeout: %s", mac)

    async def _refresh_access_token(self):
        url = f"{self._api_base}/oauth2/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
        }

        try:
            async with async_timeout.timeout(10):
                async with self._session.post(url, headers=headers, data=data) as resp:
                    if resp.status != 200:
                        txt = await resp.text()
                        raise RuntimeError(f"Token refresh failed {resp.status}: {txt}")
                    auth = await resp.json()
                    self._access_token = auth.get("access_token")
                    _LOGGER.debug("Access token refreshed")
        except Exception as e:
            _LOGGER.error("Failed to refresh access token: %s", e)
            raise
