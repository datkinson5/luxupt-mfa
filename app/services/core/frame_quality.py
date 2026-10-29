"""Frame checks (fork): spot dark, blurred and rain-spotted frames before they go into a video.

Each check is switched on separately in the scheduler settings, and all are off by default.
With "Leave flagged frames out" off (the default), the checks only report: every frame still
goes into the video and the timelapse records what was found. With it on, flagged frames are
dropped (a roll-up tries the next frame in the same slot instead).

How a frame is scored (Pillow only, on a greyscale copy about 480 px wide):

  brightness  mean grey level, 0 (black) to 255 (white).
  sharpness   variance of the Laplacian (an edge filter): crisp detail gives a high value,
              a soft or smeared picture a low one.
  tiles       the same sharpness for each cell of a 6 x 4 grid over the picture.

Sharpness depends on the scene (a busy site scores higher than an empty field), so blur and
rain are judged against the batch the frame belongs to: the median of that day's (or that
roll-up's) frames, leaving out dark ones. That suits a fixed camera, where most frames show
the same view.

  dark  brightness below the dark level (default 40 of 255): night, or a lens covered.
  blur  sharpness below a share of the typical frame (default 40%): the whole picture has
        gone soft (focus hunting, fog, a wet or dirty lens all over).
  rain  part of the picture has gone soft while the rest is still sharp: at least a share
        (default 25%) of the detailed grid cells are under half their usual sharpness, and
        at least one cell is still near normal. Water drops on the lens look like this.
        Cells with little detail even on a good day (sky, a blank wall) are ignored.

Dark frames are not scored for blur or rain (they are always soft), and with fewer than three
lit frames in a batch there is nothing to compare against, so only the dark check runs.
Limitations: a day that is wet or foggy from start to finish sets its own (low) baseline and
is not flagged; heavy rain streaks in the air rather than drops on the lens are not detected.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageStat

ANALYSIS_WIDTH = 480
GRID_COLUMNS, GRID_ROWS = 6, 4
SOFT_TILE = 0.5  # a cell under half its usual sharpness has gone soft
STILL_SHARP = 0.75  # a cell at three quarters or more is still sharp
TEXTURE_FLOOR = 0.2  # cells under a fifth of the median cell carry too little detail to judge
MIN_BASELINE = 3
EXAMPLES = 6
WORKERS = 2  # Pillow releases the GIL while decoding; two keeps a small NAS responsive

LAPLACIAN = ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128)


class Check(StrEnum):
    DARK = "dark"
    BLUR = "blur"
    RAIN = "rain"

    @property
    def label(self) -> str:
        return {"dark": "Dark", "blur": "Blurred", "rain": "Rain on lens"}[self.value]


@dataclass(frozen=True)
class QualitySettings:
    dark: bool = False
    blur: bool = False
    rain: bool = False
    exclude: bool = False
    dark_level: int = 40
    blur_percent: int = 40
    rain_percent: int = 25

    @property
    def any(self) -> bool:
        return self.dark or self.blur or self.rain

    @property
    def checks(self) -> list[Check]:
        return [c for c in Check if getattr(self, c.value)]

    @classmethod
    def from_scheduler(cls, settings: Any) -> QualitySettings:
        return cls(
            dark=bool(getattr(settings, "quality_dark_enabled", False)),
            blur=bool(getattr(settings, "quality_blur_enabled", False)),
            rain=bool(getattr(settings, "quality_rain_enabled", False)),
            exclude=bool(getattr(settings, "quality_exclude", False)),
            dark_level=int(getattr(settings, "quality_dark_level", 40)),
            blur_percent=int(getattr(settings, "quality_blur_percent", 40)),
            rain_percent=int(getattr(settings, "quality_rain_percent", 25)),
        )


@dataclass(frozen=True)
class FrameMeasure:
    brightness: float
    sharpness: float
    tiles: tuple[float, ...]


@dataclass(frozen=True)
class Baseline:
    sharpness: float
    tiles: tuple[float, ...]
    textured: tuple[bool, ...]


def measure(path: Path) -> FrameMeasure | None:
    """Score one frame; None if it cannot be read (left for ffmpeg to report)."""
    try:
        with Image.open(path) as image:
            image.draft("L", (ANALYSIS_WIDTH, ANALYSIS_WIDTH))  # fast JPEG decode; no-op for PNG
            gray = image.convert("L")
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    if gray.width > ANALYSIS_WIDTH:
        height = max(1, round(gray.height * ANALYSIS_WIDTH / gray.width))
        gray = gray.resize((ANALYSIS_WIDTH, height), Image.Resampling.BILINEAR)
    edges = gray.filter(LAPLACIAN)
    width, height = edges.size
    tiles = tuple(
        ImageStat.Stat(
            edges.crop(
                (
                    col * width // GRID_COLUMNS,
                    row * height // GRID_ROWS,
                    (col + 1) * width // GRID_COLUMNS,
                    (row + 1) * height // GRID_ROWS,
                )
            )
        ).var[0]
        for row in range(GRID_ROWS)
        for col in range(GRID_COLUMNS)
    )
    return FrameMeasure(
        brightness=ImageStat.Stat(gray).mean[0],
        sharpness=ImageStat.Stat(edges).var[0],
        tiles=tiles,
    )


def measure_all(paths: Sequence[Path]) -> list[FrameMeasure | None]:
    if len(paths) < 8:
        return [measure(p) for p in paths]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(measure, paths))


def baseline(measures: Iterable[FrameMeasure | None], dark_level: int) -> Baseline | None:
    """The typical lit frame of a batch, or None when there are too few to compare against."""
    lit = [m for m in measures if m is not None and m.brightness >= dark_level]
    if len(lit) < MIN_BASELINE:
        return None
    count = len(lit[0].tiles)
    tiles = tuple(statistics.median(m.tiles[i] for m in lit) for i in range(count))
    middle = statistics.median(tiles)
    return Baseline(
        sharpness=statistics.median(m.sharpness for m in lit),
        tiles=tiles,
        textured=tuple(t > 0 and t >= middle * TEXTURE_FLOOR for t in tiles),
    )


def soft_share(frame: FrameMeasure, base: Baseline) -> tuple[float, float]:
    """(share of detailed cells gone soft, best cell ratio) against the baseline."""
    ratios = [
        frame.tiles[i] / base.tiles[i]
        for i in range(len(base.tiles))
        if base.textured[i] and i < len(frame.tiles)
    ]
    if not ratios:
        return 0.0, 1.0
    return sum(r < SOFT_TILE for r in ratios) / len(ratios), max(ratios)


def judge(
    frame: FrameMeasure | None, base: Baseline | None, settings: QualitySettings
) -> frozenset[Check]:
    if frame is None:
        return frozenset()
    found: set[Check] = set()
    dark = frame.brightness < settings.dark_level
    if dark:
        if settings.dark:
            found.add(Check.DARK)
        return frozenset(found)
    if base is None:
        return frozenset(found)
    if (
        settings.blur
        and base.sharpness > 0
        and frame.sharpness < base.sharpness * settings.blur_percent / 100
    ):
        found.add(Check.BLUR)
    if settings.rain:
        share, best = soft_share(frame, base)
        if share >= settings.rain_percent / 100 and best >= STILL_SHARP:
            found.add(Check.RAIN)
    return frozenset(found)


@dataclass
class QualityReport:
    """What the checks found in one batch; stored on the timelapse as JSON."""

    exclude: bool
    checks: list[str]
    checked: int = 0
    flagged_frames: int = 0
    excluded: int = 0
    replaced: int = 0
    kept_all: bool = False
    sampled_from: int | None = None
    flagged: dict[str, int] = field(default_factory=dict)
    examples: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def start(cls, settings: QualitySettings) -> QualityReport:
        checks = [c.value for c in settings.checks]
        return cls(
            exclude=settings.exclude,
            checks=checks,
            flagged={c: 0 for c in checks},
            examples={c: [] for c in checks},
        )

    def add(self, path: Path, reasons: frozenset[Check]) -> None:
        self.checked += 1
        if not reasons:
            return
        self.flagged_frames += 1
        for reason in reasons:
            self.flagged[reason.value] = self.flagged.get(reason.value, 0) + 1
            examples = self.examples.setdefault(reason.value, [])
            if len(examples) < EXAMPLES:
                examples.append(path.name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "exclude": self.exclude,
            "checks": self.checks,
            "checked": self.checked,
            "flagged_frames": self.flagged_frames,
            "excluded": self.excluded,
            "replaced": self.replaced,
            "kept_all": self.kept_all,
            "sampled_from": self.sampled_from,
            "flagged": self.flagged,
            "examples": self.examples,
        }


def screen_frames(
    paths: Sequence[Path], settings: QualitySettings
) -> tuple[list[Path], QualityReport]:
    """Check a day's (or a range's) frames. Report-only returns every frame unchanged."""
    report = QualityReport.start(settings)
    measures = measure_all(paths)
    base = baseline(measures, settings.dark_level)
    kept: list[Path] = []
    for path, frame in zip(paths, measures, strict=True):
        reasons = judge(frame, base, settings)
        report.add(path, reasons)
        if not (settings.exclude and reasons):
            kept.append(path)
    if not settings.exclude:
        return list(paths), report
    if len(kept) < 2:
        report.kept_all = True  # leaving them out would leave nothing to encode
        return list(paths), report
    report.excluded = len(paths) - len(kept)
    return kept, report


def screen_rollup(
    candidates: Sequence[Sequence[Path]], settings: QualitySettings, tries: int = 6
) -> tuple[list[Path], QualityReport]:
    """Check a roll-up's picks (one per slot, best first). When leaving flagged frames out,
    a flagged pick is swapped for the next clean candidate in its slot, if there is one."""
    report = QualityReport.start(settings)
    firsts = [slot[0] for slot in candidates if slot]
    measures = measure_all(firsts)
    base = baseline(measures, settings.dark_level)
    chosen: list[Path] = []
    for slot, frame in zip((s for s in candidates if s), measures, strict=True):
        reasons = judge(frame, base, settings)
        report.add(slot[0], reasons)
        if not (settings.exclude and reasons):
            chosen.append(slot[0])
            continue
        for alternative in slot[1:tries]:
            if not judge(measure(alternative), base, settings):
                chosen.append(alternative)
                report.replaced += 1
                break
        else:
            report.excluded += 1
    if settings.exclude and len(chosen) < 2:
        report.kept_all, report.excluded, report.replaced = True, 0, 0
        return firsts, report
    return (chosen if settings.exclude else firsts), report


def sample(paths: Sequence[Path], limit: int) -> list[Path]:
    """Up to ``limit`` frames spread evenly through ``paths`` (kept in order)."""
    if len(paths) <= limit:
        return list(paths)
    step = len(paths) / limit
    return [paths[int(i * step)] for i in range(limit)]


# Reports wait here between the build (which knows the frames) and the job's finalise step
# (which writes the timelapse record), keyed by the output file.
_pending: dict[str, dict[str, Any]] = {}


def remember_report(output_path: Path, report: QualityReport | None) -> None:
    if report is None:
        _pending.pop(str(output_path), None)
    else:
        _pending[str(output_path)] = report.to_dict()


def take_report(output_path: Path) -> dict[str, Any] | None:
    return _pending.pop(str(output_path), None)
