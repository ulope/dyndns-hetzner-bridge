"""Container health check: ``python -m dyndns_hetzner_bridge.healthcheck``."""

from __future__ import annotations

import os
import sys
import urllib.request


def main() -> int:
    host = os.environ.get("LISTEN_HOST", "127.0.0.1")
    if host in ("0.0.0.0", "::", ""):  # noqa: S104
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    port = os.environ.get("LISTEN_PORT", "8245")
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=5) as response:
            return 0 if response.status == 200 else 1
    except OSError as exc:
        print(f"Health check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
