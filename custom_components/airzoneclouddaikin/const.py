"""Constants for DKN Cloud for HASS integration.

This file includes API endpoints and basic configuration constants.
"""

from __future__ import annotations

# Domain key (must match manifest.json and the integration folder name)
DOMAIN = "airzoneclouddaikin"

# Public manufacturer label used across platforms (Device Registry consistency).
MANUFACTURER = "Daikin / Airzone"

# ----------------------------- API Endpoints ------------------------------
API_LOGIN = "/users/sign_in"
API_LOGOUT = "/users/sign_out"
API_INSTALLATION_RELATIONS = "/installation_relations"
API_DEVICES = "/devices"
API_EVENTS = "/events"

# Base URL for the API
BASE_URL = "https://dkn.airzonecloud.com"

# ----------------------------- HTTP Defaults ------------------------------
# Standard User-Agent to be used in all API requests
# (Kept browser-like; do not disclose Home Assistant in UA)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

# Global HTTP timeout (seconds).
REQUEST_TIMEOUT = 30

# --- Endpoint-specific minimal headers (browser-like) ---------------------
HEADERS_EVENTS = {
    # "User-Agent" is intentionally omitted here to avoid duplication;
    # _request() always injects the global USER_AGENT.
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
}

# --- Shared optimistic timings (used by climate/switch/number) ----
OPTIMISTIC_TTL_SEC: float = 2.5
POST_WRITE_REFRESH_DELAY_SEC: float = 1.0

# -------------------- UX: Persistent notifications (PR A) -----------------
# Debounce to avoid flapping (seconds the device must remain offline
# before raising a notification).
OFFLINE_DEBOUNCE_SEC = 90

# Time to auto-dismiss the "back online" banner (seconds).
ONLINE_BANNER_TTL_SEC = 20

# Notification ID prefix (stable per device_id to avoid duplicates).
PN_KEY_PREFIX = f"{DOMAIN}:wserver_offline:"

# ---------------- Connectivity / online sensors (fixed policy) ------------
# Fixed internal threshold for passive connectivity sensors (e.g. wserver_online):
# If last contact age <= 10 minutes, consider "online" (notifications add a 90s debounce).
INTERNAL_STALE_AFTER_SEC = 10 * 60
