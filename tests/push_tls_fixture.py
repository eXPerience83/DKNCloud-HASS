"""Synthetic TLS fixture with independent REST and WebSocket cipher profiles."""

import asyncio
import datetime
import hashlib
import hmac
import json
import ssl
from pathlib import Path

from aiohttp import TCPConnector, web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from scripts.lab_tls_backend import LoopbackResolver, SyntheticBackend, certificate


class Resolver(LoopbackResolver):
    def __init__(self, rest, push):
        self.rest, self.push = rest, push

    async def resolve(self, host, port=0, family=0):
        self.port = self.push if port == 8080 else self.rest
        return await super().resolve(host, port, family)


class Backend(SyntheticBackend):
    def __init__(self, directory, *, error=False):
        super().__init__(directory)
        self.error = error
        self.app_key = "synthetic"
        self.redirect_socket = False
        self.authorized_sockets = []
        self.socket_wait = None
        self.auth_wait = None
        self.socket_entered = asyncio.Event()
        self.auth_entered = asyncio.Event()
        self.gets = 0
        self.resynchronized = asyncio.Event()
        self.pending_get = asyncio.Event()
        self.release_get = asyncio.Event()
        self.hold_get = False
        self.signature_verified = False
        self.rest_ciphers = []
        self.push_ciphers = []

    def signature(self, socket_id, channel):
        digest = hmac.new(
            b"synthetic-secret", f"{socket_id}:{channel}".encode(), hashlib.sha256
        ).hexdigest()
        return f"{self.app_key}:{digest}"

    async def handle(self, request):
        cipher = request.transport.get_extra_info("ssl_object").cipher()[0]
        if request.path.startswith("/app/"):
            self.app_key = request.path.rsplit("/", 1)[-1]
            self.push_ciphers.append(cipher)
            self.calls += 1
            self.socket_entered.set()
            if self.socket_wait is not None:
                await self.socket_wait.wait()
            if self.redirect_socket:
                return web.Response(
                    status=302,
                    headers={"Location": "https://elsewhere.invalid/app/synthetic"},
                )
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            self.sockets.append(socket)
            socket_id = f"1.{len(self.sockets)}"
            await socket.send_json(
                {
                    "event": "pusher:connection_established",
                    "data": {"socket_id": socket_id, "activity_timeout": 30},
                }
            )
            async for frame in socket:
                value = json.loads(frame.data)
                if value["event"] == "pusher:subscribe":
                    data = value["data"]
                    self.signature_verified = data[
                        "channel"
                    ] == "private-device" and data["auth"] == self.signature(
                        socket_id, "private-device"
                    )
                    if self.error or not self.signature_verified:
                        await socket.send_json(
                            {"event": "pusher:error", "data": {"code": 4009}}
                        )
                        continue
                    await socket.send_json(
                        {
                            "event": "pusher_internal:subscription_succeeded",
                            "channel": "private-device",
                            "data": {},
                        }
                    )
                    if not self.emit_events:
                        continue
                    await self.resynchronized.wait()
                    await socket.send_json(
                        {
                            "event": "device",
                            "channel": "private-device",
                            "data": {"id": "device", "power": "0"},
                        }
                    )
                elif value["event"] == "pusher:ping":
                    await socket.send_json({"event": "pusher:pong", "data": {}})
            return socket
        self.rest_ciphers.append(cipher)
        if request.path == "/pusher/auth":
            self.calls += 1
            data = await request.post()
            self.authorized_sockets.append(data["socket_id"])
            self.auth_entered.set()
            if self.auth_wait is not None:
                await self.auth_wait.wait()
            return web.json_response(
                {"auth": self.signature(data["socket_id"], data["channel_name"])}
            )
        if request.path == "/devices":
            self.gets += 1
            if self.gets >= 2:
                self.resynchronized.set()
            if self.hold_get:
                self.pending_get.set()
                await self.release_get.wait()
        return await super().handle(request)

    async def start(self, *, expired=False, wrong_hostname=False, rsa_only=True):
        push_ssl, self.client_ssl = certificate(self.directory)
        if expired or wrong_hostname:
            pem = Path(self.directory) / "synthetic-cert.pem"
            private = Path(self.directory) / "synthetic-key.pem"
            old = x509.load_pem_x509_certificate(pem.read_bytes())
            key = serialization.load_pem_private_key(private.read_bytes(), None)
            now = datetime.datetime.now(datetime.UTC)
            cert = (
                x509.CertificateBuilder()
                .subject_name(old.subject)
                .issuer_name(old.issuer)
                .public_key(old.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=3))
                .not_valid_after(now + datetime.timedelta(days=-2 if expired else 1))
                .add_extension(
                    x509.SubjectAlternativeName(
                        [x509.DNSName("other.invalid")]
                        if wrong_hostname
                        else [x509.DNSName("dkn.airzonecloud.com")]
                    ),
                    critical=False,
                )
                .sign(key, hashes.SHA256())
            )
            pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            push_ssl.load_cert_chain(pem, private)
            self.client_ssl = ssl.create_default_context(cafile=str(pem))
        if rsa_only:
            push_ssl.minimum_version = push_ssl.maximum_version = ssl.TLSVersion.TLSv1_2
            push_ssl.set_ciphers("AES128-GCM-SHA256")
        rest_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        rest_ssl.minimum_version = ssl.TLSVersion.TLSv1_2
        rest_ssl.load_cert_chain(
            Path(self.directory) / "synthetic-cert.pem",
            Path(self.directory) / "synthetic-key.pem",
        )
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0, ssl_context=rest_ssl)
        self.push_site = web.TCPSite(self.runner, "127.0.0.1", 0, ssl_context=push_ssl)
        await self.site.start()
        await self.push_site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.push_port = self.push_site._server.sockets[0].getsockname()[1]
        return self

    def connector(self, *, rsa_rest=False):
        return TCPConnector(
            resolver=Resolver(
                self.push_port if rsa_rest else self.port, self.push_port
            ),
            ssl=self.client_ssl,
        )

    async def close(self):
        for wait in (self.socket_wait, self.auth_wait):
            if wait is not None:
                wait.set()
        self.release_get.set()
        await super().close()
