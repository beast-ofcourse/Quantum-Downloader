# Quantum-Downloader — Ultimate Implementation Roadmap

Phased plan covering every item from the repo analysis: hardening, internal
refactor, new commands, limitation fixes, scale features, and polish. Ordered by
dependency — later phases build on earlier ones. Each subtask lists objective,
files touched, concrete subtasks, acceptance criteria, and verification.

## Progress
- [x] **Phase 0** — Foundation & Hardening (0.1 CI/release, 0.2 dynamic version, 0.3 ffmpeg pre-check, 0.4 `--retries`, 0.5 Skipped-count fix)
- [x] **Phase 1** — Internal Refactor (streamlined: `planner.py` + `archiver.py` seams; `download` is a thin adapter; 0.5 fix carried into `plan_downloads`)
- [x] **Phase 2** — Core Commands (`verify`, `update`)
- [x] **Phase 3** — Close Documented Limitations (native date filter, `--proxy`, `--cookies-from-browser`)
- [ ] **Phase 4** — Scale Features (concurrent downloads, SQLite manifest backend)
- [ ] **Phase 5** — Polish & Developer Experience

**Dependency chain (read first):**
- Phase 0 must land before anything else (CI gates all merges; pre-checks stop
  bad runs early).
- Phase 1 (Archiver/Planner refactor) is the foundation for testable commands;
  do it before Phase 2+ so new commands are built on the clean seam.
- Phase 4 (concurrency, SQLite) depends on Phase 1's `Archiver`/`Planner` seams
  and the `Manifest` interface being stable.
- Phase 5 is independent polish; can run in parallel with any phase once the
  relevant code exists.

**Conventions:** keep `Manifest` the source of truth; never infer state from disk
alone; CLI flags > config > defaults; every new flag gets a config-key entry and
a unit test; every new command gets a `CliRunner` test after Phase 1.

---

## Phase 0 — Foundation & Hardening

> ✅ **Status: Complete & verified** — 0.1–0.5 done; full suite (67 tests) + ruff + mypy green.

Goal: stop bad releases and bad runs. No new user features.

### 0.1 CI pipeline (GitHub Actions)
- **Files:** `.github/workflows/ci.yml` (new), `.github/workflows/release.yml` (new)
- **Subtasks:**
  1. `ci.yml`: matrix Python 3.9–3.12; steps `pip install -e ".[dev]"`, `ruff check ytchannel`, `mypy ytchannel`, `pytest -q`.
  2. Fail the build on any ruff/mypy error; upload pytest summary.
  3. `release.yml`: on tag `v*`, build wheel + sdist, build Windows exe via PyInstaller, publish to GitHub Releases.
  4. **(M4 fix)** Add a PyInstaller build step: create `build_exe.py` (or `ytchannel.spec`) that invokes `ytchannel.cli:main` as the entry point, and call it from `release.yml` to produce `ytchannel.exe`. The current `dist/ytchannel.exe` was hand-built; the release job must be self-sufficient.
- **Acceptance:** push to a branch runs CI green; tagging produces a Release with wheel + exe built by the pipeline (not a pre-committed binary).
- **Verify:** open a PR; confirm all jobs pass. Tag a test release in a fork/branch.

### 0.2 Single-source version
- **Files:** `pyproject.toml`, `ytchannel/__init__.py`
- **Subtasks:**
  1. **(M2 fix)** Use `attr: ytchannel.__version__` in `pyproject.toml` (`[tool.setuptools] ...` or `[project]` dynamic version via `attr:`). Do NOT use setuptools-scm — it requires git tags at build time and breaks the current hand-built dist/exe flow. Remove the duplicate hardcoded `version = "1.0.0"` literal from `pyproject.toml`.
- **Acceptance:** `python -m build` produces a wheel whose version matches `__init__.__version__`; no hardcoded version string remains in `pyproject.toml`.
- **Verify:** `pip install dist/*.whl` then `ytchannel --version` matches source.

### 0.3 ffmpeg pre-check
- **Files:** `ytchannel/cli.py` (new helper), `ytchannel/downloader.py` (optional guard)
- **Subtasks:**
  1. Add `_check_ffmpeg()` using `shutil.which("ffmpeg")`; call before any download.
  2. **(M3 fix)** Always emit a non-fatal *warning* when ffmpeg is missing. *Hard-fail* (clear, actionable error: "ffmpeg not found on PATH; install from ffmpeg.org") only when `audio_only` or an explicit merge is actually required. Do NOT claim plain `best` works without ffmpeg — `best` frequently selects a video+audio pair that yt-dlp must merge, so a missing ffmpeg may still fail at merge time; the warning covers that case.
