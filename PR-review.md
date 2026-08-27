# PR Review

**Reviewed:** PR #3 — feat: Phases 4-5 + web UI — concurrency, SQLite manifest, polish, local web control panel (base `main` -> head `feat/phases-0-3-hardening-commands-limits`)
**Verdict per PR:** changes requested — CI is red (Critical); must be green before merge.
**Reviewed by:** pr-reviewer

## PR #3 — feat: Phases 4-5 + web UI

### Summary
Implements the remaining roadmap work: Phase 4 (opt-in `--concurrency` threaded downloads + SQLite manifest backend via a `Manifest` ABC and `Manifest.open` factory), Phase 5 polish (`--quiet`/`--verbose`, `--log`, Windows path guard, `--template`, batch ETA, `--jsonl`, CHANGELOG, completions), and a local web UI (`ytchannel serve`: FastAPI backend with `JobStore`/`EventBus`/`WebReporter`/per-target run guard/cancel/origin enforcement, plus a split-action static SPA). 38 files changed, +4532 / -227.

### Critical
1. **`.github/workflows/ci.yml:21` + `tests/web/test_api.py:13`** — CI installs only `pip install -e ".[dev]"`, but `tests/web/test_api.py` imports `fastapi.testclient` (and `ytchannel.web.app`) at module top. With `fastapi` absent, the module fails to import -> pytest aborts collection -> the **entire test suite fails on all 4 Python versions** (`mergeStateStatus: UNSTABLE`). Root cause: the web test dependency (`[web]` extra) is not part of the CI install, so the suite cannot even collect. Impact: the repo's merge gate is red; also, web tests never actually run in CI, so backend regressions would go undetected. Fix: (a) change the CI install to `pip install -e ".[dev,web]"` so the web tests run and are validated; (b) add `pytest.importorskip("fastapi")` at the top of `tests/web/test_api.py` as defense-in-depth so a missing extra skips gracefully instead of breaking collection. Status: **Confirmed** (directly observable: ci.yml line 21 installs `.[dev]`; test_api.py:13 `from fastapi.testclient import TestClient`; standard pytest behavior on import error is a collection failure that fails the run).

### Major
(none)

### Minor
1. **`ytchannel/web/app.py:224-235` (`index_export`)** — `tempfile.mkstemp(...)` creates a temp file that is returned via `FileResponse(tmp, ...)` and never deleted. Root cause: no cleanup hook is attached to the response. Impact: every `POST /api/index` call leaks a temp file in the system temp dir; under repeated use this accumulates and can exhaust disk. Fix: return `FileResponse(tmp, ..., background=BackgroundTask(os.remove, tmp))` (or use `fastapi.BackgroundTasks`) so the file is removed after the response is sent. Status: **Confirmed** (code path returns the temp path with no deletion; `BackgroundTask` is the standard FastAPI pattern).

2. **`ytchannel/web/service.py:208` (`list_targets`)** — scans `output_dir` for `*.manifest.json` only, so targets stored with the SQLite backend (`--manifest sqlite`, which writes `<key>.sqlite`) are never listed in the web "Targets" table, and `snapshot()` (which opens `*.manifest.json`) would report `exists: False` for them. Root cause: the scanner hard-codes the JSON suffix and ignores the SQLite sibling. Impact: users who opt into SQLite manifests get an empty/broken Targets view in the web UI (the web service itself defaults to JSON, so the common path works, but the inconsistency is a real gap). Fix: also match `*.sqlite` (or derive the manifest path the same way the download flow does via `storage.manifest_path` + backend) and open with `Manifest.open(..., backend="auto")`. Status: **Suspected** (logic is clear from the code; not reproduced at runtime).

### Optional
1. **`AGENTS.md` committed in this PR** — the PR stages `AGENTS.md` (the agent operating-system / "Golden Rules" instructions file). Root cause: it was untracked in the working tree and got included in the commit. Impact: committing it publishes the project's agent-config (including the "Zero Refusal Policy" etc.) to the public repo, which may be unintended. Fix: confirm whether `AGENTS.md` belongs in the repo; if not, remove it from the branch (`git rm --cached AGENTS.md` and add to `.gitignore`). Status: **Confirmed** (file is in the staged set).

2. **`pyproject.toml` sdist safety (no `MANIFEST.in`)** — the wheel build was verified to include `ytchannel/web/static/{app.js,index.html,styles.css}` and all web modules, so `pip install quantum-downloader[web]` works. However, there is no `MANIFEST.in`; an sdist built separately might omit the static assets. Fix (optional): add `MANIFEST.in` with `recursive-include ytchannel/web/static *` and/or `include_package_data = true` so sdist and wheel stay consistent. Status: **Verified Clean for wheel** (built `quantum_downloader-1.0.0-py3-none-any.whl` and confirmed static files present); sdist not separately verified.

3. **`ytchannel/cli.py:371-379` (`serve` free-port probe)** — the bind-probe-then-`uvicorn.run` has a TOCTOU race (another process could take the port between probe and bind). Impact: low (local dev tool); a `OSError` from uvicorn would surface to the user. Fix (optional): pass the already-probed `actual_port` and let uvicorn bind, or catch `OSError` on `uvicorn.run` and retry. Status: **Suspected** (noted in plan; acceptable for localhost tooling).

4. **`ytchannel/web/service.py` `start_job` blocks the event loop during resolve/plan** — the synchronous `resolve_*` + `filter_videos` + `reconcile` run inline in the async handler before the worker thread starts. Impact: for very large channels this briefly blocks the ASGI loop. Fix (optional): move resolve/plan into the worker thread too. Status: **Suspected** (known limitation, noted in plan).

### Verified Clean
- **Packaging**: wheel includes the static SPA + all web modules (built and inspected).
- **CLI isolation**: `serve` imports `create_app`/`webbrowser`/`uvicorn` lazily inside the function (`cli.py:360,363,390`), so `ytchannel download`/`index`/etc. work without the `[web]` extra installed.
- **Origin enforcement**: `verify_origin` (app.py:32) and `_origin_allowed` (app.py:55) reject cross-origin `Origin` headers while allowing no-Origin / localhost clients.
- **Local test suite**: 93 passed; `ruff check ytchannel tests` clean; `mypy ytchannel` clean (19 source files) — all green on the local machine (the CI failure is purely the missing `[web]` install, not a code defect).
- **Web UI end-to-end**: verified in a real browser (Playwright) against a running server with a fake downloader — URL -> live WebSocket progress -> completed report rendered correctly.
- **Mergeability**: `mergeable: MERGEABLE` (no conflicts with `main`); only `mergeStateStatus: UNSTABLE` due to the CI failure above.

### Fix order
1. **Critical** — fix CI: install `.[dev,web]` in `.github/workflows/ci.yml` and add `pytest.importorskip("fastapi")` to `tests/web/test_api.py`. Re-run CI until green.
2. **Minor** — `POST /api/index` temp-file leak: attach `BackgroundTask(os.remove, tmp)` to the `FileResponse` in `app.py`.
3. **Minor** — `list_targets` (and `snapshot`) should also recognize SQLite-backend manifests.
4. **Optional** — decide on `AGENTS.md` inclusion; add `MANIFEST.in` for sdist safety; address the `serve` TOCTOU and event-loop blocking as desired.
