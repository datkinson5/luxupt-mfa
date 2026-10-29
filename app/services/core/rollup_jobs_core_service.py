"""Create roll-up jobs from the scheduler settings (fork feature).

The scheduler calls this after its nightly daily builds (only the kinds that are due that day),
and "Build roll-ups now" calls it for the latest periods. Each job is one camera, interval and
kind; the JobProcessor builds it like any other job, so it shows in the jobs list.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import camera_crud, job_crud, scheduler_settings_crud
from app.crud.fetch_settings_crud import fetch_settings_crud
from app.models.enum_model import JobStatus
from app.models.job_model import Job
from app.models.scheduler_settings_model import SchedulerSettings
from app.services.core.rollup_core import RollupKind, RollupPlan
from app.utils.timezones import display_zone

logger = logging.getLogger(__name__)

PeriodFor = Callable[[RollupKind], "tuple[date, date] | None"]


@dataclass(frozen=True)
class QueuedRollup:
    job_id: str
    date_str: str
    camera_safe_name: str
    interval: int


def plan_for(kind: RollupKind, settings: SchedulerSettings) -> RollupPlan:
    return RollupPlan(
        kind=kind,
        step_minutes=getattr(settings, f"rollup_{kind.value}_step_minutes"),
        frame_rate=getattr(settings, f"rollup_{kind.value}_frame_rate"),
        target_time=settings.rollup_target_time,
        window_start=settings.timelapse_active_start,
        window_end=settings.timelapse_active_end,
    )


def enabled_kinds(settings: SchedulerSettings) -> list[RollupKind]:
    return [k for k in RollupKind if getattr(settings, f"rollup_{k.value}_enabled")]


def period_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """A period's local calendar days as UTC instants: start-of-first to end-of-last day."""
    zone = display_zone()
    return (
        datetime.combine(start, time(0, 0), tzinfo=zone).astimezone(UTC),
        datetime.combine(end, time(23, 59, 59), tzinfo=zone).astimezone(UTC),
    )


async def create_rollup_jobs(
    db: AsyncSession,
    period_for: PeriodFor,
    *,
    only_cameras: list[str] | None = None,
    only_intervals: list[int] | None = None,
) -> list[QueuedRollup]:
    """Create a pending job per enabled roll-up kind that ``period_for`` gives a period, for each
    active camera and interval. Skips a roll-up already pending or running."""
    settings = await scheduler_settings_crud.get_settings(db)
    kinds: list[tuple[RollupKind, tuple[date, date]]] = []
    for kind in enabled_kinds(settings):
        period = period_for(kind)
        if period is not None:
            kinds.append((kind, period))
    if not kinds:
        return []

    cameras = await camera_crud.get_active(db)
    cameras_filter = only_cameras if only_cameras is not None else settings.enabled_cameras
    if cameras_filter:
        cameras = [c for c in cameras if c.safe_name in cameras_filter]
    intervals = only_intervals or settings.enabled_intervals
    if not intervals:
        intervals = (await fetch_settings_crud.get_settings(db)).get_intervals()

    queued: list[QueuedRollup] = []
    for kind, (start, end) in kinds:
        start_at, end_at = period_bounds(start, end)
        for camera in cameras:
            for interval in intervals:
                title = f"{camera.safe_name}_{start:%Y%m%d}_to_{end:%Y%m%d}_{interval}s_{kind.value}"
                busy = (
                    await db.execute(
                        select(Job.id).where(
                            Job.title == title,
                            or_(Job.status == JobStatus.PENDING, Job.status == JobStatus.RUNNING),
                        )
                    )
                ).first()
                if busy:
                    logger.debug("Roll-up already queued", extra={"title": title})
                    continue
                job = await job_crud.create_job(
                    db,
                    title=title,
                    camera_safe_name=camera.safe_name,
                    camera_id=camera.camera_id,
                    target_date=start,
                    interval=interval,
                    job_type=kind.job_type,
                    start_at=start_at,
                    end_at=end_at,
                )
                queued.append(QueuedRollup(job.job_id, start.isoformat(), camera.safe_name, interval))
    logger.info(
        "Roll-up jobs created",
        extra={"count": len(queued), "kinds": [k.value for k, _ in kinds]},
    )
    return queued
