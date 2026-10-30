"""Deterministic ingest core for the AI-first "add files" flow.

Turns messy tracker uploads (nested zips, Shift-JIS filenames, mixed volumes)
into a clean, agent-readable manifest — pure code, no LLM. The agent only makes
the *decisions* (which group is which volume, what series this is); everything
mechanical lives here:

  stage_upload()             save uploaded files into backend/data/staging/{id}/
  extract_and_manifest()     recursively extract zips (encoding-safe), group
                             images by directory, return the manifest dict
  load_manifest()/…          manifest persistence next to the staged files
  extract_group_to_volume()  copy a group's images into the object store and
                             create the Volume + Page rows (same shapes as the
                             manual upload endpoint)
  cleanup_staging()          delete a staging dir when the session ends

Windows note: Python's zipfile decodes member names without the UTF-8 flag as
cp437 (per the ZIP spec), which mangles Shift-JIS names and can produce
characters invalid in Windows paths. We re-decode cp437 -> cp932 -> utf-8 and
sanitize the result, so extraction never fails on naming.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

from natsort import natsorted
from PIL import Image

from config import DATABASE_URL  # noqa: F401  (root anchor comes from config)
from logging_config import get_logger

log = get_logger("ingest")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
# Tracker archives routinely lie about their extension (RARs named .zip), so we
# accept these names but always identify the real format from magic bytes.
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".cbz", ".cbr"}

# Zip-bomb guards: nested archives beyond this depth are listed, not extracted;
# extraction stops (with a warning) past the size cap.
MAX_ZIP_DEPTH = 3
MAX_TOTAL_BYTES = 8 * 1024**3

_root = Path(__file__).resolve().parent.parent  # backend/
STAGING_DIR = _root / "data" / "staging"


# --- archive format detection --------------------------------------------------

def sniff_archive(path: Path) -> str | None:
    """Identify an archive by magic bytes ('zip'|'rar'|'7z'|None). Extensions
    are worthless on tracker rips — most of the failed real-world uploads were
    RAR files renamed to .zip."""
    try:
        head = path.open("rb").read(8)
    except OSError:
        return None
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip"
    if head[:4] == b"Rar!":
        return "rar"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    return None


def _find_7z() -> str | None:
    for name in ("7z", "7za", "7zz"):
        exe = shutil.which(name)
        if exe:
            return exe
    for candidate in (r"C:\Program Files\7-Zip\7z.exe",
                      r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if Path(candidate).exists():
            return candidate
    return None


# --- filename decoding -------------------------------------------------------

def decode_zip_name(info: zipfile.ZipInfo) -> tuple[str, bool]:
    """Best-effort original name for a zip member.

    Returns (name, was_redecoded). If the entry's UTF-8 flag is set the name is
    already correct. Otherwise zipfile gave us cp437; round-trip it and try the
    encodings actually seen in the wild for LN scans (cp932 first).
    """
    if info.flag_bits & 0x800:
        return info.filename, False
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename, False
    for enc in ("cp932", "utf-8"):
        try:
            decoded = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        return decoded, decoded != info.filename
    return info.filename, False


_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


def _sanitize_part(part: str) -> str:
    """Make one path segment safe on Windows (and everywhere else)."""
    part = _BAD_CHARS.sub("_", part).strip().rstrip(". ")
    if part in ("", ".", ".."):
        part = "_"
    return part


def _safe_relpath(name: str) -> Path:
    """Zip member name -> sanitized relative Path (no traversal, no drives)."""
    name = name.replace("\\", "/")
    parts = [_sanitize_part(p) for p in name.split("/") if p not in ("", ".", "..")]
    return Path(*parts) if parts else Path("_")


# --- staging + extraction ----------------------------------------------------

def new_upload_id() -> str:
    return uuid.uuid4().hex[:12]


def staging_path(upload_id: str) -> Path:
    return STAGING_DIR / upload_id


def stage_upload(upload_id: str, filename: str, data: bytes) -> Path:
    """Write one uploaded file into the staging dir (created on demand)."""
    dest_dir = staging_path(upload_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _sanitize_part(Path(filename or "upload.bin").name)
    dest.write_bytes(data)
    return dest


def stage_local_path(upload_id: str, local_path: str) -> list[str]:
    """Copy a server-side file or directory into staging (self-hosted LAN app —
    lets the user point at a folder instead of pushing GBs through the browser).
    Returns the staged top-level names."""
    src = Path(local_path)
    if not src.exists():
        raise ValueError(f"Path not found: {local_path}")
    dest_dir = staging_path(upload_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if src.is_file():
        shutil.copy2(src, dest_dir / _sanitize_part(src.name))
        return [src.name]
    names: list[str] = []
    for item in sorted(src.iterdir()):
        target = dest_dir / _sanitize_part(item.name)
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)
        names.append(item.name)
    return names


class _Budget:
    """Shared byte budget across the whole recursive extraction."""

    def __init__(self, limit: int) -> None:
        self.remaining = limit
        self.exceeded = False

    def take(self, n: int) -> bool:
        if self.remaining - n < 0:
            self.exceeded = True
            return False
        self.remaining -= n
        return True


def _extract_zip_members(zip_path: Path, dest_dir: Path,
                         warnings: list[str], budget: _Budget) -> None:
    """Extract one zip's members into dest_dir (encoding-safe, no recursion)."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        warnings.append(f"'{zip_path.name}' looks like a zip but is corrupt; skipped.")
        return

    redecoded = 0
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name, fixed = decode_zip_name(info)
            redecoded += int(fixed)
            rel = _safe_relpath(name)
            suffix = rel.suffix.lower()
            keep = suffix in IMAGE_EXTENSIONS or suffix in ARCHIVE_EXTENSIONS
            if not keep and info.file_size > 32 * 1024**2:
                continue  # skip huge non-image payloads (video, etc.)
            if not budget.take(info.file_size):
                warnings.append("Size cap reached; some files were not extracted.")
                return
            target = dest_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
            except Exception as e:  # noqa: BLE001 — corrupt members (zlib errors,
                # bad CRCs) are common in old tracker rips; skip the file, keep the rest
                target.unlink(missing_ok=True)  # don't leave a truncated image behind
                warnings.append(f"Could not extract '{name}' from '{zip_path.name}': {e}")
                continue
    if redecoded:
        warnings.append(
            f"{redecoded} filename(s) in '{zip_path.name}' were re-decoded "
            "(Shift-JIS names without the zip UTF-8 flag)."
        )


