"""The same laboratory transport is exercised with real TLS and local DNS only."""

import asyncio

import pytest
from aiohttp import ClientConnectorCertificateError, ClientSession

from scripts.lab_policy import ReadOnlyPolicy
from scripts.lab_tls_backend import SyntheticBackend
from scripts.protected_lab import LabStopped, LabSupervisor, create_gateway, real_policy


@pytest.fixture
async def tls_backend(socket_enabled, tmp_path):
    backend = await SyntheticBackend(tmp_path).start()
    yield backend
    await backend.close()


async def make_gateway(backend, **limits):
    supervisor = LabSupervisor(spacing=0, **limits)
    policy = ReadOnlyPolicy({"device"}, {"site"}, "synthetic", enabled=True)
    gateway = await create_gateway(policy, supervisor, connector=backend.connector())
    return gateway


async def test_tls_runtime_operations_and_alternate_session(tls_backend):
    gateway = await make_gateway(tls_backend)
    try:
        with gateway.enforce():
            async with gateway.request(
                "GET", "https://dkn.airzonecloud.com/devices"
            ) as response:
                assert response.status == 200
            async with ClientSession() as other:
                with pytest.raises(LabStopped):
                    async with other.get("https://dkn.airzonecloud.com/devices"):
                        pass
        assert gateway.supervisor.reason == "alternate_session"
        assert tls_backend.calls == 1
    finally:
        await gateway.protected.session.close()


@pytest.mark.parametrize(
    "path,options",
    [
        ("https://elsewhere.invalid/devices", {}),
        (
            "https://dkn.airzonecloud.com/devices",
            {"headers": {"X-HTTP-Method-Override": "PUT"}},
        ),
        (
            "https://dkn.airzonecloud.com/events",
            {"json": {"event": {"cgi": "modmaquina"}}},
        ),
    ],
)
async def test_forbidden_operation_stops_before_network(tls_backend, path, options):
    gateway = await make_gateway(tls_backend)
    try:
        with gateway.enforce(), pytest.raises(LabStopped):
            gateway.request("POST", path, **options)
        assert tls_backend.calls == 0
        assert gateway.supervisor.stopped.is_set()
    finally:
        await gateway.protected.session.close()


@pytest.mark.parametrize(
    ("method", "url", "options", "expected"),
    [
        ("GET", "https://elsewhere.invalid/devices", {}, 0),
        ("POST", "https://dkn.airzonecloud.com/unknown", {}, 0),
        (
            "PUT",
            "https://dkn.airzonecloud.com/devices/device",
            {"json": {"device": {"scenary": "occupied"}}},
            1,
        ),
        (
            "POST",
            "https://dkn.airzonecloud.com/events",
            {"json": {"event": {"cgi": "modmaquina"}}},
            1,
        ),
        (
            "POST",
            "https://dkn.airzonecloud.com/devices/device",
            {"headers": {"X-HTTP-Method-Override": "PUT"}},
            1,
        ),
        (
            "POST",
            "https://dkn.airzonecloud.com/devices/device",
            {"headers": {"x-http-method-override": "PUT"}},
            1,
        ),
        (
            "POST",
            "https://dkn.airzonecloud.com/devices/device",
            {"headers": {"x-HtTp-MeThOd-OvErRiDe": "pUt"}},
            1,
        ),
        (
            "POST",
            "https://dkn.airzonecloud.com/devices/device",
            {"headers": {"X-HTTP-Method-Override": "GET"}},
            0,
        ),
        (
            "POST",
            "https://dkn.airzonecloud.com/devices/device",
            {
                "headers": {
                    "X-HTTP-Method-Override": "GET",
                    "x-http-method-override": "PUT",
                }
            },
            1,
        ),
    ],
)
async def test_blocked_control_counter_only_tracks_control_writes(
    tls_backend, method, url, options, expected
):
    """Forbidden requests always stop; only HVAC commands count as control."""
    gateway = await make_gateway(tls_backend)
    try:
        with gateway.enforce(), pytest.raises(LabStopped):
            gateway.request(method, url, **options)
        assert gateway.observations["blocked_control_attempts"] == expected
        assert gateway.supervisor.reason == "forbidden_operation"
        assert tls_backend.calls == 0
    finally:
        await gateway.protected.session.close()


