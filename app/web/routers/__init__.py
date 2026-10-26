"""Web routers for the application."""

from app.web.routers.account_router import router as account_router
from app.web.routers.admin_bootstrap_router import router as admin_bootstrap_router
from app.web.routers.cameras_router import router as cameras_router
from app.web.routers.entra_router import router as entra_router
from app.web.routers.images_router import router as images_router
from app.web.routers.pages_router import router as pages_router
from app.web.routers.system_router import router as system_router
from app.web.routers.timelapse_jobs_router import router as timelapse_jobs_router
from app.web.routers.timelapse_scheduler_router import (
    router as timelapse_scheduler_router,
)
from app.web.routers.timelapses_router import router as timelapses_router

__all__ = [
    "account_router",
    "admin_bootstrap_router",
    "cameras_router",
    "entra_router",
    "images_router",
    "pages_router",
    "system_router",
    "timelapse_jobs_router",
    "timelapse_scheduler_router",
    "timelapses_router",
]
