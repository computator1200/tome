import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from db.database import SessionLocal, get_session
from db.models import Job, JobStatus, Volume, VolumeStatus
from models.api import JobOut, ProcessIn
from services.pipeline import request_cancel
from services.worker import enqueue

_ACTIVE = (VolumeStatus.QUEUED.value, VolumeStatus.PROCESSING.value)

router = APIRouter(prefix="/api/volumes", tags=["processing"])


@router.post("/{volume_id}/process")
async def reprocess(volume_id: str, body: ProcessIn, session: AsyncSession = Depends(get_session)):
    """Re-run (or re-configure and re-run) a volume's pipeline."""
    volume = (await session.execute(select(Volume).where(Volume.id == volume_id))).scalar_one_or_none()
    if volume is None:
        raise HTTPException(status_code=404, detail="Volume not found")
    if volume.status in (VolumeStatus.QUEUED.value, VolumeStatus.PROCESSING.value):
        raise HTTPException(status_code=409, detail=f"Already {volume.status}")

    volume.engine_mode = body.engine_mode.value
    volume.batch_size = body.batch_size
    volume.status = VolumeStatus.QUEUED.value

    job = (await session.execute(select(Job).where(Job.volume_id == volume_id))).scalar_one_or_none()
    if job is None:
        job = Job(volume_id=volume_id)
        session.add(job)
    job.status = JobStatus.QUEUED.value
    job.step = ""
    job.current = 0
    job.total = 0
    job.message = "Queued"
    job.error = ""
    await session.commit()

    await enqueue(volume_id)
    return {"message": "Processing queued", "volume_id": volume_id}


@router.post("/{volume_id}/cancel")
async def cancel(volume_id: str, session: AsyncSession = Depends(get_session)):
    """Request a graceful stop. The pipeline halts at its next checkpoint
    (between pages/batches), keeping work done so far so it can be resumed."""
    volume = (await session.execute(select(Volume).where(Volume.id == volume_id))).scalar_one_or_none()
    if volume is None:
        raise HTTPException(status_code=404, detail="Volume not found")
    if volume.status not in _ACTIVE:
        raise HTTPException(status_code=409, detail=f"Volume is not running (status: {volume.status})")

    request_cancel(volume_id)
    job = (await session.execute(select(Job).where(Job.volume_id == volume_id))).scalar_one_or_none()
    if job is not None:
        job.message = "Stopping..."
        await session.commit()
    return {"message": "Stopping", "volume_id": volume_id}


@router.post("/{volume_id}/resume")
async def resume(volume_id: str, session: AsyncSession = Depends(get_session)):
    """Continue a paused (or failed) volume from its last finished page."""
    volume = (await session.execute(select(Volume).where(Volume.id == volume_id))).scalar_one_or_none()
    if volume is None:
        raise HTTPException(status_code=404, detail="Volume not found")
    if volume.status in _ACTIVE:
        raise HTTPException(status_code=409, detail=f"Already {volume.status}")

    volume.status = VolumeStatus.QUEUED.value
    job = (await session.execute(select(Job).where(Job.volume_id == volume_id))).scalar_one_or_none()
    if job is None:
        job = Job(volume_id=volume_id)
        session.add(job)
    job.status = JobStatus.QUEUED.value
    job.message = "Resuming..."
    job.error = ""
    await session.commit()

    await enqueue(volume_id, resume=True)
    return {"message": "Resuming", "volume_id": volume_id}


@router.get("/{volume_id}/progress")
async def progress(volume_id: str, session: AsyncSession = Depends(get_session)):
    """SSE stream of the Job row. Reads the DB, so a refresh mid-run resumes
    the live bar instead of going blank."""

    volume = (await session.execute(
        select(Volume).where(Volume.id == volume_id))).scalar_one_or_none()
    if volume is None:
        # Streaming "unknown" forever left the client waiting on a volume that
        # will never exist.
        raise HTTPException(status_code=404, detail="Volume not found")

    async def event_generator():
        last = None
        while True:
            async with SessionLocal() as session:
                job = (await session.execute(
                    select(Job).where(Job.volume_id == volume_id)
                )).scalar_one_or_none()
            if job is None:
                snapshot = {"status": "unknown", "step": "", "current": 0, "total": 0, "message": "", "error": ""}
            else:
                snapshot = JobOut.model_validate(job).model_dump()
                if (snapshot["status"] == JobStatus.CANCELLED.value
                        and volume.status == "paused"):
                    # Pausing cancels the job; the volume, and the UI, call it
                    # paused. Reporting cancelled closed the stream and lost the
                    # Resume affordance.
                    snapshot["status"] = "paused"
            if snapshot != last:
                last = snapshot
                yield {"event": "progress", "data": json.dumps(snapshot)}
                if snapshot["status"] in (JobStatus.DONE.value, JobStatus.ERROR.value,
                                          JobStatus.CANCELLED.value):
                    break
            await asyncio.sleep(0.5)

    return EventSourceResponse(event_generator())

# --- whole-series -----------------------------------------------------------
# A series is the unit of work, so starting one is a series-level action: queue
# every volume that is not already running, in reading order.

series_router = APIRouter(prefix="/api/series", tags=["processing"])


@series_router.post("/{series_id}/process")
async def process_series(series_id: str, body: ProcessIn,
                         session: AsyncSession = Depends(get_session)):
    """Queue every volume of a series that is not already queued or running.

    Volumes are queued in `order`, so volume 1 is translated before volume 2 and
    the memory of the earlier one is available to the later one. Finished volumes
    are left alone unless `body.force` is true.
    """
    volumes = (await session.execute(
        select(Volume).where(Volume.series_id == series_id).order_by(Volume.order)
    )).scalars().all()
    if not volumes:
        raise HTTPException(status_code=404, detail="Series has no volumes")

    queued, skipped = [], []
    for volume in volumes:
        if volume.status in (VolumeStatus.QUEUED.value, VolumeStatus.PROCESSING.value):
            skipped.append({"volume_id": volume.id, "label": volume.label, "reason": volume.status})
            continue
        if volume.status == VolumeStatus.DONE.value and not body.force:
            skipped.append({"volume_id": volume.id, "label": volume.label, "reason": "done"})
            continue

        volume.engine_mode = body.engine_mode.value
        volume.batch_size = body.batch_size
        volume.status = VolumeStatus.QUEUED.value

        job = (await session.execute(
            select(Job).where(Job.volume_id == volume.id))).scalar_one_or_none()
        if job is None:
            job = Job(volume_id=volume.id)
            session.add(job)
        job.status = JobStatus.QUEUED.value
        job.step = ""
        job.current = 0
        job.total = 0
        job.message = "Queued"
        job.error = ""
        queued.append({"volume_id": volume.id, "label": volume.label, "order": volume.order})

    await session.commit()
    for item in queued:
        await enqueue(item["volume_id"])

    return {"queued": queued, "skipped": skipped, "total": len(volumes)}
