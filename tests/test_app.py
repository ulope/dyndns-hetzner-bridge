from __future__ import annotations

import base64
import json
import logging

import httpx2
import pytest
from starlette.testclient import TestClient

from .conftest import CLIENT_IP, PASSWORD, USERNAME, ClientFactory
from .fake_hetzner import FakeHetzner

AUTH = (USERNAME, PASSWORD)


def update(
    client: TestClient, auth: tuple[str, str] | None = AUTH, **params: str
) -> httpx2.Response:
    return client.get("/nic/update", params=params, auth=auth)


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.text == "OK"


def test_unknown_path_is_404(client: TestClient) -> None:
    assert client.get("/").status_code == 404


def test_unknown_path_is_logged(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    # What inadyn sends when its ddns-path is left empty: the hostname as the path.
    with caplog.at_level(logging.INFO, logger="dyndns_hetzner_bridge"):
        response = client.get(
            "/bopp.example.com",
            params={"password": "do-not-log-me"},
            headers={"User-Agent": "inadyn/2.13.0"},
        )
    assert response.status_code == 404
    assert response.text == "Not Found"
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "GET /bopp.example.com from 203.0.113.10" in message
    assert "inadyn/2.13.0" in message
    assert "/nic/update" in message
    assert "do-not-log-me" not in message


# --- authentication -------------------------------------------------------------------


def test_missing_credentials_are_challenged(client: TestClient, fake: FakeHetzner) -> None:
    response = update(client, auth=None, hostname="home.example.com", myip="1.2.3.4")
    assert response.status_code == 401
    assert response.text == "badauth"
    assert response.headers["WWW-Authenticate"].startswith("Basic ")
    assert fake.requests == []


@pytest.mark.parametrize("auth", [(USERNAME, "wrong"), ("wrong", PASSWORD), ("", "")])
def test_wrong_credentials(client: TestClient, fake: FakeHetzner, auth: tuple[str, str]) -> None:
    response = update(client, auth=auth, hostname="home.example.com", myip="1.2.3.4")
    assert response.status_code == 401
    assert response.text == "badauth"
    assert fake.requests == []


def test_malformed_basic_auth(client: TestClient) -> None:
    for header in ("Basic !!!notbase64", "Basic " + base64.b64encode(b"no-colon").decode()):
        response = client.get(
            "/nic/update",
            params={"hostname": "home.example.com", "myip": "1.2.3.4"},
            headers={"Authorization": header},
        )
        assert response.text == "badauth"


def test_credentials_in_query_string(client: TestClient, fake: FakeHetzner) -> None:
    response = update(
        client,
        auth=None,
        hostname="home.example.com",
        myip="1.2.3.4",
        username=USERNAME,
        password=PASSWORD,
    )
    assert response.text == "good 1.2.3.4"
    assert fake.values("example.com", "home", "A") == ["1.2.3.4"]


def test_managed_mode_uses_configured_token(client: TestClient, fake: FakeHetzner) -> None:
    update(client, hostname="home.example.com", myip="1.2.3.4")
    assert {request.headers["Authorization"] for request in fake.requests} == {
        f"Bearer {fake.token}"
    }


def test_managed_mode_with_rejected_token_is_server_error(
    make_client: ClientFactory, fake: FakeHetzner
) -> None:
    client = make_client(api_token="revoked")
    response = update(client, hostname="home.example.com", myip="1.2.3.4")
    assert response.status_code == 200
    assert response.text == "911"


def test_passthrough_mode_uses_password_as_token(
    passthrough_client: TestClient, fake: FakeHetzner
) -> None:
    response = update(
        passthrough_client,
        auth=("example.com", fake.token),
        hostname="home.example.com",
        myip="1.2.3.4",
    )
    assert response.text == "good 1.2.3.4"
    assert fake.values("example.com", "home", "A") == ["1.2.3.4"]


def test_passthrough_mode_with_invalid_token(
    passthrough_client: TestClient, fake: FakeHetzner
) -> None:
    response = update(
        passthrough_client,
        auth=("example.com", "invalid"),
        hostname="home.example.com",
        myip="1.2.3.4",
    )
    assert response.text == "badauth"
    assert fake.rrsets == {}


def test_passthrough_mode_with_empty_password(
    passthrough_client: TestClient, fake: FakeHetzner
) -> None:
    response = update(
        passthrough_client, auth=("example.com", ""), hostname="home.example.com", myip="1.2.3.4"
    )
    assert response.status_code == 401
    assert fake.requests == []


# --- record updates -------------------------------------------------------------------


def test_creates_missing_record(client: TestClient, fake: FakeHetzner) -> None:
    response = update(client, hostname="home.example.com", myip="1.2.3.4")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "good 1.2.3.4"
    rrset = fake.rrsets[(1, "home", "A")]
    assert rrset.ttl == 60
    assert rrset.records == [{"value": "1.2.3.4"}]


def test_created_record_uses_configured_ttl(make_client: ClientFactory, fake: FakeHetzner) -> None:
    update(make_client(record_ttl=300), hostname="home.example.com", myip="1.2.3.4")
    assert fake.rrsets[(1, "home", "A")].ttl == 300


def test_unchanged_record_is_nochg(client: TestClient, fake: FakeHetzner) -> None:
    fake.add("example.com", "home", "A", "1.2.3.4")
    response = update(client, hostname="home.example.com", myip="1.2.3.4")
    assert response.text == "nochg 1.2.3.4"
    assert fake.writes() == []


def test_updates_changed_record_keeping_ttl_and_comment(
    client: TestClient, fake: FakeHetzner
) -> None:
    fake.add("example.com", "home", "A", "5.6.7.8", ttl=120)
    fake.rrsets[(1, "home", "A")].records[0]["comment"] = "router"
    response = update(client, hostname="home.example.com", myip="1.2.3.4")
    assert response.text == "good 1.2.3.4"
    rrset = fake.rrsets[(1, "home", "A")]
    assert rrset.records == [{"value": "1.2.3.4", "comment": "router"}]
    assert rrset.ttl == 120
    (write,) = fake.writes()
    assert write.url.path == "/v1/zones/1/rrsets/home/A/actions/set_records"


def test_collapses_multiple_records_into_one(client: TestClient, fake: FakeHetzner) -> None:
    fake.add("example.com", "home", "A", "1.2.3.4", "5.6.7.8")
    response = update(client, hostname="home.example.com", myip="1.2.3.4")
    assert response.text == "good 1.2.3.4"
    assert fake.values("example.com", "home", "A") == ["1.2.3.4"]


def test_ipv6_via_myipv6(client: TestClient, fake: FakeHetzner) -> None:
    response = update(client, hostname="home.example.com", myipv6="2001:DB8::1")
    assert response.text == "good 2001:db8::1"
    assert fake.values("example.com", "home", "AAAA") == ["2001:db8::1"]
    assert fake.values("example.com", "home", "A") is None


def test_ipv6_comparison_is_semantic(client: TestClient, fake: FakeHetzner) -> None:
    fake.add("example.com", "home", "AAAA", "2001:0db8:0000:0000:0000:0000:0000:0001")
    response = update(client, hostname="home.example.com", myipv6="2001:db8::1")
    assert response.text == "nochg 2001:db8::1"
    assert fake.writes() == []


def test_dual_stack_in_myip(client: TestClient, fake: FakeHetzner) -> None:
    fake.add("example.com", "home", "A", "1.2.3.4")
    response = update(client, hostname="home.example.com", myip="2001:db8::1,1.2.3.4")
    assert response.text == "good 1.2.3.4,2001:db8::1"
    assert fake.values("example.com", "home", "A") == ["1.2.3.4"]
    assert fake.values("example.com", "home", "AAAA") == ["2001:db8::1"]


def test_dual_stack_fritzbox_style(client: TestClient, fake: FakeHetzner) -> None:
    response = update(client, hostname="home.example.com", myip="1.2.3.4", myipv6="2001:db8::1")
    assert response.text == "good 1.2.3.4,2001:db8::1"


def test_empty_myipv6_is_ignored(client: TestClient, fake: FakeHetzner) -> None:
    response = update(client, hostname="home.example.com", myip="1.2.3.4", myipv6="")
    assert response.text == "good 1.2.3.4"
    assert fake.values("example.com", "home", "AAAA") is None


def test_dual_stack_is_one_lookup(client: TestClient, fake: FakeHetzner) -> None:
    update(client, hostname="home.example.com", myip="1.2.3.4,2001:db8::1")
    rrset_reads = [
        request
        for request in fake.requests
        if request.method == "GET" and request.url.path.endswith("/rrsets")
    ]
    assert len(rrset_reads) == 1
    assert rrset_reads[0].url.params.get_list("type") == ["A", "AAAA"]


def test_apex_record(client: TestClient, fake: FakeHetzner) -> None:
    response = update(client, hostname="example.com", myip="1.2.3.4")
    assert response.text == "good 1.2.3.4"
    assert fake.values("example.com", "@", "A") == ["1.2.3.4"]


def test_apex_record_update_path(client: TestClient, fake: FakeHetzner) -> None:
    fake.add("example.com", "@", "A", "5.6.7.8")
    assert update(client, hostname="example.com", myip="1.2.3.4").text == "good 1.2.3.4"
    (write,) = fake.writes()
    assert write.url.path == "/v1/zones/1/rrsets/@/A/actions/set_records"


def test_deep_subdomain(client: TestClient, fake: FakeHetzner) -> None:
    response = update(client, hostname="a.b.example.com", myip="1.2.3.4")
    assert response.text == "good 1.2.3.4"
    assert fake.values("example.com", "a.b", "A") == ["1.2.3.4"]


def test_most_specific_zone_wins(client: TestClient, fake: FakeHetzner) -> None:
    fake.zones["dyn.example.com"] = 2
    response = update(client, hostname="home.dyn.example.com", myip="1.2.3.4")
    assert response.text == "good 1.2.3.4"
    assert fake.values("dyn.example.com", "home", "A") == ["1.2.3.4"]
    assert fake.values("example.com", "home.dyn", "A") is None


def test_hostname_is_normalized(client: TestClient, fake: FakeHetzner) -> None:
    response = update(client, hostname="Home.Example.COM.", myip="1.2.3.4")
    assert response.text == "good 1.2.3.4"
    assert fake.values("example.com", "home", "A") == ["1.2.3.4"]


def test_internationalized_hostname(client: TestClient, fake: FakeHetzner) -> None:
    fake.zones["xn--bcher-kva.example"] = 3
    response = update(client, hostname="heim.bücher.example", myip="1.2.3.4")
    assert response.text == "good 1.2.3.4"
    assert fake.values("xn--bcher-kva.example", "heim", "A") == ["1.2.3.4"]


def test_multiple_hostnames(client: TestClient, fake: FakeHetzner) -> None:
    fake.add("example.com", "b", "A", "1.2.3.4")
    response = update(
        client, hostname="a.example.com,b.example.com,c.unknown.org,localhost", myip="1.2.3.4"
    )
    assert response.text.splitlines() == ["good 1.2.3.4", "nochg 1.2.3.4", "nohost", "notfqdn"]


def test_uses_client_address_without_myip(client: TestClient, fake: FakeHetzner) -> None:
    response = update(client, hostname="home.example.com")
    assert response.text == f"good {CLIENT_IP}"
    assert fake.values("example.com", "home", "A") == [CLIENT_IP]


def test_uses_ipv6_client_address(make_client: ClientFactory, fake: FakeHetzner) -> None:
    client = make_client(client_ip="2001:db8::42")
    assert update(client, hostname="home.example.com").text == "good 2001:db8::42"
    assert fake.values("example.com", "home", "AAAA") == ["2001:db8::42"]


def test_repeated_update(client: TestClient, fake: FakeHetzner) -> None:
    assert update(client, hostname="home.example.com", myip="1.2.3.4").text == "good 1.2.3.4"
    assert update(client, hostname="home.example.com", myip="1.2.3.4").text == "nochg 1.2.3.4"


# --- request validation ---------------------------------------------------------------


def test_missing_hostname(client: TestClient) -> None:
    assert update(client, myip="1.2.3.4").text == "notfqdn"


@pytest.mark.parametrize(
    "hostname", ["localhost", "under_score.example.com", "-bad.example.com", "a..example.com"]
)
def test_invalid_hostname(client: TestClient, fake: FakeHetzner, hostname: str) -> None:
    assert update(client, hostname=hostname, myip="1.2.3.4").text == "notfqdn"
    assert fake.requests == []


def test_too_many_hostnames(client: TestClient, fake: FakeHetzner) -> None:
    hostnames = ",".join(f"h{i}.example.com" for i in range(21))
    assert update(client, hostname=hostnames, myip="1.2.3.4").text == "numhost"
    assert fake.requests == []


@pytest.mark.parametrize(
    "params",
    [
        {"myip": "not-an-ip"},
        {"myip": "1.2.3.4,5.6.7.8"},
        {"myip": "1.2.3.4", "myipv4": "5.6.7.8"},
        {"myipv6": "fe80::1%eth0"},
    ],
)
def test_invalid_addresses(client: TestClient, fake: FakeHetzner, params: dict[str, str]) -> None:
    response = client.get(
        "/nic/update", params={"hostname": "home.example.com", **params}, auth=AUTH
    )
    assert response.text == "badagent"
    assert fake.requests == []


def test_empty_address_params_use_client_address(client: TestClient) -> None:
    response = update(client, hostname="home.example.com", myip="", myipv6="")
    assert response.text == f"good {CLIENT_IP}"


def test_client_address_must_be_an_ip(make_client: ClientFactory, fake: FakeHetzner) -> None:
    client = make_client(client_ip="testclient")
    assert update(client, hostname="home.example.com").text == "badagent"
    assert fake.requests == []


def test_unknown_zone(client: TestClient) -> None:
    assert update(client, hostname="home.example.org", myip="1.2.3.4").text == "nohost"


def test_allowed_hostnames(make_client: ClientFactory, fake: FakeHetzner) -> None:
    client = make_client(allowed_hostnames=("home.example.com", "*.dyn.example.com"))
    response = update(
        client, hostname="home.example.com,www.example.com,a.dyn.example.com", myip="1.2.3.4"
    )
    assert response.text.splitlines() == ["good 1.2.3.4", "nohost", "good 1.2.3.4"]
    assert fake.values("example.com", "www", "A") is None


def test_post_is_not_allowed(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="dyndns_hetzner_bridge"):
        response = client.post("/nic/update", params={"hostname": "home.example.com"}, auth=AUTH)
    assert response.status_code == 405
    assert response.headers["Allow"] == "GET, HEAD"
    assert "Ignored POST /nic/update" in caplog.text


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"myip": "1.2.3.4"}, "no hostname given"),
        ({"hostname": ",".join(f"h{i}.example.com" for i in range(21))}, "21 hostnames"),
    ],
)
def test_rejected_requests_are_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture, params: dict[str, str], expected: str
) -> None:
    with caplog.at_level(logging.INFO, logger="dyndns_hetzner_bridge"):
        client.get("/nic/update", params=params, auth=AUTH)
    assert expected in caplog.text


