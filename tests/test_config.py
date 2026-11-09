from __future__ import annotations

from pathlib import Path

import pytest

from dyndns_hetzner_bridge.config import DEFAULT_API_URL, ConfigError, Settings


def test_defaults_to_passthrough_mode() -> None:
    settings = Settings.from_env({})
    assert settings.passthrough
    assert settings.api_url == DEFAULT_API_URL
    assert settings.listen_port == 8245
    assert settings.record_ttl == 60
    assert settings.allowed_hostnames == ()


def test_managed_mode() -> None:
    settings = Settings.from_env(
        {
            "HETZNER_API_TOKEN": " token\n",
            "DYNDNS_USERNAME": "router",
            "DYNDNS_PASSWORD": "secret",
            "ALLOWED_HOSTNAMES": "Home.example.com., *.dyn.example.com\nfoo.example.org",
            "RECORD_TTL": "300",
            "LISTEN_HOST": "::",
            "LISTEN_PORT": "8080",
            "LOG_LEVEL": "debug",
            "HETZNER_API_URL": "https://example.test/v1/",
        }
    )
    assert not settings.passthrough
    assert settings.api_token == "token"
    assert settings.allowed_hostnames == (
        "home.example.com",
        "*.dyn.example.com",
        "foo.example.org",
    )
    assert settings.record_ttl == 300
    assert settings.listen_host == "::"
    assert settings.listen_port == 8080
    assert settings.log_level == "DEBUG"
    assert settings.api_url == "https://example.test/v1"


def test_secrets_from_files(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("file-token\n")
    password_file = tmp_path / "password"
    password_file.write_text("file-password\n")
    settings = Settings.from_env(
        {
            "HETZNER_API_TOKEN_FILE": str(token_file),
            "DYNDNS_USERNAME": "router",
            "DYNDNS_PASSWORD_FILE": str(password_file),
        }
    )
    assert settings.api_token == "file-token"
    assert settings.password == "file-password"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"HETZNER_API_TOKEN": "t"}, "DYNDNS_USERNAME and DYNDNS_PASSWORD are required"),
        ({"HETZNER_API_TOKEN": "t", "DYNDNS_USERNAME": "u"}, "are required"),
        ({"DYNDNS_USERNAME": "u", "DYNDNS_PASSWORD": "p"}, "only used together"),
        ({"HETZNER_API_TOKEN": "t", "HETZNER_API_TOKEN_FILE": "/x"}, "not both"),
        ({"HETZNER_API_TOKEN_FILE": "/does/not/exist"}, "Cannot read"),
        ({"RECORD_TTL": "30"}, "RECORD_TTL"),
        ({"RECORD_TTL": "sixty"}, "must be a number"),
        ({"LISTEN_PORT": "70000"}, "LISTEN_PORT"),
        ({"API_TIMEOUT": "0"}, "API_TIMEOUT"),
        ({"LOG_LEVEL": "LOUD"}, "LOG_LEVEL"),
    ],
)
def test_invalid_configuration(env: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        Settings.from_env(env)
