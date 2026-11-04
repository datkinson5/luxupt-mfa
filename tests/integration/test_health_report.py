"""Health report (fork): the zip download, its findings, and that it carries no secrets."""

from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import job_crud
from app.crud.fetch_settings_crud import fetch_settings_crud
from app.models.camera_model import Camera
from app.models.capture_model import Capture
from app.models.enum_model import CaptureMethod, CaptureStatus
from app.models.timelapse_model import Timelapse
from app.utils.timezones import to_display

CAMERA = "Health_Cam"


async def _seed(db: AsyncSession) -> datetime:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    start = now - timedelta(hours=3)
    db.add(
        Camera(
            camera_id="health-cam-uuid",
            name="Health Cam",
            safe_name=CAMERA,
            is_active=True,
            is_connected=True,
            capture_method=CaptureMethod.AUTO,
            rtsp_quality="high",
            enabled_intervals=[60],
            first_discovered_at=start,
        )
    )
    moment = start
    n = 0
    while moment < now:
        in_gap = start + timedelta(minutes=60) <= moment < start + timedelta(minutes=100)
        failed = n % 30 == 7
        if not in_gap:
            db.add(
                Capture(
                    camera_id="health-cam-uuid",
                    camera_safe_name=CAMERA,
                    timestamp=int(moment.timestamp()),
                    capture_datetime=moment,
                    capture_date=to_display(moment).date(),
                    interval=60,
                    status=CaptureStatus.FAILED if failed else CaptureStatus.SUCCESS,
                    error_message=f"RTSP timeout after {n} ms on 10.0.0.{n % 9}" if failed else None,
                    capture_method=CaptureMethod.RTSP,
                    capture_duration_ms=None if failed else 800 + n,
                    file_size=None if failed else 250_000,
                )
            )
        moment += timedelta(minutes=1)
        n += 1
    job = await job_crud.create_job(
        db, title="Health_Cam_daily", camera_safe_name=CAMERA, target_date=now.date(), interval=60
    )
    await job_crud.fail_job(db, job.job_id, error="Timelapse creation failed")
    db.add(
        Timelapse(
            camera_id="health-cam-uuid",
            camera_safe_name=CAMERA,
            timelapse_date=to_display(now).date() - timedelta(days=1),
            interval=60,
            frame_count=100,
            frame_rate=24,
            duration_seconds=4.2,
            status="completed",
            file_size=5_000_000,
            quality_report={"checked": 100, "flagged": {"dark": 12}, "exclude": False, "excluded": 0, "replaced": 0},
        )
    )
    settings = await fetch_settings_crud.get_settings(db)
    settings.api_key = "SECRET-API-KEY-abc123"
    settings.username = "svc-luxupt"
    settings.password = "SECRET-PASSWORD-xyz789"
    await db.flush()
    return now


