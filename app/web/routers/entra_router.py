"""Sign in with Microsoft Entra ID (fork feature).

GET /auth/entra/login     sends the browser to Microsoft, remembering this attempt in a cookie
GET /auth/entra/callback  Microsoft sends it back here with a code; sign in and set the session

Both are public paths (the AuthRedirectMiddleware lets them through) and 404 unless Entra sign-in
is configured.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import cast

import jwt
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from app.clients import entra_client
from app.clients.entra_client import EntraSignInError
from app.core import config
from app.services.core.entra_sso_core_service import EntraSsoCoreService
from app.web.auth import ALGORITHM, COOKIE_NAME, SECRET_KEY, AuthService
from app.web.deps import DbSession
from app.web.rate_limit import LOGIN_RATE, client_ip, limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["entra"])

FLOW_COOKIE = f"entra_flow_{config.WEB_PORT}"
FLOW_PATH = "/auth/entra"
FLOW_MINUTES = 10
FLOW_TYPE = "entra_flow"


def _require_enabled() -> None:
    if not config.ENTRA_ENABLED:
        raise HTTPException(status_code=404)


def _login_error(request: Request, message: str, status_code: int = 403) -> Response:
    templates = request.app.state.templates
    response = cast(
        Response,
        templates.TemplateResponse(
            request,
            "pages/login.html",
            {"success": None, "error": message},
            status_code=status_code,
        ),
    )
    response.delete_cookie(FLOW_COOKIE, path=FLOW_PATH)
    return response


@router.get("/auth/entra/login")
@limiter.limit(LOGIN_RATE)
async def entra_login(request: Request) -> Response:
    """Start a Microsoft sign-in."""
    _require_enabled()
    attempt = entra_client.begin_sign_in()
    # Signed with the session key and typed, so it can never stand in for a session cookie.
    flow = jwt.encode(
        {
            "typ": FLOW_TYPE,
            "state": attempt.state,
            "nonce": attempt.nonce,
            "verifier": attempt.code_verifier,
            "exp": datetime.now(UTC) + timedelta(minutes=FLOW_MINUTES),
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    response = RedirectResponse(url=attempt.authorize_url, status_code=302)
    response.set_cookie(
        key=FLOW_COOKIE,
        value=flow,
        max_age=FLOW_MINUTES * 60,
        path=FLOW_PATH,
        httponly=True,
        secure=True,
        # Lax: the cookie must come back on Microsoft's top-level redirect to the callback.
        samesite="lax",
    )
    return response


@router.get("/auth/entra/callback")
@limiter.limit(LOGIN_RATE)
async def entra_callback(request: Request, db: DbSession) -> Response:
    """Finish a Microsoft sign-in and start a LuxUPT session."""
    _require_enabled()
    params = request.query_params
    if params.get("error"):
        detail = params.get("error_description") or params.get("error") or ""
        logger.warning("entra_sign_in_error", extra={"error": params.get("error")})
        return _login_error(request, f"Microsoft sign-in did not complete: {detail[:300]}")

    try:
        flow = jwt.decode(request.cookies.get(FLOW_COOKIE, ""), SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return _login_error(request, "The sign-in took too long or was started elsewhere. Please try again.", 400)
    if flow.get("typ") != FLOW_TYPE or not params.get("state") or params.get("state") != flow.get("state"):
        return _login_error(request, "The sign-in could not be matched to this browser. Please try again.", 400)
    code = params.get("code")
    if not code:
        return _login_error(request, "Microsoft did not return a sign-in code.", 400)

    try:
        identity = await entra_client.complete_sign_in(code, str(flow["verifier"]), str(flow["nonce"]))
        user = await EntraSsoCoreService(db).sign_in(identity)
    except EntraSignInError as exc:
        # Returned, not raised: a refusal still commits (it ends a removed user's sessions).
        return _login_error(request, str(exc))

    user.last_login_at = datetime.now(UTC)
    await db.flush()
    logger.info(
        "Successful login",
        extra={
            "username": user.username,
            "method": "entra",
            "role": "admin" if user.is_admin else "read",
            "client_ip": client_ip(request),
        },
    )
    token = AuthService.create_access_token(
        data={"sub": user.username, "sv": user.session_version},
        expires_delta=timedelta(hours=config.ENTRA_SESSION_HOURS),
    )
    response = RedirectResponse(url="/cameras", status_code=302)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=config.ENTRA_SESSION_HOURS * 3600,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    response.delete_cookie(FLOW_COOKIE, path=FLOW_PATH)
    return response
