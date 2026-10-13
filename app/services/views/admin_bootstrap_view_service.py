# luxarch:admin-bootstrap-view asset v2 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit admin-bootstrap-view`.
"""The web seam of the admin bootstrap. `luxarch --emit admin-bootstrap-view`.

Drop this in at `app/services/views/admin_bootstrap_view_service.py`. It turns the `/setup` form into
a call on the core service (`luxarch --emit admin-bootstrap`) and the core's refusals into what the
page shows. It also answers the two questions a login handler asks: where does the login page go
while there are no users, and where does a user who just signed in go.

The standard is `luxarch --doc FLEET-ADMIN-BOOTSTRAP-STANDARD`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

# isort: split
# --- EDIT THIS: the core service (fleet home shown).
from app.services.core.admin_bootstrap_core_service import (
    LOGIN_KIND,
    AdminBootstrapCoreService,
    PasswordPolicyError,
    SetupClosedError,
    SetupTokenError,
)

# --- EDIT THIS: where your app keeps these pages.
SETUP_PATH = "/setup"
LOGIN_PATH = "/login?setup_complete=1"
CHANGE_PASSWORD_PATH = "/account/password"


@dataclass(frozen=True)
class SetupForm:
    token: str
    login: str  # v2: what the user signs in with, an email or a username per LOGIN_KIND
    password: str
    password_confirm: str


@dataclass(frozen=True)
class SetupOutcome:
    """What the router renders: a redirect, the form again with an error, or 404 (closed)."""

    closed: bool = False
    redirect: str = ""
    status_code: int = 200
    context: dict[str, str] = field(default_factory=dict)


class AdminBootstrapViewService:
    def __init__(self) -> None:
        self.core = AdminBootstrapCoreService()

    async def setup_page(self, db: AsyncSession) -> dict[str, str] | None:
        """The empty form's context, or None when `/setup` no longer exists."""
        if not await self.core.setup_open(db):
            return None
        return {"login": "", "login_kind": LOGIN_KIND, "error": ""}

    async def submit_setup(self, db: AsyncSession, form: SetupForm) -> SetupOutcome:
        login = form.login.strip()
        if form.password != form.password_confirm:
            return self._again(login, "The passwords do not match.", 400)
        try:
            await self.core.create_first_admin(db, form.token, login, form.password)
        except SetupClosedError:
            return SetupOutcome(closed=True)
        except SetupTokenError:
            return self._again(
                login, "That is not the setup token printed in the server log.", 403
            )
        except PasswordPolicyError as exc:
            return self._again(login, f"Not accepted: {exc}.", 400)
        return SetupOutcome(redirect=LOGIN_PATH)

    async def login_page_redirect(self, db: AsyncSession) -> str:
        """Where the login page sends a visitor instead of rendering: `/setup` while there are no
        users to sign in as, otherwise "" (render the login page)."""
        return SETUP_PATH if await self.core.setup_open(db) else ""

    async def login_destination(
        self, db: AsyncSession, login: str, next_path: str
    ) -> str:
        """Where a user goes straight after signing in. A seeded or reset account goes to the change
        form first: `must_change_password` is enforced AT LOGIN, before any other page."""
        if await self.core.needs_password_change(db, login):
            return CHANGE_PASSWORD_PATH
        return next_path

    @staticmethod
    def _again(login: str, error: str, status_code: int) -> SetupOutcome:
        return SetupOutcome(
            status_code=status_code,
            context={"login": login, "login_kind": LOGIN_KIND, "error": error},
        )
