"""Book metadata from international catalogues.

Four providers, all free and keyless, chosen for coverage rather than fashion:

- **Open Library** (Internet Archive) - the widest multilingual catalogue of
  published editions, with covers and a work/edition split that survives
  translation. No key.
- **Google Books** - best coverage of in-print and translated editions, and the
  only one of the four that reliably carries descriptions in many languages.
- **Internet Archive** - scanned books, with a stable cover endpoint per item.
- **Crossref** - publisher-deposited metadata for academic and reference books,
  where the trade catalogues are thin.

They are queried in that order and the first provider that returns anything
wins, because the UI shows one list. Every provider's own failure is reported in
`notes` rather than hidden, so a user who gets no results can see whether the
answer was "nothing found" or "everything timed out".

All functions are sync, matching the rest of the metadata layer; callers run them
in a thread.
"""
from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from logging_config import get_logger

log = get_logger("books_metadata")

TIMEOUT = httpx.Timeout(8.0, connect=5.0)
# Open Library is regularly 8-12 s for a common title from this host, and the
# search used to give up on it at 8 s and hand back archive.org noise instead.
# How long a search may spend waiting for the fast catalogues.
FAST_DEADLINE = 4.0
CACHE_TTL = 900.0
_CACHE: dict[tuple[str, int], tuple[float, list["BookHit"], list[str]]] = {}
# How long Open Library gets on its own before the others are allowed to answer.
OL_WAIT = 5.0
UA = {"User-Agent": "tome/1.0 (book metadata lookup; contact: metadata@tome.local)"}

SOURCE_OPENLIBRARY = "openlibrary"
SOURCE_GOOGLE = "googlebooks"
SOURCE_ARCHIVE = "internetarchive"
SOURCE_DNB = "dnb"
SOURCE_BNF = "bnf"
SOURCE_CROSSREF = "crossref"

# Order: broad multilingual, then the national libraries for depth in German
# and French, then Crossref (scholarly books only, no covers).
# Crossref is deliberately absent: it carries journal articles and proceedings,
# and for a novel title it returns "Process Mining" and clinical case reports.
# The adapter stays for anyone who wants it; it is not in the default chain.
PROVIDER_ORDER = [SOURCE_OPENLIBRARY, SOURCE_GOOGLE, SOURCE_ARCHIVE,
                  SOURCE_DNB, SOURCE_BNF]

# Google Books needs a key from this host: anonymous calls answer 429 every time.
GOOGLE_BOOKS_API_KEY = os.getenv("GOOGLE_BOOKS_API_KEY", "").strip()
if not GOOGLE_BOOKS_API_KEY:
    PROVIDER_ORDER.remove(SOURCE_GOOGLE)

# A near-exact title from one of these is trusted enough to stop waiting.
AUTHORITATIVE = {SOURCE_OPENLIBRARY, SOURCE_GOOGLE, SOURCE_DNB, SOURCE_BNF}


@dataclass
class BookHit:
    source: str
    ref: str
    title: str
    description: str = ""
    image: str | None = None
    genres: list[str] = field(default_factory=list)
    year: str | None = None
    authors: list[str] = field(default_factory=list)
    language: str | None = None

    # the shape the rest of the app already expects
    @property
    def author(self) -> str:
        return ", ".join(self.authors[:2])


SLOW_TIMEOUT = httpx.Timeout(25.0, connect=5.0)


def _get(url: str, params: dict | None = None, timeout: httpx.Timeout | None = None) -> dict:
    with httpx.Client(timeout=timeout or TIMEOUT, headers=UA, follow_redirects=True) as client:
        r = client.get(url, params=params or {})
        r.raise_for_status()
        return r.json()


def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", str(text))
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _year(value) -> str | None:
    m = re.search(r"(1[5-9]\d{2}|20\d{2})", str(value or ""))
    return m.group(1) if m else None


def _norm_title(text: str) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^\w\s]", " ", text.casefold()).strip()


