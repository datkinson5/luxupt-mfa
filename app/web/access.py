"""What a read-only user may reach (fork feature: Timelapse Read role).

A user without ``is_admin`` can look at cameras, images, timelapses and activity and download
timelapses. They cannot change, create or delete anything, and cannot open the settings and
admin pages, some of which show credentials (the Protect API key). Enforced once, in the auth
middleware, for every request, rather than route by route, so a new route is read-only for them
unless listed here.
"""

from __future__ import annotations

import re

from fastapi import Request

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Changes a read-only user may still make: signing out and their own password (local accounts).
READER_WRITABLE = ("/logout", "/account/password")

# Pages and panels a read-only user may not open even to look.
_READER_HIDDEN = re.compile(
    r"^/(?:"
    r"cameras/fetch-settings"
    r"|cameras/[^/]+/settings"
    r"|images/delete"
    r"|timelapses/(?:create|historical|scheduler)"
    r"|components"
    r"|system(?:/?$|/(?!activity|partials/activity|about))"
    r")"
)


def reader_forbidden(method: str, path: str) -> bool:
    """True when a read-only user may not make this request."""
    if method.upper() not in SAFE_METHODS:
        return not path.startswith(READER_WRITABLE)
    return bool(_READER_HIDDEN.match(path))


def can_manage(request: Request) -> bool:
    """For templates: show controls that change things only to administrators."""
    return bool(getattr(request.state, "session_is_admin", False))
