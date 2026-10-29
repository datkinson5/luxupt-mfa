"""Roll-up periods and frame picking, and active-hours windows (fork feature)."""

from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.services.core.rollup_core import (
    RollupKind,
    RollupPlan,
    frame_moment,
    kind_for_job_type,
    latest_period,
    output_name,
    scheduled_period,
    select_frames,
)
from app.utils.time_windows import in_window, is_all_day, parse_time_field, window_label

UK = ZoneInfo("Europe/London")
ALL_DAY = (time(0, 0), time(0, 0))


def plan(step: int, *, window: tuple[time, time] = ALL_DAY, target: time = time(12, 0)) -> RollupPlan:
    return RollupPlan(RollupKind.WEEKLY, step, 24, target, window[0], window[1])


def frames_every(minutes: int, day: date, *, hours: int = 24) -> list[Path]:
    start = datetime.combine(day, time(0, 0), tzinfo=UK)
    return [
        Path(f"/img/Cam_{int(start.timestamp()) + i * minutes * 60}.png")
        for i in range(hours * 60 // minutes)
    ]


def local_times(paths: list[Path]) -> list[str]:
    return [f"{frame_moment(p, UK):%m-%d %H:%M}" for p in paths]


# --- active hours ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "start", "end", "inside"),
    [
        (time(3, 0), time(0, 0), time(0, 0), True),  # all day
        (time(7, 30), time(7, 30), time(17, 30), True),
        (time(17, 30), time(7, 30), time(17, 30), False),  # end is exclusive
        (time(7, 29), time(7, 30), time(17, 30), False),
        (time(23, 0), time(22, 0), time(6, 0), True),  # wraps midnight
        (time(5, 59), time(22, 0), time(6, 0), True),
        (time(12, 0), time(22, 0), time(6, 0), False),
    ],
)
def test_in_window(moment: time, start: time, end: time, inside: bool) -> None:
    assert in_window(moment, start, end) is inside


def test_window_helpers() -> None:
    assert is_all_day(time(0, 0), time(0, 0)) and is_all_day(None, None)
    assert window_label(time(0, 0), time(0, 0)) == "all day"
    assert window_label(time(7, 30), time(17, 0)) == "07:30–17:00"
    assert parse_time_field("07:30", time(0, 0)) == time(7, 30)
    assert parse_time_field("", time(1, 0)) == time(1, 0)
    assert parse_time_field("nonsense", time(1, 0)) == time(1, 0)


# --- periods --------------------------------------------------------------------------------


def test_weekly_runs_on_monday_for_the_previous_week() -> None:
    assert scheduled_period(RollupKind.WEEKLY, date(2026, 11, 2)) == (
        date(2026, 10, 26),
        date(2026, 11, 1),
    )
    assert scheduled_period(RollupKind.WEEKLY, date(2026, 11, 3)) is None


def test_monthly_and_yearly_run_on_the_first() -> None:
    assert scheduled_period(RollupKind.MONTHLY, date(2026, 11, 1)) == (
        date(2026, 10, 1),
        date(2026, 10, 31),
    )
    assert scheduled_period(RollupKind.YEARLY, date(2026, 11, 1)) == (
        date(2026, 1, 1),
        date(2026, 10, 31),
    )
    assert scheduled_period(RollupKind.YEARLY, date(2027, 1, 1)) == (
        date(2026, 1, 1),
        date(2026, 12, 31),
    )
    assert scheduled_period(RollupKind.MONTHLY, date(2026, 11, 2)) is None


def test_build_now_periods_end_yesterday() -> None:
    thursday = date(2026, 10, 29)
    assert latest_period(RollupKind.WEEKLY, thursday) == (date(2026, 10, 19), date(2026, 10, 25))
    assert latest_period(RollupKind.MONTHLY, thursday) == (date(2026, 10, 1), date(2026, 10, 28))
    assert latest_period(RollupKind.YEARLY, thursday) == (date(2026, 1, 1), date(2026, 10, 28))
    # On the 1st, month-to-date means the whole previous month.
    assert latest_period(RollupKind.MONTHLY, date(2026, 11, 1)) == (
        date(2026, 10, 1),
        date(2026, 10, 31),
    )


def test_job_types_and_names() -> None:
    assert RollupKind.WEEKLY.job_type == "rollup_weekly"
    assert kind_for_job_type("rollup_yearly") is RollupKind.YEARLY
    assert kind_for_job_type("live_daily") is None
    assert kind_for_job_type("rollup_daily") is None
    assert (
        output_name("Cam", 60, RollupKind.MONTHLY, date(2026, 10, 1), date(2026, 10, 31))
        == "Cam_20261001_to_20261031_60s_monthly.mp4"
    )


# --- frame picking --------------------------------------------------------------------------


def test_hourly_picks_the_first_frame_of_each_hour() -> None:
    day = date(2026, 7, 1)  # BST
    picked = select_frames(frames_every(1, day), UK, day, day, plan(60))
    assert len(picked) == 24
    assert local_times(picked)[:3] == ["07-01 00:00", "07-01 01:00", "07-01 02:00"]


def test_active_hours_limit_the_frames() -> None:
    day = date(2026, 7, 1)
    picked = select_frames(
        frames_every(5, day), UK, day, day, plan(60, window=(time(7, 30), time(17, 30)))
    )
    times = local_times(picked)
    assert times[0] == "07-01 07:30" and times[-1] == "07-01 17:00"
    assert len(picked) == 11  # hourly slots 07 (from 07:30) to 17 (to 17:25); 17:30 is excluded


def test_once_a_day_picks_the_frame_nearest_the_target_time() -> None:
    days = [date(2026, 10, d) for d in (20, 21, 22)]
    paths = [p for d in days for p in frames_every(7, d)]
    picked = select_frames(paths, UK, days[0], days[-1], plan(1440, target=time(12, 0)))
    assert len(picked) == 3
    for moment in (frame_moment(p, UK) for p in picked):
        assert abs((moment.hour * 60 + moment.minute) - 12 * 60) <= 4


def test_range_bounds_are_local_days() -> None:
    paths = frames_every(60, date(2026, 10, 24)) + frames_every(60, date(2026, 10, 25))
    picked = select_frames(paths, UK, date(2026, 10, 25), date(2026, 10, 25), plan(60))
    # 25 Oct 2026 is the clocks-go-back day: 25 local hours, one frame per slot.
    assert all(f"{frame_moment(p, UK):%d}" == "25" for p in picked)
    assert 23 <= len(picked) <= 25


def test_unparseable_names_are_ignored_and_output_is_sorted() -> None:
    day = date(2026, 7, 1)
    paths = list(reversed(frames_every(30, day))) + [Path("/img/notes.txt"), Path("/img/Cam_x.png")]
    picked = select_frames(paths, UK, day, day, plan(60))
    moments = [frame_moment(p, UK) for p in picked]
    assert moments == sorted(moments) and len(picked) == 24


def test_frame_moment_reads_utc_seconds() -> None:
    moment = frame_moment(Path("/x/Some_Cam_Name_1782950400.jpg"), UK)
    assert moment == datetime(2026, 7, 2, 0, 0, tzinfo=UTC).astimezone(UK)
