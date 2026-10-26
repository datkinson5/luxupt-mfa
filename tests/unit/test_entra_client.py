"""Microsoft Entra ID token validation, PKCE and group-to-role mapping (fork feature)."""

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import jwt
import pytest

from app.clients import entra_client
from app.clients.entra_client import EntraSignInError
from app.core import config

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ADMIN_GROUP = "admin-group-oid"
READ_GROUP = "read-group-oid"
NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def _entra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ENTRA_TENANT_ID", TENANT)
    monkeypatch.setattr(config, "ENTRA_CLIENT_ID", CLIENT)
    monkeypatch.setattr(config, "ENTRA_CLIENT_SECRET", "secret")
    monkeypatch.setattr(config, "ENTRA_REDIRECT_URI", "https://nas.test:8443/auth/entra/callback")
    monkeypatch.setattr(config, "ENTRA_ADMIN_GROUP", ADMIN_GROUP)
    monkeypatch.setattr(config, "ENTRA_READ_GROUP", READ_GROUP)


def _claims(**overrides: object) -> dict:
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "tid": TENANT,
        "aud": CLIENT,
        "exp": NOW + 3600,
        "nbf": NOW - 60,
        "nonce": "the-nonce",
        "oid": "user-oid-1",
        "preferred_username": "Jo.Bloggs@Example.com",
        "name": "Jo Bloggs",
        "groups": [READ_GROUP],
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _token(**overrides: object) -> str:
    return jwt.encode(_claims(**overrides), "unused-signing-key-of-sufficient-length", algorithm="HS256")


def test_begin_sign_in_builds_a_pkce_authorize_url() -> None:
    attempt = entra_client.begin_sign_in()
    url = urlparse(attempt.authorize_url)
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "login.microsoftonline.com"
    assert url.path == f"/{TENANT}/oauth2/v2.0/authorize"
    assert query["client_id"] == CLIENT
    assert query["response_type"] == "code"
    assert query["redirect_uri"] == config.ENTRA_REDIRECT_URI
    assert query["state"] == attempt.state and query["nonce"] == attempt.nonce
    assert query["code_challenge_method"] == "S256"
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(attempt.code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    assert query["code_challenge"] == expected
    assert "openid" in query["scope"].split()


def test_each_attempt_is_unique() -> None:
    a, b = entra_client.begin_sign_in(), entra_client.begin_sign_in()
    assert len({a.state, b.state, a.nonce, b.nonce, a.code_verifier, b.code_verifier}) == 6


def test_valid_token_gives_the_identity() -> None:
    claims = entra_client.validate_id_token(_token(), "the-nonce", now=NOW)
    identity = entra_client.identity_from_claims(claims)
    assert identity.object_id == "user-oid-1"
    assert identity.username == "jo.bloggs@example.com"
    assert identity.memberships == {READ_GROUP}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"tid": "99999999-0000-0000-0000-000000000000"}, "different Microsoft tenant"),
        ({"iss": "https://login.microsoftonline.com/other/v2.0"}, "not issued by this tenant"),
        ({"aud": "some-other-app"}, "different application"),
        ({"exp": NOW - 3600}, "expired"),
        ({"nbf": NOW + 3600}, "not valid yet"),
        ({"nonce": "someone-elses-nonce"}, "matched to this browser"),
        ({"oid": None}, "does not identify"),
    ],
)
def test_untrustworthy_tokens_are_refused(overrides: dict, message: str) -> None:
    with pytest.raises(EntraSignInError, match=message):
        entra_client.validate_id_token(_token(**overrides), "the-nonce", now=NOW)


def test_garbage_token_is_refused() -> None:
    with pytest.raises(EntraSignInError, match="unreadable"):
        entra_client.validate_id_token("not-a-jwt", "the-nonce", now=NOW)


def test_clock_skew_is_tolerated() -> None:
    entra_client.validate_id_token(_token(exp=NOW - 60), "the-nonce", now=NOW)


@pytest.mark.parametrize(
    ("claims", "role"),
    [
        ({"groups": [ADMIN_GROUP]}, "admin"),
        ({"groups": [READ_GROUP]}, "read"),
        ({"groups": [READ_GROUP, ADMIN_GROUP]}, "admin"),
        ({"groups": ["unrelated"]}, None),
        ({"groups": None}, None),
        ({"groups": None, "roles": [ADMIN_GROUP]}, "admin"),
        ({"groups": None, "roles": [READ_GROUP]}, "read"),
    ],
)
def test_role_comes_from_group_or_app_role(claims: dict, role: str | None) -> None:
    identity = entra_client.identity_from_claims(_claims(**claims))
    assert entra_client.role_for(identity) == role


def test_unset_group_never_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ENTRA_ADMIN_GROUP", "")
    identity = entra_client.identity_from_claims(_claims(groups=[""]))
    assert entra_client.role_for(identity) is None


def test_group_overage_is_detected() -> None:
    identity = entra_client.identity_from_claims(
        _claims(groups=None, _claim_names={"groups": "src1"})
    )
    assert identity.groups_overflowed


def test_identity_falls_back_to_email() -> None:
    identity = entra_client.identity_from_claims(
        _claims(preferred_username=None, email="Someone@Example.com")
    )
    assert identity.username == "someone@example.com"
