"""Daily active-hours windows (fork feature).

A window is two wall-clock times in the display zone. Equal start and end means all day, which
is the default, so an unset window changes nothing. An end earlier than the start wraps past
midnight (22:00 -> 06:00).
"""

from __future__ import annotations

from datetime import time

ALL_DAY = (time(0, 0), time(0, 0))


def is_all_day(start: time | None, end: time | None) -> bool:
    return start is None or end is None or start == end


def in_window(moment: time, start: time | None, end: time | None) -> bool:
    """True when the wall-clock ``moment`` falls inside [start, end)."""
    if is_all_day(start, end):
        return True
    assert start is not None and end is not None
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


def window_label(start: time | None, end: time | None) -> str:
    if is_all_day(start, end):
        return "all day"
    assert start is not None and end is not None
    return f"{start:%H:%M}–{end:%H:%M}"


def parse_time_field(value: str | None, default: time) -> time:
    """A form's HH:MM value; blank or malformed keeps the default."""
    text = (value or "").strip()
    if not text:
        return default
    try:
        return time.fromisoformat(text)
    except ValueError:
        return default
