"""Filesystem-safe naming and output directory layout.

Output structure: <output_dir>/<channel_name>/<upload_date>_<video_title>.<ext>

Filenames are sanitized so they are safe across operating systems (Windows in
particular has a strict set of illegal characters and a 260-char path limit).
"""

from __future__ import annotations

import hashlib
import os
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


# Windows' legacy MAX_PATH limit for the CreateFileW "short" path form.
_MAX_PATH = 259


def safe_output_path(
    channel_dir: str | Path,
    upload_date: str | None,
    title: str,
    ext: str,
) -> str:
    """Return a filesystem path for a downloaded video, truncating on Windows.

    The base form is ``<channel_dir>/<upload_date>_<title>.<ext>``. On Windows
    (``os.name == "nt"``) when that path would exceed the legacy 259-character
    limit, the ``title`` segment is truncated so the total length is <= 259 and a
    short hash suffix (first 8 hex chars of the md5 of the original title) is
    appended to avoid collisions between two titles that truncate to the same
    string. On non-Windows platforms the normal path is returned unchanged.
    """
    base = Path(channel_dir)
    date_seg = (upload_date or "") if upload_date else ""
    name = f"{date_seg}_{title}" if date_seg else (title or "untitled")
    suffix = ext if ext.startswith(".") else (("." + ext) if ext else "")

    full = str(base / (name + suffix))

    if os.name != "nt" or len(full) <= _MAX_PATH:
        return full

    # Reserve room for the hash suffix ("_<8 hex>") and the extension.
    hash_suffix = "_" + hashlib.md5(title.encode("utf-8")).hexdigest()[:8]
    # channel_dir + separator + date prefix + "_" + title + hash_suffix + suffix
    # Build the longest title that fits.
    prefix = str(base / (date_seg + "_" if date_seg else ""))
    # Account for the path separator between prefix and title.
    overhead = len(prefix) + 1 + len(hash_suffix) + len(suffix)
    max_title = _MAX_PATH - overhead
    if max_title < 1:
        # Extremely long channel_dir; fall back to a hash-only name.
        truncated = hash_suffix
    else:
        safe_title = title[:max_title]
        truncated = safe_title + hash_suffix
    return str(base / (truncated + suffix))
