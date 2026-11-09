"""Command line entry point: ``dyndns-hetzner-bridge`` / ``python -m dyndns_hetzner_bridge``."""

from __future__ import annotations

import logging
import sys

import uvicorn

from .app import __version__, create_app
from .config import ConfigError, Settings


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # httpx2 logs every request at INFO level; only show that when debugging.
    if settings.log_level != "DEBUG":
        logging.getLogger("httpx2").setLevel(logging.WARNING)

    log = logging.getLogger("dyndns_hetzner_bridge")
    log.info(
        "dyndns-hetzner-bridge %s starting in %s mode on %s:%d",
        __version__,
        "passthrough" if settings.passthrough else "managed token",
        settings.listen_host,
        settings.listen_port,
    )
    if settings.allowed_hostnames:
        log.info("Allowed hostnames: %s", ", ".join(settings.allowed_hostnames))

    uvicorn.run(
        create_app(settings),
        host=settings.listen_host,
        port=settings.listen_port,
        # Use the logging configuration above instead of uvicorn's own.
        log_config=None,
        # The access log would contain the query string, which can carry credentials.
        access_log=False,
        server_header=False,
        # Honour X-Forwarded-For from the proxies listed in FORWARDED_ALLOW_IPS
        # (default 127.0.0.1) so that requests without myip use the real client address.
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
