"""users.entra_oid: link a user to their Microsoft Entra account (fork: Entra sign-in)

A user who signs in with Microsoft is identified by the Entra object ID in the ID token's ``oid``
claim. The username follows their UPN and may change; the object ID does not. Null for local
password accounts.

Revision ID: e7a3c9d14b62
Revises: 5b8e2d71c4a9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7a3c9d14b62"
down_revision: str | None = "5b8e2d71c4a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("entra_oid", sa.String(length=64), nullable=True))
        batch.create_unique_constraint("uq_users_entra_oid", ["entra_oid"])


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("uq_users_entra_oid", type_="unique")
        batch.drop_column("entra_oid")
