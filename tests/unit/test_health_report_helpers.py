"""Health report helpers (fork): active-hours arithmetic, error grouping, the log buffer."""

from __future__ import annotations

import logging
from datetime import datetime, time

import pytest

from app.core import log_buffer
from app.services.core.health_report_core_service import active_seconds, error_kind, safe_host
from app.utils.timezones import display_zone


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=display_zone())


def test_active_seconds_all_day() -> None:
    assert active_seconds(_at(2, 0), _at(3, 0), (time(0), time(0))) == 86400


def test_active_seconds_daytime_window() -> None:
    window = (time(7, 0), time(18, 0))
    assert active_seconds(_at(2, 0), _at(3, 0), window) == 11 * 3600
    assert active_seconds(_at(2, 17), _at(3, 8), window) == 2 * 3600  # 17-18, 07-08
    assert active_seconds(_at(2, 19), _at(2, 23), window) == 0


def test_active_seconds_window_past_midnight() -> None:
    window = (time(22, 0), time(6, 0))
    assert active_seconds(_at(2, 0), _at(3, 0), window) == 8 * 3600  # 00-06 and 22-24
    assert active_seconds(_at(2, 5), _at(2, 23), window) == 2 * 3600


@pytest.mark.parametrize(
    "message,kind",
    [
        ("Timeout after 15.2s talking to 192.168.1.4", "Timeout after #s talking to #"),
        ("HTTP 429 from /api/cameras/65f1a2b3c4d5e6f7a8b9c0d1/snapshot", "HTTP # from /api/cameras/#/snapshot"),
        (None, "(no message)"),
    ],
)
def test_error_kind_groups_the_same_fault(message: str | None, kind: str) -> None:
    assert error_kind(message) == kind


def test_safe_host() -> None:
    assert safe_host("timelapse1.example.co.uk") == "timelapse1.example.co.uk"
    assert safe_host('x"; rm -rf /') == "x---rm--rf--"
    assert safe_host(None) == "luxupt"


def test_log_buffer_keeps_warnings_and_app_info() -> None:
    log_buffer.install()
    log_buffer.install()  # idempotent
    logging.getLogger("app.unit_test").warning("disk nearly full: %s%%", 91)
    logging.getLogger("app.unit_test").info("built a timelapse")
    logging.getLogger("thirdparty.chatter").info("ignored")
    problems, context = log_buffer.recent()
    assert any("disk nearly full: 91%" in str(p.get("message")) for p in problems)
    assert any("built a timelapse" in str(c.get("message")) for c in context)
    assert not any("ignored" in str(c.get("message")) for c in context)
