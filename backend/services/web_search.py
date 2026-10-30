"""Best-effort web search for the master agent (DuckDuckGo HTML endpoint).

No API key needed. Used to identify niche titles and resolve volume/chapter
organization questions when NovelUpdates comes up empty. Failures raise — the
agent tool layer turns that into an honest error message for the model.
"""
from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from logging_config import get_logger

log = get_logger("web_search")

_UA = "Mozilla/5.0"


def _real_url(href: str) -> str:
    """DDG wraps results in /l/?uddg=<encoded> redirects — unwrap them."""
    if href.startswith("//duckduckgo.com/l/") or href.startswith("/l/"):
        qs = parse_qs(urlparse(href).query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    return href


def search(query: str, max_results: int = 6) -> list[dict]:
    """Search the web through the endpoint that is still a search page.

    html.duckduckgo.com now answers a datacenter IP with a bot challenge (HTTP
    202, "select all squares containing a duck"), which parsed to zero results
    and made the agent guess. lite.duckduckgo.com still serves ordinary results;
    Mojeek is the fallback if it ever stops.
    """
    import httpx

    for name, fetcher in (("bing", _bing), ("ddg-lite", _ddg_lite)):
        try:
            results = fetcher(query, max_results)
        except Exception as e:  # noqa: BLE001 - a dead engine is not an agent failure
            log.warning(f"web search via {name} failed: {e}")
            continue
        if results:
            log.info(f"web search '{query}': {len(results)} result(s) via {name}")
            return results
        log.warning(f"web search via {name}: 0 result(s)")
    return []


def _fetch(url: str, data: dict | None = None) -> str:
    import httpx

    r = httpx.post(url, data=data, headers={"User-Agent": _UA}, timeout=20,
                   follow_redirects=True) if data else \
        httpx.get(url, headers={"User-Agent": _UA}, timeout=20, follow_redirects=True)
    r.raise_for_status()
    if "select all squares" in r.text.lower():
        raise RuntimeError("bot challenge")
    return r.text


def _bing(query: str, max_results: int) -> list[dict]:
    """Bing's HTML results page.

    DuckDuckGo answers a datacenter IP with a bot challenge and this host is
    rate-limited by it, so Bing is the primary engine. It serves ordinary HTML
    with no key.
    """
    soup = BeautifulSoup(
        _fetch(f"https://www.bing.com/search?q={quote_plus(query)}&setlang=en"),
        "html.parser")
    results: list[dict] = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if a is None or not a.get("href"):
            continue
        caption = li.select_one(".b_caption p") or li.select_one("p")
        results.append({
            "title": a.get_text(" ", strip=True),
            "url": a["href"],
            "snippet": caption.get_text(" ", strip=True) if caption else "",
        })
        if len(results) >= max_results:
            break
    return results


def _ddg_lite(query: str, max_results: int) -> list[dict]:
    soup = BeautifulSoup(
        _fetch(f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}"), "html.parser")
    results: list[dict] = []
    for a in soup.select("a.result-link"):
        href = a.get("href") or ""
        snippet_el = a.find_next("td", class_="result-snippet")
        results.append({
            "title": a.get_text(" ", strip=True),
            "url": _real_url(href),
            "snippet": snippet_el.get_text(" ", strip=True) if snippet_el else "",
        })
        if len(results) >= max_results:
            break
    return results


def _mojeek(query: str, max_results: int) -> list[dict]:
    soup = BeautifulSoup(_fetch(f"https://www.mojeek.com/search?q={quote_plus(query)}"), "html.parser")
    results: list[dict] = []
    for li in soup.select("ul.results-standard li"):
        a = li.select_one("a.title") or li.select_one("h2 a")
        if a is None:
            continue
        p = li.select_one("p.s")
        results.append({
            "title": a.get_text(" ", strip=True),
            "url": a.get("href") or "",
            "snippet": p.get_text(" ", strip=True) if p else "",
        })
        if len(results) >= max_results:
            break
    return results