def _open(content: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return {name: archive.read(name).decode() for name in archive.namelist()}


async def test_download_is_a_zip_with_findings_and_no_secrets(
    auth_client: AsyncClient, db: AsyncSession
) -> None:
    await _seed(db)
    logging.getLogger("app.health_test").warning("Teleport tunnel dropped for 42 seconds")

    resp = await auth_client.get("/system/health-report?period=24h")
    assert resp.status_code == 200, resp.text[:300]
    assert resp.headers["content-type"] == "application/zip"
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment;") and "luxupt-health-" in disposition

    files = _open(resp.content)
    assert set(files) == {"summary.txt", "report.json", "recent-problems.jsonl", "recent-activity.jsonl"}
    everything = "".join(files.values())
    for secret in ("SECRET-API-KEY", "SECRET-PASSWORD", "svc-luxupt"):
        assert secret not in everything

    report: dict[str, Any] = json.loads(files["report.json"])
    assert report["settings"]["capture"]["api_key_set"] is True
    assert report["settings"]["capture"]["protect_login_set"] is True
    [camera] = [c for c in report["cameras"] if c["camera"] == CAMERA]
    [stats] = camera["intervals"]
    assert 175 <= stats["expected"] <= 181
    assert stats["failed"] == 4 and stats["captured"] == 136
    assert stats["gap_count"] >= 1 and 38 <= stats["gaps"][0]["missed"] <= 41
    [error] = stats["errors"]
    assert error["count"] == 4 and error["error"] == "RTSP timeout after # ms on #"
    assert report["jobs"]["failed_count"] == 1
    assert report["timelapses"]["frame_checks"][0]["flagged"] == {"dark": 12}
    texts = " ".join(f["text"] for f in report["findings"])
    assert "Health Cam 60s" in texts and "gap" in texts and "failed job" in texts

    summary = files["summary.txt"]
    assert "FINDINGS" in summary and "Health Cam" in summary
    assert "Teleport tunnel dropped" in files["recent-problems.jsonl"]


async def test_period_choice_and_unknown_period(auth_client: AsyncClient, db: AsyncSession) -> None:
    await _seed(db)
    week = json.loads(_open((await auth_client.get("/system/health-report?period=7d")).content)["report.json"])
    assert week["period"]["hours"] == 168
    odd = json.loads(_open((await auth_client.get("/system/health-report?period=nonsense")).content)["report.json"])
    assert odd["period"]["hours"] == 24


async def test_system_page_offers_the_download(auth_client: AsyncClient) -> None:
    page = await auth_client.get("/system")
    assert page.status_code == 200
    assert "Health Report" in page.text and 'action="/system/health-report"' in page.text


async def test_readers_cannot_download(client: AsyncClient, db: AsyncSession) -> None:
    from app.crud.user_crud import user_crud

    await user_crud.create_user(db, username="admin", password="a-long-password-123")
    await user_crud.create_user(db, username="viewer", password="a-long-password-123", is_admin=False)
    await client.post("/login", data={"username": "viewer", "password": "a-long-password-123"})
    assert (await client.get("/system/health-report")).status_code == 403


async def test_new_and_renamed_cameras_are_counted_fairly(
    auth_client: AsyncClient, db: AsyncSession
) -> None:
    """A camera with no discovery date is expected from its first capture; captures under a
    name no camera has now are shown, not dropped; a connected camera with nothing is flagged."""
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    for safe, discovered in (("New_Cam", None), ("Quiet_Cam", None)):
        db.add(
            Camera(
                camera_id=f"{safe}-uuid",
                name=safe.replace("_", " "),
                safe_name=safe,
                is_active=True,
                is_connected=True,
                capture_method=CaptureMethod.AUTO,
                rtsp_quality="high",
                enabled_intervals=[60],
                first_discovered_at=discovered,
            )
        )
    def capture(name: str, minutes_ago: int, *, rtsp_bug: bool = False) -> Capture:
        moment = now - timedelta(minutes=minutes_ago)
        return Capture(
            camera_id=f"{name}-uuid",
            camera_safe_name=name,
            timestamp=int(moment.timestamp()),
            capture_datetime=moment,
            capture_date=to_display(moment).date(),
            interval=60,
            status=CaptureStatus.SUCCESS,
            capture_method=CaptureMethod.RTSP,
            capture_duration_ms=int(moment.timestamp() * 1000) if rtsp_bug else 900,
            file_size=1000,
        )

    for m in range(30, 0, -1):  # New_Cam: captures every minute for the last 30 minutes
        db.add(capture("New_Cam", m, rtsp_bug=m % 2 == 0))
    db.add(capture("Quiet_Cam", 60 * 30))  # Quiet_Cam: last captured 30 hours ago
    for m in range(120, 100, -1):  # an old name, 20 captures two hours ago
        db.add(capture("Old_Name", m))
    await db.flush()

    files = _open((await auth_client.get("/system/health-report?period=24h")).content)
    report = json.loads(files["report.json"])
    cams = {c["camera"]: c for c in report["cameras"]}

    new = cams["New_Cam"]["intervals"][0]
    assert 29 <= new["expected"] <= 31 and new["captured"] == 30 and new["gap_count"] == 0
    assert new["capture_ms"]["median"] == 900  # the bad RTSP durations are ignored

    assert cams["Quiet_Cam"]["intervals"][0]["captured"] == 0
    assert cams["Quiet_Cam"]["last_capture"] is not None
    assert cams["Old_Name"]["state"] == "unknown" and cams["Old_Name"]["intervals"][0]["captured"] == 20

    texts = " ".join(f["text"] for f in report["findings"])
    assert "Quiet Cam: connected but nothing captured" in texts
    assert '20 captures saved under "Old_Name"' in texts
    assert "New Cam 60s: only" not in texts