@pytest.mark.parametrize("failure", ["redirect", "malformed", "401", "429"])
async def test_response_failures_stop_without_private_exception(tls_backend, failure):
    if failure.isdigit():
        tls_backend.status = int(failure)
    else:
        setattr(tls_backend, failure, True)
    gateway = await make_gateway(tls_backend)
    try:
        with gateway.enforce(), pytest.raises(LabStopped, match="Laboratory stopped"):
            async with gateway.request("GET", "https://dkn.airzonecloud.com/devices"):
                pass
        assert tls_backend.calls == 1
    finally:
        await gateway.protected.session.close()


async def test_quota_counts_real_failed_attempts(tls_backend):
    gateway = await make_gateway(tls_backend, state_limit=1)
    try:
        with gateway.enforce():
            async with gateway.request("GET", "https://dkn.airzonecloud.com/devices"):
                pass
            with pytest.raises(LabStopped):
                async with gateway.request(
                    "GET", "https://dkn.airzonecloud.com/devices"
                ):
                    pass
        assert gateway.supervisor.reason == "quota"
        assert tls_backend.calls == 1
    finally:
        await gateway.protected.session.close()


async def test_untrusted_tls_never_reaches_backend(tls_backend):
    supervisor = LabSupervisor()
    gateway = await create_gateway(
        ReadOnlyPolicy({"device"}, {"site"}, "synthetic", enabled=True),
        supervisor,
        connector=tls_backend.connector(trusted=False),
    )
    try:
        with gateway.enforce(), pytest.raises(ClientConnectorCertificateError):
            async with gateway.request("GET", "https://dkn.airzonecloud.com/devices"):
                pass
        assert tls_backend.calls == 0
    finally:
        await gateway.protected.session.close()


def test_real_launch_is_disabled():
    with pytest.raises(LabStopped, match="disabled"):
        real_policy({"device"}, {"site"}, "synthetic")


async def test_tls_pusher_subscription_and_cancel(tls_backend):
    from custom_components.airzoneclouddaikin.airzone_api import AirzoneAPI
    from custom_components.airzoneclouddaikin.pusher_client import PusherClient

    gateway = await make_gateway(tls_backend)
    received = asyncio.Event()

    async def event(*_):
        received.set()

    api = AirzoneAPI("synthetic@example.invalid", gateway, token="synthetic")
    client = PusherClient(
        gateway,
        "wss://dkn.airzonecloud.com:8080/app/synthetic?protocol=7&client=python-dkncloud&version=1.0",
        {"private-device": "device"},
        api.authorize_channel,
        event,
        lambda *_: asyncio.sleep(0),
    )
    try:
        with gateway.enforce():
            task = asyncio.create_task(client.connect())
            try:
                async with asyncio.timeout(2):
                    await received.wait()
            finally:
                task.cancel()
                result = await asyncio.gather(task, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError), result
    finally:
        await gateway.protected.session.close()


@pytest.mark.parametrize(
    "case",
    [
        "normal",
        "no_events",
        "invalid_event",
        "reconnect",
        "sleep",
        "duration",
        "authorization",
        "cooldown",
    ],
)
def test_independent_protected_launcher(case, protected_launcher):
    """Run the same full subprocess assertions as the original launcher test."""
    protected_launcher(case)


async def test_websocket_redirect_and_unknown_message_stop(tls_backend):
    gateway = await make_gateway(tls_backend)
    url = "wss://dkn.airzonecloud.com:8080/app/synthetic?protocol=7&client=python-dkncloud&version=1.0"
    try:
        with gateway.enforce():
            async with gateway.ws_connect(url) as ws:
                with pytest.raises(LabStopped):
                    await ws.send_json({"event": "client-control", "data": {}})
        assert gateway.supervisor.reason == "forbidden_socket_message"
    finally:
        await gateway.protected.session.close()
    gateway = await make_gateway(tls_backend)
    tls_backend.redirect = True
    try:
        with gateway.enforce(), pytest.raises(PermissionError):
            async with gateway.ws_connect(url):
                pass
        assert gateway.supervisor.reason == "transport_security"
    finally:
        await gateway.protected.session.close()


