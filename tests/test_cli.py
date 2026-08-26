"""Tests for CLI helpers (storage key + manifest path derivation)."""

from ytchannel.cli import _manifest_path, _storage_key


def test_storage_key_combines_type_and_id():
    assert _storage_key({"target_type": "playlist", "target_id": "PLabc"}) == "playlist_PLabc"
    assert _storage_key({"target_type": "channel", "target_id": "UC123"}) == "channel_UC123"


def test_storage_key_falls_back_to_name():
    assert _storage_key({"target_type": "channel"}) == "channel_unknown"
    assert _storage_key({"target_type": "playlist", "target_name": "My List"}) == "playlist_My List"


def test_manifest_path_uses_storage_key():
    p = _manifest_path("./downloads", "playlist_PLabc")
    assert "playlist_PLabc" in p
    assert p.endswith(".manifest.json")
    assert "downloads" in p


def test_manifest_path_sanitizes_key():
    p = _manifest_path("./out", "playlist/PL:a*b?c")
    # Illegal filename chars are collapsed to underscores.
    assert "playlist_PL_a_b_c" in p
