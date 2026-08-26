"""Tests for filesystem organization helpers."""

from ytchannel.utils.organize import build_channel_dir, output_template, sanitize_segment


def test_sanitize_removes_illegal_chars():
    # a / b : c * ?  ->  a _ b _ c _ _
    assert sanitize_segment('a/b:c*?') == "a_b_c__"
    assert "<>:\"/\\|?*" not in sanitize_segment('<>:"/\\|?*')


def test_sanitize_collapses_whitespace_and_strips():
    assert sanitize_segment("  hello   world  ") == "hello world"


def test_sanitize_handles_empty_and_none():
    assert sanitize_segment("") == "untitled"
    assert sanitize_segment(None) == "untitled"


def test_sanitize_truncates_long_names():
    long = "x" * 500
    assert len(sanitize_segment(long, max_len=50)) == 50


def test_sanitize_strips_trailing_dot():
    assert sanitize_segment("title.") == "title"


def test_build_channel_dir_sanitizes_name():
    path = build_channel_dir("./downloads", "My Channel: Best/2024")
    assert path.endswith("My Channel_ Best_2024")
    assert path.startswith("downloads")


def test_output_template_has_ext_placeholder():
    t = output_template("/dl/chan")
    assert t.endswith("%(upload_date)s_%(title)s.%(ext)s")
    assert "chan" in t
    assert "dl" in t
