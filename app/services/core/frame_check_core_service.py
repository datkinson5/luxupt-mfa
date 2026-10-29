"""Check yesterday's frames (fork): run the frame checks over saved frames without building.

For tuning the checks in report-only mode: pick a camera's frames for a day, score an even
sample of them, and show what each switched-on check would flag, with example frames.
"""

from __future__ import annotations

import asyncio
import io
import re
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import config
from app.crud import camera_crud, scheduler_settings_crud
from app.crud.fetch_settings_crud import fetch_settings_crud
from app.services.core.frame_quality import QualitySettings, sample, screen_frames
from app.services.core.rollup_core import frame_moment
from app.utils.time_windows import in_window
from app.utils.timezones import display_zone

SAMPLE = 240
PREVIEW_WIDTH = 320
_SAFE = re.compile(r"^[A-Za-z0-9_\-]+$")


@dataclass(frozen=True)
class Example:
    reason: str
    label: str
    time: str
    url: str


def _day_dir(camera: str, interval: int, day: date) -> Path:
    return (
        config.IMAGE_OUTPUT_PATH
        / camera
        / f"{interval}s"
        / f"{day:%Y}"
        / f"{day:%m}"
        / f"{day:%d}"
    )


def _frames(camera: str, interval: int, day: date, window: tuple[Any, Any]) -> list[Path]:
    folder = _day_dir(camera, interval, day)
    if not folder.is_dir():
        return []
    zone = display_zone()
    found = {fmt: sorted(folder.glob(f"{camera}_*.{fmt}")) for fmt in ("png", "jpg")}
    files = max(found.values(), key=len)
    kept = []
    for path in files:
        moment = frame_moment(path, zone)
        if moment is not None and in_window(moment.time(), *window):
            kept.append(path)
    return kept


def _check(
    camera: str, interval: int, day: date, window: tuple[Any, Any], quality: QualitySettings
) -> dict[str, Any] | None:
    frames = _frames(camera, interval, day, window)
    if not frames:
        return None
    _, report = screen_frames(sample(frames, SAMPLE), quality)
    zone = display_zone()
    examples: list[Example] = []
    for reason in quality.checks:
        for name in report.examples.get(reason.value, [])[:4]:
            moment = frame_moment(Path(name), zone)
            examples.append(
                Example(
                    reason=reason.value,
                    label=reason.label,
                    time=f"{moment:%H:%M}" if moment else "",
                    url=(
                        f"/timelapses/scheduler/frame-checks/frame/{camera}/{interval}/"
                        f"{day:%Y}/{day:%m}/{day:%d}/{name}"
                    ),
                )
            )
    return {
        "camera": camera,
        "interval": interval,
        "total": len(frames),
        "checked": report.checked,
        "flagged": [
            {"label": c.label, "count": report.flagged.get(c.value, 0)} for c in quality.checks
        ],
        "flagged_frames": report.flagged_frames,
        "examples": examples,
    }


async def check_saved_frames(db: AsyncSession, day: date) -> dict[str, Any]:
    """What the switched-on checks would flag in ``day``'s frames, per camera and interval."""
    settings = await scheduler_settings_crud.get_settings(db)
    quality = QualitySettings.from_scheduler(settings)
    if not quality.any:
        return {"day": day, "results": [], "reason": "Switch on at least one check and save first."}
    cameras = await camera_crud.get_active(db)
    if settings.enabled_cameras:
        cameras = [c for c in cameras if c.safe_name in settings.enabled_cameras]
    intervals = settings.enabled_intervals or (
        await fetch_settings_crud.get_settings(db)
    ).get_intervals()
    window = (settings.timelapse_active_start, settings.timelapse_active_end)
    # Report what would be flagged; leaving frames out does not apply to a check.
    quality = replace(quality, exclude=False)

    results = []
    for camera in cameras:
        for interval in intervals:
            result = await asyncio.to_thread(
                _check, camera.safe_name, interval, day, window, quality
            )
            if result is not None:
                results.append(result)
    return {
        "day": day,
        "results": results,
        "reason": "" if results else "No saved frames for that day (is Keep Source Images on?).",
    }


def preview(camera: str, interval: int, year: str, month: str, day: str, name: str) -> bytes | None:
    """A small JPEG of one saved frame, or None if the request does not name one."""
    if not (_SAFE.match(camera) and year.isdigit() and month.isdigit() and day.isdigit()):
        return None
    if not re.fullmatch(rf"{re.escape(camera)}_\d+\.(jpg|png)", name):
        return None
    root = config.IMAGE_OUTPUT_PATH.resolve()
    path = (config.IMAGE_OUTPUT_PATH / camera / f"{interval}s" / year / month / day / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    with Image.open(path) as image:
        image.draft("RGB", (PREVIEW_WIDTH, PREVIEW_WIDTH))
        image = image.convert("RGB")
        image.thumbnail((PREVIEW_WIDTH, PREVIEW_WIDTH * 2))
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=80)
    return out.getvalue()

