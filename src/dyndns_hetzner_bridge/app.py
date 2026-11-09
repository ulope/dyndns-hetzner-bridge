"""HTTP front end speaking the dyndns2 protocol."""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

import httpx2
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from .config import Settings
from .dyndns import (
    MAX_HOSTNAMES,
    InvalidAddressError,
    Result,
    ReturnCode,
    Updater,
    normalize_hostname,
    parse_addresses,
)
from .hetzner import HetznerDNS

log = logging.getLogger(__name__)

try:
    __version__ = version("dyndns-hetzner-bridge")
except PackageNotFoundError:  # pragma: no cover - running from a source checkout
    __version__ = "0.0.0"

REALM = "dyndns-hetzner-bridge"


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    poll_interval: float = 0.5,
) -> Starlette:
    """Build the ASGI application.

    ``transport`` and ``poll_interval`` exist for tests, which replace the Hetzner API
    with an in-process fake.
    """
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with httpx2.AsyncClient(
            base_url=settings.api_url,
            timeout=settings.api_timeout,
            transport=transport,
            headers={"User-Agent": f"dyndns-hetzner-bridge/{__version__}"},
        ) as http:
            dns = HetznerDNS(
                http, action_timeout=settings.action_timeout, poll_interval=poll_interval
            )
            app.state.updater = Updater(dns, settings)
            yield

    async def update(request: Request) -> PlainTextResponse:
        credentials = _credentials(request)
        token = _authenticate(settings, credentials)
        if token is None:
            log.info("Rejected update from %s: bad credentials", _peer(request))
            return PlainTextResponse(
                str(ReturnCode.BADAUTH),
                status_code=401,
                headers={"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'},
            )

        query = request.query_params
        hostnames = [
            hostname.strip()
            for value in query.getlist("hostname")
            for hostname in value.split(",")
            if hostname.strip()
        ]
        if not hostnames:
            log.info("Rejected update from %s: no hostname given", _peer(request))
            return _reply(ReturnCode.NOTFQDN)
        if len(hostnames) > MAX_HOSTNAMES:
            log.info(
                "Rejected update from %s: %d hostnames, at most %d allowed",
                _peer(request),
                len(hostnames),
                MAX_HOSTNAMES,
            )
            return _reply(ReturnCode.NUMHOST)

        address_params = [
            value for name in ("myip", "myipv4", "myipv6") for value in query.getlist(name)
        ]
        try:
            addresses = parse_addresses(address_params)
            if not addresses:
                # dyndns2: without myip the address of the client is used.
                addresses = parse_addresses([request.client.host] if request.client else [])
        except InvalidAddressError as exc:
            log.info("Rejected update from %s: %s", _peer(request), exc)
            return _reply(ReturnCode.BADAGENT)
        if not addresses:
            log.info("Rejected update from %s: no usable IP address", _peer(request))
            return _reply(ReturnCode.BADAGENT)

        updater: Updater = request.app.state.updater
        results: list[Result] = []
        for raw_hostname in hostnames:
            hostname = normalize_hostname(raw_hostname)
            if hostname is None:
                result = Result(ReturnCode.NOTFQDN)
            elif not settings.hostname_allowed(hostname):
                log.warning("Hostname %s is not in ALLOWED_HOSTNAMES", hostname)
                result = Result(ReturnCode.NOHOST)
            else:
                result = await updater.update(token, hostname, addresses)
            log.info(
                "Update %s %s from %s: %s",
                hostname or raw_hostname,
                " ".join(f"{rtype}={address}" for rtype, address in addresses.items()),
                _peer(request),
                result,
            )
            results.append(result)
        return PlainTextResponse("\n".join(str(result) for result in results))

    async def health(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    return Starlette(
        routes=[
            Route("/nic/update", update, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
        ],
        exception_handlers={404: _unhandled_request, 405: _unhandled_request},
        lifespan=lifespan,
    )


async def _unhandled_request(request: Request, exc: Exception) -> Response:
    """Answer requests for unknown paths or methods, and log them.

    The access log is disabled (see ``__main__``), so without this a misconfigured
    client (e.g. a wrong update path) would leave no trace at all.
    """
    assert isinstance(exc, HTTPException)  # noqa: S101
    log.warning(
        "Ignored %s %s from %s (User-Agent: %s): %d %s; updates must be sent to GET /nic/update",
        request.method,
        _truncate(request.url.path),
        _peer(request),
        _truncate(request.headers.get("User-Agent", "-")),
        exc.status_code,
        exc.detail,
    )
    return PlainTextResponse(exc.detail, status_code=exc.status_code, headers=exc.headers)


def _truncate(value: str, limit: int = 200) -> str:
    return value if len(value) <= limit else f"{value[:limit]}..."


def _reply(code: ReturnCode) -> PlainTextResponse:
    return PlainTextResponse(str(code))


def _peer(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _credentials(request: Request) -> tuple[str, str] | None:
    """Extract credentials from HTTP basic auth, falling back to query parameters."""
    scheme, _, encoded = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "basic" and encoded.strip():
        try:
            raw = base64.b64decode(encoded.strip(), validate=True)
        except (binascii.Error, ValueError):
            return None
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            decoded = raw.decode("latin-1")
        username, separator, password = decoded.partition(":")
        return (username, password) if separator else None

    query_username = request.query_params.get("username")
    query_password = request.query_params.get("password")
    if query_username is not None and query_password is not None:
        return query_username, query_password
    return None


def _authenticate(settings: Settings, credentials: tuple[str, str] | None) -> str | None:
    """Return the Hetzner API token to use for the request, or ``None`` if unauthorized."""
    if credentials is None:
        return None
    username, password = credentials
    if settings.passthrough:
        # The password is the client's own Hetzner API token; the API validates it.
        return password.strip() or None
    assert settings.username is not None and settings.password is not None  # noqa: S101
    valid_user = hmac.compare_digest(username.encode(), settings.username.encode())
    valid_password = hmac.compare_digest(password.encode(), settings.password.encode())
    if valid_user and valid_password:
        return settings.api_token
    return None
