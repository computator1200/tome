"""Single in-process background worker.

Jobs live in the DB (the Job table), so progress survives refreshes and restarts.
On startup we re-enqueue anything that was left queued/running (its in-memory
task died with the previous process). One worker drains an asyncio.Queue — more
than enough for a single-node self-hosted app, and avoids a Redis/Celery dep.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from db.database import SessionLocal
from db.models import Job, JobStatus, Volume, VolumeStatus
from logging_config import get_logger
from services.pipeline import process_volume

log = get_logger("worker")

_queue: asyncio.Queue[tuple[str, bool]] = asyncio.Queue()
_worker_task: asyncio.Task | None = None


async def enqueue(volume_id: str, resume: bool = False) -> None:
    await _queue.put((volume_id, resume))
    log.info(f"enqueued volume {volume_id} (resume={resume}, queue size {_queue.qsize()})")


async def _run() -> None:
    log.info("worker loop started")
    while True:
        volume_id, resume = await _queue.get()
        try:
            await process_volume(volume_id, resume=resume)
        except Exception as e:  # noqa: BLE001 — process_volume handles its own errors
            log.error(f"worker error on {volume_id}: {e}")
        finally:
            _queue.task_done()


async def start_worker() -> None:
    """Start the worker and PAUSE any orphaned jobs. Call on app startup.

    A job left queued/running by a dead process is paused (not auto-resumed) so
    a crash or restart never silently burns LLM usage re-running an unfinished
    volume. Progress is preserved; the user chooses when to resume or delete.
    """
    global _worker_task

    async with SessionLocal() as session:
        stmt = select(Volume).join(Job).where(
            Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value])
        )
        orphans = (await session.execute(stmt)).scalars().all()
        for vol in orphans:
            vol.status = VolumeStatus.PAUSED.value
            job = (await session.execute(
                select(Job).where(Job.volume_id == vol.id)
            )).scalar_one()
            job.status = JobStatus.CANCELLED.value
            job.message = "Paused after restart. Resume to continue."
        if orphans:
            await session.commit()
            log.warning(f"paused {len(orphans)} orphaned job(s) after restart "
                        "(resume manually to continue)")

    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_run())
