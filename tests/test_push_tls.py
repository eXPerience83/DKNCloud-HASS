"""Scoped TLS compatibility: synthetic certificates, local DNS and real I/O."""

import asyncio
import logging
import ssl
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientConnectorCertificateError, ClientSession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util.ssl import get_default_context
from push_tls_fixture import Backend

from custom_components.airzoneclouddaikin import push_tls
from custom_components.airzoneclouddaikin.airzone_api import AirzoneAPI
from custom_components.airzoneclouddaikin.push_adapter import build_verified_adapter
from custom_components.airzoneclouddaikin.pusher_client import PusherProtocolError
from custom_components.airzoneclouddaikin.sync_manager import SyncManager
from scripts.lab_policy import ReadOnlyPolicy, create_protected_session
from scripts.protected_lab import LaboratoryGateway, LabStopped, LabSupervisor

URL = "wss://dkn.airzonecloud.com:8080/app/synthetic?protocol=7&client=python-dkncloud&version=1.0"


@pytest.fixture
async def backend_factory(socket_enabled, tmp_path, monkeypatch):
    backends = []

    async def create(*, trusted=True, **kwargs):
        directory = tmp_path / str(len(backends))
        directory.mkdir()
        backend = await Backend(directory).start(**kwargs)
        backends.append(backend)
        if trusted:
            monkeypatch.setenv(
                "REQUESTS_CA_BUNDLE", str(directory / "synthetic-cert.pem")
            )
        else:
            monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        return backend

    yield create
    for backend in backends:
        await backend.close()


def owned_sessions(monkeypatch, backend):
    sessions = []

    def create(**kwargs):
        session = ClientSession(connector=backend.connector(), **kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(push_tls, "ClientSession", create)
    return sessions


def test_compatibility_disabled_and_modern_default_preserved():
    assert not push_tls.STATIC_RSA_COMPATIBILITY_ENABLED
    shared = get_default_context()
    before = [c["name"] for c in shared.get_ciphers()]
    context = push_tls.create_compatibility_context()
    assert context is not shared
    assert [c["name"] for c in context.get_ciphers()] == before + ["AES128-GCM-SHA256"]
    assert [c["name"] for c in shared.get_ciphers()] == before
    assert "AES128-GCM-SHA256" not in {
        c["name"] for c in ssl.create_default_context().get_ciphers()
    }
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert context.maximum_version == shared.maximum_version
    assert context.security_level == shared.security_level >= 2


@pytest.mark.parametrize(
    "url",
    [
        "wss://elsewhere.invalid:8080/app/synthetic",
        "wss://dkn.airzonecloud.com/app/synthetic",
        "ws://dkn.airzonecloud.com:8080/app/synthetic",
        "https://dkn.airzonecloud.com:8080/app/synthetic",
        "wss://user@dkn.airzonecloud.com:8080/app/synthetic",
        URL + "#fragment",
    ],
)
def test_compatibility_rejects_other_destinations(url):
    with pytest.raises(ValueError, match="policy rejected"):
        push_tls.validate_context(url, get_default_context())


def test_non_verifying_context_rejected():
    # A server context has no client certificate/hostname verification policy.
    with pytest.raises(ValueError, match="policy rejected"):
        push_tls.validate_context(URL, ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER))


async def test_default_factory_never_creates_compatibility_context(monkeypatch):
    context = Mock(side_effect=AssertionError("Default must retain TLS policy"))
    monkeypatch.setattr(push_tls, "create_compatibility_context", context)
    wire = Mock(return_value=SimpleNamespace(connect=AsyncMock()))
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.push_adapter.PusherClient", wire
    )
    manager = SimpleNamespace(
        state=SimpleNamespace(generation=0),
        schedulers={},
        event=AsyncMock(),
        subscription=AsyncMock(),
        _stopped=False,
        _auth_failed=False,
    )
    adapter = build_verified_adapter(Mock(), Mock())
    await adapter.connect(manager)
    context.assert_not_called()
    assert "websocket_connect" not in wire.call_args.kwargs


