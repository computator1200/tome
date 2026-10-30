"""Webnovel fetching for the master agent: paste a URL, get chapters.

First-class support for Syosetu (ncode.syosetu.com / novel18.syosetu.com — the
R18 subdomain needs an over18=yes cookie), plus a generic single-page fallback
for anything else. Sync functions (call via asyncio.to_thread); chapter fetches
are politely rate-limited.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from lxml import html as lxml_html

from logging_config import get_logger

log = get_logger("webnovel")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_CHAPTER_DELAY_S = 0.8
MAX_CHAPTERS_PER_FETCH = 200


@dataclass
class NovelIndex:
    url: str
    title: str
    author: str = ""
    synopsis: str = ""
    chapters: list[dict] = field(default_factory=list)  # [{num, title, url}]
    site: str = "generic"


def _get(url: str, attempts: int = 3) -> str:
    """GET with small retries — a single flaky connection must not kill an
    80-chapter batch fetch."""
    import httpx

    last: Exception | None = None
    for i in range(attempts):
        try:
            r = httpx.get(url, headers={"User-Agent": _UA}, cookies={"over18": "yes"},
                          timeout=30, follow_redirects=True)
            r.raise_for_status()
            return r.text
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            last = e
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
                raise  # 4xx won't heal on retry
            log.warning(f"webnovel GET {url} attempt {i+1} failed: {e}")
            time.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


def _is_syosetu(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("syosetu.com")


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


# --- Syosetu ---------------------------------------------------------------------

def _syosetu_index(url: str) -> NovelIndex:
    base = url if url.endswith("/") else url + "/"
    doc = lxml_html.fromstring(_get(base))

    title = _text(next(iter(doc.cssselect(".p-novel__title, .novel_title")), None))
    author = _text(next(iter(doc.cssselect(".p-novel__author a, .novel_writername a")), None))
    if not author:
        m = re.search(r"作者[：:]\s*(\S+)", doc.text_content())
        author = m.group(1) if m else ""
    synopsis = _text(next(iter(doc.cssselect("#novel_ex, .p-novel__summary")), None))

    ncode = urlparse(base).path.strip("/").split("/")[0]
    chapters: list[dict] = []
    seen: set[str] = set()
    page_doc, page_no = doc, 1
    while True:
        for a in page_doc.cssselect(f'a[href*="/{ncode}/"]'):
            href = a.get("href") or ""
            m = re.search(rf"/{ncode}/(\d+)/?$", href)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                chapters.append({"num": int(m.group(1)), "title": _text(a),
                                 "url": f"https://{urlparse(base).netloc}/{ncode}/{m.group(1)}/"})
        # long novels paginate the index (?p=2 …)
        next_link = page_doc.cssselect('a[href*="?p="].c-pager__item--next, a.novelview_pager-next')
        if not next_link:
            break
        page_no += 1
        if page_no > 50:
            break
        time.sleep(_CHAPTER_DELAY_S)
        page_doc = lxml_html.fromstring(_get(f"{base}?p={page_no}"))

    chapters.sort(key=lambda c: c["num"])
    if not title:
        m = re.search(r"<title>(.*?)</title>", lxml_html.tostring(doc, encoding="unicode"))
        title = (m.group(1).split("|")[0].strip() if m else ncode)
    return NovelIndex(url=base, title=title, author=author, synopsis=synopsis,
                      chapters=chapters, site="syosetu")


def _syosetu_chapter(url: str) -> dict:
    doc = lxml_html.fromstring(_get(url))
    title = _text(next(iter(doc.cssselect(".p-novel__title, .novel_subtitle")), None))
    body = next(iter(doc.cssselect(
        ".p-novel__body .p-novel__text, #novel_honbun")), None)
    paras: list[str] = []
    # new markup has several .p-novel__text sections (preface/body/afterword)
    sections = doc.cssselect(".p-novel__body .p-novel__text") or ([body] if body is not None else [])
    for section in sections:
        for p in section.cssselect("p") or [section]:
            t = _text(p)
            if t:
                paras.append(t)
    return {"title": title, "blocks": paras}


# --- generic ----------------------------------------------------------------------

def _generic_chapter(url: str) -> dict:
    from services.documents import _block_text, iter_blocks  # shared block logic

    doc = lxml_html.fromstring(_get(url))
    title = _text(next(iter(doc.cssselect("title")), None)) or url
    blocks = [b for b in (_block_text(e)
                          for e in iter_blocks(lxml_html.etree.ElementTree(doc))) if b]
    return {"title": title, "blocks": blocks}


def _generic_page(url: str) -> NovelIndex:
    ch = _generic_chapter(url)
    return NovelIndex(url=url, title=ch["title"],
                      chapters=[{"num": 1, "title": ch["title"], "url": url}],
                      synopsis=" ".join(ch["blocks"][:3])[:400], site="generic")


# --- public API --------------------------------------------------------------------

def fetch_index(url: str) -> NovelIndex:
    if _is_syosetu(url):
        return _syosetu_index(url)
    return _generic_page(url)


def fetch_chapters(index: NovelIndex, start: int = 1, end: int | None = None) -> list[dict]:
    """Fetch chapter bodies [{title, blocks}] for chapters[start-1:end] (1-based,
    inclusive), rate-limited, capped at MAX_CHAPTERS_PER_FETCH."""
    selected = index.chapters[start - 1: end if end is not None else len(index.chapters)]
    if len(selected) > MAX_CHAPTERS_PER_FETCH:
        raise ValueError(f"{len(selected)} chapters requested; cap is "
                         f"{MAX_CHAPTERS_PER_FETCH} per volume — use start/end ranges.")
    out: list[dict] = []
    for i, ch in enumerate(selected):
        if index.site == "syosetu":
            body = _syosetu_chapter(ch["url"])
        else:
            body = _generic_chapter(ch["url"])
        if not body["title"]:
            body["title"] = ch["title"]
        if body["blocks"]:
            out.append(body)
        else:
            log.warning(f"webnovel chapter {ch['url']} had no text")
        if i < len(selected) - 1:
            time.sleep(_CHAPTER_DELAY_S)
    log.info(f"webnovel: fetched {len(out)} chapter(s) from {index.url}")
    return out
