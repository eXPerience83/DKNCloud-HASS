"""Explicitly gated TLS compatibility for the service's WebSocket only."""

import logging
import ssl
from contextlib import asynccontextmanager

from aiohttp import ClientSession, DummyCookieJar, TraceConfig
from homeassistant.util.ssl import SSL_ALPN_HTTP11, create_client_context
from yarl import URL

_LOGGER = logging.getLogger(__name__)

# Static RSA has no forward secrecy. Activation requires a separate security
# decision; there is deliberately no automatic fallback or configuration toggle.
STATIC_RSA_COMPATIBILITY_ENABLED = False
PUSHER_HOST = "dkn.airzonecloud.com"
PUSHER_PORT = 8080
_ORIGIN = URL.build(scheme="wss", host=PUSHER_HOST, port=PUSHER_PORT)


def validate_context(url: str, context: ssl.SSLContext) -> None:
    """Reject a different destination or weakened certificate/protocol policy."""
    target = URL(url)
    if (
        target.origin() != _ORIGIN
        or target.user is not None
        or target.fragment
        or not target.path.startswith("/app/")
        or not isinstance(context, ssl.SSLContext)
        or not context.check_hostname
        or context.verify_mode != ssl.CERT_REQUIRED
        or context.minimum_version < ssl.TLSVersion.TLSv1_2
        or context.security_level < 2
    ):
        raise ValueError("WebSocket TLS compatibility policy rejected")


def create_compatibility_context() -> ssl.SSLContext:
    """Create fresh HA trust, preserving modern suites and adding only one.

    Certificate loading is blocking: the adapter runs this in an executor.
    Never mutate HA's cached contexts or change the REST session/connector.
    """
    context = create_client_context(alpn_protocols=SSL_ALPN_HTTP11)
    modern = [c["name"] for c in context.get_ciphers() if c["protocol"] != "TLSv1.3"]
    context.set_ciphers(":".join(modern + ["AES128-GCM-SHA256"]))
    context.minimum_version = max(context.minimum_version, ssl.TLSVersion.TLSv1_2)
    _LOGGER.warning(
        "WebSocket TLS compatibility permits static RSA without forward secrecy"
    )
    return context


@asynccontextmanager
async def compatibility_websocket(
    url: str,
    *,
    ssl_context: ssl.SSLContext,
    autoping: bool = True,
    max_msg_size: int = 1024 * 1024,
):
    """Own only this socket's transport; deny redirects before another request.

    aiohttp's ws_connect has no allow_redirects option. An independent session
    installs the redirect guard without changing HA's shared REST session.
    """
    validate_context(url, ssl_context)

    async def deny_redirect(*_):
        raise ValueError("WebSocket redirect rejected")

    trace = TraceConfig()
    trace.on_request_redirect.append(deny_redirect)
    async with ClientSession(
        trace_configs=[trace], cookie_jar=DummyCookieJar(), trust_env=False
    ) as session:
        async with session.ws_connect(
            url, ssl=ssl_context, autoping=autoping, max_msg_size=max_msg_size
        ) as socket:
            yield socket
