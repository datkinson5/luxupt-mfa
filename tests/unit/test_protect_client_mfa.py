"""Protect sign-in with a Ubiquiti account protected by an authenticator app (TOTP).

Consoles in a UniFi Fabric cannot have local users, so the private API (recording
snapshots, video export) is reachable only as a Ubiquiti account, and those have
MFA. The client sends the current TOTP code in the login's `token` field.
"""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import httpx
import pytest

from app.clients import protect_client as pc_module
from app.clients.protect_client import (
    ProtectAuthError,
    ProtectClient,
    ProtectMfaRequiredError,
)
from app.utils.totp import TotpSecretError

HOST = "https://console.example.test"
SECRET = "JBSWY3DPEHPK3PXP"
MFA_REQUIRED = {"code": "MFA_AUTH_REQUIRED", "message": "MFA required"}


@pytest.fixture
def codes(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Deterministic TOTP codes ("000001", "000002", ...) and a recorded, instant sleep."""
    counter = iter(range(1, 1000))
    monkeypatch.setattr(pc_module, "totp", lambda _key: f"{next(counter):06d}")
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(pc_module.asyncio, "sleep", fake_sleep)
    yield sleeps


def _client(
    handler: Callable[[httpx.Request], httpx.Response], *, totp_secret: str = ""
) -> tuple[ProtectClient, list[dict]]:
    """A ProtectClient on a mock transport, plus the login bodies it sent."""
    logins: list[dict] = []

    def recording(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            logins.append(json.loads(request.content))
        return handler(request)

    client = ProtectClient(
        f"{HOST}/proxy/protect/integration/v1",
        "svc@example.test",
        "pw",
        totp_secret=totp_secret,
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    return client, logins


def _ok(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/auth/login":
        return httpx.Response(200, json={"username": "svc"})
    return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})


async def test_local_user_sends_no_token(codes: list[float]) -> None:
    client, logins = _client(_ok)
    await client._login()
    assert logins == [{"username": "svc@example.test", "password": "pw", "rememberMe": True}]
    await client.close()


async def test_totp_code_goes_in_the_first_login(codes: list[float]) -> None:
    """Sending the code up front means the console never emails or pushes a prompt."""
    client, logins = _client(_ok, totp_secret=SECRET)
    await client._login()
    assert len(logins) == 1
    assert logins[0]["token"] == "000001"
    assert codes == []
    await client.close()


async def test_mfa_challenge_without_secret_says_what_to_set(codes: list[float]) -> None:
    client, logins = _client(lambda _: httpx.Response(499, json=MFA_REQUIRED))
    with pytest.raises(ProtectMfaRequiredError, match="UNIFI_PROTECT_TOTP_SECRET"):
        await client._login()
    assert len(logins) == 1
    await client.close()


async def test_challenge_is_retried_once_with_the_next_code(codes: list[float]) -> None:
    replies = iter([httpx.Response(499, json=MFA_REQUIRED), httpx.Response(200, json={})])
    client, logins = _client(lambda _: next(replies), totp_secret=SECRET)
    await client._login()
    assert [body["token"] for body in logins] == ["000001", "000002"]
    assert len(codes) == 1 and 1 < codes[0] <= 31  # waited for the next 30 s step
    await client.close()


async def test_code_rejected_twice_stops(codes: list[float]) -> None:
    client, logins = _client(lambda _: httpx.Response(499, json=MFA_REQUIRED), totp_secret=SECRET)
    with pytest.raises(ProtectMfaRequiredError, match="rejected the TOTP code"):
        await client._login()
    assert len(logins) == 2
    await client.close()


async def test_mfa_named_in_a_401_body_is_a_challenge(codes: list[float]) -> None:
    client, _ = _client(
        lambda _: httpx.Response(401, json={"error": "2FA token required"})
    )
    with pytest.raises(ProtectMfaRequiredError):
        await client._login()
    await client.close()


async def test_wrong_password_is_not_mistaken_for_mfa(codes: list[float]) -> None:
    client, logins = _client(
        lambda _: httpx.Response(401, json={"message": "Invalid username or password"}),
        totp_secret=SECRET,
    )
    with pytest.raises(ProtectAuthError) as raised:
        await client._login()
    assert not isinstance(raised.value, ProtectMfaRequiredError)
    assert len(logins) == 1 and codes == []
    await client.close()


async def test_expired_session_logs_in_again_with_a_fresh_code(codes: list[float]) -> None:
    snapshot_replies = iter([httpx.Response(401), None, None])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/auth/login":
            reply = next(snapshot_replies)
            if reply is not None:
                return reply
        return _ok(request)

    client, logins = _client(handler, totp_secret=SECRET)
    jpg = await client.historical_snapshot("cam1", datetime(2026, 10, 1, tzinfo=UTC))
    assert jpg.startswith(b"\xff\xd8")
    assert [body["token"] for body in logins] == ["000001", "000002"]
    await client.close()


def test_bad_secret_fails_at_construction() -> None:
    with pytest.raises(TotpSecretError):
        ProtectClient(HOST, "u", "p", totp_secret="not base32!")
