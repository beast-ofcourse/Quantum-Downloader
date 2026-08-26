"""Tests for CLI helpers (storage key + manifest path derivation)."""

from typer.testing import CliRunner

from ytchannel import cli as cli_mod
from ytchannel.cli import _manifest_path, _storage_key, app


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


def test_download_skips_complete_but_not_permanent_failures(monkeypatch, tmp_path):
    """Regression for 0.5: 'Skipped (already complete)' must count only completed
    videos, not permanent failures (the old len(videos)-len(pending) formula
    conflated the two)."""

    import re

    from ytchannel.manifest import Manifest

    def fake_resolve(url, quiet=True):
        return {
            "target_type": "channel",
            "target_name": "Test",
            "target_id": "UCx",
            "url": url,
            "videos": [{"video_id": f"v{i}", "title": f"V{i}"} for i in range(5)],
        }

    downloaded = []

    class FakeDownloader:
        def __init__(self, *args, **kwargs):
            pass

        def download(self, video, manifest, reporter=None):
            downloaded.append(video["video_id"])
            manifest.mark_complete(video["video_id"], "p.mp4")
            return {"video_id": video["video_id"], "status": "complete", "file_path": "p.mp4"}

    monkeypatch.setattr(cli_mod, "resolve_channel", fake_resolve)
    monkeypatch.setattr(cli_mod, "Downloader", FakeDownloader)

    # Pre-populate: v0,v1 complete; v2 permanent-failed; v3,v4 pending.
    mpath = tmp_path / "dl" / "channel_UCx.manifest.json"
    m = Manifest(str(mpath))
    for i in range(5):
        m.entries[f"v{i}"] = {
            "video_id": f"v{i}",
            "title": f"V{i}",
            "status": "pending",
            "file_path": None,
            "downloaded_at": None,
            "attempts": 0,
            "last_error": None,
            "permanent": False,
        }
    m.entries["v0"]["status"] = "complete"
    m.entries["v0"]["file_path"] = "p.mp4"
    m.entries["v1"]["status"] = "complete"
    m.entries["v1"]["file_path"] = "p.mp4"
    m.entries["v2"]["status"] = "failed"
    m.entries["v2"]["permanent"] = True
    m.save()

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["download", "https://www.youtube.com/@test", "-o", str(tmp_path / "dl")],
    )
    assert result.exit_code == 0, result.output
    # Only the two genuinely pending videos should be downloaded.
    assert set(downloaded) == {"v3", "v4"}
    # Skipped count must be 2 (complete only), not 3 (complete + permanent-failed).
    match = re.search(r"Skipped \(already complete\)\D+(\d+)", result.output)
    assert match is not None, result.output
    assert int(match.group(1)) == 2


def test_verify_manifest_path_reports_present(tmp_path):
    import json
    import os
    from pathlib import Path

    from ytchannel.utils.organize import build_channel_dir

    channel_dir = build_channel_dir(str(tmp_path), "TestChan")
    os.makedirs(channel_dir)
    media_path = str(Path(channel_dir) / "v0.mp4")
    Path(media_path).write_text("x", encoding="utf-8")

    manifest_path = tmp_path / "channel_TC.manifest.json"
    manifest_path.write_text(
        json.dumps({
            "channel_name": "TestChan",
            "videos": {
                "v0": {
                    "video_id": "v0", "title": "V0", "status": "complete",
                    "file_path": media_path, "downloaded_at": None,
                    "attempts": 0, "last_error": None, "permanent": False,
                }
            },
        }),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["verify", str(manifest_path)])
    assert result.exit_code == 0, result.output
    assert "Complete (file present)" in result.output
    assert "1" in result.output


def test_update_command_adds_videos(monkeypatch, tmp_path):
    def fake_resolve(url, quiet=True):
        return {
            "target_type": "channel",
            "target_name": "Test",
            "target_id": "UCx",
            "url": url,
            "videos": [{"video_id": f"v{i}", "title": f"V{i}"} for i in range(3)],
        }

    monkeypatch.setattr(cli_mod, "resolve_channel", fake_resolve)

    runner = CliRunner()
    result = runner.invoke(
        app, ["update", "https://yt/@t", "-o", str(tmp_path / "dl")]
    )
    assert result.exit_code == 0, result.output
    assert "added 3 new video(s)" in result.output.lower()


def test_download_rejects_both_cookie_sources(monkeypatch):
    def fake_resolve(url, quiet=True):
        return {
            "target_type": "channel",
            "target_name": "T",
            "target_id": "UCx",
            "url": url,
            "videos": [{"video_id": "v0", "title": "V0"}],
        }

    monkeypatch.setattr(cli_mod, "resolve_channel", fake_resolve)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "download",
            "https://yt/@t",
            "--cookies",
            "c.txt",
            "--cookies-from-browser",
            "chrome",
        ],
    )
    assert result.exit_code != 0
    assert "both" in result.output.lower()
