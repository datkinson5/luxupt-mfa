"""Recent log lines kept in memory for the health report (fork).

The container's own log is the full record, but on a NAS without SSH it is hard to get out and is
lost when the container is recreated. This keeps the most recent warnings and errors (and a
smaller window of the app's own INFO lines, for context) since the last start, formatted exactly
as the main log writes them, so the health report can include them.
"""

from __future__ import annotations

import functools
import json
import logging
import threading
from collections import deque
from typing import Any

PROBLEMS = 5000
CONTEXT = 2000


class _BufferHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.problems: deque[str] = deque(maxlen=PROBLEMS)
        self.context: deque[str] = deque(maxlen=CONTEXT)
        self._guard = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING and not record.name.startswith("app."):
            return
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001 - a log line must never break the caller
            return
        with self._guard:
            (self.problems if record.levelno >= logging.WARNING else self.context).append(line)

    def snapshot(self) -> tuple[list[str], list[str]]:
        with self._guard:
            return list(self.problems), list(self.context)


_handler: _BufferHandler | None = None


def install() -> None:
    """Attach the buffer to the root logger, using the main handler's formatter (JSON, redacted).

    ``configure_logging`` replaces the root logger's handlers, and it runs again after startup
    (the database migrations call it inside the server), which would silently drop the buffer.
    So this also wraps ``configure_logging`` to re-attach the buffer every time it runs, without
    changing the shared logging module itself. Safe to call more than once.
    """
    from app.core import logging_config  # noqa: PLC0415 - avoid an import cycle at load

    current = logging_config.configure_logging
    if not getattr(current, "_keeps_log_buffer", False):

        @functools.wraps(current)
        def configure_and_keep_buffer(*args: Any, **kwargs: Any) -> None:
            current(*args, **kwargs)
            _attach()

        configure_and_keep_buffer._keeps_log_buffer = True  # type: ignore[attr-defined]
        logging_config.configure_logging = configure_and_keep_buffer
    _attach()


def _attach() -> None:
    global _handler  # noqa: PLW0603 - one buffer per process, kept across reconfiguration
    if _handler is None:
        _handler = _BufferHandler()
    root = logging.getLogger()
    main = next(
        (h for h in root.handlers if h is not _handler and h.formatter is not None), None
    )
    if main is not None:
        _handler.setFormatter(main.formatter)
    if _handler not in root.handlers:
        root.addHandler(_handler)


def recent() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(warnings and errors, recent app INFO lines), oldest first, each parsed back to a dict."""
    if _handler is None:
        return [], []
    problems, context = _handler.snapshot()
    return [_parse(line) for line in problems], [_parse(line) for line in context]


def _parse(line: str) -> dict[str, Any]:
    try:
        value = json.loads(line)
    except ValueError:
        return {"message": line}
    return value if isinstance(value, dict) else {"message": line}
