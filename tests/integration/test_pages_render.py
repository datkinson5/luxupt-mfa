"""Page-render smoke tests — the coverage that was missing when the fastapi/starlette
bump silently broke every `TemplateResponse` call.

Each page must render (HTTP 200, HTML) rather than 500. A regression in the
template signature, a bad context, or a broken template surfaces here as a 500.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.user_crud import user_crud


@pytest_asyncio.fixture
async def a_user(db: AsyncSession) -> None:
    """An installed instance: one user exists, so /login is the sign-in page, not /setup."""
    await user_crud.create_user(db, username="someone", password="someone-password-1")


class TestPublicPages:
    """Pages reachable without authentication (PUBLIC_PATHS in AuthRedirectMiddleware)."""

    async def test_login_renders(self, client: AsyncClient, a_user: None) -> None:
        resp = await client.get("/login")
        assert resp.status_code == 200, resp.text[:300]
        assert "text/html" in resp.headers["content-type"]

    async def test_health_responds(self, client: AsyncClient) -> None:
        # 200 healthy or 503 degraded — both mean the endpoint answered (services
        # aren't started under ASGITransport, which skips the lifespan). Not a 500.
        resp = await client.get("/health")
        assert resp.status_code in (200, 503)


class TestAuthRedirects:
    """Unauthenticated page requests redirect to login, never 500."""

    @pytest.mark.parametrize("path", ["/cameras", "/timelapses", "/images", "/system"])
    async def test_protected_page_redirects_when_anonymous(
        self, client: AsyncClient, a_user: None, path: str
    ) -> None:
        resp = await client.get(path, follow_redirects=False)
        assert resp.status_code in (302, 303), f"{path}: {resp.status_code}"
        assert "/login" in resp.headers.get("location", "")

    @pytest.mark.parametrize("path", ["/login", "/cameras"])
    async def test_no_users_redirects_to_setup(
        self, client: AsyncClient, path: str
    ) -> None:
        # A fresh install has nobody to sign in as: the way in is /setup.
        resp = await client.get(path, follow_redirects=False)
        assert resp.status_code in (302, 303), f"{path}: {resp.status_code}"
        assert resp.headers.get("location", "") == "/setup"


class TestAuthenticatedPages:
    """The real render coverage: authenticated pages must return 200 HTML."""

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/cameras",
            "/timelapses",
            "/timelapses/jobs",  # jobs router (domain split)
            "/timelapses/scheduler",  # scheduler router (domain split)
            "/images",
            "/system",
        ],
    )
    async def test_page_renders_for_authed_user(
        self, auth_client: AsyncClient, path: str
    ) -> None:
        resp = await auth_client.get(path, follow_redirects=True)
        assert resp.status_code == 200, (
            f"{path}: {resp.status_code} — {resp.text[:300]}"
        )
        assert "text/html" in resp.headers["content-type"]

    async def test_unknown_path_is_handled(self, auth_client: AsyncClient) -> None:
        # An unmatched route is FastAPI's default 404 (JSON) — not the custom
        # pages/errors/404.html handler, which only fires on an in-app raise HTTPException(404).
        # The value here is that an unknown path is handled gracefully, never a 500.
        resp = await auth_client.get("/no/such/route", follow_redirects=True)
        assert resp.status_code == 404
