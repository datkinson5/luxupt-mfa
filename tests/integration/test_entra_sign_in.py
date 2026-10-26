"""Sign in with Microsoft Entra ID, end to end through the real routes (fork feature).

Microsoft's token endpoint is an httpx.MockTransport; everything else is the app: the login
redirect, the flow cookie, state/nonce/PKCE checks, user creation, the session cookie, and the
read-only role the session then carries.
"""

from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import entra_client
from app.core import config
from app.models.user_model import User
from app.web.templates import templates

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ADMIN_GROUP = "admin-group-oid"
READ_GROUP = "read-group-oid"


class FakeMicrosoft:
    """The tenant's token endpoint: checks the code exchange, returns an ID token."""

    def __init__(self) -> None:
        self.groups: list[str] = [READ_GROUP]
        self.oid = "oid-jo"
        self.upn = "jo.bloggs@example.com"
        self.nonce = ""
        self.challenge = ""
        self.exchanges: list[dict[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/{TENANT}/oauth2/v2.0/token"
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.exchanges.append(form)
        verifier_hash = base64.urlsafe_b64encode(
            hashlib.sha256(form["code_verifier"].encode()).digest()
        ).rstrip(b"=").decode()
        if form["code"] != "good-code" or verifier_hash != self.challenge:
            return httpx.Response(400, json={"error": "invalid_grant"})
        assert form["client_secret"] == "secret"
        claims = {
            "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
            "tid": TENANT,
            "aud": CLIENT,
            "exp": int(time.time()) + 3600,
            "nonce": self.nonce,
            "oid": self.oid,
            "preferred_username": self.upn,
            "name": "Jo Bloggs",
            "groups": self.groups,
        }
        token = jwt.encode(claims, "unused-signing-key-of-sufficient-length", algorithm="HS256")
        return httpx.Response(200, json={"id_token": token, "token_type": "Bearer"})


@pytest.fixture
def microsoft(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMicrosoft]:
    monkeypatch.setattr(config, "ENTRA_TENANT_ID", TENANT)
    monkeypatch.setattr(config, "ENTRA_CLIENT_ID", CLIENT)
    monkeypatch.setattr(config, "ENTRA_CLIENT_SECRET", "secret")
    monkeypatch.setattr(config, "ENTRA_REDIRECT_URI", "https://test/auth/entra/callback")
    monkeypatch.setattr(config, "ENTRA_ADMIN_GROUP", ADMIN_GROUP)
    monkeypatch.setattr(config, "ENTRA_READ_GROUP", READ_GROUP)
    monkeypatch.setattr(config, "ENTRA_ENABLED", True)
    monkeypatch.setitem(templates.env.globals, "entra_enabled", True)
    fake = FakeMicrosoft()
    monkeypatch.setattr(entra_client, "transport", httpx.MockTransport(fake.handler))
    yield fake


async def _sign_in(client: AsyncClient, ms: FakeMicrosoft, code: str = "good-code") -> httpx.Response:
    start = await client.get("/auth/entra/login")
    assert start.status_code == 302, start.text[:300]
    query = {k: v[0] for k, v in parse_qs(urlparse(start.headers["location"]).query).items()}
    ms.nonce, ms.challenge = query["nonce"], query["code_challenge"]
    return await client.get(
        "/auth/entra/callback", params={"code": code, "state": query["state"]}
    )


async def _user(db: AsyncSession, oid: str) -> User | None:
    db.expire_all()
    return (await db.execute(select(User).where(User.entra_oid == oid))).scalar_one_or_none()


async def test_login_page_offers_microsoft(client: AsyncClient, microsoft: FakeMicrosoft, db: AsyncSession) -> None:
    from app.crud.user_crud import user_crud

    await user_crud.create_user(db, username="admin", password="a-long-password-123")
    page = await client.get("/login")
    assert "Sign in with Microsoft" in page.text
    assert "/auth/entra/login" in page.text


async def test_login_page_has_no_microsoft_button_when_not_configured(
    client: AsyncClient, db: AsyncSession
) -> None:
    from app.crud.user_crud import user_crud

    await user_crud.create_user(db, username="admin", password="a-long-password-123")
    page = await client.get("/login")
    assert "Sign in with Microsoft" not in page.text
    assert (await client.get("/auth/entra/login")).status_code == 404


async def test_start_redirects_to_the_tenant(client: AsyncClient, microsoft: FakeMicrosoft) -> None:
    resp = await client.get("/auth/entra/login")
    assert resp.status_code == 302
    url = urlparse(resp.headers["location"])
    assert url.netloc == "login.microsoftonline.com"
    assert url.path == f"/{TENANT}/oauth2/v2.0/authorize"
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "path=/auth/entra" in cookie


async def test_read_group_member_is_created_read_only(
    client: AsyncClient, microsoft: FakeMicrosoft, db: AsyncSession
) -> None:
    resp = await _sign_in(client, microsoft)
    assert resp.status_code == 302 and resp.headers["location"] == "/cameras"
    user = await _user(db, "oid-jo")
    assert user is not None
    assert user.username == "jo.bloggs@example.com"
    assert not user.is_admin
    assert user.last_login_at is not None

    cameras = await client.get("/cameras")
    assert cameras.status_code == 200
    assert "Capture Settings" not in cameras.text
    assert 'href="/system"' not in cameras.text

    assert (await client.post("/cameras/failures/clear")).status_code == 403
    assert (await client.get("/cameras/fetch-settings")).status_code == 403
    assert (await client.get("/system")).status_code == 403
    assert (await client.post("/timelapses/create", data={})).status_code == 403
    assert (await client.get("/timelapses")).status_code == 200
    assert (await client.get("/images")).status_code == 200


async def test_admin_group_member_is_created_admin(
    client: AsyncClient, microsoft: FakeMicrosoft, db: AsyncSession
) -> None:
    microsoft.groups = [ADMIN_GROUP]
    assert (await _sign_in(client, microsoft)).status_code == 302
    user = await _user(db, "oid-jo")
    assert user is not None and user.is_admin
    cameras = await client.get("/cameras")
    assert "Capture Settings" in cameras.text
    assert (await client.get("/cameras/fetch-settings")).status_code == 200


async def test_someone_in_neither_group_is_refused_and_not_created(
    client: AsyncClient, microsoft: FakeMicrosoft, db: AsyncSession
) -> None:
    microsoft.groups = ["some-other-group"]
    resp = await _sign_in(client, microsoft)
    assert resp.status_code == 403
    assert "has not been given access" in resp.text
    assert await _user(db, "oid-jo") is None
    assert (await client.get("/cameras")).status_code == 302


async def test_group_change_applies_at_next_sign_in(
    client: AsyncClient, microsoft: FakeMicrosoft, db: AsyncSession
) -> None:
    microsoft.groups = [ADMIN_GROUP]
    await _sign_in(client, microsoft)
    microsoft.groups = [READ_GROUP]
    await _sign_in(client, microsoft)
    user = await _user(db, "oid-jo")
    assert user is not None and not user.is_admin
    assert (await client.get("/cameras/fetch-settings")).status_code == 403


async def test_removal_from_both_groups_ends_the_session(
    client: AsyncClient, microsoft: FakeMicrosoft, db: AsyncSession
) -> None:
    await _sign_in(client, microsoft)
    assert (await client.get("/cameras")).status_code == 200
    old_cookies = dict(client.cookies)

    microsoft.groups = []
    assert (await _sign_in(client, microsoft)).status_code == 403

    client.cookies.clear()
    for name, value in old_cookies.items():
        client.cookies.set(name, value)
    assert (await client.get("/cameras")).status_code == 302


async def test_upn_change_keeps_the_same_user(
    client: AsyncClient, microsoft: FakeMicrosoft, db: AsyncSession
) -> None:
    await _sign_in(client, microsoft)
    microsoft.upn = "jo.smith@example.com"
    assert (await _sign_in(client, microsoft)).status_code == 302
    user = await _user(db, "oid-jo")
    assert user is not None and user.username == "jo.smith@example.com"
    assert (await client.get("/cameras")).status_code == 200


async def test_mismatched_state_is_refused(
    client: AsyncClient, microsoft: FakeMicrosoft, db: AsyncSession
) -> None:
    await client.get("/auth/entra/login")
    resp = await client.get("/auth/entra/callback", params={"code": "good-code", "state": "forged"})
    assert resp.status_code == 400
    assert microsoft.exchanges == []


async def test_callback_without_starting_is_refused(
    client: AsyncClient, microsoft: FakeMicrosoft
) -> None:
    resp = await client.get("/auth/entra/callback", params={"code": "good-code", "state": "x"})
    assert resp.status_code == 400
    assert microsoft.exchanges == []


async def test_bad_code_is_refused(
    client: AsyncClient, microsoft: FakeMicrosoft, db: AsyncSession
) -> None:
    resp = await _sign_in(client, microsoft, code="stolen-code")
    assert resp.status_code == 403
    assert "refused the sign-in" in resp.text
    assert await _user(db, "oid-jo") is None


async def test_microsoft_error_is_shown(client: AsyncClient, microsoft: FakeMicrosoft) -> None:
    resp = await client.get(
        "/auth/entra/callback",
        params={"error": "access_denied", "error_description": "User cancelled"},
    )
    assert resp.status_code == 403
    assert "User cancelled" in resp.text


async def test_microsoft_user_has_no_password_login(
    client: AsyncClient, microsoft: FakeMicrosoft, db: AsyncSession
) -> None:
    await _sign_in(client, microsoft)
    client.cookies.clear()
    resp = await client.post(
        "/login", data={"username": "jo.bloggs@example.com", "password": "ENTRA_SSO"}
    )
    assert resp.status_code == 400


async def test_an_existing_local_account_with_the_upn_is_linked(
    client: AsyncClient, microsoft: FakeMicrosoft, db: AsyncSession
) -> None:
    from app.crud.user_crud import user_crud

    local = await user_crud.create_user(
        db, username="jo.bloggs@example.com", password="a-long-password-123", is_admin=True
    )
    await _sign_in(client, microsoft)
    db.expire_all()
    linked = await _user(db, "oid-jo")
    assert linked is not None and linked.id == local.id
    assert not linked.is_admin  # the group decides


async def test_a_local_reader_is_read_only_too(client: AsyncClient, db: AsyncSession) -> None:
    from app.crud.user_crud import user_crud

    await user_crud.create_user(db, username="admin", password="a-long-password-123")
    await user_crud.create_user(
        db, username="viewer", password="a-long-password-123", is_admin=False
    )
    resp = await client.post("/login", data={"username": "viewer", "password": "a-long-password-123"})
    assert resp.status_code in (302, 303)
    assert (await client.get("/cameras")).status_code == 200
    refused = await client.post("/cameras/failures/clear")
    assert refused.status_code == 403
    assert "read-only" in refused.text


def test_flow_cookie_cannot_be_a_session() -> None:
    from app.web.auth import SECRET_KEY, AuthService

    token = jwt.encode(
        {"typ": "entra_flow", "state": "s", "nonce": "n", "verifier": "v", "exp": int(time.time()) + 60},
        SECRET_KEY,
        algorithm="HS256",
    )
    assert AuthService.decode_session(token) is None
