"""Offline smoke test of the master-agent runtime (mocked LLM, real DB/store).

Simulates the full "add files" story against the dev database: stage a
synthetic nested-zip upload -> ingest agent runs a scripted tool sequence
(list_groups, sample_pages, create_series, create_volume, ask_user) ->
suspends -> the user answers -> resumes (enqueue_processing, final report) ->
then one series-chat turn. Everything it creates is deleted afterwards.

Run from backend/:  python scripts/smoke_agent.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import zipfile
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402

from db.database import SessionLocal, init_db  # noqa: E402
from db.models import AgentMessage, AgentSession, AgentStatus, Series, Volume  # noqa: E402
from services import agent, ingest, llm, metadata  # noqa: E402
from storage_backend import get_store  # noqa: E402

# Keep the smoke test offline: the ingest pre-search would otherwise hit
# NovelUpdates/AniList for the fake title.
metadata.search = lambda q: ([], [f"(smoke test: metadata search stubbed for '{q}')"])


def _jpeg() -> bytes:
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (4, 4), "white").save(buf, format="JPEG")
    return buf.getvalue()


def _tc(i: int, name: str, **args) -> dict:
    return {"id": f"call_{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class ScriptedLLM:
    """Returns the next scripted assistant message on each chat_with_tools call."""

    def __init__(self) -> None:
        self.script: list[dict] = []
        self.calls = 0

    def __call__(self, messages, tools, model=None, max_retries=3):
        # Every stored turn must replay into a valid OpenAI conversation:
        # each assistant tool_call id needs a later tool result.
        pending = set()
        for m in messages:
            for tc in (m.get("tool_calls") or []):
                pending.add(tc["id"])
            if m.get("role") == "tool":
                pending.discard(m.get("tool_call_id"))
        assert not pending, f"unanswered tool calls in replayed history: {pending}"
        self.calls += 1
        return self.script.pop(0)


async def main() -> None:
    await init_db()
    store = get_store()

    # --- stage a synthetic messy upload -------------------------------------
    jpg = _jpeg()
    inner = BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        for i in range(1, 6):
            zf.writestr(f"vol01/p{i:03d}.jpg", jpg)
    outer = BytesIO()
    with zipfile.ZipFile(outer, "w") as zf:
        zf.writestr("volume_1.zip", inner.getvalue())

    upload_id = ingest.new_upload_id()
    ingest.stage_upload(upload_id, "Smoke LN v01.zip", outer.getvalue())
    manifest = ingest.extract_and_manifest(upload_id)
    group_id = manifest["groups"][0]["id"]
    print(f"staged upload {upload_id}: groups={[(g['id'], g['image_count']) for g in manifest['groups']]}")

    # --- mock the LLM --------------------------------------------------------
    scripted = ScriptedLLM()
    real_chat, real_describe = llm.chat_with_tools, llm.describe_page
    llm.chat_with_tools = scripted
    llm.describe_page = lambda img, model=None: {
        "content_type": "novel_text", "language": "Japanese",
        "description": "A scanned Japanese book text page."}

    series_id = session_id = None
    try:
        scripted.script = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(1, "list_groups")]},
            {"role": "assistant", "content": "Checking what these pages actually are.",
             "tool_calls": [_tc(2, "sample_pages", group_id=group_id, count=2)]},
            {"role": "assistant", "content": "",
             "tool_calls": [_tc(3, "create_series", title="Smoke Test LN",
                                description="A test series.", source_language="Japanese",
                                target_language="English")]},
            {"role": "assistant", "content": "",
             "tool_calls": [_tc(4, "create_volumes",
                                volumes=[{"label": "Volume 1", "number": 1,
                                          "group_ids": [group_id]}])]},
            {"role": "assistant", "content": "",
             "tool_calls": [_tc(5, "ask_user",
                                question="Volume 1 (5 pages) is ready. Start translation now?",
                                options=["Yes, translate", "Not yet"])]},
        ]
        session_id = await agent.start_ingest_session(
            upload_id, ["Smoke LN v01.zip"], "smoke test", None)
        await agent._active[session_id]  # wait for the run to suspend

        row = await agent._get(session_id)
        assert row.status == AgentStatus.WAITING_USER.value, row.status
        q = json.loads(row.pending_question)
        assert "Start translation" in q["question"] and len(q["options"]) == 2
        assert row.series_id, "create_series should have attached the series"
        series_id = row.series_id
        print(f"suspended on ask_user OK: {q['question']!r}")

        async with SessionLocal() as s:
            vol = (await s.execute(
                select(Volume).where(Volume.series_id == series_id)
            )).scalar_one()
            assert vol.page_count == 5 and vol.status == "uploaded"
            volume_id = vol.id
        print(f"volume created OK: {volume_id} (5 pages, uploaded)")

        # --- user answers -> resume ------------------------------------------
        scripted.script = [
            {"role": "assistant", "content": "",
             "tool_calls": [_tc(6, "enqueue_processing", volume_ids=[volume_id])]},
            {"role": "assistant",
             "content": "Imported 'Smoke Test LN' with Volume 1 (5 pages); translation queued."},
        ]
        await agent.post_user_message(session_id, "Yes, translate")
        await agent._active[session_id]

        row = await agent._get(session_id)
        assert row.status == AgentStatus.DONE.value, (row.status, row.error)
        async with SessionLocal() as s:
            vol = (await s.execute(select(Volume).where(Volume.id == volume_id))).scalar_one()
            assert vol.status == "queued", vol.status
            n_msgs = len((await s.execute(
                select(AgentMessage).where(AgentMessage.session_id == session_id)
            )).scalars().all())
        print(f"resume -> enqueue -> done OK ({n_msgs} messages persisted)")

        # --- one chat turn -----------------------------------------------------
        chat = await agent.get_or_create_chat_session(series_id)
        scripted.script = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(7, "get_series_info")]},
            {"role": "assistant", "content": "The library has Volume 1 with 5 pages (queued)."},
        ]
        await agent.post_user_message(chat.id, "What volumes do we have?")
        await agent._active[chat.id]
        row = await agent._get(chat.id)
        assert row.status == AgentStatus.DONE.value, (row.status, row.error)
        async with SessionLocal() as s:
            msgs = (await s.execute(
                select(AgentMessage).where(AgentMessage.session_id == chat.id)
                .order_by(AgentMessage.seq)
            )).scalars().all()
        assert msgs[-1].content.startswith("The library has Volume 1")
        assert any(m.role == "tool" and "Smoke Test LN" in m.content for m in msgs)
        print("series chat turn OK (tool grounded)")

        print(f"\nOK — agent runtime behaves ({scripted.calls} scripted LLM calls)")
    finally:
        llm.chat_with_tools, llm.describe_page = real_chat, real_describe
        async with SessionLocal() as s:  # clean up everything we created
            if series_id:
                series = (await s.execute(
                    select(Series).where(Series.id == series_id)
                )).scalar_one_or_none()
                if series:
                    for v in (await s.execute(
                        select(Volume).where(Volume.series_id == series_id)
                    )).scalars().all():
                        store.delete_prefix(f"volumes/{v.id}")
                    store.delete_prefix(f"series/{series_id}")
                    await s.delete(series)   # cascades volumes/pages/jobs/agent sessions
            elif session_id:
                row = (await s.execute(
                    select(AgentSession).where(AgentSession.id == session_id)
                )).scalar_one_or_none()
                if row:
                    await s.delete(row)
            await s.commit()
        ingest.cleanup_staging(upload_id)
        print("cleaned up test series/session/staging")


if __name__ == "__main__":
    asyncio.run(main())
