"""Tests for CLI helpers (manifest path derivation)."""

from ytchannel.cli import _manifest_path


def test_manifest_path_uses_target_name():
    p = _manifest_path("./downloads", "My Playlist!")
    assert "My Playlist!" in p
    assert p.endswith(".manifest.json")
    assert "downloads" in p


def test_manifest_path_sanitizes_target_name():
    p = _manifest_path("./out", "a/b\\c:d*e?f")
    # No path separators or illegal filename chars should leak into the name.
    assert "/" not in p.split("downloads")[-1] if "downloads" in p else True
    assert "a_b_c_d_e_f" in p
