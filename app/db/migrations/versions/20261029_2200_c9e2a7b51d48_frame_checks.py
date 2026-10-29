"""frame checks (fork)

scheduler_settings.quality_*: the dark / blur / rain frame checks, each off by default, plus
whether flagged frames are left out (off: report only) and the three thresholds.
timelapses.quality_report: what the checks found for that build (JSON; null when none ran).

Revision ID: c9e2a7b51d48
Revises: b4d1f6a83c27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e2a7b51d48"
down_revision: str | None = "b4d1f6a83c27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QUALITY_COLUMNS = [
    ("quality_dark_enabled", sa.Boolean(), sa.false()),
    ("quality_blur_enabled", sa.Boolean(), sa.false()),
    ("quality_rain_enabled", sa.Boolean(), sa.false()),
    ("quality_exclude", sa.Boolean(), sa.false()),
    ("quality_dark_level", sa.Integer(), "40"),
    ("quality_blur_percent", sa.Integer(), "40"),
    ("quality_rain_percent", sa.Integer(), "25"),
]


def upgrade() -> None:
    with op.batch_alter_table("scheduler_settings") as batch:
        for name, kind, default in QUALITY_COLUMNS:
            batch.add_column(sa.Column(name, kind, nullable=False, server_default=default))
    with op.batch_alter_table("timelapses") as batch:
        batch.add_column(sa.Column("quality_report", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("timelapses") as batch:
        batch.drop_column("quality_report")
    with op.batch_alter_table("scheduler_settings") as batch:
        for name, _, _ in reversed(QUALITY_COLUMNS):
            batch.drop_column(name)
