"""Module to interact with the Airzone Cloud API (adapted for dkn.airzonecloud.com).

This module implements:
- Authentication via the /users/sign_in endpoint.
- Fetching installations via the /installation_relations endpoint.
- Fetching devices for a given installation via the /devices endpoint.
- Sending events via the /events endpoint.
Endpoints and constants are imported from const.py.
"""

import logging
import aiohttp
from typing import List, Dict
from .const import API_LOGIN, API_INSTALLATION_RELATIONS, API_DEVICES, API_EVENTS, BASE_URL, USER_AGENT

_LOGGER = logging.getLogger(__name__)

class AirzoneAPI:
    """Client to interact with the Airzone Cloud API."""

    def __init__(self, username: str, password: str, session: aiohttp.ClientSession):
        """Initialize with user credentials and an aiohttp session."""
        self._username = username
        self._password = password
        self._session = session
        self.token: str = ""
        self.installations: List[Dict] = []

    async def login(self) -> bool:
        """Authenticate with the API and obtain a token.

        Sends a POST request to the /users/sign_in endpoint.
        Returns True if successful, False otherwise.
        """
        url = f"{BASE_URL}{API_LOGIN}"
        payload = {"email": self._username, "password": self._password}
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json"
        }
        try:
            async with self._session.post(url, json=payload, headers=headers) as response:
                if response.status == 201:
                    data = await response.json()
                    self.token = data.get("user", {}).get("authentication_token", "")
                    if self.token:
                        _LOGGER.debug("Login successful, token: %s", self.token)
                        return True
                    else:
                        _LOGGER.error("Login failed: No token received.")
                        return False
                else:
                    _LOGGER.error("Login failed, status code: %s", response.status)
                    return False
        except Exception as err:
            _LOGGER.error("Exception during login: %s", err)
            return False

    async def fetch_installations(self) -> List[Dict]:
        """Fetch installations using the obtained token.

        Sends a GET request to the /installation_relations endpoint.
        Returns a list of installations if successful.
        """
        if not self.token:
            _LOGGER.error("Cannot fetch installations without a valid token.")
            return []
        url = f"{BASE_URL}{API_INSTALLATION_RELATIONS}"
        params = {"format": "json", "user_email": self._username, "user_token": self.token}
        headers = {"User-Agent": USER_AGENT}
        try:
            async with self._session.get(url, params=params, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    installations = data.get("installation_relations", [])
                    _LOGGER.debug("Fetched installations: %s", installations)
                    return installations
                else:
                    _LOGGER.error("Failed to fetch installations, status code: %s", response.status)
                    return []
        except Exception as err:
            _LOGGER.error("Exception fetching installations: %s", err)
            return []

    async def fetch_devices(self, installation_id: str) -> List[Dict]:
        """Fetch devices for a given installation using the obtained token.

        Sends a GET request to the /devices endpoint with the installation_id parameter.
        Returns a list of devices if successful.
        """
        url = f"{BASE_URL}{API_DEVICES}"
        params = {
            "format": "json",
            "installation_id": installation_id,
            "user_email": self._username,
            "user_token": self.token
        }
        headers = {"User-Agent": USER_AGENT}
        try:
            async with self._session.get(url, params=params, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    devices = data.get("devices", [])
                    _LOGGER.debug("Fetched devices for installation %s: %s", installation_id, devices)
                    return devices
                else:
                    _LOGGER.error("Failed to fetch devices for installation %s, status code: %s", installation_id, response.status)
                    return []
        except Exception as err:
            _LOGGER.error("Exception fetching devices: %s", err)
            return []

    async def send_event(self, payload: dict) -> dict:
        """Send an event to the API via the /events endpoint."""
        url = f"{BASE_URL}{API_EVENTS}"
        params = {"format": "json", "user_email": self._username, "user_token": self.token}
        headers = {
            "User-Agent": USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*"
        }
        async with self._session.post(url, json=payload, params=params, headers=headers) as response:
            response.raise_for_status()
            return await response.json()
