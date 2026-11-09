"""dyndns2 protocol handling and the record update logic.

Protocol reference: https://help.dyn.com/remote-access-api/ (archived copies exist on
archive.org). Return codes are sent as the plain-text response body, one line per
hostname for multi-host updates.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import weakref
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from .config import Settings
from .hetzner import (
    HetznerAuthError,
    HetznerDNS,
    HetznerError,
    HetznerUnavailableError,
    Record,
    Zone,
)

log = logging.getLogger(__name__)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# The dyndns2 protocol allows up to 20 hostnames per request.
MAX_HOSTNAMES = 20

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class ReturnCode(StrEnum):
    GOOD = "good"
    NOCHG = "nochg"
    BADAUTH = "badauth"
    NOTFQDN = "notfqdn"
    NOHOST = "nohost"
    NUMHOST = "numhost"
    BADAGENT = "badagent"
    DNSERR = "dnserr"
    SERVER_ERROR = "911"


@dataclass(frozen=True)
class Result:
    code: ReturnCode
    addresses: tuple[IPAddress, ...] = ()

    def __str__(self) -> str:
        if self.code in (ReturnCode.GOOD, ReturnCode.NOCHG) and self.addresses:
            return f"{self.code} {','.join(str(address) for address in self.addresses)}"
        return str(self.code)


class InvalidAddressError(ValueError):
    pass


def normalize_hostname(raw: str) -> str | None:
    """Return the lower-case ASCII (IDNA) form of ``raw``, or ``None`` if invalid.

    A valid hostname has at least two labels, i.e. it is fully qualified.
    """
    hostname = raw.strip().rstrip(".").lower()
    if not hostname:
        return None
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    labels = hostname.split(".")
    if len(hostname) > 253 or len(labels) < 2:
        return None
    if not all(_LABEL.match(label) for label in labels):
        return None
    return hostname


def parse_addresses(values: Iterable[str]) -> dict[str, IPAddress]:
    """Parse IP addresses and map them to their record type (``A`` / ``AAAA``).

    Each value may contain several comma-separated addresses, e.g. ``1.2.3.4,2001:db8::1``.
    Empty values are ignored. Raises :class:`InvalidAddressError` for addresses that
    can't be parsed or for two different addresses of the same family.
    """
    result: dict[str, IPAddress] = {}
    for value in values:
        for part in value.split(","):
            part = part.strip().strip("[]")
            if not part:
                continue
            try:
                address = ipaddress.ip_address(part)
            except ValueError as exc:
                raise InvalidAddressError(f"Invalid IP address {part!r}") from exc
            if isinstance(address, ipaddress.IPv6Address) and address.scope_id:
                raise InvalidAddressError(f"Scoped IPv6 address {part!r} not supported")
            rtype = "A" if address.version == 4 else "AAAA"
            if result.get(rtype, address) != address:
                raise InvalidAddressError(f"More than one {rtype} address given")
            result[rtype] = address
    return result


def record_name(hostname: str, zone: str) -> str:
    """Name of the RRSet for ``hostname`` relative to ``zone`` (``@`` for the apex)."""
    if hostname == zone:
        return "@"
    return hostname.removesuffix(f".{zone}")


def _same_address(value: str, address: IPAddress) -> bool:
    try:
        return ipaddress.ip_address(value) == address
    except ValueError:
        return False


class Updater:
    """Applies address updates for hostnames to Hetzner DNS."""

    def __init__(self, dns: HetznerDNS, settings: Settings) -> None:
        self._dns = dns
        self._settings = settings
        # Serializes concurrent updates of the same record (e.g. separate IPv4 and
        # IPv6 requests sent at the same time), which would otherwise race to create it.
        self._locks: weakref.WeakValueDictionary[tuple[int, str], asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    async def update(self, token: str, hostname: str, addresses: dict[str, IPAddress]) -> Result:
        """Point ``hostname`` at ``addresses``; never raises for API errors."""
        result_addresses = tuple(addresses[rtype] for rtype in ("A", "AAAA") if rtype in addresses)
        try:
            zone = await self._dns.find_zone(token, hostname)
            if zone is None:
                log.info("No zone found for %s", hostname)
                return Result(ReturnCode.NOHOST)
            changed = await self._apply(token, zone, record_name(hostname, zone.name), addresses)
        except HetznerAuthError as exc:
            if self._settings.passthrough:
                log.info("Hetzner API rejected the client's token for %s: %s", hostname, exc)
                return Result(ReturnCode.BADAUTH)
            log.error("Hetzner API rejected the configured HETZNER_API_TOKEN: %s", exc)
            return Result(ReturnCode.SERVER_ERROR)
        except HetznerUnavailableError as exc:
            log.error("Hetzner API unavailable while updating %s: %s", hostname, exc)
            return Result(ReturnCode.SERVER_ERROR)
        except HetznerError as exc:
            log.error("Hetzner API error while updating %s: %s", hostname, exc)
            return Result(ReturnCode.DNSERR)
        code = ReturnCode.GOOD if changed else ReturnCode.NOCHG
        return Result(code, result_addresses)

    async def _apply(
        self, token: str, zone: Zone, name: str, addresses: dict[str, IPAddress]
    ) -> bool:
        key = (zone.id, name)
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        fqdn = zone.name if name == "@" else f"{name}.{zone.name}"
        async with lock:
            existing = await self._dns.get_rrsets(token, zone, name, addresses)
            changed = False
            for rtype, address in addresses.items():
                value = str(address)
                rrset = existing.get(rtype)
                if rrset is None:
                    log.info("Creating %s %s -> %s", rtype, fqdn, value)
                    await self._dns.create_rrset(
                        token, zone, name, rtype, [Record(value)], ttl=self._settings.record_ttl
                    )
                    changed = True
                elif len(rrset.records) != 1 or not _same_address(rrset.records[0].value, address):
                    old = ", ".join(record.value for record in rrset.records) or "-"
                    log.info("Updating %s %s: %s -> %s", rtype, fqdn, old, value)
                    comment = rrset.records[0].comment if rrset.records else None
                    await self._dns.set_records(token, zone, name, rtype, [Record(value, comment)])
                    changed = True
                else:
                    log.debug("%s %s already points to %s", rtype, fqdn, value)
            return changed
