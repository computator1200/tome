"""NovelUpdates search + series metadata.

Vendored and hardened from jckli/novelupdates.py (the published package is
unmaintained, its series_info is broken, and NovelUpdates sits behind
Cloudflare). We use cloudscraper to get past the JS challenge, fall back to
plain httpx, and parse defensively — any missing field degrades to a default
rather than raising, because the whole flow has a manual-entry fallback.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from logging_config import get_logger

log = get_logger("novelupdates")

_BASE = "https://www.novelupdates.com"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class NUSearchResult:
    title: str
    nu_url: str
    image: str | None = None
    description: str = ""
    genres: list[str] = field(default_factory=list)


@dataclass
class NUSeriesDetail:
    title: str
    nu_url: str
    image: str | None = None
    description: str = ""
    author: str | None = None
    genres: list[str] = field(default_factory=list)
    associated_names: list[str] = field(default_factory=list)
    status: str | None = None
    year: str | None = None


def _scraper():
    import cloudscraper  # imported lazily so the app boots without it

    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True}
    )


def _fetch(url: str, params: dict | None = None) -> str:
    """GET a NovelUpdates page: curl_cffi (browser TLS fingerprint) first, then
    cloudscraper, then httpx. Raises on total failure so callers can surface a
    clear error. Note: when Cloudflare serves its full JS challenge ("Just a
    moment..."), nothing server-side passes — services/metadata.py falls back
    to AniList in that case."""
    last_err: Exception | None = None
    try:
        from curl_cffi import requests as curl_requests

        r = curl_requests.get(url, params=params, impersonate="chrome", timeout=30)
        r.raise_for_status()
        return r.text
    except Exception as e:  # noqa: BLE001
        last_err = e
        log.warning(f"curl_cffi failed for {url}: {e}; trying cloudscraper")

    try:
        r = _scraper().get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.text
    except Exception as e:  # noqa: BLE001
        last_err = e
        log.warning(f"cloudscraper failed for {url}: {e}; trying httpx")

    try:
        import httpx

        r = httpx.get(url, params=params, headers={"User-Agent": _UA}, timeout=30, follow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception as e:  # noqa: BLE001
        last_err = e

    raise RuntimeError(f"Could not reach NovelUpdates ({url}): {last_err}")


def fetch_image(url: str) -> bytes:
    """Download a poster image (NovelUpdates images can be hotlink/Cloudflare
    protected, so reuse the same scraper path)."""
    try:
        import cloudscraper

        scraper = cloudscraper.create_scraper()
        r = scraper.get(url, timeout=30)
        r.raise_for_status()
        return r.content
    except Exception:  # noqa: BLE001
        import httpx

        r = httpx.get(url, headers={"User-Agent": _UA, "Referer": _BASE}, timeout=30, follow_redirects=True)
        r.raise_for_status()
        return r.content


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _abs_img(src: str | None) -> str | None:
    if not src or "noimage" in src:
        return None
    if src.startswith("//"):
        return "https:" + src
    return src


def search(name: str) -> list[NUSearchResult]:
    """Search NovelUpdates via its live-search AJAX endpoint.

    The old `/?s=` WordPress search is disabled (returns 404). The live-search
    endpoint returns a small result list (title, series URL, thumbnail); richer
    metadata (description, genres) is fetched on selection via fetch_series().
    """
    resp = _scraper().post(
        f"{_BASE}/wp-admin/admin-ajax.php",
        data={"action": "nd_ajaxsearchmain", "strType": "desktop",
              "strOne": name, "strSearchType": "series"},
        timeout=30,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    results: list[NUSearchResult] = []
    for li in soup.find_all("li", class_="search_li_results"):
        a = li.find("a", class_="a_search")
        if not a:
            continue
        link = a.get("href", "")
        img = a.find("img")
        image = _abs_img(img.get("src") if img else None)
        # Title is the <a> text minus the image; search_hl spans are part of it.
        title = _clean(a.get_text(" "))
        if title and link:
            results.append(NUSearchResult(title=title, nu_url=link, image=image))
    log.info(f"NU search {name!r} -> {len(results)} results")
    return results


def fetch_series(nu_url: str) -> NUSeriesDetail:
    """Fetch full metadata for a series page."""
    html = _fetch(nu_url)
    soup = BeautifulSoup(html, "html.parser")

    def _text_by_id(div_id: str) -> str:
        el = soup.find("div", id=div_id)
        return _clean(el.get_text(" ")) if el else ""

    def _links_by_id(div_id: str) -> list[str]:
        el = soup.find("div", id=div_id)
        if not el:
            return []
        return [_clean(a.text) for a in el.find_all("a") if _clean(a.text)]

    title_el = soup.find("div", class_="seriestitlenu")
    title = _clean(title_el.text) if title_el else ""

    image = None
    img_el = soup.find("div", class_="seriesimg")
    if img_el and img_el.find("img"):
        image = img_el.find("img").get("src")

    # Description lives in #editdescription (with <br> → newlines).
    description = ""
    desc_el = soup.find("div", id="editdescription")
    if desc_el:
        for br in desc_el.find_all("br"):
            br.replace_with("\n")
        description = desc_el.get_text("\n").strip()

    genres = _links_by_id("seriesgenre")
    authors = _links_by_id("showauthors")
    author = authors[0] if authors else None
    status = _text_by_id("editstatus") or None
    year = _text_by_id("edityear") or None

    associated = []
    assoc_el = soup.find("div", id="editassociated")
    if assoc_el:
        for br in assoc_el.find_all("br"):
            br.replace_with("\n")
        associated = [_clean(s) for s in assoc_el.get_text("\n").split("\n") if _clean(s)]

    if not title:
        raise RuntimeError("Could not parse series page (NovelUpdates layout changed or blocked)")

    return NUSeriesDetail(
        title=title, nu_url=nu_url, image=image, description=description,
        author=author, genres=genres, associated_names=associated,
        status=status, year=year,
    )
