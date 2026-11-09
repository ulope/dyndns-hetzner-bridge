from __future__ import annotations

import asyncio
import ipaddress

import httpx2
import pytest

from dyndns_hetzner_bridge.config import Settings
from dyndns_hetzner_bridge.dyndns import (
    InvalidAddressError,
    ReturnCode,
    Updater,
    normalize_hostname,
    parse_addresses,
    record_name,
)
from dyndns_hetzner_bridge.hetzner import HetznerDNS

from .fake_hetzner import API_URL, FakeHetzner


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("home.example.com", "home.example.com"),
        (" HOME.Example.com. ", "home.example.com"),
        ("bücher.example", "xn--bcher-kva.example"),
        ("xn--bcher-kva.example", "xn--bcher-kva.example"),
        ("a-b.c-d.example", "a-b.c-d.example"),
        ("123.example.com", "123.example.com"),
        ("", None),
        (".", None),
        ("localhost", None),
        ("a..example.com", None),
        ("-a.example.com", None),
        ("a-.example.com", None),
        ("a_b.example.com", None),
        ("*.example.com", None),
        ("a b.example.com", None),
        ("x" * 64 + ".example.com", None),
        (".".join(["a" * 63] * 4) + ".com", None),
    ],
)
def test_normalize_hostname(raw: str, expected: str | None) -> None:
    assert normalize_hostname(raw) == expected


def test_parse_addresses() -> None:
    assert parse_addresses([]) == {}
    assert parse_addresses(["", " "]) == {}
    assert parse_addresses(["1.2.3.4"]) == {"A": ipaddress.ip_address("1.2.3.4")}
    assert parse_addresses(["1.2.3.4, [2001:db8::1]", "1.2.3.4"]) == {
        "A": ipaddress.ip_address("1.2.3.4"),
        "AAAA": ipaddress.ip_address("2001:db8::1"),
    }


@pytest.mark.parametrize(
    "values", [["nope"], ["1.2.3.4", "1.2.3.5"], ["2001:db8::1,2001:db8::2"], ["fe80::1%1"]]
)
def test_parse_addresses_rejects(values: list[str]) -> None:
    with pytest.raises(InvalidAddressError):
        parse_addresses(values)


def test_record_name() -> None:
    assert record_name("example.com", "example.com") == "@"
    assert record_name("home.example.com", "example.com") == "home"
    assert record_name("a.b.example.com", "example.com") == "a.b"


def test_settings_hostname_patterns() -> None:
    settings = Settings(allowed_hostnames=("home.example.com", "*.dyn.example.com"))
    assert settings.hostname_allowed("home.example.com")
    assert settings.hostname_allowed("a.dyn.example.com")
    assert not settings.hostname_allowed("dyn.example.com")
    assert not settings.hostname_allowed("www.example.com")
    assert Settings().hostname_allowed("anything.example")


def test_concurrent_updates_of_one_record_are_serialized() -> None:
    """Two simultaneous requests for a new record must not both try to create it."""
    fake = FakeHetzner()

    async def slow_handler(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0.01)
        return fake.handle(request)

    async def run() -> list[ReturnCode]:
        async with httpx2.AsyncClient(
            base_url=API_URL, transport=httpx2.MockTransport(slow_handler)
        ) as http:
            updater = Updater(HetznerDNS(http, poll_interval=0), Settings())
            address = {"A": ipaddress.ip_address("1.2.3.4")}
            results = await asyncio.gather(
                updater.update(fake.token, "home.example.com", address),
                updater.update(fake.token, "home.example.com", address),
            )
            return sorted(result.code for result in results)

    assert asyncio.run(run()) == [ReturnCode.GOOD, ReturnCode.NOCHG]
    assert fake.values("example.com", "home", "A") == ["1.2.3.4"]