- **Acceptance:** `--audio-only` without ffmpeg fails fast with a clear message; a `best` run without ffmpeg proceeds but prints a warning that merges may fail.
- **Verify:** unit test the helper; manual run with ffmpeg removed from PATH (assert warning for `best`, hard error for `--audio-only`).

### 0.4 `--retries N` CLI flag
- **Files:** `ytchannel/config.py` (add `max_retries`, config key), `ytchannel/cli.py` (flag), `ytchannel/downloader.py` (pass through)
- **Subtasks:**
  1. Add `max_retries: int = 3` to `Config` + `CONFIG_KEYS`.
  2. Add `--retries` option in `download`; thread into `Downloader(...)`.
- **Acceptance:** `--retries 5` changes retry attempts; default unchanged at 3; config file can set it.
- **Verify:** unit test `Config.merge_cli` + a downloader test asserting attempt count.

### 0.5 Fix "Skipped (already complete)" count
- **Files:** `ytchannel/cli.py` (lines ~326)
- **Subtasks:**
  1. Compute `already_complete` as `len(filtered ∩ manifest.complete)`, not `len(videos) - len(pending)`.
  2. Add a regression test using a pre-populated manifest + `--limit`.
- **Acceptance:** with `--limit 5` on a channel where 0 are complete, "Skipped" reads 0, not 5.
- **Verify:** `tests/test_cli.py` (post-Phase-1) or a focused unit test on the count logic.
- **Note (m2):** when Phase 1 moves the count logic into `Archiver`/`Planner`, carry this corrected computation into `RunReport` (see Phase 1.2).

---

## Phase 1 — Internal Refactor (Archiver + Planner)

> ✅ **Status: Complete (streamlined)** — `planner.py` + `archiver.py` shipped; `download` is a thin adapter; 0.5 fix lives in `plan_downloads`; dry-run and real download share `filter_videos`. **Deferred:** the fuller class-based `Archiver`/`Planner` + `RunReport` + `dry_run_summary` delegation + "archiver never imports typer/rich" from this spec.

Goal: make the download flow testable and eliminate the dry-run/reality divergence. Implements the existing `plans/archiver.md` and `plans/planner.md` specs.

### 1.1 Planner module
- **Files:** `ytchannel/planner.py` (new), `ytchannel/indexer.py` (delegate `dry_run_summary`), `tests/test_planner.py` (new)
- **Subtasks:**
  1. `Plan` dataclass: `to_download`, `already_done`, `skipped`, `count`, `date_range`.
  2. `Planner.plan(videos, manifest, *, after, before, limit)` — owns date filter + limit + `manifest.get_pending()` intersection + `date_range` derivation.
  3. `dry_run_summary` delegates to `Planner` (or is removed; update its single caller).
- **Acceptance:** dry-run and real download compute the plan in exactly one place; `to_download == filtered ∩ pending`.
- **Verify:** `test_planner_date_filter`, `test_planner_limit`, `test_planner_pending_intersection`, `test_planner_date_range`.

### 1.2 Archiver module
- **Files:** `ytchannel/archiver.py` (new), `tests/test_archiver.py` (new)
- **Subtasks:**
  1. `RunReport` dataclass (target_name/type, planned, downloaded, skipped, failed, failures, date_range, interrupted). **(m2)** `skipped` must use the corrected count from Phase 0.5 (`filtered ∩ manifest.complete`), not `filtered − pending`.
  2. `Archiver(config, *, downloader=, reporter=)` with `run(url, *, playlist=, dry_run=)` — moves resolve + filter + reconcile + loop + KeyboardInterrupt handling out of `cli.download`.
  3. Archiver raises on resolution errors; CLI catches and prints. Archiver never imports `typer`/`rich`.
- **Acceptance:** `cli.download` shrinks to build Config → `Archiver.run` → render `RunReport`.
- **Verify:** `test_archiver_run_downloads_pending`, `test_archiver_dry_run_no_download`, `test_archiver_resume`, `test_archiver_keyboard_interrupt_saves`, `test_archiver_resolve_error`.

### 1.3 Thin CLI + CLI tests
- **Files:** `ytchannel/cli.py`, `tests/test_cli.py`
- **Subtasks:**
  1. `download` becomes a thin adapter; `index` shares the resolve helper.
  2. Add `CliRunner` tests for both commands (currently only 17 lines of CLI tests).
- **Acceptance:** `test_cli.py` covers both commands' happy + error paths without network.
- **Verify:** `pytest tests/test_cli.py`.

