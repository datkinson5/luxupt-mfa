"""A day rebuilt from recordings (JPG) beside partial live captures (PNG) uses the fuller set."""

from __future__ import annotations

from pathlib import Path

from app.workers.timelapse_service import TimelapseService


def _touch(folder: Path, ext: str, count: int, start: int = 1_700_000_000) -> None:
    for i in range(count):
        (folder / f"Cam_{start + i * 60}.{ext}").write_bytes(b"x")


def _find(folder: Path) -> tuple[int, str]:
    files, fmt = TimelapseService.__new__(TimelapseService)._find_image_files(folder, "Cam")
    return len(files), fmt


def test_live_only_day_is_unchanged(tmp_path: Path) -> None:
    _touch(tmp_path, "png", 10)
    assert _find(tmp_path) == (10, "png")


def test_recordings_rebuild_wins_over_a_gappy_live_day(tmp_path: Path) -> None:
    _touch(tmp_path, "png", 600)
    _touch(tmp_path, "jpg", 1440, start=1_700_000_030)
    assert _find(tmp_path) == (1440, "jpg")


def test_a_tie_keeps_live_frames(tmp_path: Path) -> None:
    _touch(tmp_path, "png", 5)
    _touch(tmp_path, "jpg", 5, start=1_700_000_030)
    assert _find(tmp_path) == (5, "png")


def test_jpg_only_and_empty(tmp_path: Path) -> None:
    _touch(tmp_path, "jpg", 3)
    assert _find(tmp_path) == (3, "jpg")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _find(empty)[0] == 0
