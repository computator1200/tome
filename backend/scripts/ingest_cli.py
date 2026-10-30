"""Run an "add files" ingest session from the command line (no server needed).

Stages a server-side path and/or URL, starts the master agent with the real
LLM, prints the transcript live, and auto-answers agent questions. Useful for
testing curation without burning translation budget:

  python scripts/ingest_cli.py --path "L:/Books/Some Series [raw]" \
      --note "curate only" --answer "No, do not start translation."
  python scripts/ingest_cli.py --url https://ncode.syosetu.com/nXXXXxx/

Do NOT run while the backend server is running (single-writer SQLite).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402

from db.database import SessionLocal, init_db  # noqa: E402
from db.models import AgentMessage, AgentStatus, Volume  # noqa: E402
from services import agent, ingest, provider  # noqa: E402

DEFAULT_ANSWER = ("No, do not start translation yet — just finish the curation "
                  "report.")


async def _print_new_messages(session_id: str, after: int) -> int:
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(AgentMessage).where(AgentMessage.session_id == session_id,
                                       AgentMessage.seq > after)
            .order_by(AgentMessage.seq)
        )).scalars().all()
    for m in rows:
        after = m.seq
        if m.role == "assistant":
            if m.content:
                print(f"  [assistant] {m.content[:600]}")
            for tc in m.tool_calls or []:
                print(f"    -> {tc['function']['name']}({tc['function']['arguments'][:220]})")
        elif m.role == "tool":
            print(f"    <- {m.tool_name}: {m.content[:300]}")
        else:
            print(f"  [user] {m.content[:200]}")
    return after


async def _follow(session_id: str, timeout: float) -> "object":
    t0, seq = time.time(), 0
    while True:
        seq = await _print_new_messages(session_id, seq)
        if not agent.is_running(session_id):
            seq = await _print_new_messages(session_id, seq)
            return await agent._get(session_id)
        if time.time() - t0 > timeout:
            agent.request_cancel(session_id)
            raise TimeoutError(f"agent run exceeded {timeout:.0f}s")
        await asyncio.sleep(2)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", help="server-side file or directory to ingest")
    ap.add_argument("--url", help="webnovel URL to ingest")
    ap.add_argument("--note", default="", help="note passed to the agent")
    ap.add_argument("--answer", default=DEFAULT_ANSWER,
                    help="auto-answer for any agent question")
    ap.add_argument("--series-id", default=None, help="attach to an existing series")
    ap.add_argument("--timeout", type=float, default=1500)
    args = ap.parse_args()
    if not args.path and not args.url:
        ap.error("need --path and/or --url")

    await init_db()
    await provider.load_from_db()

    upload_id = ingest.new_upload_id()
    names: list[str] = []
    if args.path:
        print(f"staging {args.path} ...")
        names = await asyncio.to_thread(ingest.stage_local_path, upload_id, args.path)
    manifest = await asyncio.to_thread(ingest.extract_and_manifest, upload_id)
    print(f"manifest: {len(manifest['groups'])} image group(s), "
          f"{len(manifest['documents'])} document(s), "
          f"{len(manifest['warnings'])} warning(s)")
    for w in manifest["warnings"]:
        print(f"  ! {w}")

    session_id = await agent.start_ingest_session(
        upload_id, names, args.note, args.series_id, url=args.url)
    print(f"=== agent session {session_id} ===")

    row = await _follow(session_id, args.timeout)
    while row.status == AgentStatus.WAITING_USER.value:
        q = json.loads(row.pending_question or "{}")
        print(f"\n  ?? AGENT ASKS: {q.get('question')}")
        print(f"  >> auto-answer: {args.answer}\n")
        await agent.post_user_message(session_id, args.answer)
        row = await _follow(session_id, args.timeout)

    print(f"\n=== finished: status={row.status} error={row.error or '-'} "
          f"series={row.series_id or '-'} ===")
    if row.series_id:
        async with SessionLocal() as s:
            vols = (await s.execute(
                select(Volume).where(Volume.series_id == row.series_id)
                .order_by(Volume.order)
            )).scalars().all()
            print(f"volumes ({len(vols)}):")
            for v in vols:
                print(f"  #{v.order:>2} {v.label} [{v.kind}] {v.page_count} "
                      f"{'chapters' if v.kind == 'text' else 'pages'} status={v.status}")


if __name__ == "__main__":
    asyncio.run(main())
