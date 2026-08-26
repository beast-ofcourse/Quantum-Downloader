# Implementation Plan — YT Channel Archiver

Companion to `project-overview.md`. Tasks are grouped into phases; each phase should leave the tool in a working, demoable state. Check off items as completed.

---

## Phase 0 — Project setup

- [ ] Initialize repo with `pyproject.toml` (use `hatchling` or `setuptools` as build backend)
- [ ] Set up package structure: `ytchannel/` with `__init__.py`, `cli.py`, and empty module stubs matching the architecture in `project-overview.md`
- [ ] Add core dependencies: `yt-dlp`, `typer`, `rich`
- [ ] Add dev dependencies: `pytest`, `ruff` (lint/format), `mypy` (optional but recommended given type-hint-heavy `typer` usage)
- [ ] Set up console script entry point in `pyproject.toml` so `ytchannel` resolves after `pip install -e .`
- [ ] Add `.gitignore` (Python defaults + `downloads/`, `*.manifest.json`)
- [ ] Write initial `README.md` with install instructions and a scope/legal disclaimer (see project-overview.md "Intended use cases")
- [ ] Confirm `yt-dlp` is importable and can extract info for a single test video (`yt_dlp.YoutubeDL().extract_info(url, download=False)`) — sanity check before building anything on top

**Exit criteria:** `pip install -e .` works, `ytchannel --help` prints something (even if commands are stubs).

---

## Phase 1 — Channel resolution & indexing (no downloads yet)

- [ ] Implement `resolver.py`: accept a channel URL in any common format (`/channel/UC...`, `/c/name`, `/@handle`, `/user/name`) and normalize it
  - [ ] Handle the "videos" tab specifically (append `/videos` if not present) so playlist extraction targets the right list
  - [ ] Validate the URL actually resolves to a channel (fail fast with a clear error if not)
- [ ] Implement flat playlist extraction: get the full list of video IDs + basic metadata (title, upload date, duration, URL) without triggering per-video downloads
  - [ ] Use `yt-dlp`'s `extract_flat` option for speed
  - [ ] Handle pagination correctly — confirm large channels return their *full* list, not a truncated page
- [ ] Implement `indexer.py`: turn the resolved video list into a structured export
  - [ ] `ytchannel index <url> --output channel.json` — JSON export
  - [ ] `--output channel.csv` — CSV export (title, video_id, url, upload_date, duration)
- [ ] Implement `--dry-run` flag behavior (shared with `download` command later): print video count, estimated total size if available, date range
- [ ] Wire up `ytchannel index` command in `cli.py`
- [ ] Write tests for URL normalization covering all known channel URL formats (use recorded/mocked responses, not live network calls, so tests are deterministic)

**Exit criteria:** `ytchannel index <any-channel-url>` reliably produces a complete, accurate video list for channels of varying sizes (test with a small channel and one with 200+ videos).

---

## Phase 2 — Manifest & state tracking

- [ ] Design manifest schema (JSON, keyed by video ID):
  ```json
  {
    "video_id": "...",
    "title": "...",
    "status": "pending | downloading | complete | failed",
    "file_path": "...",
    "downloaded_at": "...",
    "attempts": 0,
    "last_error": null
  }
  ```
- [ ] Implement `manifest.py`:
  - [ ] Load existing manifest if present, initialize new one if not
  - [ ] Atomic writes (write to temp file, rename) so a crash mid-write doesn't corrupt the manifest
  - [ ] Methods: `mark_pending`, `mark_downloading`, `mark_complete`, `mark_failed`, `is_complete(video_id)`, `get_pending()`
- [ ] Reconcile manifest with fresh channel index on each run: new videos get added as `pending`, already-`complete` videos are left alone
- [ ] Write tests for manifest reconciliation logic (new videos added, completed videos untouched, failed videos retried)

**Exit criteria:** Manifest correctly tracks state across multiple runs; killing the process and re-running produces a correct "pending" list without duplicating completed work.

---

## Phase 3 — Core download functionality

