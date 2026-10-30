"""Visual PDF export: each page is the original scan, and text/mixed pages get
the translation laid over it (matching the reader). When every translated
region has a bounding box, each translation is placed where its source text
sits; otherwise the page is dimmed and the translation set as one column. Uses
fpdf2's built-in Times font so the translation is real, selectable PDF text and
no font file needs bundling. Latin target languages (incl. accented French /
German / Spanish) render fine; non-Latin targets would need a Unicode TTF.
"""
from __future__ import annotations

from io import BytesIO

from fpdf import FPDF
from PIL import Image

from storage_backend import get_store

# Smart punctuation the LLM tends to emit, mapped to Latin-1 equivalents.
_PUNCT = {
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "—": "-", "–": "-", "…": "...", "•": "-",
}


def _sanitize(text: str) -> str:
    for k, v in _PUNCT.items():
        text = text.replace(k, v)
    # Guarantee encodability for the core (Latin-1) font; stray glyphs -> '?'.
    return text.encode("latin-1", "replace").decode("latin-1")


def _render_text_page(pdf, lines: list[str], theme: str = "dark") -> None:
    """A page for a volume that has no scan behind it.

    Digital volumes carry their text and nothing else: every page has an empty
    image_key, so the image path skipped all of them and fpdf emitted one blank
    page. This lays the translation out as ordinary prose on A4.
    """
    width, height = 595.0, 842.0
    margin = 56.0
    font_size = 11.5
    line_h = font_size * 1.5
    if theme == "light":
        pdf.set_fill_color(255, 255, 255)
    else:
        pdf.set_fill_color(18, 16, 24)
    pdf.add_page(format=(width, height))
    pdf.set_fill_color(18, 16, 24) if theme != "light" else pdf.set_fill_color(255, 255, 255)
    pdf.rect(0, 0, width, height, style="F")
    pdf.set_text_color(238, 238, 238) if theme != "light" else pdf.set_text_color(24, 24, 24)
    pdf.set_font("Times", size=font_size)
    pdf.set_xy(margin, margin)
    usable = width - 2 * margin
    for paragraph in lines:
        text = _sanitize((paragraph or "").strip())
        if not text:
            continue
        pdf.set_x(margin)
        pdf.multi_cell(usable, line_h, text)
        pdf.ln(line_h * 0.5)


def _fit(pdf, text: str, w: float, h: float, start: float) -> tuple[float, float]:
    """The largest font size, and its line height, at which `text` fits w x h."""
    size = start
    while size > 5:
        line_h = size * 1.2
        pdf.set_font("Times", size=size)
        lines = pdf.multi_cell(w, line_h, text, align="L", dry_run=True, output="LINES")
        if len(lines) * line_h <= h:
            return size, line_h
        size -= 0.5
    return 5.0, 6.0


def _render_boxes(pdf, boxes: list[dict], w: int, h: int, scrim, text_color) -> None:
    """Cover each source block and set its translation inside the same box.

    Vertical Japanese text makes tall, narrow boxes that English cannot fill at a
    readable size, so a box too narrow for its text is widened about its centre
    (up to three times, within the page) before the font is shrunk.
    """
    pad = max(2.0, w * 0.004)
    for b in boxes:
        text = _sanitize(b["text"].strip())
        if not text:
            continue
        bx, by = b["x"] / 100 * w, b["y"] / 100 * h
        bw, bh = b["width"] / 100 * w, b["height"] / 100 * h
        floor = max(9.0, w / 90)
        size, line_h = _fit(pdf, text, bw - 2 * pad, bh - 2 * pad, min(28.0, bh / 2))
        if size < floor and bw < bh:
            cx = bx + bw / 2
            bw = min(bw * 3, w)
            bx = min(max(cx - bw / 2, 0), w - bw)
            size, line_h = _fit(pdf, text, bw - 2 * pad, bh - 2 * pad, min(28.0, bh / 2))
        pdf.set_fill_color(*scrim)
        pdf.rect(bx, by, bw, bh, style="F")
        pdf.set_text_color(*text_color)
        pdf.set_font("Times", size=size)
        pdf.set_xy(bx + pad, by + pad)
        pdf.multi_cell(bw - 2 * pad, line_h, text, align="L")


def build_pdf(pages: list[dict], overlay_dim: float = 0.85, theme: str = "dark",
              path: str | None = None) -> bytes | None:
    """pages: ordered list of {image_key, is_text, lines: [str], boxes: [box]},
    where a box is {x, y, width, height, text} in page percentages.
    `theme` ('dark'|'light') sets the scrim + text colors to match the site.
    With `path`, the PDF is written there and nothing is returned."""
    if theme == "light":
        scrim, text_color = (245, 245, 245), (24, 24, 24)
    else:
        scrim, text_color = (18, 16, 24), (238, 238, 238)
    store = get_store()
    pdf = FPDF(unit="pt")
    pdf.set_auto_page_break(False)

    for page in pages:
        lines = page.get("lines") or []
        im = None
        if page.get("image_key"):
            try:
                data = store.get(page["image_key"])
                im = Image.open(BytesIO(data)).convert("RGB")
            except Exception:
                im = None
        if im is None:
            # Digital volume: text only, no scan to lay it over.
            if lines:
                _render_text_page(pdf, lines, theme)
            continue
        w, h = im.size
        pdf.add_page(format=(w, h))
        pdf.image(im, x=0, y=0, w=w, h=h)

        if not page.get("is_text") or not lines:
            continue

        if page.get("boxes"):
            _render_boxes(pdf, page["boxes"], w, h, scrim, text_color)
            continue

        # Dim the scan, then lay the translation on top as readable text.
        with pdf.local_context(fill_opacity=overlay_dim):
            pdf.set_fill_color(*scrim)
            pdf.rect(0, 0, w, h, style="F")

        font_size = max(11, min(28, int(w / 45)))
        margin = w * 0.12
        pdf.set_text_color(*text_color)
        pdf.set_font("Times", size=font_size)
        pdf.set_xy(margin, h * 0.07)
        line_h = font_size * 1.45
        for paragraph in lines:
            text = _sanitize(paragraph.strip())
            if not text:
                continue
            pdf.set_x(margin)
            pdf.multi_cell(w - 2 * margin, line_h, text)
            pdf.ln(line_h * 0.45)

    if path:
        pdf.output(path)
        return None
    return bytes(pdf.output())