async def test_context_loaded_off_loop_and_reused_on_reconnection(monkeypatch):
    monkeypatch.setattr(push_tls, "STATIC_RSA_COMPATIBILITY_ENABLED", True)
    thread = threading.get_ident()
    create = push_tls.create_compatibility_context
    threads = []

    def context():
        threads.append(threading.get_ident())
        return create()

    monkeypatch.setattr(push_tls, "create_compatibility_context", context)
    wire = Mock(return_value=SimpleNamespace(connect=AsyncMock()))
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.push_adapter.PusherClient", wire
    )
    manager = SimpleNamespace(
        state=SimpleNamespace(generation=0),
        schedulers={},
        event=AsyncMock(),
        subscription=AsyncMock(),
        _stopped=False,
        _auth_failed=False,
    )
    session = Mock(compatibility_websocket=None)
    adapter = build_verified_adapter(Mock(), session)
    await adapter.connect(manager)
    await adapter.connect(manager)
    assert len(threads) == 1 and threads[0] != thread
    first, second = [c.kwargs["websocket_connect"] for c in wire.call_args_list]
    assert first.keywords["ssl_context"] is second.keywords["ssl_context"]
    session.ws_connect.assert_not_called()
    session.close.assert_not_called()


@pytest.mark.parametrize("flag", ["_stopped", "_auth_failed"])
async def test_stop_or_auth_suspension_while_context_is_preparing_prevents_io(
    monkeypatch, flag
):
    monkeypatch.setattr(push_tls, "STATIC_RSA_COMPATIBILITY_ENABLED", True)
    entered, release = threading.Event(), threading.Event()
    context = get_default_context()

    def create():
        entered.set()
        assert release.wait(3)
        return context

    monkeypatch.setattr(push_tls, "create_compatibility_context", create)
    wire = Mock()
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.push_adapter.PusherClient", wire
    )
    manager = SimpleNamespace(_stopped=False, _auth_failed=False)
    adapter = build_verified_adapter(Mock(), Mock())
    task = asyncio.create_task(adapter.connect(manager))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        setattr(manager, flag, True)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        wire.assert_not_called()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_default_tls_fails_against_rsa_only_socket(backend_factory):
    backend = await backend_factory()
    async with ClientSession(connector=backend.connector()) as session:
        with pytest.raises(OSError):
            async with session.ws_connect(URL):
                pytest.fail("Static RSA unexpectedly offered by default")
    assert backend.calls == 0


@pytest.mark.parametrize("failure", ["untrusted", "expired", "hostname"])
async def test_certificate_failures_close_owned_session(
    backend_factory, monkeypatch, failure
):
    backend = await backend_factory(
        trusted=failure != "untrusted",
        expired=failure == "expired",
        wrong_hostname=failure == "hostname",
    )
    sessions = owned_sessions(monkeypatch, backend)
    context = await asyncio.to_thread(push_tls.create_compatibility_context)
    with pytest.raises(ClientConnectorCertificateError) as error:
        async with push_tls.compatibility_websocket(URL, ssl_context=context):
            pytest.fail("Invalid synthetic certificate accepted")
    if failure == "expired":
        assert error.value.certificate_error.verify_code == 10
    assert sessions and all(session.closed for session in sessions)
    assert backend.calls == 0


async def test_modern_suite_remains_preferred(backend_factory, monkeypatch):
    backend = await backend_factory(rsa_only=False)
    sessions = owned_sessions(monkeypatch, backend)
    context = await asyncio.to_thread(push_tls.create_compatibility_context)
    async with push_tls.compatibility_websocket(URL, ssl_context=context) as socket:
        await socket.receive()
    assert backend.push_ciphers[0] != "AES128-GCM-SHA256"
    assert all(session.closed for session in sessions)


async def test_redirect_rejected_before_other_destination_and_rest_stays_open(
    backend_factory, monkeypatch
):
    backend = await backend_factory()
    backend.redirect_socket = True
    sessions = owned_sessions(monkeypatch, backend)
    context = await asyncio.to_thread(push_tls.create_compatibility_context)
    async with ClientSession(connector=backend.connector()) as rest:
        with pytest.raises(ValueError, match="redirect rejected"):
            async with push_tls.compatibility_websocket(URL, ssl_context=context):
                pytest.fail("Redirect followed")
        assert backend.calls == 1
        assert not rest.closed
        assert all(session.closed for session in sessions)
        async with rest.get("https://dkn.airzonecloud.com/devices") as response:
            assert response.status == 200


async def test_compatibility_does_not_change_rest_ciphers(backend_factory):
    backend = await backend_factory()
    await asyncio.to_thread(push_tls.create_compatibility_context)
    async with ClientSession(connector=backend.connector(rsa_rest=True)) as rest:
        with pytest.raises(OSError):
            async with rest.get("https://dkn.airzonecloud.com/devices"):
                pytest.fail("Compatibility leaked into REST")
    assert backend.calls == 0


