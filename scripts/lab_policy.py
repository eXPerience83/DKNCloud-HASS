"""Disabled-by-default read-only boundary for a supervised integration experiment."""

from aiohttp import ClientSession, TraceConfig
from yarl import URL


class ReadOnlyPolicy:
    """Validate every destination and operation before any network delegation."""

    def __init__(self, devices, installations, app_key, *, enabled=False):
        self.devices = frozenset(devices)
        self.installations = frozenset(installations)
        self.app_key = app_key
        self.enabled = enabled

    def validate(self, method, url, *, params=None, json=None, data=None):
        target = URL(url)
        if (
            target.origin() != URL("https://dkn.airzonecloud.com")
            or target.user is not None
            or target.fragment
            or target.query
        ):
            raise PermissionError("Destination denied")
        params = params or {}
        if set(params) - {"user_email", "user_token", "format", "installation_id"}:
            raise PermissionError("Parameters denied")
        if (
            "installation_id" in params
            and str(params["installation_id"]) not in self.installations
        ):
            raise PermissionError("Installation denied")
        path = target.path
        permitted = (
            method == "GET"
            and path in {"/devices", "/installation_relations"}
            and json is None
            and data is None
        )
        permitted |= (
            method == "POST"
            and path == "/users/sign_in"
            and not params
            and data is None
            and isinstance(json, dict)
            and set(json) == {"email", "password"}
        )
        permitted |= (
            method == "POST"
            and path == "/pusher/auth"
            and not params
            and json is None
            and isinstance(data, dict)
            and set(data) == {"user_email", "user_token", "socket_id", "channel_name"}
            and data.get("channel_name") in {f"private-{d}" for d in self.devices}
        )
        event = json.get("event") if isinstance(json, dict) else None
        permitted |= (
            method == "POST"
            and path == "/events"
            and data is None
            and isinstance(event, dict)
            and set(json) == {"event"}
            and set(event) == {"cgi", "device_id", "option", "value"}
            and event["cgi"] == "infomaquina"
            and str(event["device_id"]) in self.devices
            and event["option"] == event["value"] == ""
        )
        if not permitted:
            raise PermissionError("Operation denied")

    def validate_socket(self, url):
        target = URL(url)
        if (
            target.origin() != URL("wss://ws.pusherapp.com")
            or target.user is not None
            or target.fragment
            or target.path != f"/app/{self.app_key}"
            or len(target.query) != 3
            or dict(target.query)
            != {"protocol": "7", "client": "python-dkncloud", "version": "1.0"}
        ):
            raise PermissionError("Socket denied")


class ProtectedSession:
    """Use the actual integration session without redirects or alternate transports."""

    def __init__(self, session, policy, *, redirect_guard=False):
        self.session, self.policy = session, policy
        self._redirect_guard = redirect_guard

    def request(self, method, url, **kwargs):
        if not self.policy.enabled:
            raise PermissionError("Real network experiment disabled")
        if set(kwargs) - {"params", "json", "data", "headers", "timeout"}:
            raise PermissionError("Request options denied")
        if any(
            k.lower() in {"host", "authorization", "cookie"}
            for k in kwargs.get("headers", {})
        ):
            raise PermissionError("Header denied")
        self.policy.validate(
            method, url, **{k: kwargs.get(k) for k in ("params", "json", "data")}
        )
        return self.session.request(method, url, allow_redirects=False, **kwargs)

    def ws_connect(self, url, **kwargs):
        if (
            not self.policy.enabled
            or not self._redirect_guard
            or set(kwargs) - {"autoping", "max_msg_size"}
        ):
            raise PermissionError("Socket options denied")
        self.policy.validate_socket(url)
        return self.session.ws_connect(url, **kwargs)


async def create_protected_session(policy):
    """Own a session whose HTTP and WebSocket handshakes reject redirects."""

    async def deny_redirect(*_):
        raise PermissionError("Redirect denied")

    trace = TraceConfig()
    trace.on_request_redirect.append(deny_redirect)
    session = ClientSession(trace_configs=[trace], trust_env=False)
    return ProtectedSession(session, policy, redirect_guard=True)
