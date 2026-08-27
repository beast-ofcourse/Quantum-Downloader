"""Tests for filesystem organization helpers."""

import os
import sys

import pytest

from ytchannel.utils.organize import (
    build_channel_dir,
    output_template,
    safe_output_path,
    sanitize_segment,
)

# The truncation branch in safe_output_path only runs when os.name == "nt" and
# relies on WindowsPath, which cannot be instantiated off Windows. Guard the
# Windows-specific cases so they run only on a real Windows host.
_requires_windows = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows-specific path truncation behavior"
)


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


def test_safe_output_path_unchanged_when_not_windows():
    # On a non-Windows host the path is returned unchanged. On Windows we
    # exercise the truncation branch via the os.name='nt' monkeypatch tests
    # below (forcing 'posix' here would break pathlib.Path on Windows).
    if os.name != "nt":
        p = safe_output_path("/dl/chan", "20230101", "A" * 1000, ".mp4")
        assert p.endswith("20230101_" + "A" * 1000 + ".mp4")


@_requires_windows
def test_safe_output_path_truncates_on_nt(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    long_title = "T" * 1000
    p = safe_output_path("/dl/chan", "20230101", long_title, ".mp4")
    assert len(p) <= 259
    # A hash suffix is appended to avoid collisions between truncated titles.
    assert "_" in p


@_requires_windows
def test_safe_output_path_distinct_suffixes_for_similar_titles(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    a = "A" * 1000
    b = "B" + "A" * 999  # differs only in the first char; truncates to same prefix
    pa = safe_output_path("/dl/chan", "20230101", a, ".mp4")
    pb = safe_output_path("/dl/chan", "20230101", b, ".mp4")
    assert pa != pb
    assert len(pa) <= 259
    assert len(pb) <= 259


@_requires_windows
def test_safe_output_path_handles_missing_date(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    p = safe_output_path("/dl/chan", None, "X" * 1000, ".mkv")
    assert len(p) <= 259
    # The path stays under the channel directory (separator-agnostic check).
    assert "chan" in p
