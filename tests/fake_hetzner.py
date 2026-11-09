"""In-memory stand-in for the zone / RRSet endpoints of the Hetzner Cloud API."""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx2

API_URL = "https://api.hetzner.test/v1"


@dataclass
class StoredRRSet:
    ttl: int | None
    records: list[dict[str, Any]]


@dataclass
class FakeHetzner:
    token: str = "hetzner-token"
    zones: dict[str, int] = field(default_factory=lambda: {"example.com": 1})
    rrsets: dict[tuple[int, str, str], StoredRRSet] = field(default_factory=dict)
    requests: list[httpx2.Request] = field(default_factory=list)
    # Number of polls an action stays "running" before it finishes.
    action_polls: int = 0
    # Final status of actions ("success" or "error").
    action_result: str = "success"
    # When set, every request is answered with this status and error code.
    fail: tuple[int, str] | None = None
    raise_error: Exception | None = None
    # When set, every request is answered with this response.
    raw_response: httpx2.Response | None = None
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(100))
    _actions: dict[int, int] = field(default_factory=dict)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def add(self, zone: str, name: str, rtype: str, *values: str, ttl: int | None = 3600) -> None:
        self.rrsets[(self.zones[zone], name, rtype)] = StoredRRSet(
            ttl=ttl, records=[{"value": value, "comment": ""} for value in values]
        )

    def values(self, zone: str, name: str, rtype: str) -> list[str] | None:
        rrset = self.rrsets.get((self.zones[zone], name, rtype))
        return None if rrset is None else [record["value"] for record in rrset.records]

    def writes(self) -> list[httpx2.Request]:
        return [request for request in self.requests if request.method != "GET"]

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.raise_error is not None:
            raise self.raise_error
        if self.raw_response is not None:
            return self.raw_response
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return _error(401, "unauthorized", "Request was made with an invalid or unknown token.")
        if self.fail is not None:
            return _error(self.fail[0], self.fail[1], "simulated failure")

        path = request.url.path.removeprefix("/v1")
        params = request.url.params
        if request.method == "GET" and path == "/zones":
            name = params.get("name")
            zones = [
                {"id": zone_id, "name": zone_name, "mode": "primary"}
                for zone_name, zone_id in self.zones.items()
                if name is None or zone_name == name
            ]
            return httpx2.Response(200, json={"zones": zones, "meta": _meta(len(zones))})

        if match := re.fullmatch(r"/zones/(\d+)/rrsets", path):
            zone_id = int(match[1])
            if request.method == "GET":
                types = params.get_list("type")
                rrsets = [
                    self._rrset_json(key)
                    for key in self.rrsets
                    if key[0] == zone_id
                    and key[1] == params.get("name", key[1])
                    and (not types or key[2] in types)
                ]
                return httpx2.Response(200, json={"rrsets": rrsets, "meta": _meta(len(rrsets))})
            body = json.loads(request.content)
            key = (zone_id, body["name"], body["type"])
            if key in self.rrsets:
                return _error(409, "uniqueness_error", "rrset already exists")
            self.rrsets[key] = StoredRRSet(ttl=body.get("ttl"), records=body["records"])
            return httpx2.Response(
                201,
                json={"rrset": self._rrset_json(key), "action": self._action("create_rrset")},
            )

        pattern = r"/zones/(\d+)/rrsets/([^/]+)/([A-Z]+)/actions/set_records"
        if request.method == "POST" and (match := re.fullmatch(pattern, path)):
            key = (int(match[1]), match[2], match[3])
            if key not in self.rrsets:
                return _error(404, "not_found", "Entity not found.")
            self.rrsets[key].records = json.loads(request.content)["records"]
            return httpx2.Response(201, json={"action": self._action("set_records")})

        if request.method == "GET" and (match := re.fullmatch(r"/zones/actions/(\d+)", path)):
            action_id = int(match[1])
            self._actions[action_id] -= 1
            return httpx2.Response(200, json={"action": self._action_json(action_id, "poll")})

        return _error(404, "not_found", "Entity not found.")

    def _rrset_json(self, key: tuple[int, str, str]) -> dict[str, Any]:
        rrset = self.rrsets[key]
        return {
            "id": f"{key[1]}/{key[2]}",
            "name": key[1],
            "type": key[2],
            "ttl": rrset.ttl,
            "labels": {},
            "protection": {"change": False},
            "records": rrset.records,
            "zone": key[0],
        }

    def _action(self, command: str) -> dict[str, Any]:
        action_id = next(self._ids)
        self._actions[action_id] = self.action_polls
        return self._action_json(action_id, command)

    def _action_json(self, action_id: int, command: str) -> dict[str, Any]:
        remaining = self._actions[action_id]
        status = "running" if remaining > 0 else self.action_result
        error = {"code": "action_failed", "message": "Action failed"}
        return {
            "id": action_id,
            "command": command,
            "status": status,
            "progress": 0 if status == "running" else 100,
            "error": error if status == "error" else None,
        }


def _meta(total: int) -> dict[str, Any]:
    return {
        "pagination": {
            "page": 1,
            "per_page": 50,
            "previous_page": None,
            "next_page": None,
            "last_page": 1,
            "total_entries": total,
        }
    }


def _error(status: int, code: str, message: str) -> httpx2.Response:
    return httpx2.Response(
        status, json={"error": {"code": code, "message": message, "details": {}}}
    )
