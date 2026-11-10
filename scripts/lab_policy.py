"""Disabled-by-default read-only boundary for a supervised integration experiment."""

import time

from aiohttp import ClientSession, TraceConfig
from yarl import URL


class ReadOnlyPolicy:
    """Validate every destination and operation before any network delegation."""

    def __init__(self, devices, installations, app_key, *, enabled=False):
        self.devices = frozenset(devices)
        self.installations = frozenset(installations)
        self.app_key = app_key
        self.enabled = enabled
        self.deadline = time.monotonic() + 300
        self.counts = {"state": 0, "auth": 0}

    def reserve(self, method, url):
        """Count attempted HTTP operations before delegation; never refund failures."""
        if time.monotonic() >= self.deadline:
            raise PermissionError("Experiment duration exceeded")
        if len(self.devices) != 1:
            raise PermissionError("Exactly one device required")
        category = (
            "auth" if URL(url).path in {"/users/sign_in", "/pusher/auth"} else "state"
        )
        limit = 6 if category == "auth" else 40
        if self.counts[category] >= limit:
            raise PermissionError("Experiment quota exceeded")
        self.counts[category] += 1

    def validate(self, method, url, *, params=None, json=None, data=None):
        target = URL(url)
        if (
            target.origin() != URL("https://dkn.airzonecloud.com")
            or target.user is not None
            or target.fragment
            or target.query
        ):
            raise PermissionError("Destination denied")
        if params is not None and type(params) is not dict:
            raise PermissionError("Parameters denied")
        params = params or {}
        if set(params) - {"user_email", "user_token", "format", "installation_id"}:
            raise PermissionError("Parameters denied")
        if (
            "installation_id" in params
            and str(params["installation_id"]) not in self.installations
        ):
            raise PermissionError("Installation denied")
        if "format" in params and params["format"] != "json":
            raise PermissionError("Format denied")
        if method != "GET" and set(params) - {"user_email", "user_token"}:
            raise PermissionError("Parameters denied")
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
        if time.monotonic() >= self.deadline:
            raise PermissionError("Experiment duration exceeded")
        target = URL(url)
        if (
            target.origin() != URL("wss://dkn.airzonecloud.com:8080")
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

    def validate_request(self, method, url, **kwargs):
        if not self.policy.enabled:
            raise PermissionError("Real network experiment disabled")
        if "headers" in kwargs and type(kwargs["headers"]) is not dict:
            raise PermissionError("Headers denied")
        if set(kwargs) - {"params", "json", "data", "headers", "timeout"}:
            raise PermissionError("Request options denied")
        if set(k.lower() for k in kwargs.get("headers", {})) - {
            "user-agent",
            "accept",
            "content-type",
            "x-requested-with",
            "origin",
            "referer",
        }:
            raise PermissionError("Header denied")
        if any(
            k.lower() in {"host", "authorization", "cookie"}
            for k in kwargs.get("headers", {})
        ):
            raise PermissionError("Header denied")
        for key, value in kwargs.get("headers", {}).items():
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise PermissionError("Header value denied")
            if key.lower() == "x-requested-with" and value != "XMLHttpRequest":
                raise PermissionError("Header value denied")
            if key.lower() == "content-type" and value not in {
                "application/json",
                "application/json;charset=UTF-8",
                "application/x-www-form-urlencoded",
            }:
                raise PermissionError("Header value denied")
            if key.lower() in {"origin", "referer"} and value not in {
                "https://dkn.airzonecloud.com",
                "https://dkn.airzonecloud.com/",
            }:
                raise PermissionError("Header value denied")
        self.policy.validate(
            method, url, **{k: kwargs.get(k) for k in ("params", "json", "data")}
        )

    def request(self, method, url, **kwargs):
        self.validate_request(method, url, **kwargs)
        self.policy.reserve(method, url)
        return self.session.request(method, url, allow_redirects=False, **kwargs)

    def validate_socket_options(self, url, **kwargs):
        if (
            not self.policy.enabled
            or not self._redirect_guard
            or set(kwargs) - {"autoping", "max_msg_size"}
        ):
            raise PermissionError("Socket options denied")
        if ("autoping" in kwargs and kwargs["autoping"] is not True) or (
            "max_msg_size" in kwargs and not 0 < kwargs["max_msg_size"] <= 1024 * 1024
        ):
            raise PermissionError("Socket options denied")
        self.policy.validate_socket(url)

    def ws_connect(self, url, **kwargs):
        self.validate_socket_options(url, **kwargs)
        return ProtectedSocketContext(
            self.session.ws_connect(url, **kwargs), self.policy
        )


async def create_protected_session(policy, *, connector=None):
    """Own a session whose HTTP and WebSocket handshakes reject redirects."""

    async def deny_redirect(*_):
        raise PermissionError("Redirect denied")

    trace = TraceConfig()
    trace.on_request_redirect.append(deny_redirect)
    session = ClientSession(trace_configs=[trace], trust_env=False, connector=connector)
    return ProtectedSession(session, policy, redirect_guard=True)


class ProtectedSocket:
    """Only protocol liveness and approved private subscriptions may be sent."""

    def __init__(self, socket, policy):
        self._socket, self._policy = socket, policy

    @property
    def close_code(self):
        return self._socket.close_code

    async def receive(self):
        return await self._socket.receive()

    async def send_json(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"event", "data"}:
            raise PermissionError("Socket operation denied")
        event, data = payload["event"], payload["data"]
        permitted = event in {"pusher:ping", "pusher:pong"} and data == {}
        permitted |= (
            event == "pusher:subscribe"
            and isinstance(data, dict)
            and set(data) == {"channel", "auth"}
            and data["channel"] in {f"private-{d}" for d in self._policy.devices}
            and isinstance(data["auth"], str)
            and bool(data["auth"])
        )
        if not permitted:
            raise PermissionError("Socket operation denied")
        await self._socket.send_json(payload)


class ProtectedSocketContext:
    """Keep socket lifecycle ownership with the underlying aiohttp session."""

    def __init__(self, context, policy):
        self.context, self.policy = context, policy

    async def __aenter__(self):
        return ProtectedSocket(await self.context.__aenter__(), self.policy)

    async def __aexit__(self, *args):
        return await self.context.__aexit__(*args)


class LocalReadOnlySession:
    """Test-only boundary; forward permitted operations exclusively to loopback."""

    def __init__(self, session, origin):
        target = URL(origin)
        if (
            target.scheme != "http"
            or target.host != "127.0.0.1"
            or target.user is not None
            or target.query
            or target.fragment
            or target.path != "/"
        ):
            raise PermissionError("Only loopback laboratory origins are permitted")
        self.session, self.origin = session, origin
        self.blocked = 0

    def request(self, method, url, **kwargs):
        path = URL(url).path.rstrip("/")
        allowed = method == "GET" and path in {"/devices", "/installation_relations"}
        allowed |= method == "POST" and path in {"/users/sign_in", "/pusher/auth"}
        payload = kwargs.get("json") or {}
        event = payload.get("event", {})
        allowed |= (
            method == "POST"
            and path == "/events"
            and set(payload) == {"event"}
            and event.get("cgi") == "infomaquina"
            and set(event) <= {"cgi", "device_id", "option", "value"}
            and event.get("option", "") == ""
            and event.get("value", "") == ""
        )
        if not allowed:
            self.blocked += 1
            raise PermissionError("Control operation denied")
        return self.session.request(
            method, self.origin + path, allow_redirects=False, **kwargs
        )

    def ws_connect(self, url, **kwargs):
        target = URL(url)
        if (
            target.origin() != URL("wss://dkn.airzonecloud.com:8080")
            or target.user is not None
            or target.fragment
            or not target.path.startswith("/app/")
        ):
            raise PermissionError("Unknown WebSocket denied")
        return self.session.ws_connect(self.origin + "/socket", **kwargs)
