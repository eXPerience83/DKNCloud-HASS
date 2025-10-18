"""Constants for DKN Cloud for HASS integration.

This file includes API endpoints and basic configuration constants.
"""

DOMAIN = "airzoneclouddaikin"
CONF_USERNAME = "username"  # Your Airzone Cloud account email
CONF_PASSWORD = "password"  # Your Airzone Cloud account password

# Public manufacturer label used across platforms (Device Registry consistency).
MANUFACTURER = "Daikin / Airzone"

# API Endpoints (as defined in the original package)
API_LOGIN = "/users/sign_in"
API_INSTALLATION_RELATIONS = "/installation_relations"
API_DEVICES = "/devices"
API_EVENTS = "/events"

# Base URL for the API
BASE_URL = "https://dkn.airzonecloud.com"

# Standard User-Agent to be used in all API requests
# (Kept as provided by the project; do not disclose Home Assistant in UA)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36"
)

# Global HTTP timeout (seconds).
# English: Align with HA expectations and slow links; was 15, now 30 by default.
REQUEST_TIMEOUT = 30

# --- Endpoint-specific minimal headers (browser-like) ---------------------
# English: Keep headers minimal and consistent with the cURL examples.
# - GET /devices: only a browser-like User-Agent.
HEADERS_DEVICES = {
    "User-Agent": USER_AGENT,
}

# - POST /events: JSON payload plus XHR-style headers as per the cURL example.
HEADERS_EVENTS = {
    "User-Agent": USER_AGENT,
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
}

# --- Shared optimistic timings (used by climate/switch/number/select) ----
# English: Centralized values to keep UX consistent and ease future tuning.
OPTIMISTIC_TTL_SEC: float = 2.5
POST_WRITE_REFRESH_DELAY_SEC: float = 1.0

# ---------------- Connectivity options (passive, without pings) -----------
# English: Threshold to consider the device offline when `connection_date` gets too old.
CONF_STALE_AFTER_MINUTES = "stale_after_minutes"
STALE_AFTER_MINUTES_DEFAULT = 10
STALE_AFTER_MINUTES_MIN = 6
STALE_AFTER_MINUTES_MAX = 30
