"""Weekly, monthly and yearly roll-up timelapses (fork feature).

A roll-up is one timelapse spanning a period, built from frames already on disk (live captures
or frames fetched from recordings) by picking a few per day, so a week or a month plays in
seconds. Inspired by the weekly/monthly/yearly lapses of Bonn93/unifi-time-machine
(Apache-2.0); this is an independent implementation.

  weekly   Monday-Sunday, built on Monday
  monthly  a calendar month, built on the 1st
  yearly   year to date, rebuilt on the 1st of each month (the full year on 1 January)

Frames are picked either every N minutes (the first frame in each N-minute slot of each day)
or, for N >= 1440, once a day: the frame nearest a target time (default noon), so a long
roll-up has steady light from day to day. Only frames inside the timelapse active hours count.

One roll-up per camera, interval, kind and start date: a rebuild replaces the previous one,
so a month-to-date built by hand is replaced by the full month on the 1st.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

from app.utils.time_windows import in_window

ONCE_A_DAY = 1440


class RollupKind(StrEnum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"

    @property
    def job_type(self) -> str:
        return f"rollup_{self.value}"

    @property
    def label(self) -> str:
        return {"weekly": "Weekly", "monthly": "Monthly", "yearly": "Year to date"}[self.value]


@dataclass(frozen=True)
class RollupPlan:
    """How one kind of roll-up picks its frames and plays them back."""

    kind: RollupKind
    step_minutes: int
    frame_rate: int
    target_time: time
    window_start: time
    window_end: time


def kind_for_job_type(job_type: str) -> RollupKind | None:
    if job_type.startswith("rollup_"):
        try:
            return RollupKind(job_type.removeprefix("rollup_"))
        except ValueError:
            return None
    return None


def _month_start(day: date) -> date:
    return day.replace(day=1)


def scheduled_period(kind: RollupKind, run_day: date) -> tuple[date, date] | None:
    """The period a scheduled run on ``run_day`` builds, or None if this kind is not due."""
    yesterday = run_day - timedelta(days=1)
    if kind is RollupKind.WEEKLY:
        if run_day.weekday() != 0:
            return None
        return run_day - timedelta(days=7), yesterday
    if run_day.day != 1:
        return None
    if kind is RollupKind.MONTHLY:
        return _month_start(yesterday), yesterday
    return date(yesterday.year, 1, 1), yesterday


def latest_period(kind: RollupKind, today: date) -> tuple[date, date]:
    """The period "Build roll-ups now" builds: the last full week, the month to date and the
    year to date, each ending yesterday (the last complete day)."""
    yesterday = today - timedelta(days=1)
    if kind is RollupKind.WEEKLY:
        this_monday = today - timedelta(days=today.weekday())
        return this_monday - timedelta(days=7), this_monday - timedelta(days=1)
    if kind is RollupKind.MONTHLY:
        return _month_start(yesterday), yesterday
    return date(yesterday.year, 1, 1), yesterday


def frame_moment(path: Path, zone: ZoneInfo) -> datetime | None:
    """When a frame was captured, from its ``{camera}_{unix seconds}.{ext}`` name."""
    try:
        seconds = int(path.stem.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, UTC).astimezone(zone)


def select_frame_candidates(
    paths: Iterable[Path],
    zone: ZoneInfo,
    start: date,
    end: date,
    plan: RollupPlan,
) -> list[list[Path]]:
    """The frames a roll-up could use for each slot, in slot order, best first.

    A slot is one N-minute step of a day (candidates in time order) or, once a day, the
    whole day (candidates nearest the target time first). The first of each is the pick;
    the rest stand in when the frame checks leave the pick out.
    """
    dated: list[tuple[datetime, Path]] = []
    for path in paths:
        moment = frame_moment(path, zone)
        if moment is None or not start <= moment.date() <= end:
            continue
        if not in_window(moment.time(), plan.window_start, plan.window_end):
            continue
        dated.append((moment, path))
    dated.sort(key=lambda item: item[0])

    slots: dict[tuple[date, int], list[tuple[float, Path]]] = {}
    once_a_day = plan.step_minutes >= ONCE_A_DAY
    step = max(1, plan.step_minutes)
    for moment, path in dated:
        if once_a_day:
            target = datetime.combine(moment.date(), plan.target_time, tzinfo=zone)
            key = (moment.date(), 0)
            rank = abs((moment - target).total_seconds())
        else:
            key = (moment.date(), (moment.hour * 60 + moment.minute) // step)
            rank = moment.timestamp()
        slots.setdefault(key, []).append((rank, path))
    # Stable sort: equal distances keep time order, so the earlier frame wins a tie.
    return [[p for _, p in sorted(slots[key], key=lambda item: item[0])] for key in sorted(slots)]


def select_frames(
    paths: Iterable[Path],
    zone: ZoneInfo,
    start: date,
    end: date,
    plan: RollupPlan,
) -> list[Path]:
    """The frames a roll-up uses, in time order."""
    return [slot[0] for slot in select_frame_candidates(paths, zone, start, end, plan)]


def output_name(camera: str, interval: int, kind: RollupKind, start: date, end: date) -> str:
    return f"{camera}_{start:%Y%m%d}_to_{end:%Y%m%d}_{interval}s_{kind.value}.mp4"
