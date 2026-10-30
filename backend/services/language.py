"""Source-language handling.

A series does not declare its language up front: the first page tells us. Every
call site that used to assume Japanese now resolves through here, so an
unspecified series is detected rather than guessed.

Detection is a script heuristic, not a classifier: it counts Unicode script
ranges and picks the script that dominates the text. That is enough to separate
Japanese from Chinese and Korean (kana are the giveaway), and it is instant and
dependency-free, which matters because it runs on the ingest path.
"""

from __future__ import annotations

import re
from collections import Counter

AUTO = "auto"

# Script names returned by detection, matching the strings the UI offers.
JAPANESE = "Japanese"
KOREAN = "Korean"
CHINESE = "Chinese"
RUSSIAN = "Russian"
ARABIC = "Arabic"
ENGLISH = "English"

_HIRAGANA = re.compile(r"[\u3040-\u309f]")
_KATAKANA = re.compile(r"[\u30a0-\u30ff]")
_HAN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_HANGUL = re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
_ARABIC = re.compile(r"[\u0600-\u06ff]")
_LATIN = re.compile(r"[A-Za-z]")
# Kana and hangul are unambiguous; han appears in Japanese text too, so it only
# decides when neither of the others is present.
_STRONG = {JAPANESE: (_HIRAGANA, _KATAKANA), KOREAN: (_HANGUL,), RUSSIAN: (_CYRILLIC,),
           ARABIC: (_ARABIC,), CHINESE: (_HAN,)}


def is_auto(value: str | None) -> bool:
    return not value or value.strip().lower() in {AUTO, "detected", "unknown", ""}


def detect_language(text: str) -> str:
    """Return the dominant language of `text`, or "" when there is not enough signal.

    Kana wins over han for Japanese because Japanese prose contains both; a
    document with only han is treated as Chinese.
    """
    if not text or not text.strip():
        return ""
    sample = text[:20000]
    counts = Counter()
    for lang, patterns in _STRONG.items():
        counts[lang] = sum(len(p.findall(sample)) for p in patterns)
    latin = len(_LATIN.findall(sample))

    # Any kana at all makes Japanese the answer: Chinese and Korean never use it.
    if counts[JAPANESE] >= 3:
        return JAPANESE
    if counts[KOREAN] >= 3:
        return KOREAN
    if counts[CHINESE] >= 10:
        return CHINESE
    if counts[RUSSIAN] >= 10:
        return RUSSIAN
    if counts[ARABIC] >= 10:
        return ARABIC
    if latin >= 20:
        return ENGLISH
    return ""


def describe_source(value: str | None, detected: str | None = None) -> str:
    """How to phrase the source language inside a prompt."""
    for candidate in (value, detected):
        if candidate and not is_auto(candidate):
            return candidate
    return "the page's source language (detect it yourself)"


def resolve(declared: str | None, detected: str | None, sample_text: str = "") -> str:
    """Prefer an explicit choice, then a stored detection, then detect now."""
    if declared and not is_auto(declared):
        return declared
    if detected and not is_auto(detected):
        return detected
    return detect_language(sample_text)
