"""PDF export: box placement and the on-disk export cache."""
import asyncio
import io
import sys
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routers import export as export_router  # noqa: E402
from services import export  # noqa: E402


def _region(text, x=0.0, y=0.0, w=0.0, h=0.0):
    return SimpleNamespace(english_text=text, japanese_text="原文", order=0,
                           x=x, y=y, width=w, height=h)


def test_boxes_only_when_every_translated_region_has_one():
    full = [_region("a", 10, 10, 20, 20), _region("b", 40, 40, 20, 20), _region("")]
    assert len(export_router._boxes(full)) == 2
    partial = [_region("a", 10, 10, 20, 20), _region("b")]
    assert export_router._boxes(partial) == []


def test_boxed_pdf_builds(monkeypatch, tmp_path):
    buf = io.BytesIO()
    Image.new("RGB", (600, 900), "white").save(buf, "JPEG")

    class Store:
        def get(self, key):
            return buf.getvalue()
    monkeypatch.setattr(export, "get_store", lambda: Store())
    pages = [{"image_key": "p.jpg", "is_text": True, "lines": ["Hello there."],
              "boxes": [{"x": 80, "y": 5, "width": 6, "height": 60,
                         "text": "A long line that does not fit a narrow vertical box."}]}]
    out = tmp_path / "out.pdf"
    export.build_pdf(pages, path=str(out))
    assert out.read_bytes().startswith(b"%PDF")


def test_export_cache_reuses_and_replaces(monkeypatch, tmp_path):
    monkeypatch.setattr(export_router, "_EXPORT_DIR", tmp_path)
    builds = []

    def build(out):
        builds.append(out)
        Path(out).write_bytes(b"data")

    async def go():
        a = await export_router._cached("vol", "pdf-dark", "one", build)
        b = await export_router._cached("vol", "pdf-dark", "one", build)
        c = await export_router._cached("vol", "pdf-dark", "two", build)
        return a, b, c

    a, b, c = asyncio.run(go())
    assert a == b and len(builds) == 2, "same fingerprint must not rebuild"
    assert c.exists() and not a.exists(), "a new fingerprint replaces the old file"
    assert not list(tmp_path.glob("*.tmp"))
