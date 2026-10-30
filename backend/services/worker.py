"""In-process background worker.

Jobs live in the DB (the Job table), so progress survives refreshes and restarts.
On startup anything left queued/running is paused (its in-memory task died with
the previous process). One dispatcher drains an asyncio.Queue and runs up to
`volume_concurrency` volumes at once (Settings, default 1), never two of the
same series: a volume's prompts carry the summaries and memory of the volumes
before it, so a series has to be translated in order. No Redis/Celery needed.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from db.database import SessionLocal
from db.models import Job, JobStatus, Volume, VolumeStatus
from logging_config import get_logger
from services import provider
from services.pipeline import process_volume

log = get_logger("worker")

_queue: asyncio.Queue[tuple[str, bool]] = asyncio.Queue()
_worker_task: asyncio.Task | None = None


async def enqueue(volume_id: str, resume: bool = False) -> None:
    await _queue.put((volume_id, resume))
    log.info(f"enqueued volume {volume_id} (resume={resume}, queue size {_queue.qsize()})")


async def _series_of(volume_id: str) -> str:
    async with SessionLocal() as s:
        sid = (await s.execute(
            select(Volume.series_id).where(Volume.id == volume_id))).scalar_one_or_none()
    return sid or volume_id


async def _process(volume_id: str, resume: bool) -> None:
    try:
        await process_volume(volume_id, resume=resume)
    except Exception as e:  # noqa: BLE001 - process_volume handles its own errors
        log.error(f"worker error on {volume_id}: {e}")


def _dispatch(waiting: list, active: dict) -> None:
    """Start waiting volumes, oldest first, while there is room and their series
    is not already running. A skipped volume keeps its place in the line."""
    limit = provider.get()["volume_concurrency"]
    busy = set(active.values())
    for item in list(waiting):
        if len(active) >= limit:
            return
        volume_id, resume, series_id = item
        if series_id in busy:
            continue
        waiting.remove(item)
        busy.add(series_id)
        active[asyncio.create_task(_process(volume_id, resume))] = series_id


async def _run() -> None:
    log.info("worker loop started")
    waiting: list[tuple[str, bool, str]] = []
    active: dict[asyncio.Task, str] = {}
    getter: asyncio.Task | None = None
    while True:
        if getter is None:
            getter = asyncio.create_task(_queue.get())
        done, _ = await asyncio.wait({getter, *active}, return_when=asyncio.FIRST_COMPLETED)
        if getter in done:
            volume_id, resume = getter.result()
            getter = None
            waiting.append((volume_id, resume, await _series_of(volume_id)))
            _queue.task_done()
        for task in done:
            active.pop(task, None)
        _dispatch(waiting, active)


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