def _clean_text(value: str | None) -> str:
    """Strip MARC-8 non-sort markers and stray C1 controls.

    German and French records come back with 0x98/0x9c wrapped around words that
    are not alphabetised ("\x98Der\x9c Process"). They are not quotes, and they
    reach the UI as garbage, so drop them.
    """
    if not value:
        return ""
    return "".join(ch for ch in value if not (0x80 <= ord(ch) <= 0x9F)).strip()


def _relevance(query: str, title: str) -> float:
    """Cheap title relevance, so a search for one word does not return the
    whole shelf the provider feels like sending."""
    import difflib

    q, t = _norm_title(query), _norm_title(title)
    if not q or not t:
        return 0.0
    if q == t:
        return 1.0
    # "dune" is a substring of "dune boy" but not the same book, and archive.org
    # is full of titles like that. A whole-word match is evidence; a substring is
    # only worth what the ratio says.
    if re.search(rf"(^|\s){re.escape(q)}(\s|$)", t):
        return 0.9
    return difflib.SequenceMatcher(None, q, t).ratio()


def _rank(query: str, hits: list[BookHit], limit: int) -> list[BookHit]:
    scored = sorted(((_relevance(query, h.title), h) for h in hits), key=lambda x: -x[0])
    strong = [h for s, h in scored if s >= 0.34]
    # Never return an empty shelf just because nothing matched the query well:
    # for a title in another script the string similarity is meaningless.
    keep = strong if len(strong) >= 2 else [h for _, h in scored]
    return keep[:limit]


# --- Open Library ---------------------------------------------------------------

def openlibrary_search(query: str, limit: int = 8) -> list[BookHit]:
    # Open Library regularly needs 8-12 s for a common title, which used to
    # arrive after our own 8 s read timeout: the request was abandoned and the
    # user got archive.org full-text noise instead. Give it room.
    data = _get("https://openlibrary.org/search.json", {
        "q": query, "limit": limit, "fields":
        "key,title,author_name,first_publish_year,cover_i,subject,language,first_sentence,subtitle",
    }, timeout=SLOW_TIMEOUT)
    hits = []
    for doc in data.get("docs", []):
        cover = doc.get("cover_i")
        hits.append(BookHit(
            source=SOURCE_OPENLIBRARY,
            ref=doc.get("key", ""),
            title=doc.get("title", "").strip(),
            description=_clean(_first(doc.get("first_sentence"))),
            image=f"https://covers.openlibrary.org/b/id/{cover}-L.jpg?default=false" if cover else None,
            genres=[g for g in (doc.get("subject") or [])][:8],
            year=_year(doc.get("first_publish_year")),
            authors=list(doc.get("author_name") or [])[:3],
            language=(doc.get("language") or [None])[0],
        ))
    return [h for h in hits if h.title]


def openlibrary_detail(ref: str) -> BookHit:
    data = _get(f"https://openlibrary.org{ref}.json")
    authors = []
    for a in (data.get("authors") or [])[:3]:
        key = (a.get("author") or {}).get("key")
        if not key:
            continue
        try:
            authors.append(_get(f"https://openlibrary.org{key}.json").get("name", ""))
        except Exception:  # noqa: BLE001 — an author lookup must not fail the book
            continue
    cover_id = (data.get("covers") or [None])[0]
    return BookHit(
        source=SOURCE_OPENLIBRARY, ref=ref, title=data.get("title", ""),
        description=_clean(_first(data.get("description"))),
        image=f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg?default=false" if cover_id else None,
        genres=[s.get("name", "") for s in (data.get("subjects") or []) if isinstance(s, dict)][:8],
        year=_year((data.get("first_publish_date") or "")),
        authors=[a for a in authors if a],
    )


def _first(value):
    """Open Library returns description/first_sentence as a string or a dict."""
    if isinstance(value, dict):
        return value.get("value", "")
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


# --- Google Books ---------------------------------------------------------------

