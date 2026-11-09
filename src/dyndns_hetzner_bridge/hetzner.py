"""Minimal async client for the DNS part of the Hetzner Cloud API.

Only the handful of endpoints needed for dynamic DNS updates are implemented:

* ``GET  /zones?name=...``                                           find a zone
* ``GET  /zones/{zone}/rrsets?name=...&type=...``                    read RRSets
* ``POST /zones/{zone}/rrsets``                                      create an RRSet
* ``POST /zones/{zone}/rrsets/{name}/{type}/actions/set_records``    replace records
* ``GET  /zones/actions/{id}``                                       poll an action

See https://docs.hetzner.cloud/reference/cloud#zones
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx2

log = logging.getLogger(__name__)


class HetznerError(Exception):
    """An error response from (or failure to reach) the Hetzner API."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code

    def __str__(self) -> str:
        details = ", ".join(
            part
            for part in (
                f"status={self.status}" if self.status else "",
                f"code={self.code}" if self.code else "",
            )
            if part
        )
        message = super().__str__()
        return f"{message} ({details})" if details else message


class HetznerAuthError(HetznerError):
    """The API token was rejected (missing, invalid or lacking permissions)."""


class HetznerUnavailableError(HetznerError):
    """The Hetzner API is unreachable or temporarily unable to serve the request."""


@dataclass(frozen=True)
class Zone:
    id: int
    name: str


@dataclass(frozen=True)
class Record:
    value: str
    comment: str | None = None

    def to_payload(self) -> dict[str, str]:
        payload = {"value": self.value}
        if self.comment:
            payload["comment"] = self.comment
        return payload


@dataclass(frozen=True)
class RRSet:
    name: str
    type: str
    ttl: int | None
    records: tuple[Record, ...]


class HetznerDNS:
    """Talks to the Hetzner Cloud API using a token supplied per call."""

    def __init__(
        self,
        http: httpx2.AsyncClient,
        *,
        action_timeout: float = 15.0,
        poll_interval: float = 0.5,
    ) -> None:
        self._http = http
        self._action_timeout = action_timeout
        self._poll_interval = poll_interval

    async def find_zone(self, token: str, hostname: str) -> Zone | None:
        """Return the most specific zone that ``hostname`` belongs to, if any."""
        labels = hostname.split(".")
        # Try the longest candidate first; a single label (the TLD) can't be a zone.
        for index in range(len(labels) - 1):
            candidate = ".".join(labels[index:])
            data = await self._request(token, "GET", "/zones", params={"name": candidate})
            for zone in data.get("zones", []):
                if str(zone.get("name", "")).lower().rstrip(".") == candidate:
                    return Zone(id=int(zone["id"]), name=candidate)
        return None

    async def get_rrsets(
        self, token: str, zone: Zone, name: str, types: Iterable[str]
    ) -> dict[str, RRSet]:
        """Return the RRSets called ``name`` with one of ``types``, keyed by type."""
        wanted = list(types)
        data = await self._request(
            token,
            "GET",
            f"/zones/{zone.id}/rrsets",
            params={"name": name, "type": wanted, "per_page": 50},
        )
        result: dict[str, RRSet] = {}
        for item in data.get("rrsets", []):
            if item.get("name") != name or item.get("type") not in wanted:
                continue
            result[item["type"]] = RRSet(
                name=item["name"],
                type=item["type"],
                ttl=item.get("ttl"),
                records=tuple(
                    Record(value=str(record["value"]), comment=record.get("comment") or None)
                    for record in item.get("records") or ()
                ),
            )
        return result

    async def create_rrset(
        self,
        token: str,
        zone: Zone,
        name: str,
        rtype: str,
        records: Sequence[Record],
        ttl: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "name": name,
            "type": rtype,
            "records": [record.to_payload() for record in records],
        }
        if ttl is not None:
            payload["ttl"] = ttl
        data = await self._request(token, "POST", f"/zones/{zone.id}/rrsets", json=payload)
        await self._wait_for_action(token, data.get("action"))

    async def set_records(
        self, token: str, zone: Zone, name: str, rtype: str, records: Sequence[Record]
    ) -> None:
        path = f"/zones/{zone.id}/rrsets/{quote(name, safe='@')}/{rtype}/actions/set_records"
        data = await self._request(
            token, "POST", path, json={"records": [record.to_payload() for record in records]}
        )
        await self._wait_for_action(token, data.get("action"))

    async def _wait_for_action(self, token: str, action: dict[str, Any] | None) -> None:
        """Wait until an asynchronous action has finished; raise if it failed."""
        deadline = time.monotonic() + self._action_timeout
        while action is not None:
            status = action.get("status")
            if status == "success":
                return
            if status == "error":
                error = action.get("error") or {}
                raise HetznerError(
                    f"Action {action.get('command', '')} failed: "
                    f"{error.get('message', 'unknown error')}",
                    code=error.get("code"),
                )
            if time.monotonic() >= deadline:
                log.warning(
                    "Action %s still %s after %.0fs, not waiting any longer",
                    action.get("id"),
                    status,
                    self._action_timeout,
                )
                return
            await asyncio.sleep(self._poll_interval)
            data = await self._request(token, "GET", f"/zones/actions/{action['id']}")
            action = data.get("action")

    async def _request(
        self,
        token: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._http.request(
                method,
                path,
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx2.HTTPError as exc:
            raise HetznerUnavailableError(
                f"Request to Hetzner API failed: {exc.__class__.__name__}: {exc}"
            ) from exc

        try:
            data = response.json()
        except ValueError:
            data = None

        if response.is_success:
            if not isinstance(data, dict):
                raise HetznerError(
                    f"Unexpected response from Hetzner API for {method} {path}",
                    status=response.status_code,
                )
            return data

        error = data.get("error") if isinstance(data, dict) else None
        error = error if isinstance(error, dict) else {}
        message = error.get("message") or response.reason_phrase or "unknown error"
        code = error.get("code")
        status = response.status_code
        if status == 401 or code == "forbidden":
            raise HetznerAuthError(message, status=status, code=code)
        if status >= 500 or status == 429 or code == "maintenance":
            raise HetznerUnavailableError(message, status=status, code=code)
        raise HetznerError(message, status=status, code=code)
