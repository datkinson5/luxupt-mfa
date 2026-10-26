"""Microsoft Entra ID sign-in: OpenID Connect authorization code flow with PKCE (fork feature).

The browser is sent to Microsoft to sign in, comes back to ``/auth/entra/callback`` with a code,
and the server swaps the code for an ID token at the tenant's token endpoint, authenticating with
the client secret and the PKCE verifier.

The ID token's signature is not checked against Microsoft's signing keys. It is received directly
from the token endpoint over TLS (certificate verified), in exchange for a single-use code and the
client secret, which OpenID Connect Core 1.0 §3.1.3.7 allows in place of checking the signature.
Its claims are still validated: issuer, tenant, audience, expiry and the nonce bound to this
browser's sign-in.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from app.core import config

AUTHORITY = "https://login.microsoftonline.com"
SCOPES = "openid profile email"
CLOCK_SKEW_SECONDS = 120

# Tests swap this for an httpx.MockTransport; production uses the default network transport.
transport: httpx.AsyncBaseTransport | None = None


class EntraSignInError(Exception):
    """Sign-in did not complete. The message is safe to show the user."""


@dataclass(frozen=True)
class SignInRequest:
    """What one sign-in attempt needs to remember between leaving for Microsoft and returning."""

    state: str
    nonce: str
    code_verifier: str
    authorize_url: str


@dataclass(frozen=True)
class EntraIdentity:
    """The signed-in person, from validated ID token claims."""

    object_id: str
    username: str
    display_name: str
    memberships: frozenset[str] = field(default_factory=frozenset)
    groups_overflowed: bool = False


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def begin_sign_in() -> SignInRequest:
    """A fresh state, nonce and PKCE pair, and the Microsoft URL to send the browser to."""
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    params = {
        "client_id": config.ENTRA_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": config.ENTRA_REDIRECT_URI,
        "response_mode": "query",
        "scope": SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    url = f"{AUTHORITY}/{config.ENTRA_TENANT_ID}/oauth2/v2.0/authorize?{urlencode(params)}"
    return SignInRequest(state=state, nonce=nonce, code_verifier=verifier, authorize_url=url)


async def complete_sign_in(code: str, code_verifier: str, nonce: str) -> EntraIdentity:
    """Swap the authorization code for an ID token and return who signed in."""
    token_url = f"{AUTHORITY}/{config.ENTRA_TENANT_ID}/oauth2/v2.0/token"
    form = {
        "client_id": config.ENTRA_CLIENT_ID,
        "client_secret": config.ENTRA_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.ENTRA_REDIRECT_URI,
        "code_verifier": code_verifier,
        "scope": SCOPES,
    }
    try:
        async with httpx.AsyncClient(transport=transport, timeout=20.0, verify=True) as client:
            resp = await client.post(token_url, data=form, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise EntraSignInError(
            f"Could not reach Microsoft to finish signing in ({type(exc).__name__})."
        ) from exc

    try:
        body: dict[str, Any] = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200 or "id_token" not in body:
        detail = body.get("error_description") or body.get("error") or f"HTTP {resp.status_code}"
        raise EntraSignInError(f"Microsoft refused the sign-in: {str(detail)[:300]}")

    claims = validate_id_token(str(body["id_token"]), nonce)
    return identity_from_claims(claims)


def validate_id_token(id_token: str, nonce: str, *, now: float | None = None) -> dict[str, Any]:
    """The ID token's claims, after checking they were issued by this tenant, for this app,
    to this sign-in, and have not expired."""
    try:
        claims: dict[str, Any] = jwt.decode(id_token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise EntraSignInError("Microsoft returned an unreadable ID token.") from exc

    tenant = config.ENTRA_TENANT_ID.lower()
    current = time.time() if now is None else now
    if str(claims.get("tid", "")).lower() != tenant:
        raise EntraSignInError("That account belongs to a different Microsoft tenant.")
    if str(claims.get("iss", "")).lower() != f"{AUTHORITY}/{tenant}/v2.0":
        raise EntraSignInError("The ID token was not issued by this tenant.")
    if claims.get("aud") != config.ENTRA_CLIENT_ID:
        raise EntraSignInError("The ID token was issued for a different application.")
    if not isinstance(claims.get("exp"), int | float) or claims["exp"] + CLOCK_SKEW_SECONDS < current:
        raise EntraSignInError("The sign-in has expired. Please try again.")
    if isinstance(claims.get("nbf"), int | float) and claims["nbf"] - CLOCK_SKEW_SECONDS > current:
        raise EntraSignInError("The ID token is not valid yet. Check this server's clock.")
    if not nonce or not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise EntraSignInError("The sign-in could not be matched to this browser. Please try again.")
    if not claims.get("oid"):
        raise EntraSignInError("The ID token does not identify the user.")
    return claims


def identity_from_claims(claims: dict[str, Any]) -> EntraIdentity:
    username = str(claims.get("preferred_username") or claims.get("email") or "").strip().lower()
    if not username:
        raise EntraSignInError("The Microsoft account has no username or email to sign in with.")
    memberships = {str(g) for g in claims.get("groups", []) or []}
    memberships |= {str(r) for r in claims.get("roles", []) or []}
    # Past ~200 groups Microsoft drops the groups claim and points at Graph instead.
    overflowed = "groups" in (claims.get("_claim_names") or {}) or bool(claims.get("hasgroups"))
    return EntraIdentity(
        object_id=str(claims["oid"]),
        username=username,
        display_name=str(claims.get("name") or username),
        memberships=frozenset(memberships),
        groups_overflowed=overflowed,
    )


def role_for(identity: EntraIdentity) -> str | None:
    """"admin", "read", or None when the person is in neither group."""
    member = identity.memberships
    if config.ENTRA_ADMIN_GROUP and config.ENTRA_ADMIN_GROUP in member:
        return "admin"
    if config.ENTRA_READ_GROUP and config.ENTRA_READ_GROUP in member:
        return "read"
    return None
