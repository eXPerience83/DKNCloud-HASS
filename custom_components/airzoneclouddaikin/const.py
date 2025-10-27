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
API_INSTALLATION_RELATIONS = "/installation_relations"
API_DEVICES = "/devices"
API_EVENTS = "/events"

# Base URL for the API
BASE_URL = "https://dkn.airzonecloud.com"

# ----------------------------- HTTP Defaults ------------------------------
# Standard User-Agent to be used in all API requests
# (Kept browser-like; do not disclose Home Assistant in UA)
# P2: slightly more generic UA to reduce fingerprinting.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
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
# UI min/max are validated in config_flow.py only (range 6..30).

# -------------------- UX: Persistent notifications (PR A) -----------------
# English: Debounce to avoid flapping (seconds the device must remain offline
# before raising a notification).
OFFLINE_DEBOUNCE_SEC = 90

# English: Time to auto-dismiss the "back online" banner (seconds).
ONLINE_BANNER_TTL_SEC = 20

# English: Notification ID prefix (stable per device_id to avoid duplicates).
PN_KEY_PREFIX = f"{DOMAIN}:wserver_offline:"

# English: Minimal i18n templates (kept here to avoid runtime i18n complexity).
# We prefer Spanish if hass.config.language starts with "es"; otherwise English.
PN_TITLES = {
    "en": {
        "offline": "DKN Cloud — {name} offline",
        "online": "DKN Cloud — {name} back online",
    },
    "es": {
        "offline": "DKN Cloud — {name} sin conexión",
        "online": "DKN Cloud — {name} en línea de nuevo",
    },
}

PN_MESSAGES = {
    "en": {
        # Keep short and privacy-safe; do not include PII (email/token/MAC/GPS).
        "offline": (
            "Connection lost at {ts_local}. "
            "Last contact: {last_iso} (about {mins} min ago)."
        ),
        "online": "Connection restored at {ts_local}.",
    },
    "es": {
        "offline": (
            "Conexión perdida a las {ts_local}. "
            "Último contacto: {last_iso} (hace ~{mins} min)."
        ),
        "online": "Conexión restablecida a las {ts_local}.",
    },
}
