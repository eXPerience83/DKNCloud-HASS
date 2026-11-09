"""Synthetic HTTPS/WSS fixture with exact destination names and no external DNS."""

import datetime
import ipaddress
import json
import socket
import ssl
from pathlib import Path

from aiohttp import TCPConnector, web
from aiohttp.abc import AbstractResolver
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

HOSTS = {"dkn.airzonecloud.com", "ws.pusherapp.com"}


def certificate(directory):
    """Generate disposable synthetic trust material, never a production key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic laboratory")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(host) for host in sorted(HOSTS)]
                + [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    directory = Path(directory)
    pem, private = directory / "synthetic-cert.pem", directory / "synthetic-key.pem"
    pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    private.chmod(0o600)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(pem, private)
    client = ssl.create_default_context(cafile=str(pem))
    return server, client


class LoopbackResolver(AbstractResolver):
    """Resolve only the two allowed fixture names to this ephemeral TLS server."""

    def __init__(self, port):
        self.port = port

    async def resolve(self, host, port=0, family=socket.AF_INET):
        if host not in HOSTS or port != 443:
            raise OSError("Synthetic destination denied")
        return [
            {
                "hostname": host,
                "host": "127.0.0.1",
                "port": self.port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": 0,
            }
        ]

    async def close(self):
        pass


class SyntheticBackend:
    """Minimal contracts; retain only counters, never complete requests."""

    def __init__(self, directory):
        self.directory = directory
        self.calls = 0
        self.operations = {}
        self.controls = 0
        self.events = 0
        self.power = "1"
        self.sockets = []
        self.status = 200
        self.redirect = False
        self.malformed = False
        self.wait = None
        self.wait_path = None

    async def handle(self, request):
        self.calls += 1
        operation = {
            "/users/sign_in": "login",
            "/pusher/auth": "authorize",
            "/devices": "get",
            "/events": "info",
            "/installation_relations": "discover",
        }.get(request.path, "socket")
        self.operations[operation] = self.operations.get(operation, 0) + 1
        if self.wait is not None and (
            self.wait_path is None or request.path == self.wait_path
        ):
            await self.wait.wait()
        if self.redirect:
            raise web.HTTPFound("https://elsewhere.invalid/forbidden")
        if self.status != 200:
            return web.json_response({}, status=self.status)
        if self.malformed:
            return web.Response(text="invalid JSON", content_type="application/json")
        if request.path.startswith("/app/"):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            self.sockets.append(ws)
            await ws.send_json(
                {
                    "event": "pusher:connection_established",
                    "data": json.dumps({"socket_id": "1.2", "activity_timeout": 30}),
                }
            )
            async for frame in ws:
                data = json.loads(frame.data)
                if data["event"] == "pusher:ping":
                    await ws.send_json({"event": "pusher:pong", "data": {}})
                elif data["event"] == "pusher:subscribe":
                    self.power = "0"
                    await ws.send_json(
                        {
                            "event": "pusher_internal:subscription_succeeded",
                            "channel": "private-device",
                            "data": {},
                        }
                    )
                    await ws.send_json(
                        {
                            "event": "device",
                            "channel": "private-device",
                            "data": {"id": "device", "power": "0"},
                        }
                    )
                    self.events += 1
            return ws
        if request.path == "/users/sign_in":
            return web.json_response({"authentication_token": "synthetic-token"})
        if request.path == "/installation_relations":
            return web.json_response([{"installation_id": "site"}])
        if request.path == "/devices":
            return web.json_response(
                [
                    {
                        "id": "device",
                        "name": "Synthetic",
                        "mac": "02:00:00:00:00:01",
                        "scenary": "occupied",
                        "power": self.power,
                        "mode": "1",
                        "modes": "11111",
                        "local_temp": "22",
                        "sleep_time": 30,
                        "cold_consign": "24",
                        "heat_consign": "20",
                        "availables_speeds": "3",
                        "connection_date": "2024-01-01T00:00:00+00:00",
                    }
                ]
            )
        if request.path == "/pusher/auth":
            return web.json_response({"auth": "synthetic-signature"})
        if request.path == "/events" and request.method == "POST":
            payload = await request.json()
            if payload.get("event", {}).get("cgi") == "infomaquina":
                return web.json_response({}, status=201)
        self.controls += 1
        return web.json_response({}, status=405)

    async def start(self):
        server_ssl, client_ssl = certificate(self.directory)
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0, ssl_context=server_ssl)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.client_ssl = client_ssl
        return self

    def connector(self, *, trusted=True):
        return TCPConnector(
            resolver=LoopbackResolver(self.port),
            ssl=self.client_ssl if trusted else True,
        )

    async def close(self):
        for ws in self.sockets:
            await ws.close()
        await self.runner.cleanup()
