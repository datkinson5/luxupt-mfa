"""Active hours and roll-ups (fork), through the real settings routes and the real job processor.

The roll-up test encodes a real MP4 with ffmpeg from small generated JPEG frames, so it covers
frame picking, encoding, the timelapse record, replacement on rebuild, and that a roll-up does
not stand in for the day's own timelapse.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import config
from app.crud import job_crud, timelapse_crud
from app.crud.fetch_settings_crud import fetch_settings_crud
from app.crud.scheduler_settings_crud import scheduler_settings_crud
from app.models.camera_model import Camera
from app.models.enum_model import CaptureMethod
from app.models.timelapse_model import Timelapse
from app.services.core.job_core_service import get_job_processor
from app.services.core.rollup_core import RollupKind
from app.services.core.rollup_jobs_core_service import create_rollup_jobs
from app.utils.timezones import display_zone

CAMERA = "Rollup_Cam"
WEEK = (date(2026, 10, 19), date(2026, 10, 25))


@pytest.fixture(autouse=True)
def _clean_media() -> Iterator[None]:
    """The image and video folders are shared by the whole session: start each test empty."""
    def wipe() -> None:
        shutil.rmtree(config.IMAGE_OUTPUT_PATH / CAMERA, ignore_errors=True)
        for folder in Path(config.VIDEO_OUTPUT_PATH).glob(f"*/*/{CAMERA}"):
            shutil.rmtree(folder, ignore_errors=True)

    wipe()
    yield
    wipe()


# --- settings -------------------------------------------------------------------------------


async def test_capture_active_hours_default_to_all_day_and_save(
    auth_client: AsyncClient, db: AsyncSession
) -> None:
    settings = await fetch_settings_crud.get_settings(db)
    assert settings.capture_active_start == time(0, 0) == settings.capture_active_end

    panel = await auth_client.get("/cameras/fetch-settings")
    assert panel.status_code == 200 and "Capture Active Hours" in panel.text

    resp = await auth_client.post(
        "/cameras/fetch-settings",
        data={"capture_active_start": "07:30", "capture_active_end": "17:30"},
    )
    assert resp.status_code == 200
    db.expire_all()
    settings = await fetch_settings_crud.get_settings(db)
    assert (settings.capture_active_start, settings.capture_active_end) == (time(7, 30), time(17, 30))

    # A save that does not send the fields leaves them alone.
    await auth_client.post("/cameras/fetch-settings", data={"enabled": "true"})
    db.expire_all()
    settings = await fetch_settings_crud.get_settings(db)
    assert settings.capture_active_start == time(7, 30)


async def test_scheduler_panel_shows_active_hours_and_rollups(auth_client: AsyncClient) -> None:
    panel = await auth_client.get("/timelapses/scheduler")
    assert panel.status_code == 200
    for text_ in ("Timelapse Active Hours", "Roll-ups", "Weekly", "Monthly", "Year to date", "Build roll-ups now"):
        assert text_ in panel.text


async def test_scheduler_saves_active_hours_and_rollups(
    auth_client: AsyncClient, db: AsyncSession
) -> None:
    settings = await scheduler_settings_crud.get_settings(db)
    assert settings.timelapse_active_start == time(0, 0) == settings.timelapse_active_end
    assert settings.rollup_weekly_enabled and settings.rollup_monthly_enabled and settings.rollup_yearly_enabled

    resp = await auth_client.post(
        "/timelapses/scheduler",
        data={
            "enabled": "on",
            "run_time": "01:00",
            "timelapse_active_start": "07:00",
            "timelapse_active_end": "18:00",
            "rollups_form": "1",
            "rollup_weekly_enabled": "true",
            "rollup_weekly_step_minutes": "30",
            "rollup_weekly_frame_rate": "12",
            "rollup_monthly_step_minutes": "1440",
            "rollup_yearly_step_minutes": "999",  # not an offered choice: ignored
            "rollup_target_time": "13:15",
        },
    )
    assert resp.status_code == 200
    db.expire_all()
    s = await scheduler_settings_crud.get_settings(db)
    assert (s.timelapse_active_start, s.timelapse_active_end) == (time(7, 0), time(18, 0))
    assert s.rollup_weekly_enabled and not s.rollup_monthly_enabled and not s.rollup_yearly_enabled
    assert (s.rollup_weekly_step_minutes, s.rollup_weekly_frame_rate) == (30, 12)
    assert s.rollup_yearly_step_minutes == 1440
    assert s.rollup_target_time == time(13, 15)

    # An older form without the roll-up section leaves the toggles alone.
    await auth_client.post("/timelapses/scheduler", data={"enabled": "on", "run_time": "01:00"})
    db.expire_all()
    s = await scheduler_settings_crud.get_settings(db)
    assert s.rollup_weekly_enabled and not s.rollup_monthly_enabled


async def test_migration_defaults_read_back_as_times(db: AsyncSession) -> None:
    """The server defaults the migration gives existing rows parse as TIME values."""
    await db.execute(
        text(
            "UPDATE scheduler_settings SET timelapse_active_start = '00:00:00.000000', "
            "rollup_target_time = '12:00:00.000000'"
        )
    )
    await db.execute(text("UPDATE fetch_settings SET capture_active_end = '00:00:00.000000'"))
    db.expire_all()
    s = await scheduler_settings_crud.get_settings(db)
    f = await fetch_settings_crud.get_settings(db)
    assert s.timelapse_active_start == time(0, 0) and s.rollup_target_time == time(12, 0)
    assert f.capture_active_end == time(0, 0)


async def test_reader_cannot_build_rollups(client: AsyncClient, db: AsyncSession) -> None:
    from app.crud.user_crud import user_crud

    await user_crud.create_user(db, username="admin", password="a-long-password-123")
    await user_crud.create_user(db, username="viewer", password="a-long-password-123", is_admin=False)
    await client.post("/login", data={"username": "viewer", "password": "a-long-password-123"})
    assert (await client.post("/timelapses/scheduler/rollups/run")).status_code == 403


# --- roll-ups end to end --------------------------------------------------------------------


def _write_frames(days: list[date], every_minutes: int) -> int:
    zone = display_zone()
    written = 0
    for n, day in enumerate(days):
        folder = config.IMAGE_OUTPUT_PATH / CAMERA / "60s" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        folder.mkdir(parents=True, exist_ok=True)
        start = datetime.combine(day, time(0, 0), tzinfo=zone)
        for i in range(24 * 60 // every_minutes):
            moment = start + timedelta(minutes=i * every_minutes)
            shade = (40 * n + i * 3) % 255
            Image.new("RGB", (64, 48), (shade, 80, 160)).save(
                folder / f"{CAMERA}_{int(moment.timestamp())}.jpg", quality=80
            )
            written += 1
    return written


async def _camera(db: AsyncSession) -> None:
    db.add(
        Camera(
            camera_id="rollup-cam-uuid",
            name="Rollup Cam",
            safe_name=CAMERA,
            is_active=True,
            is_connected=True,
            capture_method=CaptureMethod.AUTO,
            rtsp_quality="high",
            enabled_intervals=[60],
            first_discovered_at=datetime.now(UTC),
        )
    )
    await db.commit()


async def _build(db: AsyncSession, period: tuple[date, date]) -> str:
    queued = await create_rollup_jobs(
        db,
        lambda kind: period if kind is RollupKind.WEEKLY else None,
        only_cameras=[CAMERA],
        only_intervals=[60],
    )
    await db.commit()
    assert len(queued) == 1
    q = queued[0]
    await get_job_processor()._process_job(q.job_id, q.date_str, q.camera_safe_name, q.interval)
    db.expire_all()  # the processor wrote through its own sessions
    return q.job_id


async def _weekly_rollups(db: AsyncSession) -> list[Timelapse]:
    db.expire_all()
    result = await db.execute(select(Timelapse).where(Timelapse.rollup_kind == "weekly"))
    return list(result.scalars().all())


async def test_weekly_rollup_is_built_from_saved_frames(db: AsyncSession) -> None:
    await _camera(db)
    _write_frames([date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21)], every_minutes=20)

    job_id = await _build(db, WEEK)

    job = await job_crud.get_by_job_id(db, job_id)
    assert job is not None and job.status == "completed", job.error if job else None
    [rollup] = await _weekly_rollups(db)
    assert rollup.timelapse_date == WEEK[0] and rollup.end_date == WEEK[1]
    assert rollup.file_name == f"{CAMERA}_20261019_to_20261025_60s_weekly.mp4"
    assert rollup.frame_rate == 24
    assert rollup.file_path and Path(rollup.file_path).is_file()
    assert rollup.frame_count == 72  # every hour (default), 24 a day, 3 days of frames

    # A roll-up is not the day's own timelapse.
    assert (
        await timelapse_crud.get_by_camera_date_interval(
            db, camera_id="rollup-cam-uuid", timelapse_date=WEEK[0], interval=60
        )
        is None
    )


async def test_rebuilding_a_rollup_replaces_it(db: AsyncSession) -> None:
    await _camera(db)
    _write_frames([date(2026, 10, 19), date(2026, 10, 20)], every_minutes=30)

    await _build(db, (WEEK[0], date(2026, 10, 19)))  # "to date" after one day
    [first] = await _weekly_rollups(db)
    first_path = Path(first.file_path or "")

    await _build(db, WEEK)  # the full week later
    [second] = await _weekly_rollups(db)
    assert second.end_date == WEEK[1] and second.frame_count == 48
    assert not first_path.exists()


async def test_active_hours_and_spacing_shape_the_rollup(db: AsyncSession) -> None:
    await _camera(db)
    _write_frames([date(2026, 10, 19), date(2026, 10, 20)], every_minutes=10)
    settings = await scheduler_settings_crud.get_settings(db)
    settings.timelapse_active_start = time(8, 0)
    settings.timelapse_active_end = time(12, 0)
    settings.rollup_weekly_step_minutes = 30
    settings.rollup_weekly_frame_rate = 6
    await db.commit()

    await _build(db, WEEK)
    [rollup] = await _weekly_rollups(db)
    assert rollup.frame_count == 16  # 08:00–12:00 in 30-minute slots = 8 a day, 2 days
    assert rollup.frame_rate == 6


async def test_period_without_frames_fails_with_a_reason(db: AsyncSession) -> None:
    await _camera(db)
    job_id = await _build(db, (date(2026, 9, 7), date(2026, 9, 13)))
    job = await job_crud.get_by_job_id(db, job_id)
    assert job is not None and job.status == "failed"
    assert "Not enough frames" in (job.error or "")
    assert await _weekly_rollups(db) == []
