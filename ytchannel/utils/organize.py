"""Filesystem-safe naming and output directory layout.

Output structure: <output_dir>/<channel_name>/<upload_date>_<video_title>.<ext>

Filenames are sanitized so they are safe across operating systems (Windows in
particular has a strict set of illegal characters and a 260-char path limit).
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Characters illegal in Windows (and also undesirable on POSIX) filenames.
_ILLEGAL_CHARS = r'<>:"/\\|?*'
_ILLEGAL_RE = re.compile("[" + re.escape(_ILLEGAL_CHARS) + r"\x00-\x1f]")
_MAX_SEGMENT = 200


def sanitize_segment(name: str | None, max_len: int = _MAX_SEGMENT) -> str:
    """Return a filesystem-safe version of a single path segment (file or dir name)."""
    if name is None:
        name = ""
    name = str(name).strip()
    # Normalize unicode to a canonical composed form.
    name = unicodedata.normalize("NFC", name)
    # Replace illegal characters with underscores.
    name = _ILLEGAL_RE.sub("_", name)
    # Collapse runs of whitespace into a single space.
    name = re.sub(r"\s+", " ", name).strip()
    # Drop a trailing dot or space (Windows forbids these at end of names).
    name = name.rstrip(". ")
    if not name:
        name = "untitled"
    if len(name) > max_len:
        name = name[:max_len].rstrip(". ")
    return name


def build_channel_dir(output_dir: str | Path, channel_name: str | None) -> str:
    """Return the absolute-ish path to the channel's output directory, sanitized."""
    safe = sanitize_segment(channel_name or "channel")
    return str(Path(output_dir) / safe)


def output_template(channel_dir: str | Path) -> str:
    """Return a yt-dlp outtmpl placing files under channel_dir with a date+title name."""
    return str(Path(channel_dir) / "%(upload_date)s_%(title)s.%(ext)s")