### 1.4 Close dry-run divergence (covered by 1.1)
- Confirmed fixed by 1.1; add a test asserting dry-run count equals real `to_download`.

---

## Phase 2 — Core Commands

> ✅ **Status: Complete & verified** — `verify` (with `Manifest.check_files`) and `update` commands shipped.

Goal: give users visibility and incremental control. Builds on Phase 1 seams.

### 2.1 `verify` command
- **Files:** `ytchannel/cli.py` (new command), `ytchannel/manifest.py` (helper `verify_against_disk`), `tests/`
- **Subtasks:**
  1. Add `Manifest.check_files(output_dir)` → returns `{complete_missing, orphan_on_disk, complete_present}`.
  2. `ytchannel verify <url-or-manifest>` prints a table: files present / missing / orphaned (on disk but not in manifest).
  3. Optional `--delete-orphans` flag (careful — confirm before deleting).
- **Acceptance:** detects a manually deleted video file as "missing" and a stray file as "orphan".
- **Verify:** unit test with a temp dir + manifest; `CliRunner` test.

### 2.2 `update` / re-index command
- **Files:** `ytchannel/cli.py` (new command), reuses `resolve_*` + `manifest.reconcile`
- **Subtasks:**
  1. `ytchannel update <url>` re-resolves and reconciles into the manifest without downloading; reports added/removed counts.
  2. Share resolve helper with `index`/`download` (from 1.3).
- **Acceptance:** running `update` after a channel publishes new videos adds them as `pending`; no downloads occur.
- **Verify:** `CliRunner` test with a fake resolver.

---

## Phase 3 — Close Documented Limitations

> ✅ **Status: Complete & verified** — native yt-dlp date filtering, `--proxy`, `--cookies-from-browser` (with conflict guard).

Goal: make `--after/--before`, proxy, and browser cookies actually work.

### 3.1 Native date filtering via yt-dlp
- **Files:** `ytchannel/downloader.py` (`_build_ydl_opts`), `ytchannel/config.py` (pass `after`/`before` through)
- **Subtasks:**
  1. When `after`/`before` are set, pass yt-dlp `dateafter`/`datebefore` (format `YYYYMMDD`) so filtering happens at full-extraction time (works even when flat metadata lacks `upload_date`).
  2. Keep the manifest-based filter as a fallback for already-indexed runs.
- **Acceptance:** `--after 20240101` downloads only videos uploaded after that date, verified against real metadata.
- **Verify:** integration test with a mocked yt-dlp that asserts `dateafter` is set.

### 3.2 `--proxy` support
- **Files:** `ytchannel/downloader.py`, `ytchannel/cli.py`, `ytchannel/config.py`
- **Subtasks:**
  1. Add `--proxy` → yt-dlp `proxy` option (and `proxy` to config keys).
- **Acceptance:** passing `--proxy http://host:port` reaches yt-dlp's proxy option.
- **Verify:** unit test asserting opts contain `proxy`.

### 3.3 `--cookies-from-browser`
- **Files:** `ytchannel/downloader.py`, `ytchannel/cli.py`, `ytchannel/config.py`
- **Subtasks:**
  1. Add `--cookies-from-browser {chrome,firefox,...}` → yt-dlp `cookiesfrombrowser`.
  2. Document it alongside existing `--cookies`.
- **Acceptance:** flag maps to yt-dlp option; conflicts with `--cookies` are rejected.
- **Verify:** unit test + doc note.

---

## Phase 4 — Scale Features (Roadmap Items)

> ⬜ **Status: Not started**.

Goal: handle huge channels and go faster. Depends on Phase 1 seams + stable `Manifest` interface.

### 4.1 Concurrent downloads (opt-in)
- **Files:** `ytchannel/archiver.py` (worker pool), `ytchannel/cli.py` (`--concurrency`), `ytchannel/manifest.py` (confirm atomic writes are thread-safe)
- **Subtasks:**
  1. Add `--concurrency N` (default 1). Use a worker pool; each worker gets its own `Downloader` + shares the `Manifest` (writes are atomic via `os.replace`, but serialize `save()` with a per-manifest-path lock).
  2. Preserve per-video retry/backoff and the "one failure doesn't abort batch" guarantee.
  3. **(m5 fix)** Apply the rate-limiter delay **globally** (one shared limiter across all workers) by default, so total politeness is preserved; document the choice.
- **Acceptance:** `--concurrency 3` downloads faster on a large channel while staying polite; manifest stays consistent; Ctrl-C still saves.
- **Verify:** test with fake downloaders asserting parallel execution + manifest integrity.

