# syntax=docker/dockerfile:1

FROM python:3.13-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /src
# Install dependencies first so they are cached independently of source changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable


FROM python:3.13-slim

LABEL org.opencontainers.image.title="dyndns-hetzner-bridge" \
      org.opencontainers.image.description="Bridge that translates dyndns2 update requests into Hetzner Cloud DNS API calls" \
      org.opencontainers.image.source="https://github.com/ulope/dyndns-hetzner-bridge"

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER 65534:65534
EXPOSE 8245
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
    CMD ["python", "-m", "dyndns_hetzner_bridge.healthcheck"]
ENTRYPOINT ["dyndns-hetzner-bridge"]
