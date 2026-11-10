"""Laboratory-only supervision; never imported by the distributed integration."""

import asyncio
import contextvars
import sys
import time
from contextlib import contextmanager

from aiohttp import (
    ClientConnectorCertificateError,
    ClientResponseError,
    ClientSession,
    ContentTypeError,
)
from yarl import URL

from scripts.lab_policy import (
    ReadOnlyPolicy,
    create_protected_session,
)

# A later, separately authorized change must explicitly enable real destinations.
REAL_DESTINATIONS_ENABLED = False
_NETWORK_LEASE = contextvars.ContextVar("laboratory_network_lease", default=None)
_ACTIVE_GATEWAY = None


def _audit_network(event, args):
    """Cover synchronous socket users and unexpected child processes as well."""
    gateway = _ACTIVE_GATEWAY
    if gateway is None:
        return
    if event in {"subprocess.Popen", "os.system", "os.posix_spawn"}:
        gateway.supervisor.abort("alternate_process")
    if event in {"socket.sendto", "socket.sendmsg"}:
        gateway.supervisor.abort("alternate_transport")
    if event == "socket.connect" and _NETWORK_LEASE.get() is not gateway:
        gateway.supervisor.abort("alternate_transport")


sys.addaudithook(_audit_network)


class LabStopped(PermissionError):
    """A deliberately payload-free experiment failure."""


class LabSupervisor:
    """Own one experiment deadline, quotas, traffic spacing and fatal stop signal."""

    def __init__(self, *, duration=300, state_limit=40, auth_limit=6, spacing=10):
        if not (0 < duration <= 300 and 0 < state_limit <= 40 and 0 < auth_limit <= 6):
            raise ValueError("Invalid experiment limits")
        self.deadline = time.monotonic() + duration
        self.limits = {"state": state_limit, "auth": auth_limit}
        self.counts = {"state": 0, "auth": 0}
        self.spacing = spacing
        self.stopped = asyncio.Event()
        self.reason = None
        self._lock = asyncio.Lock()
        self._next_state = 0
        self._denials = 0
        self._sockets = 0

    def abort(self, reason):
        if not self.stopped.is_set():
            self.reason = reason
            self.stopped.set()
        raise LabStopped("Laboratory stopped") from None

    def check(self):
        if self.stopped.is_set():
            raise LabStopped("Laboratory stopped")
        if time.monotonic() >= self.deadline:
            self.abort("duration")

    async def admit(self, category):
        async with self._lock:
            self.check()
            if category == "state":
                await asyncio.sleep(max(0, self._next_state - time.monotonic()))
                self.check()
            if self.counts[category] >= self.limits[category]:
                self.abort("quota")
            self.counts[category] += 1
            if category == "state":
                self._next_state = time.monotonic() + self.spacing

    def response(self, status):
        if status in {401, 429} or 300 <= status < 400:
            self.abort("http_security")
        if status == 403:
            self._denials += 1
            if self._denials >= 2:
                self.abort("persistent_forbidden")

    async def watch(self):
        try:
            await asyncio.wait_for(
                self.stopped.wait(), max(0, self.deadline - time.monotonic())
            )
        except TimeoutError:
            self.reason = "duration"
            self.stopped.set()

    def socket(self):
        self.check()
        self._sockets += 1
        if self._sockets > 6:
            self.abort("socket_quota")


class GuardedContext:
    """Grant a transport lease only around a previously validated operation."""

    def __init__(self, gateway, factory, category=None, websocket=False, path=None):
        self.gateway, self.factory = gateway, factory
        self.category, self.websocket = category, websocket
        self.path = path
        self.context = None

    async def __aenter__(self):
        owner = self.gateway.supervisor
        if self.category:
            await owner.admit(self.category)
        else:
            owner.socket()
        entered = False
        token = _NETWORK_LEASE.set(self.gateway)
        try:
            self.context = self.factory()
            result = await self.context.__aenter__()
            entered = True
            if not self.websocket:
                owner.response(result.status)
                if result.status < 300 and result.status != 204:
                    # The API consumes JSON. Validate without logging or retaining it.
                    try:
                        value = await result.json()
                        if not isinstance(value, (dict, list)):
                            owner.abort("invalid_response")
                        if self.path == "/devices":
                            devices = (
                                value.get("devices")
                                if isinstance(value, dict)
                                else value
                            )
                            if not isinstance(devices, list) or any(
                                not isinstance(device, dict)
                                or str(device.get("id"))
                                not in self.gateway.protected.policy.devices
                                for device in devices
                            ):
                                owner.abort("device_scope")
                    except ValueError, TypeError, ContentTypeError:
                        owner.abort("invalid_response")
            return GuardedSocket(result, owner) if self.websocket else result
        except BaseException as error:
            if isinstance(error, PermissionError) and not isinstance(error, LabStopped):
                owner.reason = "transport_security"
                owner.stopped.set()
            if isinstance(error, ClientConnectorCertificateError):
                owner.reason = "tls_verification"
                owner.stopped.set()
            elif isinstance(error, ClientResponseError):
                owner.response(error.status)
            if entered:
                await self.context.__aexit__(None, None, None)
            raise
        finally:
            _NETWORK_LEASE.reset(token)

    async def __aexit__(self, *args):
        return await self.context.__aexit__(*args)


