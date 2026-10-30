"""Digital document engine: extract translatable text from epub/txt/html/pdf/
azw3, and rebuild ("repack") a translated epub that keeps the original book's
CSS, images, cover and structure — only the text changes, and vertical-RTL
Japanese layout is flipped to horizontal-LTR.

Model mapping (mirrors the scan pipeline so everything downstream works):
  document chapter  -> Page   (kind='text' volume; image_key='', chapter_title)
  text block        -> Region (order = block index; japanese_text = source)
For epubs, Page.index is the SPINE index and Region.order the block index
within that spine doc — `iter_blocks` enumerates deterministically, so repack
can walk the original file and substitute translations by (page.index, order).

azw3/mobi needs Calibre's ebook-convert (converted to epub first); a missing
Calibre surfaces as an honest DocumentError, never a silent skip.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urljoin

from lxml import etree, html as lxml_html

from logging_config import get_logger

log = get_logger("documents")

DOCUMENT_EXTENSIONS = {".epub", ".azw3", ".mobi", ".pdf", ".txt", ".html", ".htm", ".xhtml"}

# Leaf block elements whose text is translated. Blocks containing images are
# left untouched (usually decorative captions baked into the illustration flow).
_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
               "td", "th", "caption", "figcaption", "dt", "dd"}
_SKIP_DESCENDANTS = {"img", "image", "svg"}

_LANG_CODES = {"english": "en", "french": "fr", "german": "de", "spanish": "es",
               "japanese": "ja", "korean": "ko", "chinese": "zh"}


class DocumentError(Exception):
    """A document could not be read/converted — message is user-facing."""


def _localname(el) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def detect_format(path: Path) -> str | None:
    """Identify a document by magic bytes (extensions lie on tracker rips)."""
    try:
        head = path.open("rb").read(80)
    except OSError:
        return None
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        try:
            with zipfile.ZipFile(path) as zf:
                if "mimetype" in zf.namelist() and b"epub" in zf.read("mimetype"):
                    return "epub"
        except (zipfile.BadZipFile, KeyError, OSError):
            pass
        return None  # a plain zip is an archive, not a document
    if head[:4] == b"%PDF":
        return "pdf"
    if len(head) >= 68 and head[60:68] == b"BOOKMOBI":
        return "azw3"
    ext = path.suffix.lower()
    if ext == ".txt":
        return "txt"
    if ext in (".html", ".htm", ".xhtml"):
        return "html"
    if ext in (".azw3", ".mobi"):
        return "azw3"
    return None


# --- epub ----------------------------------------------------------------------

def _opf_name(zf: zipfile.ZipFile) -> str:
    container = etree.fromstring(zf.read("META-INF/container.xml"))
    rootfile = container.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
    if rootfile is None or not rootfile.get("full-path"):
        raise DocumentError("epub has no OPF rootfile")
    return rootfile.get("full-path")


def _parse_opf(zf: zipfile.ZipFile, opf_name: str):
    """Returns (metadata dict, spine doc archive-names)."""
    opf = etree.fromstring(zf.read(opf_name))
    ns = {"opf": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}
    meta = {
        "title": (opf.findtext(".//dc:title", namespaces=ns) or "").strip(),
        "author": (opf.findtext(".//dc:creator", namespaces=ns) or "").strip(),
        "language": (opf.findtext(".//dc:language", namespaces=ns) or "").strip(),
    }
    manifest = {item.get("id"): item.get("href")
                for item in opf.findall(".//opf:manifest/opf:item", ns)}
    base = opf_name.rsplit("/", 1)[0] + "/" if "/" in opf_name else ""
    spine = []
    for itemref in opf.findall(".//opf:spine/opf:itemref", ns):
        href = manifest.get(itemref.get("idref"))
        if href:
            spine.append(unquote(urljoin(base, href)))
    return meta, spine


def _parse_xhtml(data: bytes):
    """Parse a spine doc leniently: XML first (real xhtml), HTML fallback."""
    try:
        return etree.parse(BytesIO(data), etree.XMLParser(recover=True, resolve_entities=False))
    except etree.XMLSyntaxError:
        return etree.ElementTree(lxml_html.fromstring(data))


def iter_blocks(tree) -> list:
    """Deterministic list of translatable leaf-block elements in doc order.
    MUST stay stable between extraction and repack — it is the block index."""
    blocks = []
    for el in tree.iter():
        if _localname(el) not in _BLOCK_TAGS:
            continue
        # leaf blocks only: nested block tags (e.g. li > p) count via the child
        if any(_localname(d) in _BLOCK_TAGS for d in el.iterdescendants()):
            continue
        if any(_localname(d) in _SKIP_DESCENDANTS for d in el.iterdescendants()):
            continue
        text = "".join(el.itertext()).strip()
        if text:
            blocks.append(el)
    return blocks


def _block_text(el) -> str:
    # Ruby annotations (furigana) would duplicate readings into the text; keep
    # only the base text (rb / direct) and drop rt/rp reading elements.
    parts = []
    for node in el.iter():
        if _localname(node) in ("rt", "rp"):
            continue
        if node.text:
            parts.append(node.text)
        if node is not el and node.tail:
            parts.append(node.tail)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def extract_epub(path: Path) -> dict:
    """-> {title, author, language, chapters:[{spine_index, title, blocks:[str]}]}"""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise DocumentError(f"'{path.name}' is not a readable epub: {e}")
    with zf:
        meta, spine = _parse_opf(zf, _opf_name(zf))
        chapters = []
        for i, name in enumerate(spine):
            try:
                tree = _parse_xhtml(zf.read(name))
            except KeyError:
                continue
            els = iter_blocks(tree)
            if not els:
                continue
            title = next((_block_text(e) for e in els if _localname(e).startswith("h")), "")
            chapters.append({"spine_index": i, "title": title[:120],
                             "blocks": [_block_text(e) for e in els]})
    if not chapters:
        raise DocumentError(f"'{path.name}' contains no extractable text")
    return {**meta, "chapters": chapters}


_CSS_OVERRIDE = (b"\n/* tome: translated edition reads horizontally */\n"
                 b"html,body{writing-mode:horizontal-tb !important;"
                 b"-epub-writing-mode:horizontal-tb !important;"
                 b"-webkit-writing-mode:horizontal-tb !important;}\n")


def repack_epub(src: Path, dst: Path, translations: dict[tuple[int, int], str],
                target_language: str, title_override: str | None = None) -> int:
    """Rebuild the epub with translated text. `translations` is keyed by
    (spine_index, block_index). Returns how many blocks were replaced."""
    replaced = 0
    lang_code = _LANG_CODES.get(target_language.lower(), "en")
    with zipfile.ZipFile(src) as zin:
        opf_name = _opf_name(zin)
        _, spine = _parse_opf(zin, opf_name)
        spine_pos = {name: i for i, name in enumerate(spine)}
        with zipfile.ZipFile(dst, "w") as zout:
            # mimetype must be first and stored uncompressed (epub spec)
            zout.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip",
                          compress_type=zipfile.ZIP_STORED)
            for info in zin.infolist():
                if info.is_dir() or info.filename == "mimetype":
                    continue
                data = zin.read(info.filename)
                if info.filename in spine_pos:
                    si = spine_pos[info.filename]
                    tree = _parse_xhtml(data)
                    for j, el in enumerate(iter_blocks(tree)):
                        new_text = translations.get((si, j))
                        if new_text:
                            for child in list(el):
                                el.remove(child)
                            el.text = new_text
                            replaced += 1
                    data = etree.tostring(tree, xml_declaration=True, encoding="utf-8")
                elif info.filename == opf_name:
                    tree = etree.fromstring(data)
                    ns = {"opf": "http://www.idpf.org/2007/opf",
                          "dc": "http://purl.org/dc/elements/1.1/"}
                    spine_el = tree.find(".//opf:spine", ns)
                    if spine_el is not None:
                        spine_el.set("page-progression-direction", "ltr")
                    lang_el = tree.find(".//dc:language", ns)
                    if lang_el is not None:
                        lang_el.text = lang_code
                    if title_override:
                        title_el = tree.find(".//dc:title", ns)
                        if title_el is not None:
                            title_el.text = title_override
                    data = etree.tostring(tree, xml_declaration=True, encoding="utf-8")
                elif info.filename.lower().endswith(".css"):
                    data = data + _CSS_OVERRIDE
                zout.writestr(info.filename, data, compress_type=zipfile.ZIP_DEFLATED)
    return replaced


# --- txt / html / pdf ------------------------------------------------------------

def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp932", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")

_HEADING_RE = re.compile(r"^\s*(第\s*[0-9〇一二三四五六七八九十百]+\s*[章話部巻節]|"
                         r"プロローグ|エピローグ|序章|終章|chapter\s+\d+|part\s+\d+|"
                         r"cap[íi]tulo\s+[ivxlcdm\d]+|parte\s+[ivxlcdm\d]+)", re.IGNORECASE)
_MAX_CHAPTER_BLOCKS = 250
# A page is what the reader gets on one screen. Chapters are allowed to be long,
# but a whole novel in one page is unreadable, so a chapter is split once it
# carries this many characters. 3,500 keeps a page roughly a screen and a half.
_MAX_CHAPTER_CHARS = 3500


def _is_bare_heading(block: str) -> bool:
    """A short line in capitals, of the kind a PDF uses as a chapter title.

    Public-domain scans often set titles as "TRAFALGAR" rather than "Chapter 1",
    so a heading regex alone finds nothing and the whole book lands in one page.
    """
    text = block.strip()
    if not (2 <= len(text) <= 80) or " " not in text and len(text) < 4:
        return False
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 3:
        return False
    return all(c.isupper() for c in letters)


def _chapterize(paragraphs: list[str]) -> list[dict]:
    """Split flat paragraphs into chapters on heading heuristics, with a size
    cap so a heading-less webnovel dump still becomes manageable chunks."""
    chapters: list[dict] = []
    current: dict = {"title": "", "blocks": []}
    for p in paragraphs:
        is_heading = bool(_HEADING_RE.match(p)) or _is_bare_heading(p)
        if is_heading or len(current["blocks"]) >= _MAX_CHAPTER_BLOCKS:
            if current["blocks"]:
                chapters.append(current)
            current = {"title": p[:120] if is_heading else "", "blocks": []}
        current["blocks"].append(p)
    if current["blocks"]:
        chapters.append(current)
    return _split_long_chapters(chapters)


def _cont_title(title: str, first: bool) -> str:
    """Keep a real title on the first piece; only mark continuations that have one."""
    if first or not title:
        return title
    return f"{title} (continued)"


def _split_long_chapters(chapters: list[dict]) -> list[dict]:
    """Cut any oversized chapter into page-sized pieces, in order.

    The title stays on the first piece; the rest are marked as continuations so
    the reader can still tell where it is. Nothing is reordered or dropped.
    """
    out: list[dict] = []
    for ch in chapters:
        blocks = ch.get("blocks") or []
        title = ch.get("title") or ""
        if sum(len(b) for b in blocks) <= _MAX_CHAPTER_CHARS:
            out.append(ch)
            continue
        piece: list[str] = []
        size = 0
        first = True
        for block in blocks:
            if piece and size + len(block) > _MAX_CHAPTER_CHARS:
                out.append({"title": _cont_title(title, first),
                            "blocks": piece,
                            **({"spine_index": ch["spine_index"]} if "spine_index" in ch else {})})
                first = False
                piece, size = [], 0
            piece.append(block)
            size += len(block)
        if piece:
            out.append({"title": _cont_title(title, first),
                        "blocks": piece,
                        **({"spine_index": ch["spine_index"]} if "spine_index" in ch else {})})
    return out


def extract_txt(path: Path) -> dict:
    text = _decode_text(path.read_bytes())
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\r\n\s*\r\n", text) if p.strip()]
    if not paragraphs:  # no blank-line structure: fall back to per-line
        paragraphs = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not paragraphs:
        raise DocumentError(f"'{path.name}' contains no text")
    return {"title": path.stem, "author": "", "language": "",
            "chapters": _chapterize(paragraphs)}


def extract_html(path: Path) -> dict:
    tree = etree.ElementTree(lxml_html.fromstring(path.read_bytes()))
    blocks = [_block_text(e) for e in iter_blocks(tree)]
    if not blocks:
        raise DocumentError(f"'{path.name}' contains no text blocks")
    title = tree.findtext(".//title") or path.stem
    return {"title": title.strip(), "author": "", "language": "",
            "chapters": _chapterize(blocks)}


def _normalise_pdf_text(text: str) -> str:
    """Repair the text a PDF font hands back.

    Many Persian and Arabic PDFs embed fonts whose glyphs map straight into the
    Arabic Presentation Forms block (U+FE70-U+FEFF) or into a private-use area.
    The text is there, it is just not letters: "ﺑﻬﻤﻦ" instead of "بهمن". NFKC
    folds the presentation forms back to letters, and the private-use glyphs are
    dropped because they carry no meaning outside the font that drew them.
    """
    import unicodedata

    text = unicodedata.normalize("NFKC", text or "")
    text = "".join(
        ch for ch in text
        if not (0xE000 <= ord(ch) <= 0xF8FF or 0xF0000 <= ord(ch) <= 0x10FFFF)
    )
    text = text.replace("\u00a0", " ")
    # NFKC turns some joined words into runs of the kashida character.
    return re.sub(r"[\u0640]{2,}", "", text)



def _looks_like_glyph_soup(paragraphs: list[str]) -> bool:
    """True when a PDF's "text" is font glyph noise rather than language.

    Old Persian and Arabic typesetting often ships a font whose glyph ids never
    map back to code points. Extraction then yields runs like ",.3[ o-llrrb" that
    are long enough to look like text but contain almost no letters. Real prose,
    in any script this app handles, is mostly letters, so the ratio separates the
    two cleanly without guessing at a language.
    """
    sample = "".join(paragraphs)[:20000]
    if len(sample) < 400:
        return False
    letters = sum(ch.isalpha() for ch in sample)
    # Prose in every script here runs 0.75 letters or better. Glyph soup sits
    # near 0.4, because most of what the font emits is punctuation and digits.
    return letters / len(sample) < 0.5


def extract_pdf(path: Path) -> dict:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        pages_text = [(page.extract_text() or "") for page in reader.pages]
        pages_text = [_normalise_pdf_text(t) for t in pages_text]
    except Exception as e:  # noqa: BLE001
        raise DocumentError(f"Could not read PDF '{path.name}': {e}")
    paragraphs = []
    for t in pages_text:
        paragraphs.extend(p.strip() for p in re.split(r"\n\s*\n", t) if p.strip())
    if not paragraphs:
        raise DocumentError(
            f"'{path.name}' has no extractable text layer — if it is a scanned PDF, "
            "convert the pages to images and use the scan pipeline instead.")

    if _looks_like_glyph_soup(paragraphs):
        raise DocumentError(
            f"'{path.name}' has a text layer with no usable Unicode mapping: the "
            "characters are symbol soup, not words. The page images are fine, so "
            "call convert_pdf_to_scans and translate it as a scan instead.")

    return {"title": path.stem, "author": "", "language": "",
            "chapters": _chapterize(paragraphs)}


# --- azw3 / mobi (via Calibre) ----------------------------------------------------

def find_calibre() -> str | None:
    exe = shutil.which("ebook-convert")
    if exe:
        return exe
    for candidate in (r"C:\Program Files\Calibre2\ebook-convert.exe",
                      r"C:\Program Files (x86)\Calibre2\ebook-convert.exe"):
        if Path(candidate).exists():
            return candidate
    return None


def convert_to_epub(path: Path) -> Path:
    """azw3/mobi -> epub next to the source (staging dir), via Calibre."""
    exe = find_calibre()
    if exe is None:
        raise DocumentError(
            f"'{path.name}' is a Kindle-format book (azw3/mobi). Converting it needs "
            "Calibre's ebook-convert — install Calibre (calibre-ebook.com), then retry.")
    out = path.with_suffix(".converted.epub")
    try:
        proc = subprocess.run([exe, str(path), str(out)], capture_output=True,
                              text=True, errors="replace", timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DocumentError(f"Calibre conversion failed for '{path.name}': {e}")
    if proc.returncode != 0 or not out.exists():
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise DocumentError(f"Calibre conversion failed for '{path.name}': "
                            f"{tail[-1] if tail else 'unknown error'}")
    return out


# --- unified entry points ----------------------------------------------------------

def extract_document(path: Path) -> dict:
    """Normalized extraction for any supported document.
    -> {format, title, author, language, source_path (epub used for repack, if
        any), chapters:[{spine_index?, title, blocks:[str]}]}"""
    fmt = detect_format(path)
    if fmt == "azw3":
        path = convert_to_epub(path)
        fmt = "epub"
    if fmt == "epub":
        data = extract_epub(path)
        return {"format": "epub", "source_path": path, **data}
    if fmt == "txt":
        return {"format": "txt", "source_path": None, **extract_txt(path)}
    if fmt == "html":
        return {"format": "html", "source_path": None, **extract_html(path)}
    if fmt == "pdf":
        return {"format": "pdf", "source_path": None, **extract_pdf(path)}
    raise DocumentError(f"'{path.name}' is not a supported document format")


def probe_document(path: Path) -> dict | None:
    """Cheap manifest info for one staged file; None when it isn't a document."""
    fmt = detect_format(path)
    if fmt is None:
        return None
    info: dict = {"format": fmt, "name": path.name,
                  "size_mb": round(path.stat().st_size / 1e6, 1)}
    if fmt == "epub":
        try:
            with zipfile.ZipFile(path) as zf:
                meta, spine = _parse_opf(zf, _opf_name(zf))
            info.update(title=meta["title"], author=meta["author"],
                        language=meta["language"], spine_docs=len(spine))
        except Exception as e:  # noqa: BLE001
            info["warning"] = f"unreadable epub metadata: {e}"
    elif fmt == "azw3" and find_calibre() is None:
        info["warning"] = "needs Calibre (ebook-convert) for conversion — not installed"
    return info