def google_search(query: str, limit: int = 8) -> list[BookHit]:
    params = {"q": query, "maxResults": min(limit, 20), "printType": "books"}
    key = os.getenv("GOOGLE_BOOKS_API_KEY", "").strip()
    if key:
        params["key"] = key
    data = _get("https://www.googleapis.com/books/v1/volumes", params)
    hits = []
    for item in data.get("items", []):
        v = item.get("volumeInfo") or {}
        links = v.get("imageLinks") or {}
        image = links.get("thumbnail") or links.get("smallThumbnail")
        hits.append(BookHit(
            source=SOURCE_GOOGLE, ref=item.get("id", ""),
            title=(v.get("title") or "").strip(),
            description=_clean(v.get("description")),
            image=image.replace("http://", "https://") if image else None,
            genres=list(v.get("categories") or [])[:8],
            year=_year(v.get("publishedDate")),
            authors=list(v.get("authors") or [])[:3],
            language=v.get("language"),
        ))
    return [h for h in hits if h.title]


def google_detail(ref: str) -> BookHit:
    data = _get(f"https://www.googleapis.com/books/v1/volumes/{quote(ref)}")
    v = data.get("volumeInfo") or {}
    links = v.get("imageLinks") or {}
    image = links.get("thumbnail") or links.get("smallThumbnail")
    return BookHit(
        source=SOURCE_GOOGLE, ref=ref, title=v.get("title", ""),
        description=_clean(v.get("description")),
        image=image.replace("http://", "https://") if image else None,
        genres=list(v.get("categories") or [])[:8],
        year=_year(v.get("publishedDate")),
        authors=list(v.get("authors") or [])[:3],
        language=v.get("language"),
    )


# --- Internet Archive -----------------------------------------------------------

def archive_search(query: str, limit: int = 8) -> list[BookHit]:
    data = _get("https://archive.org/advancedsearch.php", {
        "q": f'title:({query}) AND mediatype:texts',
        "fl[]": ["identifier", "title", "creator", "year", "description", "subject", "language"],
        "rows": limit, "page": 1, "output": "json",
    })
    hits = []
    for doc in (data.get("response") or {}).get("docs", []):
        ident = doc.get("identifier")
        if not ident:
            continue
        creator = doc.get("creator")
        hits.append(BookHit(
            source=SOURCE_ARCHIVE, ref=ident,
            title=_first(doc.get("title")).strip(),
            description=_clean(_first(doc.get("description"))),
            image=f"https://archive.org/services/img/{ident}",
            genres=[s.strip() for s in (_first(doc.get("subject")) or "").split(";") if s.strip()][:8],
            year=_year(doc.get("year")),
            authors=[creator] if isinstance(creator, str) else list(creator or [])[:3],
            language=_first(doc.get("language")),
        ))
    return [h for h in hits if h.title]


def archive_detail(ref: str) -> BookHit:
    data = _get(f"https://archive.org/metadata/{quote(ref)}")
    m = data.get("metadata") or {}
    creator = m.get("creator")
    return BookHit(
        source=SOURCE_ARCHIVE, ref=ref,
        title=_first(m.get("title")),
        description=_clean(_first(m.get("description"))),
        image=f"https://archive.org/services/img/{ref}",
        genres=[s.strip() for s in (_first(m.get("subject")) or "").split(";") if s.strip()][:8],
        year=_year(m.get("date") or m.get("year")),
        authors=[creator] if isinstance(creator, str) else list(creator or [])[:3],
        language=_first(m.get("language")),
    )


# --- Crossref -------------------------------------------------------------------

def crossref_search(query: str, limit: int = 8) -> list[BookHit]:
    # No `select`: Crossref rejects unknown fields outright (400), and the
    # full record is small enough that filtering buys nothing.
    data = _get("https://api.crossref.org/works", {
        "query.bibliographic": query, "filter": "type:book", "rows": limit,
    })
    hits = []
    for item in (data.get("message") or {}).get("items", []):
        title = _first(item.get("title")).strip()
        if not title:
            continue
        parts = ((item.get("published") or {}).get("date-parts") or [[None]])[0]
        hits.append(BookHit(
            source=SOURCE_CROSSREF, ref=item.get("DOI", ""), title=title,
            description=_clean(_first(item.get("abstract"))),
            image=None,
            genres=list(item.get("subject") or [])[:8],
            year=_year(parts[0] if parts else None),
            authors=[" ".join(filter(None, [a.get("given"), a.get("family")]))
                     for a in (item.get("author") or [])][:3],
            language=item.get("language"),
        ))
    return hits


