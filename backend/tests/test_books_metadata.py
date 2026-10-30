"""Book catalogue adapters.

These tests are offline on purpose: they cover the normalisation that every
provider's payload passes through, not the providers' uptime. A network test
here would fail in CI for reasons that have nothing to do with our code.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import books_metadata as bm  # noqa: E402


def test_clean_strips_markup_and_keeps_paragraphs():
    assert bm._clean("<p>Hello<br/>world</p>") == "Hello\nworld"
    assert bm._clean(None) == ""
    assert bm._clean("<b>A</b>\n\n\n\n<b>B</b>") == "A\n\nB"


def test_year_finds_a_four_digit_year_in_messy_strings():
    assert bm._year("2008-01-01") == "2008"
    assert bm._year("published 1987, reprinted") == "1987"
    assert bm._year(2002) == "2002"
    assert bm._year("") is None
    assert bm._year("no date here") is None


def test_first_handles_the_three_shapes_open_library_returns():
    assert bm._first("plain") == "plain"
    assert bm._first({"value": "boxed"}) == "boxed"
    assert bm._first(["list", "of", "values"]) == "list"
    assert bm._first(None) == ""


def test_author_joins_the_first_two_names():
    hit = bm.BookHit(source="openlibrary", ref="x", title="t", authors=["A", "B", "C"])
    assert hit.author == "A, B"
    assert bm.BookHit(source="openlibrary", ref="x", title="t").author == ""


def test_every_catalogue_is_registered_with_matching_handlers():
    # The order is a latency decision: the national catalogues answer in about a
    # second, Open Library can take twenty, and Google Books is only asked when a
    # key is configured (anonymous calls answer 429 from a server IP). Crossref
    # is not in the chain at all - it answers a novel title with a paper.
    assert bm.PROVIDER_ORDER[:2] == [bm.SOURCE_OPENLIBRARY, bm.SOURCE_ARCHIVE]
    assert bm.SOURCE_DNB in bm.PROVIDER_ORDER
    assert bm.SOURCE_BNF in bm.PROVIDER_ORDER
    assert bm.SOURCE_CROSSREF not in bm.PROVIDER_ORDER
    if not bm.GOOGLE_BOOKS_API_KEY:
        assert bm.SOURCE_GOOGLE not in bm.PROVIDER_ORDER
    for source in bm.PROVIDER_ORDER:
        assert source in bm._SEARCHERS
        assert source in bm._DETAILERS


def test_unknown_source_is_rejected_rather_than_silently_swallowed():
    try:
        bm.fetch_detail("ani list", "1")
    except ValueError as e:
        assert "unknown metadata source" in str(e)
    else:
        raise AssertionError("an unknown source must raise")


def test_relevance_prefers_exact_and_prefix_titles():
    assert bm._relevance("Solaris", "Solaris") > bm._relevance("Solaris", "Consider Phlebas")
    assert bm._relevance("der kleine prinz", "Der Kleine Prinz") >= 0.9
    assert bm._relevance("吾輩は猫である", "吾輩は猫である") >= 0.9


def test_rank_keeps_a_shelf_when_nothing_matches_well():
    hits = [bm.BookHit(source="openlibrary", ref=str(i), title=t)
            for i, t in enumerate(["Aaa", "Bbb", "Ccc"])]
    # a Chinese query scores ~0 against latin titles, but the user still needs hits
    assert len(bm._rank("\u4e09\u4f53", hits, 3)) == 3
