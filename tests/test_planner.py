"""Tests for the download planner (filtering + pending/complete accounting)."""

from ytchannel.config import Config
from ytchannel.manifest import Manifest
from ytchannel.planner import filter_videos, plan_downloads


def _make_manifest(tmp_path):
    """Manifest with: v0,v1 complete; v2 permanent-failed; v3,v4 pending."""
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
    return m


def test_plan_downloads_excludes_permanent_failures(tmp_path):
    """Regression for 0.5: already_complete counts only completed videos,
    not permanent failures. Pending must be the two genuinely-pending videos."""
    manifest = _make_manifest(tmp_path)
    videos = [{"video_id": f"v{i}", "title": f"V{i}"} for i in range(5)]
    cfg = Config()

    plan = plan_downloads(videos, manifest, cfg)

    assert plan.pending == ["v3", "v4"]
    assert plan.already_complete == 2  # NOT 3 (must exclude the permanent failure)


def test_filter_videos_applies_limit_and_dates(tmp_path):
    """filter_videos applies date + limit filters without validating limit sign."""
    videos = [
        {"video_id": f"v{i}", "title": f"V{i}", "upload_date": f"2024010{i+1}"}
        for i in range(5)
    ]
    cfg = Config()
    cfg.after = "20240103"
    cfg.limit = 2

    filtered = filter_videos(videos, cfg)

    # after 20240103 keeps v2,v3,v4; limit 2 -> v2,v3.
    assert [v["video_id"] for v in filtered] == ["v2", "v3"]
