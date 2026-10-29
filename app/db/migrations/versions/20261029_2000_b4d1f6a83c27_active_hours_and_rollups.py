"""active hours and roll-ups (fork)

fetch_settings.capture_active_start/end and scheduler_settings.timelapse_active_start/end:
daily wall-clock windows, defaulting to all day (equal times). scheduler_settings.rollup_*:
weekly / monthly / yearly roll-up timelapses, on by default. timelapses.rollup_kind marks a
roll-up's timelapse. jobs.job_type gains rollup_* values; it is a VARCHAR without a CHECK
constraint, so that needs no change here.

Revision ID: b4d1f6a83c27
Revises: e7a3c9d14b62
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4d1f6a83c27"
down_revision: str | None = "e7a3c9d14b62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQLAlchemy's SQLite TIME storage format, so existing rows read back as the default.
MIDNIGHT = sa.text("'00:00:00.000000'")
NOON = sa.text("'12:00:00.000000'")

ROLLUP_COLUMNS = [
    ("rollup_weekly_enabled", sa.Boolean(), sa.true()),
    ("rollup_weekly_step_minutes", sa.Integer(), "60"),
    ("rollup_weekly_frame_rate", sa.Integer(), "24"),
    ("rollup_monthly_enabled", sa.Boolean(), sa.true()),
    ("rollup_monthly_step_minutes", sa.Integer(), "1440"),
    ("rollup_monthly_frame_rate", sa.Integer(), "10"),
    ("rollup_yearly_enabled", sa.Boolean(), sa.true()),
    ("rollup_yearly_step_minutes", sa.Integer(), "1440"),
    ("rollup_yearly_frame_rate", sa.Integer(), "24"),
]


def upgrade() -> None:
    with op.batch_alter_table("fetch_settings") as batch:
        batch.add_column(sa.Column("capture_active_start", sa.Time(), nullable=False, server_default=MIDNIGHT))
        batch.add_column(sa.Column("capture_active_end", sa.Time(), nullable=False, server_default=MIDNIGHT))
    with op.batch_alter_table("scheduler_settings") as batch:
        batch.add_column(sa.Column("timelapse_active_start", sa.Time(), nullable=False, server_default=MIDNIGHT))
        batch.add_column(sa.Column("timelapse_active_end", sa.Time(), nullable=False, server_default=MIDNIGHT))
        for name, kind, default in ROLLUP_COLUMNS:
            batch.add_column(sa.Column(name, kind, nullable=False, server_default=default))
        batch.add_column(sa.Column("rollup_target_time", sa.Time(), nullable=False, server_default=NOON))
    with op.batch_alter_table("timelapses") as batch:
        batch.add_column(sa.Column("rollup_kind", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("timelapses") as batch:
        batch.drop_column("rollup_kind")
    with op.batch_alter_table("scheduler_settings") as batch:
        batch.drop_column("rollup_target_time")
        for name, _, _ in reversed(ROLLUP_COLUMNS):
            batch.drop_column(name)
        batch.drop_column("timelapse_active_end")
        batch.drop_column("timelapse_active_start")
    with op.batch_alter_table("fetch_settings") as batch:
        batch.drop_column("capture_active_end")
        batch.drop_column("capture_active_start")