async def test_auth_quota_persistent_forbidden_and_direct_connect(tls_backend):
    gateway = await make_gateway(tls_backend, auth_limit=1)
    try:
        with gateway.enforce():
            async with gateway.request(
                "POST",
                "https://dkn.airzonecloud.com/users/sign_in",
                json={"email": "synthetic", "password": "synthetic"},
            ):
                pass
            with pytest.raises(LabStopped):
                async with gateway.request(
                    "POST",
                    "https://dkn.airzonecloud.com/users/sign_in",
                    json={"email": "synthetic", "password": "synthetic"},
                ):
                    pass
        assert gateway.supervisor.counts["auth"] == 1
    finally:
        await gateway.protected.session.close()
    gateway = await make_gateway(tls_backend)
    tls_backend.status = 403
    try:
        with gateway.enforce():
            async with gateway.request("GET", "https://dkn.airzonecloud.com/devices"):
                pass
            with pytest.raises(LabStopped):
                async with gateway.request(
                    "GET", "https://dkn.airzonecloud.com/devices"
                ):
                    pass
        assert gateway.supervisor.reason == "persistent_forbidden"
    finally:
        await gateway.protected.session.close()
    gateway = await make_gateway(tls_backend)
    try:
        with gateway.enforce(), pytest.raises(LabStopped):
            await asyncio.get_running_loop().create_connection(
                asyncio.Protocol, "127.0.0.1", tls_backend.port
            )
        assert gateway.supervisor.reason == "alternate_transport"
    finally:
        await gateway.protected.session.close()


@pytest.mark.parametrize("datagram", [False, True])
async def test_synchronous_socket_cannot_bypass_gateway(tls_backend, datagram):
    import socket

    gateway = await make_gateway(tls_backend)
    try:
        with (
            gateway.enforce(),
            socket.socket(
                type=socket.SOCK_DGRAM if datagram else socket.SOCK_STREAM
            ) as sock,
            pytest.raises(LabStopped),
        ):
            if datagram:
                sock.sendto(b"synthetic", ("127.0.0.1", tls_backend.port))
            else:
                sock.connect(("127.0.0.1", tls_backend.port))
        assert gateway.supervisor.reason == "alternate_transport"
    finally:
        await gateway.protected.session.close()


async def test_adapter_factory_uses_service_tls_endpoint_end_to_end(
    tls_backend, monkeypatch
):
    from types import SimpleNamespace

    # Synthetic CA is trusted only within this test; the production factory
    # still constructs its own verified, service-scoped compatibility context.
    monkeypatch.setenv(
        "REQUESTS_CA_BUNDLE", str(tls_backend.directory / "synthetic-cert.pem")
    )

    from custom_components.airzoneclouddaikin.airzone_api import AirzoneAPI
    from custom_components.airzoneclouddaikin.push_adapter import build_verified_adapter

    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.push_adapter.PUSHER_APP_KEY", "synthetic"
    )
    gateway = await make_gateway(tls_backend)
    api = AirzoneAPI("synthetic@example.invalid", gateway, token="synthetic")
    received = asyncio.Event()
    subscriptions = []
    patches = []

    async def event(device_id, patch, generation):
        patches.append((device_id, patch, generation))
        received.set()

    async def subscription(device_id, active):
        subscriptions.append((device_id, active))

    owner = SimpleNamespace(
        state=SimpleNamespace(generation=0),
        schedulers={"device": None},
        _limit=asyncio.Semaphore(1),
        _stopped=False,
        _auth_failed=False,
        channel_errors={},
        event=event,
        subscription=subscription,
    )
    try:
        with gateway.enforce():
            task = asyncio.create_task(
                build_verified_adapter(api, gateway).connect(owner)
            )
            try:
                await asyncio.wait_for(received.wait(), 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert patches == [("device", {"id": "device", "power": "0"}, 0)]
        assert subscriptions == [("device", True), ("device", False)]
        assert tls_backend.controls == 0
        assert not api._pending
        assert not gateway.protected.session.closed
    finally:
        await gateway.protected.session.close()


@pytest.mark.parametrize("status,body", [(200, b""), (201, b"accepted")])
async def test_information_accepts_successful_non_json_responses(
    tls_backend, status, body
):
    from aiohttp import web

    tls_backend.event_response = web.Response(
        status=status, body=body, content_type="text/plain"
    )
    gateway = await make_gateway(tls_backend)
    try:
        with gateway.enforce():
            async with gateway.request(
                "POST",
                "https://dkn.airzonecloud.com/events",
                json={
                    "event": {
                        "cgi": "infomaquina",
                        "device_id": "device",
                        "option": "",
                        "value": "",
                    }
                },
            ) as response:
                assert response.status == status
        assert gateway.supervisor.reason is None
    finally:
        await gateway.protected.session.close()


async def test_invalid_json_error_drops_exception_context(tls_backend):
    tls_backend.malformed = True
    gateway = await make_gateway(tls_backend)
    try:
        with gateway.enforce(), pytest.raises(LabStopped) as error:
            async with gateway.request("GET", "https://dkn.airzonecloud.com/devices"):
                pass
        assert error.value.__context__ is None
    finally:
        await gateway.protected.session.close()
