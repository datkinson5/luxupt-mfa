"""admin bootstrap: users.must_change_password + session_version; retire auth_source

``luxarch --doc FLEET-ADMIN-BOOTSTRAP-STANDARD``. A seeded or reset password must be changed at the
next sign-in, so the flag the login branches on lives on the user row.

``auth_source`` only ever distinguished the account that signed in through ``WEB_PASSWORD``
(``"env"``, with a placeholder hash) from database accounts. That login is retired: the database is
the only credential store, so the column carries nothing. A former env account keeps its row and
its placeholder hash, which verifies against nothing, so it signs in again only after
``python -m app.cli reset-password <username>``. It is flagged here so that first sign-in goes
straight to the change form.

``session_version`` is carried in every session token and bumped on each password change, so a
reset or a change ends the sessions issued before it.

Revision ID: 5b8e2d71c4a9
Revises: c4e7b1a90f22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5b8e2d71c4a9"
down_revision: str | None = "c4e7b1a90f22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(
                "must_change_password",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column(
                "session_version", sa.Integer(), nullable=False, server_default="0"
            )
        )
    op.execute(
        sa.text("UPDATE users SET must_change_password = 1 WHERE auth_source = 'env'")
    )
    with op.batch_alter_table("users") as batch:
        batch.drop_column("auth_source")


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(
                "auth_source",
                sa.String(length=32),
                nullable=False,
                server_default="database",
            )
        )
        batch.drop_column("session_version")
        batch.drop_column("must_change_password")
    # The previous release finds its env account by auth_source; without this it inserts a
    # second WEB_USERNAME row at startup and fails on the unique username.
    op.execute(
        sa.text(
            "UPDATE users SET auth_source = 'env' WHERE password_hash = 'ENV_MANAGED'"
        )
    )
