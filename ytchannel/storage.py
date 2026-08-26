"""Shared storage helpers for stable on-disk target identity.

These were originally private helpers in ``cli.py``; they are extracted here so
the web service (and any other caller) can compute the same manifest path the
CLI uses, keeping the on-disk layout identical across entry points.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .utils.organize import sanitize_segment


def storage_key(result: Dict[str, Any]) -> str:
    """Stable on-disk identity for a target: type + id.

    Uses the immutable YouTube id, not the display title, so a channel or
    playlist renaming itself does not orphan the manifest or download folder.
    """
    ttype = result.get("target_type", "target")
    tid = result.get("target_id") or result.get("target_name") or "unknown"
    return f"{ttype}_{tid}"


def manifest_path(output_dir: str, key: str) -> str:
    """Path to the manifest file for a given target (channel or playlist).

    Keyed by the stable storage key (target_type + target_id), not the display
    name, so title renames don't break resume/idempotency.
    """
    return str(Path(output_dir) / (sanitize_segment(key) + ".manifest.json"))
