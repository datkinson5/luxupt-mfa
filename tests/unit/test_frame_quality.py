"""Frame checks (fork): dark, blurred and rain-spotted frames, scored on generated images."""

from __future__ import annotations

from datetime import date, time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from PIL import Image, ImageFilter

from app.services.core import frame_check_core_service
from app.services.core.frame_quality import (
    Check,
    QualitySettings,
    measure,
    remember_report,
    sample,
    screen_frames,
    screen_rollup,
    take_report,
)
from app.services.core.rollup_core import (
    RollupKind,
    RollupPlan,
    select_frame_candidates,
    select_frames,
)

SIZE = (480, 320)
ALL = QualitySettings(dark=True, blur=True, rain=True)


def _scene(seed: int) -> Image.Image:
    """A detailed 'site' picture: noise with fine texture everywhere, mid brightness."""
    noise = Image.effect_noise(SIZE, 60 + seed % 5)
    return noise.convert("RGB")


def _dark(img: Image.Image) -> Image.Image:
    return img.point(lambda v: v // 10)


def _blurred(img: Image.Image) -> Image.Image:
    return img.filter(ImageFilter.GaussianBlur(4))


def _rain(img: Image.Image) -> Image.Image:
    """Drops on the lens: the left half smeared, the right half still sharp."""
    out = img.copy()
    box = (0, 0, SIZE[0] // 2, SIZE[1])
    out.paste(img.crop(box).filter(ImageFilter.GaussianBlur(4)), box)
    return out


def _save(folder: Path, name: str, img: Image.Image, fmt: str = "jpg") -> Path:
    path = folder / f"{name}.{fmt}"
    img.save(path, quality=90) if fmt == "jpg" else img.save(path)
    return path


@pytest.fixture
def batch(tmp_path: Path) -> dict[str, list[Path]]:
    good = [_save(tmp_path, f"good{i}", _scene(i)) for i in range(10)]
    return {
        "good": good,
        "dark": [_save(tmp_path, f"dark{i}", _dark(_scene(i))) for i in range(2)],
        "blur": [_save(tmp_path, f"blur{i}", _blurred(_scene(i))) for i in range(2)],
        "rain": [_save(tmp_path, f"rain{i}", _rain(_scene(i))) for i in range(2)],
    }


def test_measure_scores_brightness_and_sharpness(tmp_path: Path) -> None:
    sharp = measure(_save(tmp_path, "a", _scene(1), "png"))
    soft = measure(_save(tmp_path, "b", _blurred(_scene(1)), "png"))
    dark = measure(_save(tmp_path, "c", _dark(_scene(1))))
    assert sharp and soft and dark
    assert 90 < sharp.brightness < 170 and dark.brightness < 25
    assert soft.sharpness < sharp.sharpness * 0.3
    assert len(sharp.tiles) == 24


def test_unreadable_frame_is_not_flagged(tmp_path: Path) -> None:
    bad = tmp_path / "broken.jpg"
    bad.write_bytes(b"not a jpeg")
    assert measure(bad) is None
    kept, report = screen_frames([bad], ALL)
    assert kept == [bad] and report.flagged_frames == 0


def test_report_only_flags_each_kind_and_keeps_every_frame(batch: dict[str, list[Path]]) -> None:
    frames = [p for paths in batch.values() for p in paths]
    kept, report = screen_frames(frames, ALL)
    assert kept == frames
    assert report.flagged == {"dark": 2, "blur": 2, "rain": 2}
    assert report.flagged_frames == 6 and report.excluded == 0
    assert set(report.examples["rain"]) == {p.name for p in batch["rain"]}


def test_blur_is_not_called_rain_and_rain_is_not_called_blur(batch: dict[str, list[Path]]) -> None:
    frames = batch["good"] + batch["blur"] + batch["rain"]
    _, blur_only = screen_frames(frames, QualitySettings(blur=True))
    _, rain_only = screen_frames(frames, QualitySettings(rain=True))
    assert set(blur_only.examples["blur"]) == {p.name for p in batch["blur"]}
    assert set(rain_only.examples["rain"]) == {p.name for p in batch["rain"]}


def test_only_switched_on_checks_run(batch: dict[str, list[Path]]) -> None:
    frames = batch["good"] + batch["dark"] + batch["blur"]
    _, report = screen_frames(frames, QualitySettings(dark=True))
    assert report.checks == ["dark"] and report.flagged == {"dark": 2}
    assert not QualitySettings().any


def test_exclude_leaves_flagged_frames_out(batch: dict[str, list[Path]]) -> None:
    frames = [p for paths in batch.values() for p in paths]
    kept, report = screen_frames(frames, QualitySettings(dark=True, blur=True, rain=True, exclude=True))
    assert kept == batch["good"] and report.excluded == 6


def test_exclude_never_leaves_too_few_frames(batch: dict[str, list[Path]]) -> None:
    frames = batch["dark"]
    kept, report = screen_frames(frames, QualitySettings(dark=True, exclude=True))
    assert kept == frames and report.kept_all and report.excluded == 0


def test_thresholds_are_respected(batch: dict[str, list[Path]]) -> None:
    frames = batch["good"] + batch["dark"]
    _, report = screen_frames(frames, QualitySettings(dark=True, dark_level=5))
    assert report.flagged["dark"] == 0  # the dark frames average about 12, above 5


def test_too_few_lit_frames_skip_blur_and_rain(batch: dict[str, list[Path]]) -> None:
    frames = batch["good"][:1] + batch["blur"][:1]
    _, report = screen_frames(frames, QualitySettings(blur=True, rain=True))
    assert report.flagged_frames == 0


def test_rollup_swaps_a_flagged_pick_for_the_next_clean_frame(batch: dict[str, list[Path]]) -> None:
    good = batch["good"]
    slots = [[batch["dark"][0], good[0]], [good[1]], [good[2]], [batch["blur"][0], batch["dark"][1]], [good[3]]]
    chosen, report = screen_rollup(slots, QualitySettings(dark=True, blur=True, exclude=True))
    assert chosen == [good[0], good[1], good[2], good[3]]
    assert report.replaced == 1 and report.excluded == 1

    chosen, report = screen_rollup(slots, QualitySettings(dark=True, blur=True))
    assert chosen == [s[0] for s in slots] and report.flagged_frames == 2


def test_sample_spreads_evenly() -> None:
    paths = [Path(f"{i}.jpg") for i in range(1000)]
    picked = sample(paths, 10)
    assert len(picked) == 10 and picked[0] == paths[0] and picked[-1] == paths[900]
    assert sample(paths[:5], 10) == paths[:5]


def test_reports_wait_for_their_output_file(tmp_path: Path, batch: dict[str, list[Path]]) -> None:
    _, report = screen_frames(batch["good"], ALL)
    out = tmp_path / "a.mp4"
    remember_report(out, report)
    assert take_report(out)["checked"] == 10
    assert take_report(out) is None


def test_settings_read_from_scheduler_with_defaults() -> None:
    q = QualitySettings.from_scheduler(SimpleNamespace(quality_rain_enabled=True, quality_rain_percent=30))
    assert q.rain and not q.dark and q.rain_percent == 30 and q.dark_level == 40
    assert [c for c in q.checks] == [Check.RAIN]


def test_rollup_candidates_rank_each_slot() -> None:
    zone = ZoneInfo("Europe/London")
    from datetime import datetime

    def frame(h: int, m: int) -> Path:
        moment = datetime(2026, 10, 20, h, m, tzinfo=zone)
        return Path(f"Cam_{int(moment.timestamp())}.jpg")

    paths = [frame(11, 0), frame(11, 50), frame(12, 10), frame(13, 0)]
    daily = RollupPlan(RollupKind.MONTHLY, 1440, 10, time(12, 0), time(0, 0), time(0, 0))
    [slot] = select_frame_candidates(paths, zone, date(2026, 10, 20), date(2026, 10, 20), daily)
    assert slot == [frame(11, 50), frame(12, 10), frame(11, 0), frame(13, 0)]
    hourly = RollupPlan(RollupKind.WEEKLY, 60, 24, time(12, 0), time(0, 0), time(0, 0))
    slots = select_frame_candidates(paths, zone, date(2026, 10, 20), date(2026, 10, 20), hourly)
    assert slots == [[frame(11, 0), frame(11, 50)], [frame(12, 10)], [frame(13, 0)]]
    assert select_frames(paths, zone, date(2026, 10, 20), date(2026, 10, 20), hourly) == [
        frame(11, 0), frame(12, 10), frame(13, 0)
    ]


@pytest.mark.parametrize(
    "camera,name",
    [
        ("../etc", "x_1.jpg"),
        ("Cam", "../../secret_1.jpg"),
        ("Cam", "Other_1.jpg"),
        ("Cam", "Cam_1.txt"),
    ],
)
def test_preview_refuses_anything_but_a_saved_frame(camera: str, name: str) -> None:
    assert frame_check_core_service.preview(camera, 60, "2026", "10", "20", name) is None