def crossref_detail(ref: str) -> BookHit:
    data = _get(f"https://api.crossref.org/works/{quote(ref)}")
    item = data.get("message") or {}
    parts = ((item.get("published") or {}).get("date-parts") or [[None]])[0]
    return BookHit(
        source=SOURCE_CROSSREF, ref=ref, title=_first(item.get("title")),
        description=_clean(_first(item.get("abstract"))),
        genres=list(item.get("subject") or [])[:8],
        year=_year(parts[0] if parts else None),
        authors=[" ".join(filter(None, [a.get("given"), a.get("family")]))
                 for a in (item.get("author") or [])][:3],
        language=item.get("language"),
    )



# --- Deutsche Nationalbibliothek (SRU, MARC21) ---------------------------------

DNB_SRU = "https://services.dnb.de/sru/dnb"
_MARC_NS = {"m": "http://www.loc.gov/MARC21/slim"}


def _marc_texts(record, tag: str, code: str) -> list[str]:
    return [el.text.strip() for el in record.findall(
        f'm:datafield[@tag="{tag}"]/m:subfield[@code="{code}"]', _MARC_NS)
        if el.text and el.text.strip()]


def _dnb_hits(xml_text: str, limit: int) -> list[BookHit]:
    from lxml import etree

    root = etree.fromstring(xml_text.encode("utf-8"))
    hits: list[BookHit] = []
    for rec in root.findall(".//m:record", _MARC_NS)[:limit]:
        title = _clean_text((_marc_texts(rec, "245", "a") or [""])[0])
        if not title:
            continue
        authors = _marc_texts(rec, "100", "a") + _marc_texts(rec, "700", "a")
        year = (_marc_texts(rec, "264", "c") or [""])[0]
        isbn = (_marc_texts(rec, "020", "a") or [""])[0].split(" ")[0]
        hits.append(BookHit(
            source=SOURCE_DNB, ref=isbn or title, title=title,
            description="", genres=_marc_texts(rec, "655", "a")[:8],
            year=_year(year), authors=authors[:3],
            language=(_marc_texts(rec, "041", "a") or [None])[0],
            # DNB carries no cover, but an ISBN lets the UI fall back to Open Library
            image=f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false" if isbn else None,
        ))
    return hits


def dnb_search(query: str, limit: int = 8) -> list[BookHit]:
    # WOE is the all-words index and tolerates a bare title better than TIT=
    with httpx.Client(timeout=TIMEOUT, headers=UA, follow_redirects=True) as client:
        r = client.get(DNB_SRU, params={
            "version": "1.1", "operation": "searchRetrieve",
            "query": f"WOE={query}", "recordSchema": "MARC21-xml",
            "maximumRecords": limit,
        })
        r.raise_for_status()
        return _dnb_hits(r.text, limit)


def dnb_detail(ref: str) -> BookHit:
    hits = dnb_search(ref, 1)
    if not hits:
        raise ValueError(f"no DNB record for {ref}")
    return hits[0]


# --- Bibliotheque nationale de France (SRU, Dublin Core) ------------------------

BNF_SRU = "https://catalogue.bnf.fr/api/SRU"
_DC_NS = {"dc": "http://purl.org/dc/elements/1.1/", "oai": "http://www.openarchives.org/OAI/2.0/oai_dc/"}


def _dc(rec, name: str) -> list[str]:
    out = []
    for el in rec.iter():
        tag = el.tag.split("}")[-1]
        if tag == name and el.text and el.text.strip():
            out.append(el.text.strip())
    return out


