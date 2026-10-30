"""Verify the deterministic ingest core against synthetic tracker-style uploads.

Builds (in memory) the messy real-world shape: an outer zip holding nested
per-volume zips, one of which uses legacy Shift-JIS filenames without the zip
UTF-8 flag (the case that breaks extraction on Windows), plus a folder that
holds two volumes worth of pages and a stray .txt. Asserts the manifest and
group extraction behave. Run from backend/:  python scripts/test_ingest.py
"""
from __future__ import annotations

import sys
import zipfile
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # cp1252 console vs 巻

from services import ingest  # noqa: E402


class _SJISZipInfo(zipfile.ZipInfo):
    """Writes the member name as raw cp932 bytes WITHOUT the UTF-8 flag —
    exactly what legacy Japanese archivers produce."""

    def _encodeFilenameFlags(self):  # noqa: N802 (zipfile internal API)
        return self.filename.encode("cp932"), self.flag_bits & ~0x800


def _jpeg() -> bytes:
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (4, 4), "white").save(buf, format="JPEG")
    return buf.getvalue()


def _zip(entries: dict[str, bytes], sjis: bool = False) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            if sjis:
                zf.writestr(_SJISZipInfo(name), data)
            else:
                zf.writestr(name, data)
    return buf.getvalue()


def main() -> None:
    jpg = _jpeg()

    vol1 = _zip({f"第1巻/ページ{i:03d}.jpg": jpg for i in range(1, 6)}, sjis=True)
    vol23 = _zip(
        {f"v02-03/page_{i:03d}.jpg": jpg for i in range(1, 11)}
        | {"v02-03/readme.txt": b"junk", "../evil.jpg": jpg}
    )
    outer = _zip({"某ラノベ 第01巻.zip": vol1, "volume_2-3.zip": vol23})

    upload_id = ingest.new_upload_id()
    try:
        ingest.stage_upload(upload_id, "Some LN [raw] v01-03.zip", outer)
        manifest = ingest.extract_and_manifest(upload_id)

        groups = manifest["groups"]
        counts = {g["label"]: g["image_count"] for g in groups}
        print("groups:", counts)
        print("warnings:", manifest["warnings"])

        assert sum(g["image_count"] for g in groups) == 16, counts  # 5 + 10 + 1 escaped
        assert any("第1巻" in g["label"] and g["image_count"] == 5 for g in groups), \
            f"Shift-JIS volume folder missing/mangled: {counts}"
        assert any(g["image_count"] == 10 for g in groups), counts
        assert any("re-decoded" in w for w in manifest["warnings"]), manifest["warnings"]

        # Traversal entry must have been contained inside the staging dir.
        staged = ingest.staging_path(upload_id).resolve()
        for p in staged.rglob("evil.jpg"):
            assert staged in p.resolve().parents, f"path escaped staging: {p}"

        # Natural sort: page_002 before page_010.
        g10 = next(g for g in groups if g["image_count"] == 10)
        files = [p.name for p in ingest.group_files(upload_id, g10["id"])]
        assert files.index("page_002.jpg") < files.index("page_010.jpg"), files

        # Range split: first 4 of the 10-page group.
        sliced = ingest.group_files(upload_id, g10["id"])[:4]
        assert len(sliced) == 4

        print("OK — ingest core behaves (encoding, nesting, grouping, sorting, traversal)")
    finally:
        ingest.cleanup_staging(upload_id)


if __name__ == "__main__":
    main()
