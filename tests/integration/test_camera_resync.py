"""Periodic camera sync (fork): a camera adopted while the service runs gets captured, and
online/offline changes are recorded, without a restart."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.camera_manager import CameraManagerSettings
from app.crud import camera_crud
from app.models.activity_model import Activity
from app.workers.fetch_service import FetchService


def _api_camera(cid: str, name: str, connected: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=cid,
        name=name,
        safe_name=name.replace(" ", "_"),
        mac="AA:BB:CC:00:00:01",
        model_key="camera",
        video_mode="default",
        hdr_type="normal",
        state="CONNECTED" if connected else "DISCONNECTED",
        is_connected=connected,
        is_recording=True,
        supports_full_hd_snapshot=True,
        has_hdr=False,
        has_mic=False,
        has_speaker=False,
        smart_detect_types=[],
    )


class _FakeManager:
    def __init__(self) -> None:
        self.cameras: list[SimpleNamespace] = []
        self.calls = 0

    async def get_cameras(self, force_refresh: bool = False) -> list[Any]:
        self.calls += 1
        return list(self.cameras)

    async def detect_camera_capabilities(self, camera: Any) -> dict[str, Any]:
        return {}


def _settings(every: int = 300) -> CameraManagerSettings:
    return CameraManagerSettings(
        base_url="https://console.example",
        api_key="key",
        verify_ssl=False,
        request_timeout=30,
        rate_limit=10,
        rate_limit_buffer=0.8,
        min_offset_seconds=2,
        max_offset_seconds=15,
        camera_refresh_interval=every,
    )


async def test_new_camera_and_offline_change_are_picked_up(db: AsyncSession) -> None:
    service = FetchService.__new__(FetchService)
    manager = _FakeManager()
    service.camera_manager = manager  # type: ignore[assignment]

    # First check: nothing synced yet, so it syncs (as at startup).
    await service._resync_cameras_if_due(_settings())
    assert manager.calls == 1

    # A camera adopted in Protect while running: not due yet, so not picked up yet...
    manager.cameras = [_api_camera("cam-new", "AI Turret 7307")]
    await service._resync_cameras_if_due(_settings())
    assert manager.calls == 1
    # ...but once camera_refresh_interval has passed it is, with capture defaults.
    service._last_camera_sync -= 301
    await service._resync_cameras_if_due(_settings())
    cam = await camera_crud.get_by_camera_id(db, "cam-new")
    assert cam is not None and cam.is_active and cam.enabled_intervals == [60]

    # It drops off (a Teleport outage): recorded as offline at the next sync.
    manager.cameras = [_api_camera("cam-new", "AI Turret 7307", connected=False)]
    service._last_camera_sync -= 301
    await service._resync_cameras_if_due(_settings())
    db.expire_all()
    cam = await camera_crud.get_by_camera_id(db, "cam-new")
    assert cam is not None and not cam.is_connected
    events = (await db.execute(select(Activity.message))).scalars().all()
    assert "AI Turret 7307 went offline" in events


async def test_no_sync_without_api_settings(db: AsyncSession) -> None:
    service = FetchService.__new__(FetchService)
    manager = _FakeManager()
    service.camera_manager = manager  # type: ignore[assignment]
    blank = _settings()
    blank = CameraManagerSettings(**{**blank.__dict__, "api_key": ""})
    await service._resync_cameras_if_due(blank)
    assert manager.calls == 0


async def test_a_failed_sync_is_retried_next_period(db: AsyncSession) -> None:
    service = FetchService.__new__(FetchService)

    class Broken(_FakeManager):
        async def get_cameras(self, force_refresh: bool = False) -> list[Any]:
            self.calls += 1
            raise RuntimeError("console unreachable")

    manager = Broken()
    service.camera_manager = manager  # type: ignore[assignment]
    await service._resync_cameras_if_due(_settings())
    await service._resync_cameras_if_due(_settings())
    assert manager.calls == 1  # waits for the next period rather than hammering the console