def _extract_with_7z(archive: Path, dest_dir: Path,
                     warnings: list[str], budget: _Budget) -> None:
    """Extract a RAR/7z archive via a system 7-Zip (NanaZip/7za work too)."""
    exe = _find_7z()
    if exe is None:
        warnings.append(
            f"'{archive.name}' is a RAR/7z archive but no 7-Zip executable was "
            "found — install 7-Zip (or NanaZip) so it is on PATH, then re-upload."
        )
        return
    if not budget.take(archive.stat().st_size):  # floor estimate pre-extraction
        warnings.append("Size cap reached; some files were not extracted.")
        return
    try:
        proc = subprocess.run(
            [exe, "x", "-y", f"-o{dest_dir}", str(archive)],
            capture_output=True, text=True, errors="replace", timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        warnings.append(f"Could not extract '{archive.name}' with 7-Zip: {e}")
        return
    if proc.returncode > 1:  # 0 = ok, 1 = non-fatal warnings
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        warnings.append(f"7-Zip failed on '{archive.name}' "
                        f"(exit {proc.returncode}): {tail[-1] if tail else 'unknown error'}")
        return
    # Charge the real uncompressed size (the pre-check only counted compressed).
    extracted = sum(f.stat().st_size for f in dest_dir.rglob("*") if f.is_file())
    budget.take(max(0, extracted - archive.stat().st_size))


def _extract_archive(archive: Path, dest_dir: Path, depth: int,
                     warnings: list[str], budget: _Budget) -> None:
    """Extract one archive of any supported format (identified by magic bytes,
    never the extension), then recurse into nested archives it produced."""
    if budget.exceeded:
        return
    kind = sniff_archive(archive)
    if kind == "zip":
        _extract_zip_members(archive, dest_dir, warnings, budget)
    elif kind in ("rar", "7z"):
        _extract_with_7z(archive, dest_dir, warnings, budget)
    else:
        warnings.append(f"'{archive.name}' is not a recognized archive "
                        "(not zip/rar/7z); skipped.")
        return

    nested = [p for p in dest_dir.rglob("*")
              if p.is_file() and p.suffix.lower() in ARCHIVE_EXTENSIONS]
    for inner in nested:
        if depth + 1 > MAX_ZIP_DEPTH:
            warnings.append(f"'{inner.name}' is nested deeper than "
                            f"{MAX_ZIP_DEPTH} levels; left unextracted.")
            continue
        inner_dir = inner.with_name(inner.stem + "__x")
        inner_dir.mkdir(exist_ok=True)
        _extract_archive(inner, inner_dir, depth + 1, warnings, budget)
        inner.unlink(missing_ok=True)  # keep staging lean; the tree replaces it


def extract_and_manifest(upload_id: str) -> dict:
    """Extract every staged archive in place, then build the group manifest."""
    root = staging_path(upload_id)
    root.mkdir(parents=True, exist_ok=True)  # URL-only ingests stage no files
    warnings: list[str] = []
    budget = _Budget(MAX_TOTAL_BYTES)

    for item in sorted(root.iterdir()):
        if item.is_file() and item.suffix.lower() in ARCHIVE_EXTENSIONS:
            out_dir = item.with_name(item.stem + "__x")
            out_dir.mkdir(exist_ok=True)
            _extract_archive(item, out_dir, 1, warnings, budget)
            item.unlink(missing_ok=True)

    manifest = _build_manifest(root, warnings)
    manifest["upload_id"] = upload_id
    save_manifest(upload_id, manifest)
    return manifest


def _build_manifest(root: Path, warnings: list[str]) -> dict:
    """Group image files by parent directory (natural-sorted within each) and
    list digital documents (epub/pdf/txt/html/azw3) with probed metadata."""
    from services import documents as docs

    groups: dict[str, list[str]] = {}
    documents: list[dict] = []
    non_images: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            groups.setdefault(rel.parent.as_posix(), []).append(rel.name)
            continue
        if path.suffix.lower() in docs.DOCUMENT_EXTENSIONS or path.suffix.lower() == "":
            # A small .txt used to be assumed to be a release note, which made a
            # short story un-ingestable. Only skip files whose *name* says so.
            tiny = path.stat().st_size < 10_000
            looks_like_notes = any(word in path.name.lower() for word in
                                   ("release", "changelog", "readme", "licence", "license"))
            if not (path.suffix.lower() in (".txt", ".html", ".htm")
                    and tiny and looks_like_notes):
                info = docs.probe_document(path)
                if info:
                    documents.append({"path": rel.as_posix(), **info})
                    continue
        if path.suffix.lower() != ".zip":
            non_images.append(rel.as_posix())
    documents = natsorted(documents, key=lambda d: d["path"])
    for i, d in enumerate(documents, start=1):
        d["id"] = f"d{i}"

    group_list = []
    for i, (dirname, files) in enumerate(natsorted(groups.items()), start=1):
        files = natsorted(files)
        # "__x" suffixes mark directories we synthesized for nested zips —
        # strip them from the label the agent reads (paths keep them).
        label = dirname.replace("__x", "") if dirname != "." else "(top level)"
        group_list.append({
            "id": f"g{i}",
            "dir": dirname,
            "label": label,
            "image_count": len(files),
            "first_files": files[:5],
            "last_files": files[-3:] if len(files) > 5 else [],
        })
    return {
        "groups": group_list,
        "documents": documents,
        "non_image_files": non_images[:50],
        "warnings": warnings,
    }


def save_manifest(upload_id: str, manifest: dict) -> None:
    import json
    (staging_path(upload_id) / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def load_manifest(upload_id: str) -> dict | None:
    import json
    p = staging_path(upload_id) / "manifest.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def group_files(upload_id: str, group_id: str) -> list[Path]:
    """Absolute paths of a group's images, natural-sorted (reading order)."""
    manifest = load_manifest(upload_id)
    if not manifest:
        raise ValueError("Upload staging not found (already cleaned up?)")
    group = next((g for g in manifest["groups"] if g["id"] == group_id), None)
    if group is None:
        known = ", ".join(g["id"] for g in manifest["groups"]) or "none"
        raise ValueError(
            f"Unknown group '{group_id}'. Groups in this upload: {known}. "
            "Call list_groups and use an id it returned.")
    gdir = staging_path(upload_id) / group["dir"]
    files = [p for p in gdir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
    return natsorted(files, key=lambda p: p.name)


def document_path(upload_id: str, doc_id: str) -> Path:
    """Absolute path of a manifest document by its id."""
    manifest = load_manifest(upload_id)
    if not manifest:
        raise ValueError("Upload staging not found (already cleaned up?)")
    doc = next((d for d in manifest.get("documents", []) if d["id"] == doc_id), None)
    if doc is None:
        raise ValueError(f"Unknown document '{doc_id}'")
    return staging_path(upload_id) / doc["path"]


def cleanup_staging(upload_id: str) -> None:
    shutil.rmtree(staging_path(upload_id), ignore_errors=True)


# --- extraction -> text Volume -------------------------------------------------

async def create_text_volume(
    session,           # AsyncSession
    store,             # ObjectStore
    series_id: str,
    label: str,
    number: int,
    extraction: dict,  # services.documents.extract_document shape
) -> "object":
    """Create a kind='text' Volume from a document/webnovel extraction: one Page
    per chapter (image_key='', index = spine index when present so epub repack
    can map back), one Region per text block (order = block index)."""
    from db.models import Page, PageType, Region, Volume, VolumeKind, VolumeStatus

    chapters = extraction["chapters"]
    if not chapters:
        raise ValueError("No chapters to import")
    volume = Volume(
        series_id=series_id, label=label, order=number,
        status=VolumeStatus.UPLOADED.value, kind=VolumeKind.TEXT.value,
        page_count=len(chapters), batch_size=5,
    )
    session.add(volume)
    await session.flush()

    src = extraction.get("source_path")
    if src is not None:
        key = f"volumes/{volume.id}/source.epub"
        import asyncio
        await asyncio.to_thread(store.put, key, Path(src).read_bytes(),
                                "application/epub+zip")
        volume.source_key = key

    for seq, ch in enumerate(chapters):
        page = Page(
            volume_id=volume.id,
            index=ch.get("spine_index", seq),
            image_key="", chapter_title=(ch.get("title") or "")[:300],
            page_type=PageType.TEXT.value,
        )
        session.add(page)
        await session.flush()
        for j, block in enumerate(ch["blocks"]):
            session.add(Region(page_id=page.id, order=j, japanese_text=block))
    await session.commit()
    log.info(f"ingest: created text volume {volume.id} ('{label}') with "
             f"{len(chapters)} chapters")
    return volume


# --- group -> Volume ---------------------------------------------------------

async def extract_group_to_volume(
    session,           # AsyncSession
    store,             # ObjectStore
    series_id: str,
    upload_id: str,
    group_ids: list[str],
    label: str,
    number: int,
    engine_mode: str,
    start: int | None = None,
    end: int | None = None,
) -> "object":
    """Create a Volume (+ Pages) from staged groups. Mirrors the shapes produced
    by the manual upload endpoint so everything downstream (pipeline, reader,
    export) is unchanged. start/end are 1-based inclusive indexes into the
    combined, natural-sorted file list — lets the agent split a folder that
    holds several volumes.
    """
    import asyncio

    from db.models import Page, Volume, VolumeStatus

    files: list[Path] = []
    for gid in group_ids:
        files.extend(group_files(upload_id, gid))
    if start is not None or end is not None:
        files = files[(start or 1) - 1: end if end is not None else len(files)]
    if not files:
        raise ValueError("No image files in the selected group(s)/range")

    volume = Volume(
        series_id=series_id, label=label, order=number,
        status=VolumeStatus.UPLOADED.value, engine_mode=engine_mode,
        batch_size=5, page_count=len(files),
    )
    session.add(volume)
    await session.flush()  # assign volume.id

    for index, path in enumerate(files):
        data = path.read_bytes()
        key = f"volumes/{volume.id}/pages/{index:04d}{path.suffix.lower()}"
        await asyncio.to_thread(store.put, key, data, "image/jpeg")
        try:
            with Image.open(BytesIO(data)) as im:
                w, h = im.size
        except Exception:  # noqa: BLE001
            w = h = 0
        session.add(Page(volume_id=volume.id, index=index, image_key=key, width=w, height=h))
    await session.commit()
    log.info(f"ingest: created volume {volume.id} ('{label}') with {len(files)} pages "
             f"from groups {group_ids}")
    return volume

# --- pdf without a text layer -> scans ----------------------------------------

def render_pdf_group(upload_id: str, doc_id: str, dpi: int = 140) -> dict:
    """Render a PDF's pages to JPEGs and register them as an image group.

    Some PDFs, especially older non-Latin typesetting, carry a text layer whose
    font has no Unicode mapping: extraction returns symbol soup that no amount of
    normalisation can repair. The page images are perfectly legible though, so
    the honest fix is to translate them the way a scan is translated. Rendering
    happens once, on the server, and the result is an ordinary group that the
    existing scan pipeline already understands.
    """
    import subprocess

    root = staging_path(upload_id)
    manifest = load_manifest(upload_id) or {}
    doc = next((d for d in manifest.get("documents", []) if d.get("id") == doc_id), None)
    if doc is None:
        raise ValueError(f"Unknown document '{doc_id}'")

    pdf = root / doc["path"]
    if not pdf.exists():
        raise ValueError(f"'{doc['path']}' is no longer staged")

    out_dir = root / f"__scans_{doc_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not any(out_dir.iterdir()):
        proc = subprocess.run(
            ["pdftoppm", "-r", str(dpi), "-jpeg", "-jpegopt", "quality=88",
             str(pdf), str(out_dir / "page")],
            capture_output=True, timeout=1800,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pdftoppm failed: {proc.stderr.decode()[:300]}")

    pages = natsorted(p.name for p in out_dir.iterdir() if p.is_file())
    if not pages:
        raise RuntimeError("no pages were rendered")

    manifest = extract_and_manifest(upload_id)  # picks the new folder up as a group
    group = next((g for g in manifest["groups"]
                  if g["dir"] == out_dir.relative_to(root).as_posix()), None)
    if group is None:
        raise RuntimeError("rendered pages were not detected as a group")
    return {"group_id": group["id"], "pages": group["image_count"],
            "dir": group["dir"], "dpi": dpi}