class GuardedSocket:
    """Propagate forbidden WebSocket writes to the experiment supervisor."""

    def __init__(self, socket, owner):
        self.socket, self.owner = socket, owner

    @property
    def close_code(self):
        return self.socket.close_code

    async def receive(self):
        return await self.socket.receive()

    async def send_json(self, payload):
        self.owner.check()
        try:
            await self.socket.send_json(payload)
        except PermissionError:
            self.owner.abort("forbidden_socket_message")


class LaboratoryGateway:
    """The sole transport injected into every integration API and adapter."""

    def __init__(self, protected, supervisor):
        self.protected, self.supervisor = protected, supervisor

    def request(self, method, url, **kwargs):
        try:
            # Validate synchronously so no hidden task can swallow a policy failure.
            self.protected.validate_request(method, url, **kwargs)
        except PermissionError, ValueError, TypeError:
            self.supervisor.abort("forbidden_operation")
        category = (
            "auth" if str(url).endswith(("/users/sign_in", "/pusher/auth")) else "state"
        )
        return GuardedContext(
            self,
            lambda: self.protected.request(method, url, **kwargs),
            category,
            path=URL(url).path,
        )

    def ws_connect(self, url, **kwargs):
        try:
            self.protected.validate_socket_options(url, **kwargs)
        except PermissionError, ValueError, TypeError:
            self.supervisor.abort("forbidden_socket")
        return GuardedContext(
            self, lambda: self.protected.ws_connect(url, **kwargs), websocket=True
        )

    @contextmanager
    def enforce(self):
        """Fail closed for other aiohttp sessions and direct asyncio transports.

        This process-local guard is a laboratory invariant, not a malicious-code
        sandbox. The container remains the security boundary.
        """
        global _ACTIVE_GATEWAY
        if _ACTIVE_GATEWAY is not None:
            raise RuntimeError("Another laboratory owns this process")
        _ACTIVE_GATEWAY = self
        original = ClientSession._request
        loop = asyncio.get_running_loop()
        connect, sock_connect = loop.create_connection, loop.sock_connect

        async def guarded_request(session, *args, **kwargs):
            if (
                session is not self.protected.session
                or _NETWORK_LEASE.get() is not self
            ):
                self.supervisor.abort("alternate_session")
            self.supervisor.check()
            return await original(session, *args, **kwargs)

        async def guarded_connect(*args, **kwargs):
            if _NETWORK_LEASE.get() is not self:
                self.supervisor.abort("alternate_transport")
            self.supervisor.check()
            return await connect(*args, **kwargs)

        async def guarded_sock_connect(*args, **kwargs):
            if _NETWORK_LEASE.get() is not self:
                self.supervisor.abort("alternate_transport")
            self.supervisor.check()
            return await sock_connect(*args, **kwargs)

        ClientSession._request = guarded_request
        loop.create_connection, loop.sock_connect = (
            guarded_connect,
            guarded_sock_connect,
        )
        try:
            yield
        finally:
            _ACTIVE_GATEWAY = None
            ClientSession._request = original
            loop.create_connection, loop.sock_connect = connect, sock_connect


async def create_gateway(policy, supervisor, *, connector=None):
    """Create owned TLS transport; custom connectors are synthetic-test only."""
    if not policy.enabled:
        raise LabStopped("Laboratory disabled")
    protected = await create_protected_session(policy, connector=connector)
    return LaboratoryGateway(protected, supervisor)


def real_policy(devices, installations, app_key):
    """The executable real destination path remains disabled even with credentials."""
    if not REAL_DESTINATIONS_ENABLED:
        raise LabStopped("Real destinations disabled")
    return ReadOnlyPolicy(devices, installations, app_key, enabled=True)
