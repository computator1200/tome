"""Agent tools with real consequences: deletion needs the user's yes, in code."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import Base  # noqa: E402
from db.models import AgentMessage, AgentSession, Series, Volume  # noqa: E402
from services import agent_tools  # noqa: E402


class _Store:
    def delete_prefix(self, prefix):
        pass


def _setup(monkeypatch, transcript):
    """A chat session with this transcript of (role, content, asked) turns, where
    `asked` is the ask_user question an assistant turn asked, if any."""
    path = Path(tempfile.mkdtemp()) / "agent.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}", poolclass=NullPool)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(agent_tools, "SessionLocal", Session)
    monkeypatch.setattr(agent_tools, "get_store", lambda: _Store())

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as s:
            series = Series(title="T")
            s.add(series)
            await s.flush()
            vol = Volume(series_id=series.id, label="Volume 3", order=3)
            chat = AgentSession(kind="chat", series_id=series.id)
            s.add_all([vol, chat])
            await s.flush()
            for seq, (role, content, asked) in enumerate(transcript):
                calls = None
                if asked:
                    calls = [{"id": f"c{seq}", "type": "function", "function": {
                        "name": "ask_user", "arguments": json.dumps({"question": asked})}}]
                s.add(AgentMessage(session_id=chat.id, seq=seq, role=role, content=content,
                                   tool_calls=calls))
            await s.commit()
            return agent_tools.ToolContext(session_id=chat.id, kind="chat",
                                           series_id=series.id), vol.id

    return asyncio.run(go())


def _delete(ctx, vid):
    return asyncio.run(agent_tools._delete_volume(ctx, {"volume_id": vid}))


def test_delete_without_asking_is_refused(monkeypatch):
    ctx, vid = _setup(monkeypatch, [("user", "delete volume 3", None)])
    assert _delete(ctx, vid).startswith("Refused")


def test_delete_after_a_yes_to_a_question_naming_the_volume(monkeypatch):
    ctx, vid = _setup(monkeypatch, [
        ("user", "delete volume 3", None),
        ("assistant", "", "Delete 'Volume 3'? This cannot be undone."),
        ("tool", "(question shown to the user)", None),
        ("user", "Yes, delete it", None)])
    assert '"deleted"' in _delete(ctx, vid)


def test_a_no_or_a_question_about_something_else_is_not_a_yes(monkeypatch):
    ctx, vid = _setup(monkeypatch, [
        ("assistant", "", "Delete 'Volume 3'?"), ("user", "no, keep it", None)])
    assert _delete(ctx, vid).startswith("Refused")
    ctx, vid = _setup(monkeypatch, [
        ("assistant", "", "Rename the series?"), ("user", "yes", None)])
    assert _delete(ctx, vid).startswith("Refused")
