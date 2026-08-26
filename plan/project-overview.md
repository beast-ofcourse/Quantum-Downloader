# YT Channel Archiver — Project Overview

## What this is

A command-line tool that takes a YouTube channel URL and downloads every video on that channel to local storage, with resumable downloads, organized output, and metadata export. It's a workflow layer on top of `yt-dlp`, which handles the actual extraction and downloading — this project's job is channel-level orchestration, idempotency, and a good CLI experience.

## Problem it solves

Downloading a single YouTube video is a solved problem (`yt-dlp <url>`). Downloading an *entire channel* reliably is not, because:

- Channels can have hundreds or thousands of videos — you can't just fire off downloads and hope nothing crashes halfway through.
- Re-running the same command shouldn't re-download everything from scratch.
- You want to know what you're about to download (size, count, date range) before committing to a multi-hour job.
- YouTube rate-limits aggressive scraping, so naive parallelism gets you throttled or blocked.
- Metadata (titles, upload dates, thumbnails, subtitles) is often as valuable as the video itself for archival purposes.

This tool wraps all of that into a single, repeatable command.

## Intended use cases

- Archiving your **own** channel as a backup.
- Archiving channels you have explicit permission or rights to download (Creative Commons, public domain, licensed content).
- Personal offline access to content you already have the right to view/download under YouTube's terms (e.g. via YouTube Premium offline features is the *sanctioned* path — this tool is for cases outside that, where rights are clear).

This is explicitly **not** intended as a tool for redistributing or pirating copyrighted content you don't have rights to. That scope should be stated clearly in the README and license.

## Core architecture

```
ytchannel/
├── cli.py              # Entry point, argument parsing (click/typer)
├── resolver.py         # Turns a channel URL into a canonical channel ID + video list
├── downloader.py        # Wraps yt-dlp, handles format selection, progress hooks
├── manifest.py          # Tracks download state (JSON/SQLite) for resumability
├── indexer.py           # Metadata-only extraction (no download) — for --dry-run / index command
├── config.py            # Loads defaults from config file + CLI overrides
└── utils/
    ├── rate_limit.py     # Delay/backoff logic between requests
    └── organize.py       # File naming, folder structure, sanitization
```

### Data flow

1. **Resolve**: channel URL → canonical channel ID → full video ID list (via `yt-dlp`'s flat playlist extraction, which is fast and doesn't download anything).
2. **Plan**: cross-reference video list against the manifest to see what's already downloaded. Compute what's left, estimate total size if possible.
3. **Execute**: download remaining videos, one at a time or with limited concurrency, updating the manifest after each success so a crash doesn't lose progress.
4. **Report**: summary of what was downloaded, skipped, and failed.

### Why a manifest and not just "check if the file exists"

Filenames can change (title edits, sanitization differences), and checking disk state alone is fragile. A manifest (JSON or SQLite, keyed by video ID) is the source of truth for "have I processed this video," independent of what the output file happens to be named. The actual file path is stored *in* the manifest entry, not derived from it.

## Key design decisions

| Decision | Choice | Why |
|---|---|---|
| Language | Python | `yt-dlp` is a Python library — importing it directly gives clean access to progress hooks and metadata, vs. shelling out to a binary and parsing stdout. |
| CLI framework | `typer` | Type-hint-based, generates `--help` automatically, good UX for minimal boilerplate. |
| Progress display | `rich` | Multi-progress-bar support for concurrent downloads, readable tables for dry-run output. |
| Manifest storage | JSON file initially, SQLite if scale demands it | JSON is human-readable and good enough for most channels; SQLite is a drop-in upgrade for very large channels (5000+ videos) without changing the interface. |
| Concurrency | Sequential by default, opt-in limited parallelism | Avoids rate-limiting/blocking by default; power users can opt into 2-4 concurrent downloads. |
| Packaging | pip-installable, console script entry point | `pip install ytchannel` → `ytchannel download <url>` just works. |

## Out of scope (for v1)

- A GUI or web interface.
- Uploading/mirroring content elsewhere.
- Transcoding/re-encoding downloaded videos (format selection at download time is enough).
- Bypassing age restrictions, members-only content without valid cookies, or any other access control circumvention beyond what `yt-dlp` supports through legitimate authentication (cookies from a logged-in session the user already has).

## Success criteria

- Running `ytchannel download <channel-url>` on a channel with 500+ videos completes without manual intervention, survives a mid-run interruption (ctrl-C or crash), and resumes cleanly on re-run.
- `ytchannel index <channel-url>` produces an accurate metadata export (JSON/CSV) in under a minute for a typical channel, without downloading any video files.
- Default settings don't trigger YouTube rate-limiting on a normal home connection.
