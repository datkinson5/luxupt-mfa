"""Health report (fork): one download that says how captures, timelapses and the system have done.

Built from what the database already records (every capture attempt and its error, every job,
every timelapse and its frame checks, camera online/offline events) plus the recent warnings and
errors kept in memory since the last start. Delivered as a zip:

  summary.txt          the findings in plain English, worst first
  report.json          everything, for analysis or comparing two NAS units
  recent-problems.jsonl  warnings and errors from the log since the last start
  recent-activity.jsonl  the app's recent INFO lines, for context

No passwords, API keys or secrets are included: settings are copied field by field from a list.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.protect_client import shared_session_status
from app.core import config, log_buffer
from app.crud import camera_crud, scheduler_settings_crud
from app.crud.fetch_settings_crud import fetch_settings_crud
from app.models.activity_model import Activity
from app.models.camera_model import Camera
from app.models.capture_model import Capture
from app.models.enum_model import ActivityType, CaptureStatus, JobStatus
from app.models.job_model import Job
from app.models.timelapse_model import Timelapse
from app.utils.time_windows import is_all_day, window_label
from app.utils.timezones import display_zone, to_display

PERIODS = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}
GAP_SLOTS = 3  # a run of 3+ missed captures is reported as a gap
TOP = 25
STUCK_AFTER = timedelta(hours=6)
MAX_CAPTURE_MS = 10 * 60 * 1000

FETCH_FIELDS = (
    "enabled",
    "intervals",
    "default_capture_method",
    "default_rtsp_quality",
    "verify_ssl",
    "max_retries",
    "retry_delay",
    "request_timeout",
    "high_quality_snapshots",
    "rtsp_output_format",
    "rtsp_capture_timeout",
    "rate_limit",
    "rate_limit_buffer",
    "min_offset_seconds",
    "max_offset_seconds",
    "camera_refresh_interval",
    "capture_active_start",
    "capture_active_end",
)
SCHEDULER_SKIP = frozenset({"id", "created_at", "updated_at"})

_IDS = re.compile(r"\b[0-9a-f]{8,}\b|\b[0-9a-f-]{36}\b", re.IGNORECASE)
_NUMBERS = re.compile(r"\d+")
_RUNS = re.compile(r"#(?:[.:,]#)+")


# --- helpers ---------------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return to_display(value).isoformat(timespec="seconds")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if hasattr(value, "value") and not isinstance(value, (int, float, str, bool)):
        return value.value
    return value


def _local(value: datetime | None) -> str | None:
    return None if value is None else f"{to_display(value):%Y-%m-%d %H:%M}"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def error_kind(message: str | None) -> str:
    """An error message with its numbers and ids blanked, so the same fault groups together."""
    text = " ".join((message or "(no message)").split())
    text = _NUMBERS.sub("#", _IDS.sub("#", text))
    return _RUNS.sub("#", text)[:240]


def active_seconds(start: datetime, end: datetime, window: tuple[time, time]) -> float:
    """Seconds between two instants that fall inside the daily active hours (display zone)."""
    if end <= start:
        return 0.0
    if is_all_day(*window):
        return (end - start).total_seconds()
    zone = display_zone()
    first, last = to_display(start).date(), to_display(end).date()
    w_start, w_end = window
    total = 0.0
    day = first - timedelta(days=1)  # a window that wraps past midnight starts the day before
    while day <= last:
        open_at = datetime.combine(day, w_start, tzinfo=zone)
        close_day = day if w_start < w_end else day + timedelta(days=1)
        close_at = datetime.combine(close_day, w_end, tzinfo=zone)
        overlap = (min(end, close_at) - max(start, open_at)).total_seconds()
        if overlap > 0:
            total += overlap
        day += timedelta(days=1)
    return total


@dataclass
class _Series:
    """One camera and interval: the successful capture times and the failures, in order."""

    successes: list[int]
    failures: list[tuple[int, str]]
    methods: Counter[str]
    durations: list[int]


# --- the report ------------------------------------------------------------------------------


async def build_report(db: AsyncSession, hours: int, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since = now - timedelta(hours=hours)
    fetch = await fetch_settings_crud.get_settings(db)
    scheduler = await scheduler_settings_crud.get_settings(db)
    capture_window = (fetch.capture_active_start, fetch.capture_active_end)

    report: dict[str, Any] = {
        "generated_at": _local(now),
        "period": {"hours": hours, "from": _local(since), "to": _local(now)},
        "version": {"version": config.BUILD_VERSION, "commit": config.BUILD_COMMIT},
        "settings": _settings(fetch, scheduler),
    }
    report["cameras"] = await _cameras(db, fetch, since, now, capture_window)
    report["live_capture"] = bool(getattr(fetch, "enabled", True))
    report["jobs"] = await _jobs(db, since, now)
    report["timelapses"] = await _timelapses(db, scheduler, since, now)
    report["connectivity"] = await _connectivity(db, since)
    report["protect_sign_in"] = shared_session_status()
    report["storage"] = await _storage(db, since, hours)
    problems, context = log_buffer.recent()
    report["log"] = _log_summary(problems, since)
    report["findings"] = _findings(report)
    report["_log_lines"] = (problems, context)
    return report


def _settings(fetch: Any, scheduler: Any) -> dict[str, Any]:
    capture = {name: _plain(getattr(fetch, name, None)) for name in FETCH_FIELDS}
    # Set in the container's environment or saved in the settings: either counts.
    capture["api_key_set"] = bool(config.UNIFI_PROTECT_API_KEY or getattr(fetch, "api_key", None))
    capture["protect_login_set"] = bool(
        (config.UNIFI_PROTECT_USERNAME or getattr(fetch, "username", None))
        and (config.UNIFI_PROTECT_PASSWORD or getattr(fetch, "password", None))
    )
    capture["protect_mfa_set"] = bool(config.UNIFI_PROTECT_TOTP_SECRET)
    capture["console_set"] = bool(config.UNIFI_PROTECT_BASE_URL or getattr(fetch, "base_url", None))
    timelapse = {
        column.name: _plain(getattr(scheduler, column.name))
        for column in scheduler.__table__.columns
        if column.name not in SCHEDULER_SKIP
    }
    environment = {
        "capture_offset_seconds": config.CAPTURE_OFFSET_SECONDS,
        "display_zone": str(display_zone()),
        "entra_sign_in": config.ENTRA_ENABLED,
        "log_level": config.LOG_LEVEL,
    }
    return {"capture": capture, "scheduler": timelapse, "environment": environment}


async def _cameras(
    db: AsyncSession,
    fetch: Any,
    since: datetime,
    now: datetime,
    window: tuple[time, time],
) -> list[dict[str, Any]]:
    rows = await db.execute(
        select(
            Capture.camera_safe_name,
            Capture.interval,
            Capture.timestamp,
            Capture.status,
            Capture.error_message,
            Capture.capture_method,
            Capture.capture_duration_ms,
        )
        .where(Capture.timestamp >= int(since.timestamp()))
        .order_by(Capture.timestamp)
    )
    series: dict[tuple[str, int], _Series] = defaultdict(lambda: _Series([], [], Counter(), []))
    for camera, interval, stamp, status, error, method, duration in rows.all():
        entry = series[(camera, interval)]
        if status == CaptureStatus.SUCCESS:
            entry.successes.append(stamp)
            if duration and 0 < duration < MAX_CAPTURE_MS:  # older RTSP rows hold a bad value
                entry.durations.append(duration)
        else:
            entry.failures.append((stamp, error_kind(error)))
        if method is not None:
            entry.methods[_plain(method)] += 1

    # Every camera's first capture ever and last successful one, from the captures themselves:
    # a camera added part-way through the period is only expected from its first capture.
    first_seen = dict(
        (await db.execute(
            select(Capture.camera_safe_name, func.min(Capture.timestamp)).group_by(Capture.camera_safe_name)
        )).all()
    )
    last_ok = dict(
        (await db.execute(
            select(Capture.camera_safe_name, func.max(Capture.timestamp))
            .where(Capture.status == CaptureStatus.SUCCESS)
            .group_by(Capture.camera_safe_name)
        )).all()
    )

    def stamp(value: int | None) -> str | None:
        return None if value is None else _local(datetime.fromtimestamp(value, UTC))

    out = []
    default_intervals = list(getattr(fetch, "intervals", None) or [])
    capture_on = bool(getattr(fetch, "enabled", True))
    listed: dict[str, str] = {}
    for camera in (await db.execute(select(Camera).order_by(Camera.name))).scalars().all():
        listed[camera.safe_name] = camera.name
        intervals = sorted(
            set(camera.enabled_intervals or default_intervals)
            | {i for (name, i) in series if name == camera.safe_name}
        )
        start = since
        if camera.first_discovered_at is not None:
            start = max(start, _aware(camera.first_discovered_at))
        if camera.safe_name in first_seen:
            start = max(start, datetime.fromtimestamp(first_seen[camera.safe_name], UTC))
        else:
            start = now  # never captured: nothing to expect until its first capture
        if not capture_on:
            start = now  # live capture is off on this NAS (it builds from recordings)
        out.append(
            {
                "camera": camera.safe_name,
                "name": camera.name,
                "active": camera.is_active,
                "connected": camera.is_connected,
                "state": camera.state,
                "model": camera.model_key,
                "capture_method": _plain(camera.capture_method),
                "expected_from": _local(start),
                "first_capture_ever": stamp(first_seen.get(camera.safe_name)),
                "first_capture_day": (
                    to_display(datetime.fromtimestamp(first_seen[camera.safe_name], UTC)).date().isoformat()
                    if camera.safe_name in first_seen
                    else None
                ),
                "last_capture": stamp(last_ok.get(camera.safe_name)),
                "intervals": [
                    _interval_stats(series.get((camera.safe_name, i)), i, start, now, window)
                    for i in intervals
                ],
            }
        )

    # Captures under a name no camera has now (a camera renamed or removed in Protect).
    for name in sorted({name for (name, _) in series} - set(listed)):
        # "G6_Pro_Turret" -> "G6_Pro_Turret_7310": a rename in Protect usually adds to the name.
        likely = sorted(n for n in listed if n.startswith(f"{name}_"))
        out.append(
            {
                "camera": name,
                "likely_now": [listed[n] for n in likely],
                "name": f"{name} (not in the camera list: renamed or removed?)",
                "active": False,
                "connected": False,
                "state": "unknown",
                "model": None,
                "capture_method": None,
                "expected_from": None,
                "first_capture_ever": stamp(first_seen.get(name)),
                "last_capture": stamp(last_ok.get(name)),
                "intervals": [
                    _interval_stats(entry, i, now, now, window)
                    for (n, i), entry in sorted(series.items())
                    if n == name
                ],
            }
        )
    return out


def _interval_stats(
    entry: _Series | None, interval: int, start: datetime, now: datetime, window: tuple[time, time]
) -> dict[str, Any]:
    entry = entry or _Series([], [], Counter(), [])
    expected = int(active_seconds(start, now, window) // interval)
    ok, failed = len(entry.successes), len(entry.failures)

    gaps = []
    marks = [int(start.timestamp()), *entry.successes, int(now.timestamp())]
    for a, b in zip(marks, marks[1:], strict=False):
        moment_a = datetime.fromtimestamp(a, UTC)
        moment_b = datetime.fromtimestamp(b, UTC)
        missed = int(active_seconds(moment_a, moment_b, window) // interval) - 1
        if missed >= GAP_SLOTS:
            gaps.append(
                {
                    "from": _local(moment_a),
                    "to": _local(moment_b),
                    "minutes": round((b - a) / 60),
                    "missed": missed,
                }
            )
    gaps.sort(key=lambda g: g["missed"], reverse=True)

    kinds: dict[str, dict[str, Any]] = {}
    for stamp, kind in entry.failures:
        moment = _local(datetime.fromtimestamp(stamp, UTC))
        item = kinds.setdefault(kind, {"error": kind, "count": 0, "first": moment, "last": moment})
        item["count"] += 1
        item["last"] = moment
    errors = sorted(kinds.values(), key=lambda e: e["count"], reverse=True)

    durations = sorted(entry.durations)
    return {
        "interval": interval,
        "expected": expected,
        "captured": ok,
        "failed": failed,
        "missing": max(0, expected - ok),
        "success_rate": round(100 * ok / (ok + failed), 1) if ok + failed else None,
        "coverage": round(100 * min(ok, expected) / expected, 1) if expected else None,
        "methods": dict(entry.methods),
        "capture_ms": {
            "median": durations[len(durations) // 2] if durations else None,
            "p95": durations[int(len(durations) * 0.95)] if durations else None,
        },
        "gaps": gaps[:TOP],
        "gap_count": len(gaps),
        "errors": errors[:TOP],
    }


async def _jobs(db: AsyncSession, since: datetime, now: datetime) -> dict[str, Any]:
    jobs = (await db.execute(select(Job).where(Job.created_at >= since))).scalars().all()
    counts: Counter[str] = Counter(f"{_plain(j.job_type)}:{_plain(j.status)}" for j in jobs)
    failed = [
        {
            "title": j.title,
            "type": _plain(j.job_type),
            "created": _local(j.created_at),
            "error": j.error,
        }
        for j in sorted(jobs, key=lambda j: _aware(j.created_at), reverse=True)
        if j.status == JobStatus.FAILED
    ]
    open_jobs = (
        await db.execute(
            select(Job).where(Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]))
        )
    ).scalars().all()
    stuck = [
        {
            "title": j.title,
            "status": _plain(j.status),
            "created": _local(j.created_at),
            "progress": j.progress,
            "message": j.message,
        }
        for j in open_jobs
        if now - _aware(j.created_at) > STUCK_AFTER
    ]
    errors = Counter(error_kind(j["error"]) for j in failed)
    return {
        "counts": dict(sorted(counts.items())),
        "failed": failed[:TOP * 2],
        "failed_count": len(failed),
        "failure_kinds": [{"error": e, "count": n} for e, n in errors.most_common(TOP)],
        "stuck": stuck,
    }


async def _timelapses(
    db: AsyncSession, scheduler: Any, since: datetime, now: datetime
) -> dict[str, Any]:
    first_day = to_display(since).date()
    yesterday = to_display(now).date() - timedelta(days=1)
    made = (
        await db.execute(
            select(Timelapse).where(Timelapse.timelapse_date >= first_day - timedelta(days=1))
        )
    ).scalars().all()

    daily: dict[tuple[str, int], set[date]] = defaultdict(set)
    rollups, checks = [], []
    for t in made:
        kind = getattr(t, "rollup_kind", None)
        if kind:
            rollups.append(
                {
                    "camera": t.camera_safe_name,
                    "interval": t.interval,
                    "kind": kind,
                    "from": _plain(t.timelapse_date),
                    "to": _plain(t.end_date),
                    "frames": t.frame_count,
                }
            )
        elif t.end_date is None and _plain(t.status) == "completed":
            daily[(t.camera_safe_name, t.interval)].add(t.timelapse_date)
        report = getattr(t, "quality_report", None)
        if report:
            checks.append(
                {
                    "camera": t.camera_safe_name,
                    "interval": t.interval,
                    "date": _plain(t.timelapse_date),
                    "kind": kind or "daily",
                    "checked": report.get("checked"),
                    "flagged": report.get("flagged"),
                    "excluded": report.get("excluded"),
                    "replaced": report.get("replaced"),
                    "mode": "exclude" if report.get("exclude") else "report",
                }
            )

    missing = []
    first_days = dict(
        (await db.execute(
            select(Capture.camera_safe_name, func.min(Capture.capture_date)).group_by(Capture.camera_safe_name)
        )).all()
    )
    from_recordings = _plain(scheduler.source) == "historical"
    if scheduler.enabled and first_day <= yesterday:
        cameras = list(await camera_crud.get_active(db))
        if scheduler.enabled_cameras:
            cameras = [c for c in cameras if c.safe_name in scheduler.enabled_cameras]
        intervals = scheduler.enabled_intervals or sorted({i for (_, i) in daily}) or []
        for camera in cameras:
            if camera.state == "NOT_IN_PROTECT":
                continue  # removed from Protect: its missing days are not news
            for interval in intervals:
                if camera.enabled_intervals and interval not in camera.enabled_intervals:
                    continue
                # From recordings a day can be built whenever the camera existed; from live
                # captures only once it has been captured. Before either, nothing is missing.
                began = first_days.get(camera.safe_name)
                if began is None and from_recordings and camera.first_discovered_at is not None:
                    began = to_display(_aware(camera.first_discovered_at)).date()
                if began is None:
                    continue
                day = max(first_day, began)
                while day <= yesterday:
                    if day not in daily[(camera.safe_name, interval)]:
                        missing.append(
                            {"camera": camera.safe_name, "interval": interval, "date": day.isoformat()}
                        )
                    day += timedelta(days=1)

    return {
        "daily_made": sum(
            1 for days in daily.values() for d in days if first_day <= d <= yesterday
        ),
        "daily_missing": missing,
        "rollups": rollups,
        "frame_checks": sorted(checks, key=lambda c: (c["date"], c["camera"])),
    }


async def _connectivity(db: AsyncSession, since: datetime) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(Activity)
            .where(
                Activity.timestamp >= since,
                Activity.activity_type.in_(
                    [
                        ActivityType.CAMERA_OFFLINE,
                        ActivityType.CAMERA_ONLINE,
                        ActivityType.SERVICE_STARTED,
                        ActivityType.SERVICE_STOPPED,
                        ActivityType.TIMELAPSE_FAILED,
                    ]
                ),
            )
            .order_by(Activity.timestamp)
        )
    ).scalars().all()
    events = [
        {
            "time": _local(a.timestamp),
            "type": _plain(a.activity_type),
            "camera": a.camera_safe_name,
            "message": a.message,
        }
        for a in rows
    ]
    counts = Counter(e["type"] for e in events)
    return {"counts": dict(counts), "events": events[-200:]}


async def _storage(db: AsyncSession, since: datetime, hours: int) -> dict[str, Any]:
    output = config.IMAGE_OUTPUT_PATH.parent
    disk = shutil.disk_usage(output) if output.exists() else None
    total_bytes, total_rows = (
        await db.execute(select(func.coalesce(func.sum(Capture.file_size), 0), func.count(Capture.id)))
    ).one()
    recent_bytes = (
        await db.execute(
            select(func.coalesce(func.sum(Capture.file_size), 0)).where(
                Capture.timestamp >= int(since.timestamp())
            )
        )
    ).scalar_one()
    video_bytes = (
        await db.execute(select(func.coalesce(func.sum(Timelapse.file_size), 0)))
    ).scalar_one()
    from app.db.database import DATABASE_PATH  # noqa: PLC0415 - path only

    per_day = recent_bytes * 24 / hours if hours else 0
    gib = 1024**3
    return {
        "disk_total_gb": round(disk.total / gib, 1) if disk else None,
        "disk_free_gb": round(disk.free / gib, 1) if disk else None,
        "disk_used_percent": round(100 * disk.used / disk.total, 1) if disk else None,
        "images_gb": round(total_bytes / gib, 2),
        "images_stored": total_rows,
        "videos_gb": round(video_bytes / gib, 2),
        "database_mb": round(DATABASE_PATH.stat().st_size / 1024**2, 1)
        if DATABASE_PATH.exists()
        else None,
        "images_per_day_gb": round(per_day / gib, 2),
        "days_until_full": round(disk.free / per_day) if disk and per_day else None,
    }


def _log_summary(problems: list[dict[str, Any]], since: datetime) -> dict[str, Any]:
    cutoff = since.astimezone(UTC).isoformat()
    recent = [p for p in problems if str(p.get("timestamp", "")) >= cutoff[:19]]
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for line in recent:
        key = (
            str(line.get("level", "")),
            str(line.get("logger", "")),
            error_kind(str(line.get("message", ""))),
        )
        item = groups.setdefault(
            key,
            {
                "level": key[0],
                "logger": key[1],
                "message": key[2],
                "count": 0,
                "first": line.get("timestamp"),
                "last": line.get("timestamp"),
            },
        )
        item["count"] += 1
        item["last"] = line.get("timestamp")
    return {
        "since_restart_lines": len(problems),
        "in_period": len(recent),
        "oldest_kept": problems[0].get("timestamp") if problems else None,
        "groups": sorted(groups.values(), key=lambda g: g["count"], reverse=True)[:TOP * 2],
    }


# --- findings --------------------------------------------------------------------------------


def _findings(report: dict[str, Any]) -> list[dict[str, str]]:
    """The things worth a look, worst first: (level, text). Levels: problem, check, ok."""
    found: list[tuple[int, str, str]] = []

    def add(rank: int, level: str, text: str) -> None:
        found.append((rank, level, text))

    for camera in report["cameras"]:
        if camera["state"] == "unknown":
            n = sum(s["captured"] for s in camera["intervals"])
            now_named = camera.get("likely_now") or []
            hint = (
                f"probably renamed to {' / '.join(now_named)} in Protect"
                if now_named
                else "renamed or removed in Protect?"
            )
            add(3, "check", f"{n} captures saved under \"{camera['camera']}\" until {camera['last_capture']} ({hint}).")
            continue
        if not camera["active"]:
            continue
        label = camera["name"]
        expecting = any(s["expected"] for s in camera["intervals"])
        if not camera["connected"]:
            # One finding for a camera that is down; its coverage and gaps say the same thing.
            add(0, "problem", f"{label}: not connected to Protect now (last capture {camera['last_capture']}).")
            continue
        if report.get("live_capture", True) and camera["first_capture_ever"] is None:
            # Connected but never captured at all: what a camera adopted while the service
            # ran looked like before the periodic camera sync.
            add(0, "problem", f"{label}: connected but has never been captured.")
            continue
        if expecting and not any(s["captured"] for s in camera["intervals"]):
            add(0, "problem", f"{label}: connected but nothing captured in the period (last capture {camera['last_capture']}).")
            continue
        for stats in camera["intervals"]:
            cov = stats["coverage"]
            name = f"{label} {stats['interval']}s"
            if stats["expected"] and cov is not None and cov < 90:
                add(1, "problem", f"{name}: only {cov}% of expected captures ({stats['captured']} of {stats['expected']}).")
            elif stats["expected"] and cov is not None and cov < 99:
                add(3, "check", f"{name}: {cov}% of expected captures ({stats['missing']} missing).")
            if stats["gaps"]:
                worst = stats["gaps"][0]
                add(2, "check", f"{name}: {stats['gap_count']} gap(s); longest {worst['minutes']} min from {worst['from']}.")
            if stats["errors"]:
                top = stats["errors"][0]
                add(4, "check", f"{name}: {stats['failed']} failed captures; most often \"{top['error'][:90]}\" ({top['count']}x).")

    jobs = report["jobs"]
    if jobs["stuck"]:
        add(0, "problem", f"{len(jobs['stuck'])} job(s) pending or running for over 6 hours.")
    if jobs["failed_count"]:
        top = jobs["failure_kinds"][0]["error"][:90] if jobs["failure_kinds"] else ""
        add(2, "check", f"{jobs['failed_count']} failed job(s); most often \"{top}\".")

    missing = report["timelapses"]["daily_missing"]
    if missing:
        add(1, "problem", f"{len(missing)} expected daily timelapse(s) missing (e.g. {missing[0]['camera']} {missing[0]['date']}).")

    for session in report["protect_sign_in"]:
        if session["backing_off_seconds"]:
            add(0, "problem", f"Protect sign-in to {session['console']} is backing off ({session['last_failure']}).")

    offline = report["connectivity"]["counts"].get("camera_offline", 0)
    if offline:
        add(2, "check", f"{offline} camera offline event(s) in the period (see connectivity).")
    restarts = report["connectivity"]["counts"].get("service_started", 0)
    if restarts > 1:
        add(3, "check", f"The service started {restarts} times in the period.")

    storage = report["storage"]
    if storage["disk_used_percent"] is not None and storage["disk_used_percent"] >= 90:
        add(0, "problem", f"Disk {storage['disk_used_percent']}% full ({storage['disk_free_gb']} GB free).")
    if storage["days_until_full"] is not None and storage["days_until_full"] < 60:
        add(1, "check", f"At the current rate the disk fills in about {storage['days_until_full']} days.")

    errors = [g for g in report["log"]["groups"] if g["level"] in ("ERROR", "CRITICAL")]
    if errors:
        add(2, "check", f"{sum(g['count'] for g in errors)} error line(s) in the log; most often \"{errors[0]['message'][:90]}\".")

    if not found:
        add(9, "ok", "Nothing needs attention in this period.")
    return [{"level": level, "text": text} for _, level, text in sorted(found, key=lambda f: f[0])]


# --- the download ----------------------------------------------------------------------------


def _summary_text(report: dict[str, Any], host: str) -> str:
    lines = [
        f"LuxUPT health report: {host}",
        f"Period: {report['period']['from']} to {report['period']['to']} (UK time, {report['period']['hours']} h)",
        f"Version: {report['version']['version']} ({report['version']['commit']})",
        "",
        "FINDINGS",
    ]
    marks = {"problem": "!!", "check": " ?", "ok": "ok"}
    lines += [f"  {marks[f['level']]} {f['text']}" for f in report["findings"]]
    lines += [
        "",
        "CAMERAS"
        + ("" if report.get("live_capture", True) else " (live capture is OFF on this NAS: no captures expected)"),
    ]
    for camera in report["cameras"]:
        state = "connected" if camera["connected"] else "NOT CONNECTED"
        active = "" if camera["active"] else " (inactive)"
        lines.append(
            f"  {camera['name']}{active}: {state}, last capture {camera['last_capture']}, "
            f"counted from {camera['expected_from']}"
        )
        for s in camera["intervals"]:
            cov = "n/a" if s["coverage"] is None else f"{s['coverage']}%"
            lines.append(
                f"    {s['interval']}s: {s['captured']}/{s['expected']} captured ({cov}), "
                f"{s['failed']} failed, {s['gap_count']} gap(s)"
            )
            for e in s["errors"][:3]:
                lines.append(f"      {e['count']}x {e['error'][:110]}")
    t = report["timelapses"]
    lines += [
        "",
        "TIMELAPSES",
        f"  Daily made: {t['daily_made']}, missing: {len(t['daily_missing'])}, roll-ups: {len(t['rollups'])}",
    ]
    for c in t["frame_checks"][-14:]:
        lines.append(f"  Frame checks {c['date']} {c['camera']} {c['kind']}: {c['checked']} checked, flagged {c['flagged']}, {c['mode']}")
    j = report["jobs"]
    lines += ["", "JOBS", f"  {j['counts'] or 'none'}"]
    for f in j["failed"][:10]:
        lines.append(f"  failed {f['created']} {f['title']}: {(f['error'] or '')[:110]}")
    s = report["storage"]
    lines += [
        "",
        "STORAGE",
        f"  Disk: {s['disk_used_percent']}% used, {s['disk_free_gb']} GB free; images {s['images_gb']} GB "
        f"({s['images_per_day_gb']} GB/day), videos {s['videos_gb']} GB, database {s['database_mb']} MB",
        "",
        "LOG (warnings and errors kept since the last start)",
        f"  {report['log']['in_period']} in the period; oldest kept {report['log']['oldest_kept']}",
    ]
    for g in report["log"]["groups"][:15]:
        lines.append(f"  {g['count']}x {g['level']} {g['logger']}: {g['message'][:110]}")
    window = report["settings"]["capture"]
    lines += [
        "",
        f"Capture active hours: {window_label(time.fromisoformat(window['capture_active_start']), time.fromisoformat(window['capture_active_end'])) if window.get('capture_active_start') else 'all day'}",
        "Full detail: report.json. Log lines: recent-problems.jsonl, recent-activity.jsonl.",
    ]
    return "\n".join(lines) + "\n"


def safe_host(host: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9.\-]", "-", host or "luxupt")[:60] or "luxupt"


def package(report: dict[str, Any], host: str) -> tuple[str, bytes]:
    """The report as a zip, and its file name."""
    problems, context = report.pop("_log_lines", ([], []))
    host = safe_host(host)
    report["host"] = host
    stamp = datetime.now(display_zone()).strftime("%Y%m%d-%H%M")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("summary.txt", _summary_text(report, host))
        archive.writestr("report.json", json.dumps(report, indent=2, default=str))
        archive.writestr("recent-problems.jsonl", "".join(json.dumps(p, default=str) + "\n" for p in problems))
        archive.writestr("recent-activity.jsonl", "".join(json.dumps(c, default=str) + "\n" for c in context))
    return f"luxupt-health-{host}-{stamp}.zip", buffer.getvalue()


async def health_report_zip(db: AsyncSession, period: str, host: str) -> tuple[str, bytes]:
    hours = PERIODS.get(period, PERIODS["24h"])
    report = await build_report(db, hours)
    return package(report, host)


__all__ = ["PERIODS", "health_report_zip"]
