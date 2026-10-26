"""What a read-only (Timelapse Read) user may reach (fork feature)."""

import pytest

from app.web.access import reader_forbidden


@pytest.mark.parametrize(
    "path",
    [
        "/cameras",
        "/cameras/Some_Cam",
        "/cameras/Some_Cam/panel",
        "/cameras/capture-stats",
        "/cameras/partials/list",
        "/images",
        "/images/partials/grid",
        "/images/file/cam/60/1767225600",
        "/timelapses",
        "/timelapses/jobs",
        "/timelapses/5/video",
        "/system/activity",
        "/system/partials/activity",
        "/system/about",
        "/logout",
        "/account/password",
    ],
)
def test_reader_can_view(path: str) -> None:
    assert not reader_forbidden("GET", path)


@pytest.mark.parametrize(
    "path",
    [
        "/cameras/fetch-settings",
        "/cameras/abc123/settings",
        "/images/delete",
        "/images/delete/preview",
        "/timelapses/create",
        "/timelapses/historical",
        "/timelapses/scheduler",
        "/system",
        "/system/users",
        "/system/partials/users",
        "/system/partials/backup-settings",
        "/components",
    ],
)
def test_reader_cannot_open_settings_or_admin_pages(path: str) -> None:
    assert reader_forbidden("GET", path)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/cameras/failures/clear"),
        ("POST", "/cameras/fetch-settings"),
        ("POST", "/cameras/abc/settings"),
        ("DELETE", "/cameras/abc"),
        ("DELETE", "/images/delete"),
        ("POST", "/timelapses/create"),
        ("POST", "/timelapses/historical"),
        ("DELETE", "/timelapses/5"),
        ("POST", "/system/users"),
        ("PUT", "/anything"),
    ],
)
def test_reader_cannot_change_anything(method: str, path: str) -> None:
    assert reader_forbidden(method, path)


def test_reader_can_sign_out_and_change_own_password() -> None:
    assert not reader_forbidden("POST", "/account/password")
    assert not reader_forbidden("GET", "/logout")