# --- from-scratch epub (webnovel / txt / pdf sources have no file to repack) -------

_NAV_TMPL = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Contents</title></head>
<body><nav epub:type="toc"><h1>Contents</h1><ol>{items}</ol></nav></body></html>"""

_CHAPTER_TMPL = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="{lang}">
<head><title>{title}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body><section>{heading}{paras}</section></body></html>"""

_CSS = """body{font-family:serif;line-height:1.6;margin:0 6%;}
h1,h2{font-family:sans-serif;line-height:1.25;}
p{text-indent:1.2em;margin:0 0 .2em 0;}"""


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def generate_epub(dst: Path, title: str, author: str, language: str,
                  chapters: list[dict], cover: bytes | None = None) -> None:
    """Build a clean EPUB3 from chapters of plain-text blocks."""
    lang = _LANG_CODES.get(language.lower(), "en")
    with zipfile.ZipFile(dst, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0" encoding="utf-8"?>\n'
                   '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/style.css", _CSS)

        manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                    '<item id="css" href="style.css" media-type="text/css"/>']
        spine, nav_items = [], []
        if cover:
            z.writestr("OEBPS/cover.jpg", cover)
            manifest.append('<item id="cover-img" href="cover.jpg" '
                            'media-type="image/jpeg" properties="cover-image"/>')
        for i, ch in enumerate(chapters):
            name = f"ch{i:04d}.xhtml"
            ch_title = ch.get("title") or f"Chapter {i + 1}"
            heading = f"<h2>{_esc(ch_title)}</h2>" if ch.get("title") else ""
            paras = "".join(f"<p>{_esc(b)}</p>" for b in ch["blocks"])
            z.writestr(f"OEBPS/{name}", _CHAPTER_TMPL.format(
                lang=lang, title=_esc(ch_title), heading=heading, paras=paras))
            manifest.append(f'<item id="c{i}" href="{name}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="c{i}"/>')
            nav_items.append(f'<li><a href="{name}">{_esc(ch_title)}</a></li>')

        z.writestr("OEBPS/nav.xhtml", _NAV_TMPL.format(items="".join(nav_items)))
        z.writestr("OEBPS/content.opf", f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">urn:tome:{_esc(title)}</dc:identifier>
<dc:title>{_esc(title)}</dc:title><dc:creator>{_esc(author)}</dc:creator>
<dc:language>{lang}</dc:language>
<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>
</metadata>
<manifest>{''.join(manifest)}</manifest>
<spine>{''.join(spine)}</spine>
</package>""")
