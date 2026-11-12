"""video codec choice (fork)

scheduler_settings.video_codec: "h265" (the existing behaviour, the default) or "h264".

Revision ID: d3f8b2c61e95
Revises: c9e2a7b51d48
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3f8b2c61e95"
down_revision: str | None = "c9e2a7b51d48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("scheduler_settings") as batch:
        batch.add_column(
            sa.Column("video_codec", sa.String(8), nullable=False, server_default="h265")
        )


def downgrade() -> None:
    with op.batch_alter_table("scheduler_settings") as batch:
        batch.drop_column("video_codec")