# --- API failures ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (500, "server_error", "911"),
        (503, "unavailable", "911"),
        (429, "rate_limit_exceeded", "911"),
        (403, "maintenance", "911"),
        (422, "invalid_input", "dnserr"),
        (423, "protected", "dnserr"),
    ],
)
def test_api_errors(
    client: TestClient, fake: FakeHetzner, status: int, code: str, expected: str
) -> None:
    fake.fail = (status, code)
    assert update(client, hostname="home.example.com", myip="1.2.3.4").text == expected


def test_api_unreachable(client: TestClient, fake: FakeHetzner) -> None:
    fake.raise_error = httpx2.ConnectError("connection refused")
    assert update(client, hostname="home.example.com", myip="1.2.3.4").text == "911"


def test_api_returns_garbage(client: TestClient, fake: FakeHetzner) -> None:
    fake.raw_response = httpx2.Response(200, text="<html>")
    assert update(client, hostname="home.example.com", myip="1.2.3.4").text == "dnserr"


def test_waits_for_running_action(client: TestClient, fake: FakeHetzner) -> None:
    fake.action_polls = 3
    assert update(client, hostname="home.example.com", myip="1.2.3.4").text == "good 1.2.3.4"
    polls = [request for request in fake.requests if "/actions/" in request.url.path]
    assert len(polls) == 3


def test_failed_action_is_dnserr(client: TestClient, fake: FakeHetzner) -> None:
    fake.action_result = "error"
    assert update(client, hostname="home.example.com", myip="1.2.3.4").text == "dnserr"


def test_gives_up_waiting_for_slow_action(make_client: ClientFactory, fake: FakeHetzner) -> None:
    client = make_client(action_timeout=0)
    fake.action_polls = 1000
    assert update(client, hostname="home.example.com", myip="1.2.3.4").text == "good 1.2.3.4"


def test_create_request_body(client: TestClient, fake: FakeHetzner) -> None:
    update(client, hostname="home.example.com", myip="1.2.3.4")
    (write,) = fake.writes()
    assert write.url.path == "/v1/zones/1/rrsets"
    assert json.loads(write.content) == {
        "name": "home",
        "type": "A",
        "ttl": 60,
        "records": [{"value": "1.2.3.4"}],
    }
