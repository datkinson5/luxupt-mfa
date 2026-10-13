"""A request whose Host header is not one of ALLOWED_HOSTS is refused before any route runs.

nginx forwards the client's Host unchanged (`proxy_set_header Host $host`), so without this the
app answers any name pointed at it: links and redirects built from the request carry a host an
attacker chose. `make smoke` probes the same thing on the deployed stack.
"""

import logging

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from starlette.responses import PlainTextResponse
from starlette.types import Receive, Scope, Send

from app.core.config import HostSettings
from app.core.trusted_host import (
    AllowedHostsError,
    AllowedHostsMiddleware,
    parse_allowed_hosts,
)


async def test_forged_host_is_refused(client: AsyncClient) -> None:
    resp = await client.get("/health/live", headers={"Host": "smoke-forged.invalid"})
    assert resp.status_code == 400


async def test_allowed_host_is_served(client: AsyncClient) -> None:
    resp = await client.get("/health/live")
    assert resp.status_code == 200


async def test_forged_host_refusal_is_logged(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    # Every other 4xx is logged by RequestLoggingMiddleware; a refusal must not be the silent one.
    with caplog.at_level(logging.INFO, logger="app.web.middleware"):
        await client.get("/health/live", headers={"Host": "smoke-forged.invalid"})
    assert any(getattr(r, "status_code", None) == 400 for r in caplog.records), (
        "forged-Host refusal left no log line"
    )


@pytest.mark.parametrize(
    "raw", [None, "", " , ", "*", "app.example.com,*", "app.example.com:11001"]
)
def test_unusable_allowed_hosts_refuses_to_start(raw: str | None) -> None:
    # No default to fall back on: an environment nobody configured fails at its first deploy.
    with pytest.raises(AllowedHostsError):
        parse_allowed_hosts(raw)


def test_allowed_hosts_are_lowercased_and_trimmed() -> None:
    assert parse_allowed_hosts(" App.Example.com ,b.example.com") == [
        "app.example.com",
        "b.example.com",
    ]


def _probe_app() -> AllowedHostsMiddleware:
    async def ok(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    return AllowedHostsMiddleware(
        ok, allowed_hosts=["app.example.com"], health_paths=("/health/live",)
    )


async def _get(path: str, headers: dict[str, str], peer: str = "127.0.0.1") -> int:
    transport = ASGITransport(app=_probe_app(), client=(peer, 5000))
    async with AsyncClient(transport=transport, base_url="http://x") as c:
        return (await c.get(path, headers=headers)).status_code


async def test_container_healthcheck_passes_without_localhost_listed() -> None:
    assert await _get("/health/live", {"Host": "localhost:8080"}) == 200


@pytest.mark.parametrize(
    ("path", "headers", "peer"),
    [
        # localhost on any other route is a forged Host like any other
        ("/", {"Host": "localhost"}, "127.0.0.1"),
        # a request a proxy relayed never gets the probe exemption
        (
            "/health/live",
            {"Host": "localhost", "X-Forwarded-For": "127.0.0.1"},
            "127.0.0.1",
        ),
        # nor does one from off the box
        ("/health/live", {"Host": "localhost"}, "10.0.0.5"),
    ],
)
async def test_localhost_is_refused_outside_the_probe(
    path: str, headers: dict[str, str], peer: str
) -> None:
    assert await _get(path, headers, peer) == 400


def test_settings_refuse_to_load_without_allowed_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
    with pytest.raises(ValidationError, match="ALLOWED_HOSTS"):
        HostSettings()
