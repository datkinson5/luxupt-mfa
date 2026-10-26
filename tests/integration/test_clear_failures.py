"""The Cameras page's "Clear failures" button deletes failed capture rows, and only those."""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capture_model import Capture
from app.models.enum_model import CaptureMethod, CaptureStatus
from app.utils.timezones import business_day


def _capture(ts: int, status: str, error: str | None = None) -> Capture:
    return Capture(
        camera_id="clear-failures-cam",
        camera_safe_name="Clear_Failures_Cam",
        timestamp=ts,
        capture_datetime=datetime.fromtimestamp(ts, UTC),
        capture_date=business_day(datetime.fromtimestamp(ts, UTC)),
        interval=60,
        status=status,
        capture_method=CaptureMethod.PROTECT_HISTORICAL,
        file_path=None if status == CaptureStatus.FAILED else f"/nonexistent/{ts}.jpg",
        error_message=error,
    )


async def _count(db: AsyncSession, status: str) -> int:
    return (
        await db.execute(select(func.count()).select_from(Capture).where(Capture.status == status))
    ).scalar_one()


async def test_clear_failures_deletes_only_failed_rows(
    auth_client: AsyncClient, db: AsyncSession
) -> None:
    db.add_all(
        [
            _capture(1767225600, CaptureStatus.SUCCESS),
            _capture(1767225660, CaptureStatus.FAILED, "ProtectAuthError: login failed: HTTP 403"),
            _capture(1767225720, CaptureStatus.FAILED, "ReadError: "),
        ]
    )
    await db.commit()

    page = await auth_client.get("/cameras")
    assert page.status_code == 200
    assert "Clear failures" in page.text
    assert "/cameras/failures/clear" in page.text

    resp = await auth_client.post("/cameras/failures/clear")
    assert resp.status_code == 200
    assert resp.headers.get("HX-Refresh") == "true"

    db.expire_all()
    assert await _count(db, CaptureStatus.FAILED) == 0
    assert await _count(db, CaptureStatus.SUCCESS) == 1

    page = await auth_client.get("/cameras")
    assert "Recent Failures" not in page.text


async def test_clear_failures_requires_sign_in(client: AsyncClient) -> None:
    resp = await client.post("/cameras/failures/clear")
    assert resp.status_code in (302, 303, 401, 403)
