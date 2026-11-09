# dyndns-hetzner-bridge

A small HTTP service that accepts [dyndns2](https://help.dyn.com/remote-access-api/)
update requests, the protocol spoken by routers (FRITZ!Box, OpenWrt, pfSense/OPNsense,
UniFi, …) and tools like `ddclient` and `inadyn`, and applies them to DNS zones hosted
at Hetzner through the **Hetzner Cloud API** (`api.hetzner.cloud`, zones and RRSets).
It does not use the legacy `dns.hetzner.com` API.

It is a Python replacement for the archived
[marvinruder/hetzner-dyndns](https://github.com/marvinruder/hetzner-dyndns) and can be
dropped in for it (see [Migrating from hetzner-dyndns](#migrating-from-hetzner-dyndns)).

## Features

* IPv4 (`A`) and IPv6 (`AAAA`) records, in the same request or in separate ones
* Up to 20 hostnames per request, including the zone apex (`example.com`)
* The zone is detected automatically from the hostname; no per-host configuration
* Missing records are created; existing ones keep their TTL and record comment
* Waits (up to `ACTION_TIMEOUT`) until Hetzner has applied the change before answering `good`
* Two authentication modes:
  * **managed**: the Hetzner API token stays on the server and clients get their own
    username and password, optionally limited to a set of hostnames
  * **passthrough**: clients send their Hetzner API token as the password
* Small, non-root Docker image for `linux/amd64` and `linux/arm64`, with a health check

## Quick start

Create an API token in the [Hetzner Console](https://console.hetzner.com/) for the
project that holds your DNS zone (*Security → API tokens*, permission **Read & Write**).

```bash
docker run -d --name dyndns -p 8245:8245 \
  -e HETZNER_API_TOKEN=your-hetzner-api-token \
  -e DYNDNS_USERNAME=router \
  -e DYNDNS_PASSWORD=a-long-random-password \
  -e ALLOWED_HOSTNAMES=home.example.com \
  ghcr.io/ulope/dyndns-hetzner-bridge:latest
```

Then point your router at it, or try it by hand:

```bash
curl -u router:a-long-random-password \
  "http://localhost:8245/nic/update?hostname=home.example.com&myip=203.0.113.7"
```

```text
good 203.0.113.7
```

### Docker Compose

```yaml
services:
  dyndns:
    image: ghcr.io/ulope/dyndns-hetzner-bridge:latest
    restart: unless-stopped
    ports:
      - "8245:8245"
    environment:
      HETZNER_API_TOKEN_FILE: /run/secrets/hetzner_token
      DYNDNS_USERNAME: router
      DYNDNS_PASSWORD_FILE: /run/secrets/dyndns_password
      ALLOWED_HOSTNAMES: home.example.com,*.dyn.example.com
    secrets:
      - hetzner_token
      - dyndns_password

secrets:
  hetzner_token:
    file: ./hetzner_token.txt
  dyndns_password:
    file: ./dyndns_password.txt
```

### Without Docker

Requires Python 3.11 or newer.

```bash
pip install git+https://github.com/ulope/dyndns-hetzner-bridge
```

```bash
HETZNER_API_TOKEN=... DYNDNS_USERNAME=router DYNDNS_PASSWORD=... dyndns-hetzner-bridge
```

## Authentication modes

**Managed mode** is active when `HETZNER_API_TOKEN` is set. Clients authenticate with
`DYNDNS_USERNAME` / `DYNDNS_PASSWORD` and never see the Hetzner token. This is the
recommended mode, especially together with `ALLOWED_HOSTNAMES`.

**Passthrough mode** is active when `HETZNER_API_TOKEN` is not set. The password a client
sends is used as the Hetzner API token for that request, and the username is ignored.
A wrong token is reported as `badauth`. The bridge stores no secrets at all in this mode,
but every client holds a token with full access to its Hetzner project.

## Configuration

All settings are environment variables.

| Variable | Default | Description |
| --- | --- | --- |
| `HETZNER_API_TOKEN` | – | Hetzner Cloud API token (Read & Write). Enables managed mode. |
| `DYNDNS_USERNAME` | – | Username clients must send. Required in managed mode. |
| `DYNDNS_PASSWORD` | – | Password clients must send. Required in managed mode. |
| `ALLOWED_HOSTNAMES` | *(all)* | Comma-separated hostnames that may be updated. Shell-style wildcards are supported, e.g. `*.dyn.example.com`. Other hostnames get `nohost`. |
| `RECORD_TTL` | `60` | TTL in seconds for records the bridge creates (60 – 2147483647). Existing records keep their TTL. |
| `LISTEN_HOST` | `0.0.0.0` | Address to listen on (use `::` for IPv6). |
| `LISTEN_PORT` | `8245` | Port to listen on. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1,::1` | Comma-separated proxy addresses (or `*`) whose `X-Forwarded-For` header is trusted. See [Reverse proxy](#reverse-proxy-and-https). |
| `API_TIMEOUT` | `10` | Timeout in seconds for each Hetzner API request. |
| `ACTION_TIMEOUT` | `15` | How long, in seconds, to wait for Hetzner to apply a change. `0` disables waiting. |
| `HETZNER_API_URL` | `https://api.hetzner.cloud/v1` | API base URL (only useful for testing). |

`HETZNER_API_TOKEN`, `DYNDNS_USERNAME` and `DYNDNS_PASSWORD` can also be read from a file
by appending `_FILE` to the name, for example for Docker secrets.

## Client setup

The update URL is `http(s)://<bridge>/nic/update`. Credentials are sent with HTTP basic
authentication. For clients that can't do that, `username` and `password` query
parameters are accepted as well.

### FRITZ!Box

*Internet → Permit Access → DynDNS* (*Internet → Freigaben → DynDNS*):

* Use DynDNS: enabled, DynDNS provider: **User-defined** (*Benutzerdefiniert*)
* Update URL:
  `https://dyndns.example.com/nic/update?hostname=<domain>&myip=<ipaddr>&myipv6=<ip6addr>`
* Domain name: `home.example.com`
* Username / Password: your `DYNDNS_USERNAME` / `DYNDNS_PASSWORD`

The FRITZ!Box leaves `<ip6addr>` empty when it has no IPv6 address. Empty parameters are
ignored, so the same URL works with and without IPv6. If your router can't send basic
auth, append `&username=<username>&password=<pass>` to the URL.

### ddclient

```ini
protocol=dyndns2
use=web
ssl=yes
server=dyndns.example.com
login=router
password='a-long-random-password'
home.example.com
```

### Anything else

Any client with a "custom" or "dyndns2" provider works. Send a `GET` request like:

```text
https://router:password@dyndns.example.com/nic/update?hostname=home.example.com&myip=203.0.113.7
```

## Protocol

### Request parameters

| Parameter | Description |
| --- | --- |
| `hostname` | Hostname(s) to update, comma-separated (max. 20). Required. |
| `myip` | IPv4 and/or IPv6 address, comma-separated (e.g. `203.0.113.7,2001:db8::7`). |
| `myipv4` | IPv4 address. |
| `myipv6` | IPv6 address. |

At most one IPv4 and one IPv6 address may be given; empty values are ignored. Without
any address the address of the client making the request is used. Other dyndns2
parameters (`system`, `wildcard`, `mx`, `backmx`, `offline`) are accepted and ignored.

### What happens to the records

For each hostname the bridge looks for the most specific zone in the Hetzner project
that contains it. `home.example.com` in zone `example.com` becomes the RRSet `home`, and
`example.com` itself becomes `@`. For each address family that was sent:

* if there is no `A` / `AAAA` RRSet yet, it is created with `RECORD_TTL`;
* if it already contains exactly that address, nothing is changed (`nochg`);
* otherwise its records are replaced by the new address. The TTL and the comment of the
  first record are kept.

Record types that were not sent are left alone; sending only an IPv4 address never
removes an existing `AAAA` record.

### Responses

The response is plain text with one line per hostname, in request order, as defined by
the dyndns2 protocol. The HTTP status is `200`, except for `badauth`, which is sent
with `401` and a `WWW-Authenticate` challenge.

| Response | Meaning |
| --- | --- |
| `good <ips>` | The records were updated or created. |
| `nochg <ips>` | The records already had these addresses. |
| `badauth` | Wrong username/password, or (passthrough mode) the token was rejected. |
| `notfqdn` | Missing or invalid hostname. |
| `nohost` | No zone for this hostname in the project, or not in `ALLOWED_HOSTNAMES`. |
| `numhost` | More than 20 hostnames in one request. |
| `badagent` | Invalid IP address, or two different addresses of the same family. |
| `dnserr` | The Hetzner API refused the change (e.g. the RRSet is protected). |
| `911` | The Hetzner API is unreachable, rate-limited or rejected the configured token. Try again later and check the logs. |

## Reverse proxy and HTTPS

The bridge speaks plain HTTP. Put it behind a reverse proxy that terminates TLS (Caddy,
nginx, Traefik, …) whenever requests travel over the internet: they contain the
password, and in passthrough mode the Hetzner API token.

If clients omit `myip` and rely on the bridge to use their address, the bridge must know
the real client address. Set `FORWARDED_ALLOW_IPS` to the address of your proxy so its
`X-Forwarded-For` header is trusted. Inside Docker networks that is usually the proxy
container's address; `*` trusts every peer and is only safe if the bridge can't be
reached except through the proxy.

## Security notes

* Hetzner Cloud API tokens are scoped to a whole project, not to a DNS zone. A token
  can also manage servers, volumes and everything else in that project. Consider
  moving your DNS zones into a project of their own and creating the token there.
* Prefer managed mode and restrict it with `ALLOWED_HOSTNAMES`. Then a leaked router
  password can only change those records, not `www` or `MX`.
* Credentials passed in the query string end up in proxy access logs. Use basic auth
  where possible. The bridge itself never logs credentials.

## Migrating from hetzner-dyndns

The bridge listens on the same port (`8245`) and path (`/nic/update`), and in
passthrough mode accepts the same client configuration (username = zone, password =
token). The username is simply ignored, because the zone is found automatically. The
differences:

* `ZONE` and `TOKEN` are not supported. To restrict who can update what, use managed
  mode with `DYNDNS_USERNAME`, `DYNDNS_PASSWORD` and `ALLOWED_HOSTNAMES`.
* Errors use HTTP status `200` with the dyndns2 code as the body (as the protocol
  specifies). Only `badauth` uses `401`.
* An RRSet containing several records is replaced by the single new address instead of
  updating only its first record.
* Multiple hostnames per request, the zone apex and `myipv6` are supported.

## Development

The project uses [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

The tests run against an in-memory fake of the Hetzner API (`tests/fake_hetzner.py`).
No token or network access is needed.
