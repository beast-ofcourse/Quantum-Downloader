"""Tests for indexer export and dry-run summary."""

import csv
import json

from ytchannel.indexer import dry_run_summary, export_csv, export_json, export_jsonl

SAMPLE = {
    "channel_name": "TestChan",
    "channel_id": "UC123",
    "url": "https://www.youtube.com/@TestChan/videos",
    "videos": [
        {"video_id": "a", "title": "A", "url": "u1", "upload_date": "20230101", "duration": 10, "view_count": 5},
        {"video_id": "b", "title": "B", "url": "u2", "upload_date": "20230201", "duration": 20, "view_count": 6},
    ],
}


def test_export_json(tmp_path):
    out = tmp_path / "c.json"
    export_json(SAMPLE, str(out))
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["video_count"] == 2
    assert data["videos"][0]["video_id"] == "a"


def test_export_csv(tmp_path):
    out = tmp_path / "c.csv"
    export_csv(SAMPLE, str(out))
    with open(out, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["video_id"] == "a"
    assert "upload_date" in rows[0]


def test_dry_run_summary_computes_counts_and_dates():
    s = dry_run_summary(SAMPLE)
    assert s["count"] == 2
    assert s["date_range"] == ("20230101", "20230201")
    assert s["total_duration_seconds"] == 30
    assert s["estimated_size"] is None


def test_dry_run_summary_handles_missing_dates():
    no_dates = {**SAMPLE, "videos": [{"video_id": "x", "title": "X", "url": "u"}]}
    s = dry_run_summary(no_dates)
    assert s["date_range"] is None
    assert s["count"] == 1


def test_export_jsonl(tmp_path):
    out = tmp_path / "c.jsonl"
    export_jsonl(SAMPLE, str(out))
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0] == SAMPLE["videos"][0]
    assert parsed[1] == SAMPLE["videos"][1]