async def test_real_tls_reconnections_reauthorize_and_close_each_owned_transport(
    backend_factory, monkeypatch
):
    backend = await backend_factory()
    backend.resynchronized.set()
    sessions = owned_sessions(monkeypatch, backend)
    monkeypatch.setattr(push_tls, "STATIC_RSA_COMPATIBILITY_ENABLED", True)
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.push_adapter.PUSHER_APP_KEY", "synthetic"
    )
    received = asyncio.Event()
    generations = []

    async def event(device, patch, generation):
        generations.append(generation)
        received.set()

    async with ClientSession(connector=backend.connector()) as rest:
        api = AirzoneAPI("synthetic@example.invalid", rest, token="synthetic")
        owner = SimpleNamespace(
            state=SimpleNamespace(generation=0),
            schedulers={"device": None},
            _limit=asyncio.Semaphore(1),
            _stopped=False,
            _auth_failed=False,
            channel_errors={},
            event=event,
            subscription=AsyncMock(),
        )
        adapter = build_verified_adapter(api, rest)
        contexts = []
        for generation in (0, 1):
            owner.state.generation = generation
            received.clear()
            task = asyncio.create_task(adapter.connect(owner))
            try:
                await asyncio.wait_for(received.wait(), 3)
                contexts.append(adapter._ssl_context)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            assert not api._pending and not rest.closed
            assert all(session.closed for session in sessions)
        assert contexts[0] is contexts[1]
        assert backend.authorized_sockets == ["1.1", "1.2"]
        assert generations == [0, 1]
        assert owner.subscription.await_args_list[0].args == ("device", True)
        assert owner.subscription.await_args_list[-1].args == ("device", False)
        assert len(sessions) == 2 and backend.controls == 0


async def test_cancellation_during_socket_handshake_closes_owned_http_only(
    backend_factory, monkeypatch
):
    backend = await backend_factory()
    backend.socket_wait = asyncio.Event()
    sessions = owned_sessions(monkeypatch, backend)
    context = await asyncio.to_thread(push_tls.create_compatibility_context)

    async def connect():
        async with push_tls.compatibility_websocket(URL, ssl_context=context):
            pytest.fail("Blocked handshake must not complete")

    task = asyncio.create_task(connect())
    try:
        await asyncio.wait_for(backend.socket_entered.wait(), 3)
        assert not task.done() and not sessions[0].closed
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sessions[0].closed
        assert not backend.socket_wait.is_set()
        assert not backend.authorized_sockets
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_authorization_http_is_gone_when_owned_socket_session_closes(
    backend_factory, monkeypatch
):
    backend = await backend_factory()
    backend.auth_wait = asyncio.Event()
    sessions = owned_sessions(monkeypatch, backend)
    monkeypatch.setattr(push_tls, "STATIC_RSA_COMPATIBILITY_ENABLED", True)
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.push_adapter.PUSHER_APP_KEY", "synthetic"
    )
    async with ClientSession(connector=backend.connector()) as rest:
        api = AirzoneAPI("synthetic@example.invalid", rest, token="synthetic")
        owner = SimpleNamespace(
            state=SimpleNamespace(generation=0),
            schedulers={"device": None},
            _limit=asyncio.Semaphore(1),
            _stopped=False,
            _auth_failed=False,
            channel_errors={},
            event=AsyncMock(),
            subscription=AsyncMock(),
        )
        task = asyncio.create_task(build_verified_adapter(api, rest).connect(owner))
        closed = []
        try:
            await asyncio.wait_for(backend.auth_entered.wait(), 3)
            assert api._pending and not task.done()
            original_close = sessions[0].close

            async def checked_close():
                assert not api._pending
                closed.append(True)
                await original_close()

            monkeypatch.setattr(sessions[0], "close", checked_close)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed and sessions[0].closed and not rest.closed
            assert not api._pending and not backend.auth_wait.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("failure", ["origin", "context", "override", "command"])
async def test_protected_compatibility_does_not_relax_boundary(
    backend_factory, monkeypatch, failure
):
    backend = await backend_factory()
    protected = await create_protected_session(
        ReadOnlyPolicy({"device"}, {"site"}, "synthetic", enabled=True),
        connector=backend.connector(),
    )
    gateway = LaboratoryGateway(protected, LabSupervisor(spacing=0))
    context = await asyncio.to_thread(push_tls.create_compatibility_context)
    if failure == "context":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        with gateway.enforce(), pytest.raises(LabStopped):
            async with gateway.compatibility_websocket(
                URL.replace("dkn.airzonecloud.com", "elsewhere.invalid")
                if failure == "origin"
                else URL,
                ssl_context=context,
                **({"ssl": False} if failure == "override" else {}),
            ) as socket:
                await socket.send_json({"event": "physical_control", "data": {}})
        assert backend.controls == 0
        assert backend.calls == (1 if failure == "command" else 0)
    finally:
        await protected.session.close()