def bnf_search(query: str, limit: int = 8) -> list[BookHit]:
    with httpx.Client(timeout=TIMEOUT, headers=UA, follow_redirects=True) as client:
        r = client.get(BNF_SRU, params={
            "version": "1.2", "operation": "searchRetrieve",
            "query": f'bib.title all "{query}"', "recordSchema": "dublincore",
            "maximumRecords": limit,
        })
        r.raise_for_status()
        from lxml import etree

        root = etree.fromstring(r.content)
        hits: list[BookHit] = []
        for rec in root.findall(".//oai:dc", _DC_NS):
            title = _clean_text((_dc(rec, "title") or [""])[0])
            if not title:
                continue
            # BnF mixes sound recordings and stage productions into title results
            kinds = " ".join(_dc(rec, "type")).lower()
            if "sound" in kinds or "audio" in kinds or "video" in kinds:
                continue
            creators = [_clean_creator(c) for c in _dc(rec, "creator")]
            hits.append(BookHit(
                source=SOURCE_BNF, ref=(_dc(rec, "identifier") or [""])[0] or title,
                title=title, description=_clean((_dc(rec, "description") or [""])[0]),
                image=None, genres=[], year=_year((_dc(rec, "date") or [""])[0]),
                authors=[c for c in creators if c][:3],
                language=(_dc(rec, "language") or [None])[0],
            ))
        return hits


def _clean_creator(value: str) -> str:
    # "Saint-Exupery, Antoine de (1900-1944). Auteur du texte" -> the name
    return value.split(".")[0].strip()


def bnf_detail(ref: str) -> BookHit:
    hits = bnf_search(ref, 1)
    if not hits:
        raise ValueError(f"no BnF record for {ref}")
    return hits[0]


# --- registry -------------------------------------------------------------------

_SEARCHERS = {
    SOURCE_OPENLIBRARY: openlibrary_search,
    SOURCE_GOOGLE: google_search,
    SOURCE_ARCHIVE: archive_search,
    SOURCE_DNB: dnb_search,
    SOURCE_BNF: bnf_search,
    SOURCE_CROSSREF: crossref_search,
}

_DETAILERS = {
    SOURCE_OPENLIBRARY: openlibrary_detail,
    SOURCE_GOOGLE: google_detail,
    SOURCE_ARCHIVE: archive_detail,
    SOURCE_DNB: dnb_detail,
    SOURCE_BNF: bnf_detail,
    SOURCE_CROSSREF: crossref_detail,
}


def _usable(source: str, hits: list["BookHit"], query: str) -> list["BookHit"]:
    """Drop a provider's obviously irrelevant rows.

    The Internet Archive full-text index is the offender: a title search there
    returns court filings and corporate reports whose titles merely contain the
    word. A real book has an author and a title that resembles the query, so
    require both before letting an archive hit into the list.
    """
    out = []
    for hit in hits:
        if source == SOURCE_ARCHIVE:
            # Its full-text index matches any word anywhere in a scanned
            # document: a title search for "Solaris" returns a Sun certification
            # exam guide. Require a real title match, an author, and a title that
            # is not a sentence.
            if _relevance(query, hit.title) < 0.75 or not hit.authors:
                continue
            if len(hit.title.split()) > 12:
                continue
            # Accept only the same title: exact, or "Title: subtitle" / "Title
            # (qualifier)". Archive.org's index would otherwise answer "Dune"
            # with "Dune boy", "Dune buggies" and a paper on sand dunes.
            nt = _norm_title(hit.title)
            nq = _norm_title(query)
            if not (nt == nq or nt.startswith(nq + " : ") or nt.startswith(nq + " (")
                    or nt.startswith(nq + " :")):
                continue
        out.append(hit)
    return out


def _dedupe(hits: list["BookHit"]) -> list["BookHit"]:
    """Keep the first (highest-priority provider) copy of each work."""
    seen, out = set(), []
    for hit in hits:
        author = (hit.authors[0].lower() if hit.authors else "")
        key = (_norm_title(hit.title)[:40], author[:20])
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out


