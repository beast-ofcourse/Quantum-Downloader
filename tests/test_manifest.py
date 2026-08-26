"""Tests for manifest state tracking and reconciliation (Phase 2)."""

import json

from ytchannel.manifest import Manifest, ManifestError


def _videos(ids):
    return [{"video_id": v, "title": f"Title {v}"} for v in ids]


def test_reconcile_adds_new_videos_as_pending(tmp_path):
    m = Manifest(str(tmp_path / "m.json"))
    m.reconcile(_videos(["a", "b"]), "Chan")
    assert set(m.entries.keys()) == {"a", "b"}
    assert m.entries["a"]["status"] == "pending"
    assert m.is_complete("a") is False


def test_reconcile_keeps_complete_untouched(tmp_path):
    m = Manifest(str(tmp_path / "m.json"))
    m.reconcile(_videos(["a", "b"]), "Chan")
    m.mark_complete("a", "/path/a.mp4")
    # Re-run with a new video 'c'
    m.reconcile(_videos(["a", "b", "c"]), "Chan")
    assert m.is_complete("a") is True
    assert m.entries["a"]["file_path"] == "/path/a.mp4"
    assert "c" in m.entries
    assert m.entries["c"]["status"] == "pending"


def test_reconcile_resets_downloading_from_crash(tmp_path):
    m = Manifest(str(tmp_path / "m.json"))
    m.reconcile(_videos(["a"]), "Chan")
    m.mark_downloading("a")
    assert m.entries["a"]["status"] == "downloading"
    # Simulate restart
    m2 = Manifest(str(tmp_path / "m.json"))
    m2.reconcile(_videos(["a"]), "Chan")
    assert m2.entries["a"]["status"] == "pending"


def test_get_pending_retries_nonpermanent_failed(tmp_path):
    m = Manifest(str(tmp_path / "m.json"))
    m.reconcile(_videos(["a", "b", "c"]), "Chan")
    m.mark_failed("a", "transient 429", permanent=False)
    m.mark_failed("b", "private video", permanent=True)
    pending = set(m.get_pending())
    assert "a" in pending  # retried
    assert "b" not in pending  # permanent, skipped
    assert "c" in pending  # still pending


def test_atomic_write_produces_valid_json(tmp_path):
    p = str(tmp_path / "m.json")
    m = Manifest(p)
    m.reconcile(_videos(["a"]), "Chan")
    m.mark_complete("a", "/x.mp4")
    # File exists and is valid JSON with expected structure
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    assert data["channel_name"] == "Chan"
    assert data["videos"]["a"]["status"] == "complete"


def test_load_corrupt_manifest_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    try:
        Manifest(str(p))
        assert False, "expected ManifestError"
    except ManifestError:
        pass


def test_check_files_present_missing_orphan(tmp_path):
    import os
    from pathlib import Path

    from ytchannel.utils.organize import build_channel_dir

    channel_dir = build_channel_dir(str(tmp_path), "TestChan")
    os.makedirs(channel_dir)

    manifest = Manifest(str(tmp_path / "channel_TC.manifest.json"))
    manifest.channel_name = "TestChan"

    # v0: complete, file present on disk.
    present_path = str(Path(channel_dir) / "present.mp4")
    Path(present_path).write_text("x", encoding="utf-8")
    manifest.entries["v0"] = {
        "video_id": "v0", "title": "V0", "status": "complete",
        "file_path": present_path, "downloaded_at": None,
        "attempts": 0, "last_error": None, "permanent": False,
    }
    # v1: complete, file missing.
    manifest.entries["v1"] = {
        "video_id": "v1", "title": "V1", "status": "complete",
        "file_path": str(Path(channel_dir) / "gone.mp4"), "downloaded_at": None,
        "attempts": 0, "last_error": None, "permanent": False,
    }
    # v2: pending, no file_path.
    manifest.entries["v2"] = {
        "video_id": "v2", "title": "V2", "status": "pending",
        "file_path": None, "downloaded_at": None,
        "attempts": 0, "last_error": None, "permanent": False,
    }

    # Orphan media file (not referenced by any entry).
    orphan_path = str(Path(channel_dir) / "orphan.mkv")
    Path(orphan_path).write_text("x", encoding="utf-8")
    # Non-media sidecar must NOT be counted as an orphan.
    Path(channel_dir, "notes.txt").write_text("x", encoding="utf-8")

    report = manifest.check_files(str(tmp_path))

    assert len(report["complete_present"]) == 1
    assert report["complete_present"][0]["video_id"] == "v0"
    assert len(report["complete_missing"]) == 1
    assert report["complete_missing"][0]["video_id"] == "v1"
    assert any(p.endswith("orphan.mkv") for p in report["orphan_on_disk"])
    assert not any(p.endswith(".txt") for p in report["orphan_on_disk"])
