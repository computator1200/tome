"""The volume dispatcher: concurrency limit, and one volume per series at a time."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import provider, worker  # noqa: E402


def test_dispatch_respects_limit_and_series(monkeypatch):
    base = provider.get()
    monkeypatch.setattr(provider, "get", lambda: dict(base, volume_concurrency=2))
    started = []

    async def fake(volume_id, resume):
        started.append(volume_id)
        await asyncio.sleep(0)

    monkeypatch.setattr(worker, "_process", fake)

    async def go():
        waiting = [("a1", False, "A"), ("a2", False, "A"), ("b1", False, "B"), ("c1", False, "C")]
        active = {}
        worker._dispatch(waiting, active)
        await asyncio.gather(*active)
        return waiting, sorted(active.values())

    waiting, running = asyncio.run(go())
    assert running == ["A", "B"], "two at once, and never two of series A"
    assert [w[0] for w in waiting] == ["a2", "c1"], "the rest keep their order"