@pytest.mark.parametrize("boundary", ["owned_session", "protected_gateway"])
@pytest.mark.parametrize("reject", [False, True])
async def test_tls_auth_ack_reconciliation_and_owner_shutdown(
    hass, backend_factory, monkeypatch, boundary, reject
):
    backend = await backend_factory()
    backend.error = reject
    monkeypatch.setattr(push_tls, "STATIC_RSA_COMPATIBILITY_ENABLED", True)
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.push_adapter.PUSHER_APP_KEY", "synthetic"
    )
    sessions = owned_sessions(monkeypatch, backend)
    rest = ClientSession(connector=backend.connector())
    if boundary == "protected_gateway":
        protected = await create_protected_session(
            ReadOnlyPolicy({"device"}, {"site"}, "synthetic", enabled=True),
            connector=backend.connector(),
        )
        await rest.close()
        rest = protected.session
        session = LaboratoryGateway(protected, LabSupervisor(spacing=0))
        guard = session.enforce()
    else:
        from contextlib import nullcontext

        session, guard = rest, nullcontext()
    api = AirzoneAPI("synthetic@example.invalid", session, token="synthetic")
    coordinator = DataUpdateCoordinator(
        hass, logging.getLogger(__name__), name="Synthetic", config_entry=None
    )
    applied = asyncio.Event()

    def publish(data, _):
        coordinator.async_set_updated_data(data)
        if data["device"]["power"] == "0":
            applied.set()

    manager = None
    pending = None
    try:
        with guard:
            initial = await api.fetch_devices("site", retry=False)
            locks = {"device": asyncio.Lock()}
            adapter = build_verified_adapter(api, session)
            manager = SyncManager(
                {"device": initial[0]},
                {"device": "site"},
                api.request_information,
                lambda site: api.fetch_devices(site, retry=False),
                publish,
                Mock(),
                locks.__getitem__,
                connect=adapter.connect,
            )
            task = asyncio.create_task(adapter.connect(manager))
            manager._tasks.append(task)
            if reject:
                with pytest.raises(PusherProtocolError) as error:
                    await asyncio.wait_for(task, 3)
                assert error.value.code == 4009
                assert "device" not in manager._last_event
            else:
                await asyncio.wait_for(applied.wait(), 3)
                await manager._resync_tasks["device"]
                assert coordinator.data["device"]["power"] == "0"
                assert coordinator.data["device"]["mode"] == "1"
                assert manager.schedulers["device"].status.push_active
                stamp = dict(manager._last_event)
                await manager.schedulers["device"].cycle()
                assert manager._last_event == stamp
                backend.hold_get = True
                pending = asyncio.create_task(manager.recover("device"))
                await asyncio.wait_for(backend.pending_get.wait(), 12)
                assert api._pending and not pending.done()
            assert backend.signature_verified
            assert backend.push_ciphers == ["AES128-GCM-SHA256"]
            assert all(c != "AES128-GCM-SHA256" for c in backend.rest_ciphers)
            await manager.close()
            if pending:
                await asyncio.gather(pending, return_exceptions=True)
            assert task.done() and not api._pending
            assert not rest.closed and not backend.release_get.is_set()
            assert all(s.closed for s in sessions)
            assert backend.controls == 0
    finally:
        if manager:
            await manager.close()
        await rest.close()


async def test_strict_transport_rejects_redirect_before_next_request(
    backend_factory, monkeypatch
):
    backend = await backend_factory(rsa_only=False)
    backend.redirect_socket = True
    sessions = owned_sessions(monkeypatch, backend)
    with pytest.raises(ValueError, match="redirect rejected"):
        async with push_tls.strict_websocket(URL):
            pytest.fail("Strict WebSocket followed a redirect")
    assert backend.calls == 1
    assert sessions and all(session.closed for session in sessions)


async def test_default_adapter_uses_guarded_strict_transport_for_real_session(
    monkeypatch,
):
    wire = Mock(return_value=SimpleNamespace(connect=AsyncMock()))
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.push_adapter.PusherClient", wire
    )
    manager = SimpleNamespace(
        state=SimpleNamespace(generation=0),
        schedulers={},
        event=AsyncMock(),
        subscription=AsyncMock(),
        _stopped=False,
        _auth_failed=False,
    )
    async with ClientSession() as session:
        adapter = build_verified_adapter(Mock(), session)
        await adapter.connect(manager)
    assert wire.call_args.kwargs["websocket_connect"] is push_tls.strict_websocket
