"""Protect sign-in with a Ubiquiti account protected by an authenticator app (TOTP).

Consoles in a UniFi Fabric cannot have local users, so the private API (recording
snapshots, video export) is reachable only as a Ubiquiti account, and those have
MFA. The client sends the current TOTP code in the login's `token` field.
"""

import asyncio
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import httpx
import pytest

from app.clients import protect_client as pc_module
from app.clients.protect_client import (
    AUTH_BACKOFF_SECONDS,
    LOCKOUT_BACKOFF_SECONDS,
    ProtectAuthError,
    ProtectClient,
    ProtectMfaRequiredError,
    _reset_shared_sessions,
)
from app.utils.totp import TotpSecretError

HOST = "https://console.example.test"
SECRET = "JBSWY3DPEHPK3PXP"
MFA_REQUIRED = {"code": "MFA_AUTH_REQUIRED", "message": "MFA required"}


@pytest.fixture
def codes(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Deterministic TOTP codes ("000001", "000002", ...), a recorded instant sleep, and no
    shared sign-in carried over from another test."""
    _reset_shared_sessions()
    counter = iter(range(1, 1000))
    monkeypatch.setattr(pc_module, "totp", lambda _key, **_kw: f"{next(counter):06d}")
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(pc_module.asyncio, "sleep", fake_sleep)
    yield sleeps
    _reset_shared_sessions()


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    totp_secret: str = "",
    password: str = "pw",
    logins: list[dict] | None = None,
) -> tuple[ProtectClient, list[dict]]:
    """A ProtectClient on a mock transport, plus the login bodies it sent (pass `logins`
    to collect several clients' sign-ins in one list)."""
    logins = [] if logins is None else logins

    def recording(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            logins.append(json.loads(request.content))
            reply = handler(request)
            if reply.status_code < 400:
                reply.headers["set-cookie"] = f"TOKEN=session{len(logins)}; Path=/"
            return reply
        return handler(request)

    client = ProtectClient(
        f"{HOST}/proxy/protect/integration/v1",
        "svc@example.test",
        password,
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


async def test_parallel_jobs_sign_in_once(codes: list[float]) -> None:
    """Two historical jobs start together: one sign-in, one code, shared session."""
    logins: list[dict] = []
    first, _ = _client(_ok, totp_secret=SECRET, logins=logins)
    second, _ = _client(_ok, totp_secret=SECRET, logins=logins)
    ts = datetime(2026, 10, 1, tzinfo=UTC)
    await asyncio.gather(
        first.historical_snapshot("cam1", ts), second.historical_snapshot("cam2", ts)
    )
    assert len(logins) == 1
    third, _ = _client(_ok, totp_secret=SECRET, logins=logins)
    await third.start()
    await third.historical_snapshot("cam3", ts)  # adopts the session, no new login
    assert len(logins) == 1
    for client in (first, second, third):
        await client.close()


async def test_a_code_is_never_sent_twice(codes: list[float], monkeypatch: pytest.MonkeyPatch) -> None:
    """A second sign-in inside the same 30 s step waits for the next code."""
    monkeypatch.setattr(pc_module.time, "time", lambda: 1_000_000_005.0)
    snapshot_replies = iter([httpx.Response(401), None])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/auth/login":
            reply = next(snapshot_replies, None)
            if reply is not None:
                return reply
        return _ok(request)

    client, logins = _client(handler, totp_secret=SECRET)
    await client._login()
    assert codes == []
    await client.historical_snapshot("cam1", datetime(2026, 10, 1, tzinfo=UTC))
    assert len(logins) == 2
    assert len(codes) == 1 and 1 < codes[0] <= 31
    await client.close()


async def test_refused_sign_in_backs_off_without_contacting_the_console(
    codes: list[float],
) -> None:
    logins: list[dict] = []
    refused = lambda _: httpx.Response(403, json={"code": "AUTHENTICATION_FAILED"})  # noqa: E731
    first, _ = _client(refused, totp_secret=SECRET, logins=logins)
    with pytest.raises(ProtectAuthError, match="HTTP 403"):
        await first._login()
    second, _ = _client(refused, totp_secret=SECRET, logins=logins)
    await second.start()
    with pytest.raises(ProtectAuthError, match="not signing in again"):
        await second.historical_snapshot("cam1", datetime(2026, 10, 1, tzinfo=UTC))
    assert len(logins) == 1
    remaining = first._shared().failed_until - pc_module.time.monotonic()
    assert AUTH_BACKOFF_SECONDS - 5 < remaining <= AUTH_BACKOFF_SECONDS
    for client in (first, second):
        await client.close()


async def test_lockout_backs_off_longer(codes: list[float]) -> None:
    client, _ = _client(
        lambda _: httpx.Response(429, json={"code": "AUTHENTICATION_FAILED_LIMIT_REACHED"}),
        totp_secret=SECRET,
    )
    with pytest.raises(ProtectAuthError, match="HTTP 429"):
        await client._login()
    remaining = client._shared().failed_until - pc_module.time.monotonic()
    assert AUTH_BACKOFF_SECONDS < remaining <= LOCKOUT_BACKOFF_SECONDS
    await client.close()


async def test_corrected_password_is_tried_straight_away(codes: list[float]) -> None:
    logins: list[dict] = []
    wrong, _ = _client(
        lambda _: httpx.Response(403, json={}), totp_secret=SECRET, password="wrong", logins=logins
    )
    with pytest.raises(ProtectAuthError):
        await wrong._login()
    fixed, _ = _client(_ok, totp_secret=SECRET, password="right", logins=logins)
    await fixed._login()
    assert [body["password"] for body in logins] == ["wrong", "right"]
    for client in (wrong, fixed):
        await client.close()


async def test_held_session_is_reported_stale_not_the_shared_one(codes: list[float]) -> None:
    logins: list[dict] = []
    sessions_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={})
        cookie = request.headers.get("cookie", "")
        sessions_seen.append(cookie)
        return _ok(request) if cookie == f"TOKEN=session{len(logins)}" else httpx.Response(401)

    a, _ = _client(handler, totp_secret=SECRET, logins=logins)
    b, _ = _client(handler, totp_secret=SECRET, logins=logins)
    ts = datetime(2026, 10, 1, tzinfo=UTC)
    await a.historical_snapshot("cam", ts)   # signs in: session1
    await b.start()                          # adopts session1
    # The console expires session1; a notices first and signs in again: session2.
    await a._login(stale=a._held)
    assert len(logins) == 2
    await b.historical_snapshot("cam", ts)   # b holds session1 -> 401 -> adopts session2
    assert len(logins) == 2, "b should adopt the newer session, not sign in again"
    await a.close(); await b.close()
