"""Lock — job_crud.cancel_job claims the job in the statement that checks it.

A cancel races the worker finishing the same job. Read-then-write lets the cancel act on a
stale RUNNING status and overwrite COMPLETED (bite: the session's identity map still holds
the row it loaded before the worker committed). The single conditional UPDATE matches no
row once the job is terminal, so the cancel loses and the completed job keeps its status.
"""

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.crud.job_crud import job_crud
from app.models.enum_model import JobStatus
from app.models.job_model import Job

Maker = async_sessionmaker[AsyncSession]


def _job(job_id: str, status: JobStatus) -> Job:
    return Job(
        job_id=job_id,
        title="t",
        camera_safe_name="front_door",
        target_date=date(2026, 10, 1),
        interval=60,
        status=status,
        created_at=datetime.now(UTC),
    )


async def test_cancel_pending_job(db: AsyncSession) -> None:
    db.add(_job("j-pending", JobStatus.PENDING))
    await db.flush()

    job = await job_crud.cancel_job(db, "j-pending")

    assert job is not None
    assert job.status == JobStatus.CANCELLED
    assert job.message == "Cancelled"
    assert job.pid is None
    assert job.completed_at is not None


async def test_cancel_terminal_or_missing_job_is_refused(db: AsyncSession) -> None:
    db.add(_job("j-done", JobStatus.COMPLETED))
    await db.flush()

    assert await job_crud.cancel_job(db, "j-done") is None
    assert await job_crud.cancel_job(db, "no-such-job") is None
    status = await db.scalar(select(Job.status).where(Job.job_id == "j-done"))
    assert status == JobStatus.COMPLETED


async def test_cancel_loses_to_a_completion_it_has_not_seen(durable_db: Maker) -> None:
    async with durable_db() as seed:
        seed.add(_job("j-race", JobStatus.RUNNING))
        await seed.commit()

    async with durable_db() as canceller:
        # The canceller has already loaded the job while it was RUNNING ...
        loaded = await job_crud.get_by_job_id(canceller, "j-race")
        assert loaded is not None
        await canceller.commit()

        # ... then the worker completes it on another connection.
        async with durable_db() as worker:
            running = await job_crud.get_by_job_id(worker, "j-race")
            assert running is not None
            running.status = JobStatus.COMPLETED
            await worker.commit()

        assert await job_crud.cancel_job(canceller, "j-race") is None
        await canceller.commit()

    async with durable_db() as reader:
        status = await reader.scalar(select(Job.status).where(Job.job_id == "j-race"))
        assert status == JobStatus.COMPLETED
