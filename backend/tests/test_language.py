"""Source-language detection and resolution.

Detection is a heuristic, so it is tested on the cases that actually decide the
answer: kana always means Japanese, hangul means Korean, bare han means Chinese,
and an explicit choice always beats a detection.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import language  # noqa: E402

JAPANESE = "吾輩は猫である。名前はまだ無い。どこで生れたかとんと見当がつかぬ。"
CHINESE = "这是中文文本，用于测试语言识别是否正确。"
KOREAN = "이것은 한국어 텍스트입니다. 언어 감지를 테스트하기 위한 것입니다."
ENGLISH = "This is an English sentence used to test language detection."


def test_detects_japanese_even_when_han_dominates():
    # Japanese prose is mostly han with kana mixed in; kana must win.
    text = "第一章 彼女は静かに笑った。" + "猫" * 40
    assert language.detect_language(text) == language.JAPANESE


def test_detects_chinese_without_kana():
    assert language.detect_language(CHINESE) == language.CHINESE


def test_detects_korean():
    assert language.detect_language(KOREAN) == language.KOREAN


def test_detects_english():
    assert language.detect_language(ENGLISH) == language.ENGLISH


def test_empty_and_noise_return_empty():
    assert language.detect_language("") == ""
    assert language.detect_language("   \n\t ") == ""
    assert language.detect_language("123 456 !!! ???") == ""


def test_is_auto_covers_the_spellings_the_ui_and_db_use():
    for value in (None, "", "auto", "AUTO", " Auto ", "detected", "unknown"):
        assert language.is_auto(value), value
    for value in ("Japanese", "English", "Chinese"):
        assert not language.is_auto(value)


def test_explicit_choice_beats_detection():
    assert language.resolve("Korean", "Japanese", JAPANESE) == "Korean"


def test_stored_detection_is_reused_before_detecting_again():
    # declared auto, already detected once -> no work on later pages
    assert language.resolve("auto", "Chinese", JAPANESE) == "Chinese"


def test_falls_back_to_detecting_the_sample():
    assert language.resolve("auto", "", JAPANESE) == language.JAPANESE


def test_describe_source_never_leaks_the_word_auto_into_a_prompt():
    text = language.describe_source("auto", "")
    assert "auto" not in text.lower()
    assert language.describe_source("auto", "Japanese") == "Japanese"
    assert language.describe_source("French", "Japanese") == "French"