- [ ] Implement `downloader.py`:
  - [ ] Wrap `yt_dlp.YoutubeDL` with a progress hook that reports to `rich`
  - [ ] Format/quality selection (`--quality 1080p`, `--quality best`, `--audio-only`)
  - [ ] Output path templating via `organize.py` (see below)
  - [ ] On success: update manifest to `complete` with final file path
  - [ ] On failure: update manifest to `failed` with error message, continue to next video (don't let one bad video kill the whole run)
- [ ] Implement `organize.py`:
  - [ ] Sanitize video titles for filesystem safety (strip/replace illegal characters, handle very long titles, handle duplicate titles within a channel)
  - [ ] Output structure: `<output_dir>/<channel_name>/<upload_date>_<video_title>.<ext>`
- [ ] Implement `ytchannel download <url>` command:
  - [ ] Runs resolver → reconciles manifest → downloads pending videos sequentially
  - [ ] Shows overall progress (X of Y videos) plus per-video download progress
  - [ ] Prints a summary at the end: downloaded, skipped (already complete), failed (with reasons)
- [ ] Add `--output` flag for base download directory (default: `./downloads`)
- [ ] Manual end-to-end test: run against a real small channel (a handful of short videos) and confirm files land correctly, manifest updates correctly, and re-running the same command is a no-op except for a "all videos already downloaded" message

**Exit criteria:** `ytchannel download <channel-url>` works end-to-end on a real channel, is safely re-runnable, and a single failed video doesn't abort the whole batch.

---

## Phase 4 — Resilience & rate limiting

- [ ] Add configurable delay between downloads (default: a few seconds) to avoid triggering YouTube throttling
- [ ] Add retry logic with backoff for transient failures (network errors, temporary 429s) — distinct from permanent failures (video deleted, private, region-blocked), which should be marked `failed` and skipped rather than retried indefinitely
- [ ] Handle Ctrl-C gracefully: catch the interrupt, finish writing the current manifest state, exit cleanly rather than leaving a corrupted partial file
- [ ] Handle partially-downloaded files on resume (yt-dlp supports `.part` file resumption — confirm this works correctly with the manifest logic and doesn't get treated as "complete")
- [ ] Add `--limit N` (stop after N videos) and `--after DATE` / `--before DATE` (date range filtering) flags, applied at the reconciliation step
- [ ] Test interrupt-and-resume behavior explicitly: start a download, kill it mid-video, restart, confirm correct resumption

**Exit criteria:** The tool survives interruption and network flakiness without manual manifest cleanup, and respects filtering flags correctly.

---

## Phase 5 — Metadata & extras

- [ ] Add `--write-thumbnail` flag (saves thumbnail alongside video)
- [ ] Add `--write-description` flag (saves video description as `.txt` or embeds in manifest)
- [ ] Add `--write-subs` flag (downloads available subtitles/captions)
- [ ] Add cookie file support (`--cookies <path>`) for members-only or age-restricted content the user has legitimate access to
- [ ] Add config file support (`~/.config/ytchannel/config.toml` or similar) for persisting default flags (output dir, quality, delay settings) so common preferences don't need to be re-typed every run
- [ ] Document config file precedence: CLI flags > config file > built-in defaults

**Exit criteria:** Metadata extras work independently and don't break the core download flow when combined.

---

## Phase 6 — Polish & packaging

- [ ] Full `--help` text review for every command and flag — should be clear without needing the README
- [ ] Error messages reviewed for clarity (e.g. invalid URL, channel not found, network unreachable — each should say what went wrong and what to do, not just print a stack trace)
- [ ] Add `ytchannel --version`
- [ ] Finalize README: install instructions, quickstart, full flag reference, legal/scope disclaimer
- [ ] Tag a v1.0 release, confirm `pip install` from the built package (not just editable install) works cleanly in a fresh virtualenv
- [ ] (Optional) Publish to PyPI if intended for wider distribution

**Exit criteria:** A new user can install the tool, run `ytchannel download <url>` on a real channel, and succeed without reading source code.

---

## Nice-to-haves (post-v1, not blocking)

- [ ] Limited concurrent downloads (2-4 workers) as an opt-in flag for users on fast connections willing to risk throttling
- [ ] `--playlist <url>` mode as an alternative to full-channel mode (same underlying machinery, different resolver entry point)
- [ ] SQLite manifest backend for very large channels (5000+ videos) as a drop-in replacement for the JSON manifest
- [ ] Live stream / premiere detection and handling (skip, or wait-and-retry)
