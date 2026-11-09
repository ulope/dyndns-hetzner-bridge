"""Configuration, read from environment variables."""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

DEFAULT_API_URL = "https://api.hetzner.cloud/v1"

# Limits imposed by the Hetzner Cloud API for RRSet TTLs.
MIN_TTL = 60
MAX_TTL = 2_147_483_647


class ConfigError(ValueError):
    """Raised when the environment contains an invalid configuration."""


@dataclass(frozen=True)
class Settings:
    """Runtime settings of the bridge.

    When ``api_token`` is set the bridge runs in *managed* mode: the Hetzner API
    token stays on the server and clients authenticate with ``username`` /
    ``password``. Without it the bridge runs in *passthrough* mode: the password a
    client sends is used as the Hetzner API token for that request.
    """

    api_url: str = DEFAULT_API_URL
    api_token: str | None = None
    username: str | None = None
    password: str | None = None
    allowed_hostnames: tuple[str, ...] = ()
    record_ttl: int = 60
    listen_host: str = "0.0.0.0"  # noqa: S104 - binding to all interfaces is intended
    listen_port: int = 8245
    log_level: str = "INFO"
    api_timeout: float = 10.0
    action_timeout: float = 15.0

    def __post_init__(self) -> None:
        if self.api_token is not None and not (self.username and self.password):
            raise ConfigError(
                "HETZNER_API_TOKEN is set, so DYNDNS_USERNAME and DYNDNS_PASSWORD are required "
                "to authenticate clients"
            )
        if self.api_token is None and (self.username or self.password):
            raise ConfigError(
                "DYNDNS_USERNAME / DYNDNS_PASSWORD are only used together with "
                "HETZNER_API_TOKEN; without it the client password is the Hetzner API token"
            )
        if not MIN_TTL <= self.record_ttl <= MAX_TTL:
            raise ConfigError(f"RECORD_TTL must be between {MIN_TTL} and {MAX_TTL}")
        if not 0 < self.listen_port < 65536:
            raise ConfigError("LISTEN_PORT must be between 1 and 65535")
        if self.api_timeout <= 0 or self.action_timeout < 0:
            raise ConfigError("API_TIMEOUT must be positive and ACTION_TIMEOUT not negative")
        if logging.getLevelNamesMapping().get(self.log_level) is None:
            raise ConfigError(f"Unknown LOG_LEVEL {self.log_level!r}")

    @property
    def passthrough(self) -> bool:
        """Whether the client password is used as the Hetzner API token."""
        return self.api_token is None

    def hostname_allowed(self, hostname: str) -> bool:
        """Check ``hostname`` (normalized, ASCII) against ``allowed_hostnames``."""
        if not self.allowed_hostnames:
            return True
        return any(fnmatch.fnmatchcase(hostname, pattern) for pattern in self.allowed_hostnames)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        return cls(
            api_url=env.get("HETZNER_API_URL", DEFAULT_API_URL).rstrip("/"),
            api_token=_secret(env, "HETZNER_API_TOKEN"),
            username=_secret(env, "DYNDNS_USERNAME"),
            password=_secret(env, "DYNDNS_PASSWORD"),
            allowed_hostnames=_patterns(env.get("ALLOWED_HOSTNAMES", "")),
            record_ttl=_number(env, "RECORD_TTL", 60, int),
            listen_host=env.get("LISTEN_HOST", "0.0.0.0"),  # noqa: S104
            listen_port=_number(env, "LISTEN_PORT", 8245, int),
            log_level=env.get("LOG_LEVEL", "INFO").strip().upper(),
            api_timeout=_number(env, "API_TIMEOUT", 10.0, float),
            action_timeout=_number(env, "ACTION_TIMEOUT", 15.0, float),
        )


def _secret(env: Mapping[str, str], name: str) -> str | None:
    """Read ``name`` from the environment, or from the file named by ``name``_FILE."""
    value = env.get(name)
    file_name = env.get(f"{name}_FILE")
    if value and file_name:
        raise ConfigError(f"Set either {name} or {name}_FILE, not both")
    if file_name:
        try:
            value = Path(file_name).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Cannot read {name}_FILE: {exc}") from exc
    if value is None:
        return None
    value = value.strip()
    return value or None


_N = TypeVar("_N", int, float)


def _number(env: Mapping[str, str], name: str, default: _N, kind: type[_N]) -> _N:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return kind(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _patterns(raw: str) -> tuple[str, ...]:
    return tuple(
        pattern.lower().rstrip(".") for pattern in re.split(r"[\s,]+", raw) if pattern.strip()
    )
