"""Constants for DKN Cloud for HASS integration.

This file includes API endpoints and basic configuration constants.
"""

DOMAIN = "airzoneclouddaikin"
CONF_USERNAME = "username"  # Your Airzone Cloud account email
CONF_PASSWORD = "password"  # Your Airzone Cloud account password

# API Endpoints (as defined in the original package)
API_LOGIN = "/users/sign_in"
API_INSTALLATION_RELATIONS = "/installation_relations"
API_DEVICES = "/devices"
API_EVENTS = "/events"

# Base URL for the API
BASE_URL = "https://dkn.airzonecloud.com"

# Standard User-Agent to be used in all API requests
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
