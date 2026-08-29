# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] - 2026-08-29

### Added

- **Multi-platform downloads** — download a single video (and its audio via
  `--audio-only`) from **Instagram** and **JioHotstar**, plus a single YouTube
  video, in addition to the existing YouTube channel/playlist archiving. The
  platform is auto-detected from the URL (no extra flag needed). Routed through
  a new `resolve_target` entry point in `ytchannel.resolver`, with
  `classify_url` gating an exact host allowlist (SSRF-safe). The same
  resumable/idempotent manifest + Web UI drive all platforms.

### Fixed

- `resolve_target` now resolves a `watch?v=…&list=…` URL to its **playlist**
  (was incorrectly demoted to a single video), matching the documented
  behavior. Added `test_resolve_target_watch_with_list_is_playlist` to guard it.
- Removed redundant `resolve_instagram` / `resolve_hotstar` one-line wrapper
  functions; `resolve_target` calls `resolve_single_video` directly.
- Corrected the stale `resolver.py` module docstring (`target_type` is
  `channel` / `playlist` / `video`; the site lives in `platform`).
- Clarified the dry-run `target_type` → label mapping in `cli.py`.

### Added

- **Phase 0 — CI/release pipeline & foundations**
  - CI/release pipeline.
  - Single-source version (sourced from `ytchannel.__init__.__version__`).
  - ffmpeg pre-check before downloads.
  - `--retries` flag for max retries per video on transient errors.
  - Fix: correct count of already-complete (skipped) videos in the summary.

- **Phase 1 — internal refactor**
  - Internal `planner` / `archiver` refactor; `download` is now a thin adapter over them.

- **Phase 2 — new commands**
  - `verify` command: check downloaded files against the manifest and surface orphans.
  - `update` command: re-index a channel/playlist and add new videos to the manifest.

- **Phase 3 — filtering & auth**
  - Native date filtering via `--after` / `--before` (upload date, `YYYYMMDD`).
  - `--proxy` flag to route downloads through an HTTP/HTTPS proxy.
  - `--cookies-from-browser` flag to read cookies from a browser session, with a conflict guard against `--cookies`.

- **Phase 4 — concurrency & manifest backend**
  - `--concurrency` flag for threaded (parallel) downloads.
  - SQLite manifest backend via `--manifest sqlite`, with automatic migration from the JSON backend.

- **Phase 5.1–5.6 — UX & output**
  - `--quiet` flag to suppress the progress UI.
  - `--verbose` flag to pass yt-dlp warnings/debug through.
  - `--log` flag to write a per-run log file.
  - Windows path-length guard.
  - `--template` flag to override the yt-dlp output template.
  - Batch ETA shown in the progress UI.
  - `--jsonl` index export (one video per line) for the `index` command.

## Semver policy

- Versions are sourced from `ytchannel.__init__.__version__` (single source of truth, see Phase 0.2).
- Bump the **MINOR** version for new features/commands.
- Bump the **PATCH** version for fixes.
- Bump the **MAJOR** version for breaking changes.
- Tag releases as `vX.Y.Z`.
