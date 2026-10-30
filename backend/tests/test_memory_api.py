"""The memory endpoints, through the real FastAPI app on a throwaway database."""
import asyncio
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import Base, get_session  # noqa: E402
from db.models import Series, Volume  # noqa: E402
from main import app  # noqa: E402
from services import memory  # noqa: E402


def _client():
    path = Path(tempfile.mkdtemp()) / "api.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}", poolclass=NullPool)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as s:
            series = Series(title="T")
            s.add(series)
            await s.flush()
            s.add_all([Volume(series_id=series.id, order=1), Volume(series_id=series.id, order=2)])
            await memory.record_extraction(s, series.id, 1, {
                "entities": [{"name": "Lizette", "kind": "person"},
                             {"name": "Port Seren", "kind": "place"}],
                "relations": [{"src": "Lizette", "relation": "born_in", "dst": "Port Seren"}]},
                page=0)
            await memory.record_extraction(s, series.id, 2, {
                "entities": [{"name": "Liz", "kind": "person"}]}, page=0)
            await s.commit()
            return series.id

    series_id = asyncio.run(setup())

    async def override():
        async with Session() as s:
            yield s
    app.dependency_overrides[get_session] = override
    return TestClient(app), series_id


def test_memory_view_and_corrections():
    client, sid = _client()
    try:
        view = client.get(f"/api/series/{sid}/memory?volume=1").json()
        assert {e["name"] for e in view["entities"]} == {"Lizette", "Port Seren"}
        assert view["edges"][0]["id"]

        ents = {e["name"]: e for e in client.get(f"/api/series/{sid}/memory/entities").json()}
        r = client.post(f"/api/series/{sid}/memory/merge",
                        json={"keep_id": ents["Lizette"]["id"], "drop_id": ents["Liz"]["id"]})
        assert r.status_code == 200
        assert len(client.get(f"/api/series/{sid}/memory/entities").json()) == 2

        bad = client.post(f"/api/series/{sid}/memory/merge",
                          json={"keep_id": ents["Lizette"]["id"], "drop_id": ents["Lizette"]["id"]})
        assert bad.status_code == 400
        missing = client.delete(f"/api/series/{sid}/memory/edges/nope")
        assert missing.status_code == 404

        edge = view["edges"][0]["id"]
        assert client.delete(f"/api/series/{sid}/memory/edges/{edge}").status_code == 200
        assert client.get(f"/api/series/{sid}/memory").json()["edges"] == []
    finally:
        app.dependency_overrides.clear()
