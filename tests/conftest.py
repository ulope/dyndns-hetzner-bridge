from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient

from dyndns_hetzner_bridge import Settings, create_app

from .fake_hetzner import API_URL, FakeHetzner

ClientFactory = Callable[..., TestClient]

USERNAME = "router"
PASSWORD = "s3cret"
CLIENT_IP = "203.0.113.10"


@pytest.fixture
def fake() -> FakeHetzner:
    return FakeHetzner()


@pytest.fixture
def make_client(fake: FakeHetzner) -> Iterator[ClientFactory]:
    """Create a test client; defaults to managed mode with the fake's token."""
    clients: list[TestClient] = []

    def factory(*, client_ip: str = CLIENT_IP, **overrides: Any) -> TestClient:
        options: dict[str, Any] = {
            "api_url": API_URL,
            "api_token": fake.token,
            "username": USERNAME,
            "password": PASSWORD,
            "action_timeout": 5.0,
        }
        options.update(overrides)
        app = create_app(Settings(**options), transport=fake.transport(), poll_interval=0)
        client = TestClient(app, client=(client_ip, 4711))
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: ClientFactory) -> TestClient:
    return make_client()


@pytest.fixture
def passthrough_client(make_client: ClientFactory) -> TestClient:
    return make_client(api_token=None, username=None, password=None)
