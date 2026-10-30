"""Master-agent endpoints: the "add files" ingest flow, session transcripts
(+ SSE streaming for the live activity feed), user answers to agent questions,
per-series chat, and the global waiting-on-you alerts list."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sse_starlette.sse import EventSourceResponse

from db.database import SessionLocal, get_session
from db.models import AgentMessage, AgentSession, AgentStatus, Series
from logging_config import get_logger
from models.api import (
    AgentMessageIn,
    AgentMessageOut,
    AgentSessionDetail,
    AgentSessionOut,
    IngestOut,
    PendingQuestion,
)
from services import agent, ingest

router = APIRouter(prefix="/api", tags=["agent"])
log = get_logger("agent_api")

from services.documents import DOCUMENT_EXTENSIONS

_UPLOAD_EXTENSIONS = ingest.ARCHIVE_EXTENSIONS | ingest.IMAGE_EXTENSIONS | DOCUMENT_EXTENSIONS


def _session_out(row: AgentSession) -> AgentSessionOut:
    pending = None
    if row.pending_question:
        try:
            pending = PendingQuestion(**json.loads(row.pending_question))
        except (json.JSONDecodeError, TypeError, ValueError):
            pending = PendingQuestion(question=row.pending_question)
    return AgentSessionOut(
        id=row.id, kind=row.kind, status=row.status, title=row.title,
        series_id=row.series_id, pending_question=pending, error=row.error or "",
    )


@router.post("/ingest", response_model=IngestOut, status_code=201)
async def ingest_files(
    files: list[UploadFile] | None = None,
    series_id: str | None = Form(None),
    note: str = Form(""),
    url: str | None = Form(None),
    local_path: str | None = Form(None),
    session: AsyncSession = Depends(get_session),
):
    """Throw anything at the library: browser uploads (archives / images /
    epub / pdf / txt / html / azw3), a server-side path (LAN app — skips
    pushing GBs through the browser), and/or a webnovel URL. Everything is
    staged + manifested deterministically, then the master agent curates."""
    files = files or []
    url = (url or "").strip() or None
    local_path = (local_path or "").strip() or None
    if not files and not url and not local_path:
        raise HTTPException(status_code=400, detail="Provide files, a URL, or a server path")
    if series_id:
        series = (await session.execute(
            select(Series).where(Series.id == series_id)
        )).scalar_one_or_none()
        if series is None:
            raise HTTPException(status_code=404, detail="Series not found")

    upload_id = ingest.new_upload_id()
    names: list[str] = []
    for f in files:
        name = f.filename or "upload.bin"
        from pathlib import Path
        if Path(name).suffix.lower() not in _UPLOAD_EXTENSIONS:
            ingest.cleanup_staging(upload_id)
            raise HTTPException(status_code=400,
                                detail=f"'{name}': only archives (zip/rar/7z/cbz/cbr), "
                                       "documents (epub/pdf/txt/html/azw3) and images "
                                       "are accepted")
        data = await f.read()
        ingest.stage_upload(upload_id, name, data)
        names.append(name)
    if local_path:
        try:
            names += await asyncio.to_thread(ingest.stage_local_path, upload_id, local_path)
        except ValueError as e:
            ingest.cleanup_staging(upload_id)
            raise HTTPException(status_code=400, detail=str(e))

    manifest = await asyncio.to_thread(ingest.extract_and_manifest, upload_id)
    if names and not manifest["groups"] and not manifest["documents"] and not url:
        ingest.cleanup_staging(upload_id)
        raise HTTPException(status_code=400,
                            detail="No images or readable documents found in the upload"
                                   + (f" ({'; '.join(manifest['warnings'])})"
                                      if manifest["warnings"] else ""))

    session_id = await agent.start_ingest_session(upload_id, names, note, series_id, url=url)
    log.info(f"ingest upload {upload_id}: {len(names)} file(s), url={bool(url)}, "
             f"{len(manifest['groups'])} group(s), {len(manifest['documents'])} document(s) "
             f"-> session {session_id}")
    return IngestOut(session_id=session_id)


@router.get("/agent/sessions/{session_id}", response_model=AgentSessionDetail)
async def get_agent_session(session_id: str, session: AsyncSession = Depends(get_session)):
    row = (await session.execute(
        select(AgentSession).where(AgentSession.id == session_id)
        .options(selectinload(AgentSession.messages))
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Agent session not found")
    base = _session_out(row)
    return AgentSessionDetail(
        **base.model_dump(),
        messages=[AgentMessageOut.model_validate(m) for m in row.messages],
    )


@router.get("/agent/sessions/{session_id}/stream")
async def stream_agent_session(session_id: str, after: int = 0):
    """SSE feed of new messages + status. Closes once the run is no longer
    `running` (the client reopens after answering a question / sending a turn)."""

    async def event_generator():
        last_seq = after
        last_status = None
        while True:
            async with SessionLocal() as s:
                row = (await s.execute(
                    select(AgentSession).where(AgentSession.id == session_id)
                )).scalar_one_or_none()
                if row is None:
                    yield {"event": "error", "data": json.dumps({"detail": "not found"})}
                    return
                new_msgs = (await s.execute(
                    select(AgentMessage)
                    .where(AgentMessage.session_id == session_id, AgentMessage.seq > last_seq)
                    .order_by(AgentMessage.seq)
                )).scalars().all()
            for m in new_msgs:
                last_seq = m.seq
                yield {"event": "message",
                       "data": AgentMessageOut.model_validate(m).model_dump_json()}
            status_snapshot = _session_out(row).model_dump_json()
            if status_snapshot != last_status:
                last_status = status_snapshot
                yield {"event": "status", "data": status_snapshot}
            if row.status != AgentStatus.RUNNING.value:
                return
            await asyncio.sleep(0.7)

    return EventSourceResponse(event_generator())


@router.post("/agent/sessions/{session_id}/message", response_model=AgentSessionOut)
async def post_message(session_id: str, body: AgentMessageIn):
    if not body.content.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    try:
        await agent.post_user_message(session_id, body.content.strip())
    except ValueError:
        raise HTTPException(status_code=404, detail="Agent session not found")
    except agent.AgentBusy:
        raise HTTPException(status_code=409, detail="The agent is still working")
    row = (await agent._get(session_id))  # fresh status after spawn
    return _session_out(row)


@router.post("/agent/sessions/{session_id}/cancel")
async def cancel_session(session_id: str):
    await agent.cancel(session_id)
    return {"message": "Stopping", "session_id": session_id}


@router.delete("/agent/sessions/{session_id}")
async def delete_session(session_id: str, session: AsyncSession = Depends(get_session)):
    row = (await session.execute(
        select(AgentSession).where(AgentSession.id == session_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Agent session not found")
    if agent.is_running(session_id):
        raise HTTPException(status_code=409, detail="Stop the agent before deleting")
    if row.upload_id:
        await asyncio.to_thread(ingest.cleanup_staging, row.upload_id)
    await session.delete(row)
    await session.commit()
    return {"deleted": session_id}


@router.get("/series/{series_id}/chat", response_model=AgentSessionDetail)
async def series_chat(series_id: str, session: AsyncSession = Depends(get_session)):
    series = (await session.execute(
        select(Series).where(Series.id == series_id)
    )).scalar_one_or_none()
    if series is None:
        raise HTTPException(status_code=404, detail="Series not found")
    row = await agent.get_or_create_chat_session(series_id)
    return await get_agent_session(row.id, session)


@router.get("/series/{series_id}/agent/sessions", response_model=list[AgentSessionOut])
async def series_agent_sessions(series_id: str, session: AsyncSession = Depends(get_session)):
    """Every agent session attached to a series, oldest first — the original
    import run(s) plus all chats, so history is never lost."""
    rows = (await session.execute(
        select(AgentSession).where(AgentSession.series_id == series_id)
        .order_by(AgentSession.created_at)
    )).scalars().all()
    return [_session_out(r) for r in rows]


@router.post("/series/{series_id}/chat/new", response_model=AgentSessionDetail)
async def new_chat_session(series_id: str, session: AsyncSession = Depends(get_session)):
    series = (await session.execute(
        select(Series).where(Series.id == series_id)
    )).scalar_one_or_none()
    if series is None:
        raise HTTPException(status_code=404, detail="Series not found")
    from db.models import AgentKind
    row = AgentSession(kind=AgentKind.CHAT.value, status=AgentStatus.DONE.value,
                       series_id=series_id, title="Series chat")
    session.add(row)
    await session.commit()
    return await get_agent_session(row.id, session)


@router.get("/agent/alerts", response_model=list[AgentSessionOut])
async def alerts(session: AsyncSession = Depends(get_session)):
    """Sessions waiting on a user answer — surfaced as banners in the UI."""
    rows = (await session.execute(
        select(AgentSession)
        .where(AgentSession.status == AgentStatus.WAITING_USER.value)
        .order_by(AgentSession.updated_at.desc())
    )).scalars().all()
    return [_session_out(r) for r in rows]