def _prefetch_openlibrary(key: tuple[str, int], future) -> None:
    def _store() -> None:
        try:
            hits = future.result(timeout=90)
        except Exception:  # noqa: BLE001 - a prefetch that fails is just a miss
            return
        if not hits:
            return
        query, limit = key
        ranked = _rank(query, _dedupe(list(hits)), limit)
        _CACHE[key] = (time.time(), ranked, [])

    threading.Thread(target=_store, daemon=True).start()


def search(query: str, limit: int = 8) -> tuple[list["BookHit"], list[str]]:
    """Search the catalogues, fast first and better second.

    Open Library has the best coverage but from this host it answers in 13-23
    seconds, which is not a search box. The national libraries answer in under a
    second and Internet Archive in one or two. So the fast three decide the
    response, and Open Library is used when it happens to be quick; when it is
    slow its answer is cached and the next search for the same title benefits.

    Nothing here waits more than FAST_DEADLINE for a slow provider.
    """
    key = (query.strip().casefold(), limit)
    cached = _CACHE.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1], cached[2]

    notes: list[str] = []
    skip_ol = len(query.strip()) < 3
    if skip_ol:
        notes.append("openlibrary: query too short.")

    order = [s for s in PROVIDER_ORDER if not (skip_ol and s == SOURCE_OPENLIBRARY)]
    fast = [s for s in order if s != SOURCE_OPENLIBRARY]
    results: dict[str, list[BookHit]] = {}

    executor = ThreadPoolExecutor(max_workers=len(order))
    futures = {executor.submit(_SEARCHERS[s], query, limit): s for s in order}
    try:
        deadline = time.monotonic() + FAST_DEADLINE
        pending = [f for f, src in futures.items() if src in fast]
        while pending and time.monotonic() < deadline:
            done, pending = wait(pending, timeout=max(0.0, deadline - time.monotonic()),
                                 return_when=FIRST_COMPLETED)
            for fut in done:
                source = futures[fut]
                try:
                    hits = _usable(source, fut.result() or [], query)
                except Exception as e:  # noqa: BLE001 - one provider down must not stop the rest
                    log.warning(f"{source} search failed: {e}")
                    notes.append(f"{source}: unreachable ({type(e).__name__}).")
                    continue
                results[source] = hits
                if not hits:
                    notes.append(f"{source}: nothing found.")

        # Open Library counts only if it already has an answer (or answers at once).
        ol_future = next((f for f, src in futures.items() if src == SOURCE_OPENLIBRARY), None)
        if ol_future is not None:
            try:
                ol_hits = _usable(SOURCE_OPENLIBRARY, ol_future.result(timeout=0.2) or [], query)
            except Exception:  # noqa: BLE001 - still running or failed; ignore it
                ol_hits = []
                if ol_future.done():
                    notes.append("openlibrary: unreachable.")
            if ol_hits:
                results[SOURCE_OPENLIBRARY] = ol_hits
            elif ol_future is not None and not ol_future.done():
                # Open Library is 13-23 s from here. Let it finish in the
                # background so the next search for this title gets the best
                # catalogue instead of the fast one.
                _prefetch_openlibrary(key, ol_future)
    finally:
        executor.shutdown(wait=False)

    merged: list[BookHit] = []
    for source in order:
        merged.extend(results.get(source, []))
    hits = _rank(query, _dedupe(merged), limit)
    if not hits:
        notes.append("no catalogue had a match; try the original title or add it manually.")

    _CACHE[key] = (time.time(), hits, notes)
    if len(_CACHE) > 256:
        oldest = sorted(_CACHE, key=lambda k: _CACHE[k][0])[:64]
        for k in oldest:
            _CACHE.pop(k, None)
    return hits, notes


def fetch_detail(source: str, ref: str) -> BookHit:
    if source not in _DETAILERS:
        raise ValueError(f"unknown metadata source: {source}")
    return _DETAILERS[source](ref)


def fetch_cover(url: str) -> bytes:
    with httpx.Client(timeout=httpx.Timeout(20.0), headers=UA, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.content
