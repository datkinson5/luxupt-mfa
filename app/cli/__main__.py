# luxarch:admin-bootstrap-cli asset v2 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit admin-bootstrap-cli`.
"""The two non-request owners of the admin bootstrap: the startup hook and the recovery CLI.

`luxarch --emit admin-bootstrap-cli`. Drop this in at `app/cli.py`. The standard is
`luxarch --doc FLEET-ADMIN-BOOTSTRAP-STANDARD`.

    docker compose exec app python -m app.cli reset-password <login>

prints a random temporary password ONCE and sets `must_change_password`, so the owner signs in with
it and is sent straight to the change form. Being able to run a command inside the container is the
proof of ownership; no email is involved, because a self-hosted box often has no mail at all.

`bootstrap_on_startup()` is what the app's lifespan awaits. It is a separate transaction owner (it
runs before any request), like the CLI, so list `app/cli.py` in `[allowlist].non_request_files`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

# --- EDIT THIS: your non-request session owner and the core service (fleet homes shown).
from app.db.database import get_db_context
from app.services.core.admin_bootstrap_core_service import (
    AdminBootstrapCoreService,
    UnknownUserError,
)


async def bootstrap_on_startup() -> None:
    """Seed the first admin or open `/setup`. Await it from the app's lifespan."""
    async with get_db_context() as db:
        await AdminBootstrapCoreService().bootstrap(db)


async def _reset_password(login: str) -> int:
    try:
        async with get_db_context() as db:
            temporary = await AdminBootstrapCoreService().reset_password(db, login)
    except UnknownUserError:
        print(f"no user signs in as {login!r}; nothing changed", file=sys.stderr)
        return 1
    # Printed only after the transaction committed: a password shown for a change that rolled back
    # would lock the owner out a second time.
    print(f"Temporary password for {login}: {temporary}")
    print("It is shown once. They must change it when they next sign in.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    reset = commands.add_parser(
        "reset-password",
        help="give a user a one-time temporary password they must change at next sign-in",
    )
    reset.add_argument(
        "login",
        help="what the user signs in with (their email or username, per LOGIN_KIND)",
    )
    args = parser.parse_args(argv)
    login: str = args.login
    return asyncio.run(_reset_password(login))


if __name__ == "__main__":
    raise SystemExit(main())