### 4.2 SQLite manifest backend
- **Files:** `ytchannel/manifest.py` (add `SqliteManifest` implementing the same interface), factory by size/flag
- **Subtasks:**
  1. **(M5 fix)** Extract a `Manifest` protocol/ABC from the current concrete class; add `Manifest.open(path, backend="auto")` factory that selects JSON or SQLite. Update **all** call sites (`cli.py`, `archiver.py`, and the web `service.py` once it exists) to use the factory instead of constructing `Manifest` directly.
  2. `JsonManifest` (current) and `SqliteManifest` both implement the protocol.
  3. Auto-select SQLite when entry count exceeds a threshold (e.g. 5000) or via `--manifest sqlite`.
  4. Migrate existing JSON manifests on first open (best-effort, non-destructive; preserve the exact schema).
- **Acceptance:** 5000+ video channel reconciles/saves without per-video full-file rewrites; behavior identical to JSON.
- **Verify:** backend-agnostic tests run against both implementations.

---

## Phase 5 — Polish & Developer Experience

> ⬜ **Status: Not started**.

Independent; slot in as time allows. (Acceptance/Verify added per plan convention — m3.)

### 5.1 `--quiet` / `--verbose`
- **Files:** `ytchannel/cli.py`, `ytchannel/downloader.py` (`quiet`/`no_warnings` from a verbosity flag)
- **Subtasks:** map `--quiet` (suppress progress) and `--verbose` (pass warnings through) to yt-dlp opts.
- **Acceptance:** `--quiet` suppresses the progress UI; `--verbose` surfaces yt-dlp warnings.
- **Verify:** unit test that opts reflect the flag.

### 5.2 Run log file
- **Files:** `ytchannel/cli.py` (optional `--log FILE`), `ytchannel/archiver.py` (emit structured events)
- **Subtasks:** write a per-run log (timestamped events, failures) next to the manifest; default off, enabled by flag or always for long runs.
- **Acceptance:** `--log run.log` produces a file capturing start/per-video/end events.
- **Verify:** run with `--log` and assert file contents.

### 5.3 Windows path-length guard
- **Files:** `ytchannel/utils/organize.py`
- **Subtasks:** after sanitizing, if `len(full_path) > 259` on Windows, truncate the title segment further and warn; consider `restrictfilenames`. **(m6 fix)** when truncating, append a short hash suffix of the original name to avoid filename collisions between two videos that truncate to the same string.
- **Acceptance:** over-long paths are shortened without colliding with sibling files.
- **Verify:** unit test with a path exceeding the limit + a colliding sibling.

### 5.4 Custom output template
- **Files:** `ytchannel/cli.py` (`--template`), `ytchannel/utils/organize.py`
- **Subtasks:** allow a yt-dlp outtmpl override (e.g. `%(title)s` only, or `%(playlist_index)s_%(title)s`).
- **Acceptance:** `--template` changes the on-disk filename pattern.
- **Verify:** unit test that outtmpl reflects the flag.

### 5.5 Batch ETA in progress
- **Files:** `ytchannel/cli.py` (`RichReporter`)
- **Subtasks:** add an overall ETA column based on completed/total + rolling average speed.
- **Acceptance:** progress shows an ETA that converges as videos complete.
- **Verify:** unit test the ETA computation.

### 5.6 JSONL export for `index`
- **Files:** `ytchannel/indexer.py`, `ytchannel/cli.py`
- **Subtasks:** add `--jsonl` output for streaming large channels.
- **Acceptance:** `--jsonl` writes one video object per line.
- **Verify:** unit test the JSONL writer.

### 5.7 CHANGELOG + semver policy
- **Files:** `CHANGELOG.md` (new), note in `README.md`
- **Subtasks:** adopt Keep-a-Changelog; document version-bump rule tied to Phase 0.2.
- **Acceptance:** `CHANGELOG.md` exists with an entry schema; README links it.
- **Verify:** doc check.

### 5.8 Shell completions
- **Files:** `ytchannel/cli.py` (Typer supports this natively)
- **Subtasks:** document `ytchannel --install-completion`; verify in bash/zsh/fish.
- **Acceptance:** completion script installs and works in at least bash.
- **Verify:** shell smoke test.

---

## Suggested execution order (summary)

1. Phase 0 (0.1 → 0.5) — gate everything.
2. Phase 1 (1.1 → 1.4) — testable foundation.
3. Phase 2 (2.1, 2.2) — quick user-value wins.
4. Phase 3 (3.1 → 3.3) — fix the known limitations.
5. Phase 4 (4.1, 4.2) — the big roadmap items.
6. Phase 5 (any order) — polish.

Each phase ends with a green test suite + a checkpoint before the next phase starts.
