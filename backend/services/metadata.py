"""Unified series-metadata lookup: NovelUpdates first, AniList as fallback.

NovelUpdates sits behind Cloudflare and periodically serves a full JS challenge
that no server-side client can pass (observed 2026-07: every fetch 403s). When
that happens — or when NU simply has no match — we fall back to AniList's free
GraphQL API (type MANGA / format NOVEL), which resolves both English and
Japanese titles and carries description/author/genres/cover/year/status.

All functions are sync (call via asyncio.to_thread), mirroring novelupdates.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from logging_config import get_logger
from services import books_metadata as bm
from services import novelupdates as nu

log = get_logger("metadata")

_ANILIST_URL = "https://graphql.anilist.co"

SOURCE_NOVELUPDATES = "novelupdates"
SOURCE_ANILIST = "anilist"
SOURCE_MYANIMELIST = "myanimelist"
SOURCE_MANGAUPDATES = "mangaupdates"


@dataclass
class MetaResult:
    source: str          # novelupdates | anilist
    ref: str             # nu_url, or the AniList media id as a string
    title: str
    description: str = ""
    image: str | None = None
    genres: list[str] = field(default_factory=list)
    year: str | None = None


@dataclass
class MetaDetail:
    source: str
    ref: str
    title: str
    description: str = ""
    image: str | None = None
    author: str | None = None
    genres: list[str] = field(default_factory=list)
    associated_names: list[str] = field(default_factory=list)
    status: str | None = None
    year: str | None = None
    nu_url: str | None = None  # set only when the source is NovelUpdates


def _strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "")
    return re.sub(r"<[^>]+>", "", text).strip()


# --- AniList -------------------------------------------------------------------

_SEARCH_QUERY = """
query ($search: String) {
  Page(perPage: 6) {
    media(search: $search, type: MANGA, format: NOVEL) {
      id title { romaji english native } description(asHtml: false)
      genres startDate { year } coverImage { extraLarge large }
    }
  }
}"""

_DETAIL_QUERY = """
query ($id: Int) {
  Media(id: $id, type: MANGA) {
    id title { romaji english native } description(asHtml: false)
    genres startDate { year } status coverImage { extraLarge large } synonyms
    staff(perPage: 6) { edges { role node { name { full } } } }
  }
}"""

_STATUS_MAP = {"FINISHED": "Completed", "RELEASING": "Ongoing",
               "NOT_YET_RELEASED": "Not yet released", "CANCELLED": "Cancelled",
               "HIATUS": "Hiatus"}


def _retrying(request_fn, attempts: int = 3):
    """Run an httpx request fn, honoring 429 Retry-After and retrying 5xx —
    AniList/Jikan rate-limit aggressively when search+detail land back-to-back."""
    import time as _time

    import httpx

    last: Exception | None = None
    for i in range(attempts):
        try:
            r = request_fn()
            r.raise_for_status()
            return r
        except httpx.HTTPStatusError as e:
            last = e
            code = e.response.status_code
            if code == 429:
                wait = min(float(e.response.headers.get("Retry-After") or 2 * (i + 1)), 30)
            elif code >= 500:
                wait = 2 * (i + 1)
            else:
                raise
            log.warning(f"metadata source {code}; retrying in {wait:.0f}s")
            _time.sleep(wait)
        except httpx.TransportError as e:
            last = e
            _time.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


def _anilist(query: str, variables: dict) -> dict:
    import httpx

    r = _retrying(lambda: httpx.post(
        _ANILIST_URL, json={"query": query, "variables": variables}, timeout=20))
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(f"AniList error: {data['errors'][0].get('message')}")
    return data["data"]


def _al_title(m: dict) -> str:
    t = m.get("title") or {}
    return t.get("english") or t.get("romaji") or t.get("native") or "Unknown"


def anilist_search(name: str) -> list[MetaResult]:
    media = _anilist(_SEARCH_QUERY, {"search": name})["Page"]["media"]
    return [
        MetaResult(
            source=SOURCE_ANILIST, ref=str(m["id"]), title=_al_title(m),
            description=_strip_html(m.get("description") or "")[:400],
            image=(m.get("coverImage") or {}).get("extraLarge")
                  or (m.get("coverImage") or {}).get("large"),
            genres=m.get("genres") or [],
            year=str((m.get("startDate") or {}).get("year") or "") or None,
        )
        for m in media
    ]


def anilist_detail(media_id: str) -> MetaDetail:
    m = _anilist(_DETAIL_QUERY, {"id": int(media_id)})["Media"]
    t = m.get("title") or {}
    names = [n for n in (t.get("native"), t.get("romaji"), *(m.get("synonyms") or []))
             if n and n != _al_title(m)]
    author = next((e["node"]["name"]["full"] for e in (m.get("staff") or {}).get("edges", [])
                   if (e.get("role") or "").lower().startswith("story")), None)
    return MetaDetail(
        source=SOURCE_ANILIST, ref=str(m["id"]), title=_al_title(m),
        description=_strip_html(m.get("description") or ""),
        image=(m.get("coverImage") or {}).get("extraLarge")
              or (m.get("coverImage") or {}).get("large"),
        author=author, genres=m.get("genres") or [],
        associated_names=names[:8],
        status=_STATUS_MAP.get(m.get("status") or "", m.get("status")),
        year=str((m.get("startDate") or {}).get("year") or "") or None,
    )


# --- MyAnimeList (via the keyless Jikan API) ---------------------------------------

def _http_get_json(url: str, params: dict | None = None) -> dict:
    import httpx

    return _retrying(lambda: httpx.get(url, params=params, timeout=20)).json()


def jikan_search(name: str) -> list[MetaResult]:
    data = _http_get_json("https://api.jikan.moe/v4/manga",
                          {"q": name, "type": "lightnovel", "limit": 6})
    return [
        MetaResult(
            source=SOURCE_MYANIMELIST, ref=str(m["mal_id"]), title=m.get("title") or "?",
            description=(m.get("synopsis") or "")[:400],
            image=((m.get("images") or {}).get("jpg") or {}).get("large_image_url"),
            genres=[g["name"] for g in (m.get("genres") or [])],
            year=str((((m.get("published") or {}).get("prop") or {}).get("from") or {})
                     .get("year") or "") or None,
        )
        for m in data.get("data", [])
    ]


def jikan_detail(mal_id: str) -> MetaDetail:
    m = _http_get_json(f"https://api.jikan.moe/v4/manga/{int(mal_id)}/full")["data"]
    names = [t["title"] for t in (m.get("titles") or [])
             if t.get("type") in ("Japanese", "Synonym") and t.get("title")]
    return MetaDetail(
        source=SOURCE_MYANIMELIST, ref=str(m["mal_id"]), title=m.get("title") or "?",
        description=(m.get("synopsis") or "").strip(),
        image=((m.get("images") or {}).get("jpg") or {}).get("large_image_url"),
        author=", ".join(a["name"] for a in (m.get("authors") or [])[:2]) or None,
        genres=[g["name"] for g in (m.get("genres") or [])],
        associated_names=names[:8],
        status={"Finished": "Completed", "Publishing": "Ongoing"}.get(
            m.get("status") or "", m.get("status")),
        year=str((((m.get("published") or {}).get("prop") or {}).get("from") or {})
                 .get("year") or "") or None,
    )


# --- MangaUpdates (public JSON API; includes novels, flagged by type) ----------------

def mangaupdates_search(name: str) -> list[MetaResult]:
    import httpx

    r = _retrying(lambda: httpx.post("https://api.mangaupdates.com/v1/series/search",
                                     json={"search": name, "perpage": 15}, timeout=20))
    records = [x.get("record") or {} for x in r.json().get("results", [])]
    # novels first — MU mixes manga and novels in one index
    records.sort(key=lambda rec: 0 if "novel" in (rec.get("type") or "").lower()
                 or "(novel)" in (rec.get("title") or "").lower() else 1)
    return [
        MetaResult(
            source=SOURCE_MANGAUPDATES, ref=str(rec["series_id"]),
            title=_strip_html(rec.get("title") or "?"),
            description=_strip_html(rec.get("description") or "")[:400],
            image=((rec.get("image") or {}).get("url") or {}).get("original"),
            genres=[g["genre"] for g in (rec.get("genres") or [])],
            year=str(rec.get("year") or "") or None,
        )
        for rec in records[:6] if rec.get("series_id")
    ]


def mangaupdates_detail(series_id: str) -> MetaDetail:
    m = _http_get_json(f"https://api.mangaupdates.com/v1/series/{int(series_id)}")
    return MetaDetail(
        source=SOURCE_MANGAUPDATES, ref=str(m["series_id"]),
        title=_strip_html(m.get("title") or "?"),
        description=_strip_html(m.get("description") or ""),
        image=((m.get("image") or {}).get("url") or {}).get("original"),
        author=", ".join(a["name"] for a in (m.get("authors") or [])
                         if (a.get("type") or "").lower() == "author")[:80] or None,
        genres=[g["genre"] for g in (m.get("genres") or [])],
        associated_names=[_strip_html(a.get("title") or "")
                          for a in (m.get("associated") or [])][:8],
        status=(m.get("status") or "").splitlines()[0][:40] or None,
        year=str(m.get("year") or "") or None,
    )


# --- unified API -----------------------------------------------------------------

def _nu_search(name: str) -> list[MetaResult]:
    return [
        MetaResult(source=SOURCE_NOVELUPDATES, ref=r.nu_url, title=r.title,
                   description=(r.description or "")[:400], image=r.image,
                   genres=r.genres)
        for r in nu.search(name)
    ]


# Search order. NU has the best LN coverage but is Cloudflare-gated; AniList
# handles Japanese titles well; MAL (Jikan) and MangaUpdates are last-resort
# backups. First source that returns anything wins.
_SEARCHERS = [
    (SOURCE_NOVELUPDATES, _nu_search),
    (SOURCE_ANILIST, anilist_search),
    (SOURCE_MYANIMELIST, jikan_search),
    (SOURCE_MANGAUPDATES, mangaupdates_search),
]


def _as_meta(hits) -> list[MetaResult]:
    return [
        MetaResult(source=h.source, ref=h.ref, title=h.title, description=h.description,
                   image=h.image, genres=h.genres, year=h.year)
        for h in hits
    ]


def search(name: str) -> tuple[list[MetaResult], list[str]]:
    """Search the international book catalogues.

    The legacy light-novel sources (NovelUpdates, AniList, MyAnimeList,
    MangaUpdates) are deliberately not consulted any more: this is a books app,
    and a search for "Solaris" should not be answered by a manga database. Their
    detail lookups stay available so series created earlier can still be
    refreshed.
    """
    notes: list[str] = []
    try:
        hits, book_notes = bm.search(name)
        notes.extend(book_notes)
    except Exception as e:  # noqa: BLE001 - never let metadata stop series creation
        notes.append(f"book catalogues failed ({e}).")
        hits = []
    return _as_meta(hits), notes



def fetch_detail(source: str, ref: str) -> MetaDetail:
    if source in bm._DETAILERS:
        h = bm.fetch_detail(source, ref)
        return MetaDetail(
            source=h.source, ref=h.ref, title=h.title, description=h.description,
            image=h.image, author=h.author or None, genres=h.genres,
            associated_names=[], status=None, year=h.year, nu_url=None,
        )
    d = nu.fetch_series(ref)
    return MetaDetail(
        source=SOURCE_NOVELUPDATES, ref=d.nu_url, title=d.title,
        description=d.description, image=d.image, author=d.author,
        genres=d.genres, associated_names=d.associated_names,
        status=d.status, year=d.year, nu_url=d.nu_url,
    )


def fetch_cover(source: str, url: str) -> bytes:
    if source in bm._DETAILERS:
        return bm.fetch_cover(url)
    if source == SOURCE_NOVELUPDATES:
        return nu.fetch_image(url)  # NU images are Cloudflare/hotlink-protected
    import httpx

    r = httpx.get(url, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return r.content


async def apply_to_series(series, detail: MetaDetail, store) -> list[str]:
    """Overwrite a Series row's metadata (and poster) from a MetaDetail.
    Shared by the agent's link_series_metadata tool and the edit-page
    "match to NovelUpdates/AniList" endpoint. Caller commits."""
    import asyncio

    applied = ["title", "description", "author", "genres", "associated_names",
               "nu_status", "year"]
    series.title = detail.title or series.title
    series.description = detail.description or series.description
    series.author = detail.author or series.author
    series.genres = detail.genres or series.genres
    series.associated_names = detail.associated_names or series.associated_names
    series.nu_status = detail.status or series.nu_status
    series.year = detail.year or series.year
    if detail.nu_url:
        series.nu_url = detail.nu_url
        series.source = "novelupdates"
        applied.append("nu_url")
    if detail.image:
        try:
            img = await asyncio.to_thread(fetch_cover, detail.source, detail.image)
            key = f"series/{series.id}/poster.jpg"
            await asyncio.to_thread(store.put, key, img, "image/jpeg")
            series.poster_key = key
            applied.append("poster")
        except Exception as e:  # noqa: BLE001 — poster is best-effort
            log.warning(f"cover download failed for {series.id}: {e}")
    return applied
