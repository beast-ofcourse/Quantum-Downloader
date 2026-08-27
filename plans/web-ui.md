# Web UI Plan — Quantum-Downloader (Option A: Thin Control Panel)

Local, self-hosted web UI for non-technical users who don't want a terminal.
Distribution: `pip install quantum-downloader` then `ytchannel serve`, which
starts a local web server and auto-opens the default browser. No Cloudflare, no
media-server, no video player — just a control panel that drives the existing
`Archiver` engine.

---

## Assumptions (stated, not guessed)

1. **Single-user, localhost-only.** Backend binds `127.0.0.1` by default; no
   login/auth. Exposing to a LAN (`--host 0.0.0.0`) is opt-in and prints a
   security warning. Multi-user/auth is a separate future phase, not in scope here.
2. **Depends on Phase 1 of `roadmap.md`** (the `Archiver` + `Planner` refactor).
   The web backend calls `Archiver.run(url, playlist=, dry_run=)` and injects a
   `WebReporter` — so Phase 1 MUST land before any W-phase work begins.
3. **UI direction is LOCKED to the "split-action" mockup** (variant 3 in
   `sketches/003-split-action/`): a two-pane layout — left = setup form (URL +
   options + Start/Cancel), right = live progress (overall bar + per-video stream +
   status pill). `DESIGN.md` refines *visual styling* only; the layout/structure is
   decided. Where the plan says "per DESIGN.md", it means visual polish, not layout.
4. **Manifest remains the source of truth.** The web layer is a front-end over
   `Archiver`/`Manifest`/`Config` — it introduces no new state model for downloads.
5. **Architecture review:** the backend↔frontend data flow and security model
   below should be confirmed by the Architect agent (or against `DESIGN.md`)
   before W0 implementation starts. This plan is the implementation breakdown.

---

## Architecture overview

```
 browser (auto-opened)          ytchannel serve (one process)
 ┌──────────────┐              ┌────────────────────────────────────┐
 │  static SPA  │  HTTP/WS     │  FastAPI app                        │
 │ (per DESIGN) │ <──────────> │   - /api/* REST + WebSocket         │
 └──────────────┘              │   - serves static/ (the SPA)        │
                               │   - JobStore (jobs.json / sqlite)   │
                               │   - EventBus (progress pub/sub)     │
                               │   - per-target active-run guard     │
                               │         │ runs in thread            │
                               │         ▼                           │
                               │   Archiver(config)                  │
                               │      + WebReporter ──push events──► │
                               │         │                           │
                               │         ▼                           │
                               │   yt-dlp + ffmpeg (existing engine) │
                               │   Manifest (existing, source of truth)
                               └────────────────────────────────────┘
```

- **Backend:** FastAPI + uvicorn (async). yt-dlp is blocking, so `Archiver.run`
  executes in a `threadpool`/`asyncio.to_thread`; the `WebReporter` pushes
  progress events to an in-process pub/sub consumed by WebSocket clients.
- **Frontend:** static SPA (framework per `DESIGN.md`) served by FastAPI from
  `ytchannel/web/static/`. No separate build server needed at runtime.
- **Job persistence:** `JobStore` writes job metadata + final `RunReport` to disk
  (JSON files under `~/.local/share/ytchannel/jobs/` or sqlite). Live progress is
  ephemeral (WebSocket only); the manifest holds durable download state.
- **Cross-run guard (M1 fix):** the web service owns a per-target `asyncio.Lock`
  (or a single active-run slot per target) inside `service.py`. This is **independent**
  of roadmap Phase 4.1's worker-pool lock, which only governs concurrency *within* a
  single run. The web guard prevents two UI-triggered runs from targeting the same
  manifest at once.

---

## Tech stack (recommended)

- **Backend:** `fastapi`, `uvicorn[standard]` (adds websockets). Added to
  `pyproject.toml` as an extra: `pip install quantum-downloader[web]`.
- **Frontend:** static assets only (vanilla JS + a small framework per DESIGN.md,
  e.g. Preact/HTMX). Built output committed into the package; no runtime build.
- **Browser launch:** stdlib `webbrowser.open(f"http://{host}:{port}/")`.
- **Progress transport:** WebSocket (one per active job view) fed by `EventBus`.

---

## Phase W0 — Backend foundation

### W0.1 `ytchannel serve` Typer command
- **Files:** `ytchannel/cli.py` (new `serve` command), `ytchannel/web/__init__.py` (app factory)
- **Subtasks:**
  1. Add `serve` command with options: `--host` (default `127.0.0.1`), `--port`
     (default `8765`), `--no-browser` (skip auto-open), `--reload` (dev only).
  2. On start: pick a free port (try default, increment on `OSError`), print the
     URL, and `webbrowser.open(...)` unless `--no-browser`.
  3. Bind `127.0.0.1` by default; if `--host` is not localhost, print a clear
     "exposed to network — no auth" warning to stderr.
  4. **(M6 fix)** Validate the `Origin` header on all state-changing requests: reject
     any `POST`/WebSocket whose `Origin` is not the served local origin
     (`http://127.0.0.1:<port>` / the configured host). This blocks the
     cross-site/localhost attack where a malicious web page drives the server.
     Optionally issue a random same-origin CSRF token required on POSTs.
- **Acceptance:** `ytchannel serve` opens `http://127.0.0.1:8765/` in the default
  browser; `--no-browser` suppresses it; port conflict auto-resolves; a cross-origin
  POST/WS is rejected.
- **Verify:** subprocess launch test; assert the URL is reachable (HTTP 200 on `/`);
  assert a cross-origin `POST /api/jobs` is rejected (403/400).

### W0.2 FastAPI app factory + static serving + health
- **Files:** `ytchannel/web/app.py`, `ytchannel/web/static/` (placeholder `index.html`)
- **Subtasks:**
  1. `create_app()` returns a FastAPI app; mount `/` to serve `static/index.html`
     and assets; add `GET /api/health` → `{"status":"ok"}`.
  2. Lifespan handler creates the `JobStore` + `EventBus` + per-target run-guard singletons.
- **Acceptance:** `GET /` returns the SPA; `GET /api/health` returns ok.
- **Verify:** `fastapi.testclient.TestClient` smoke test.

### W0.3 JobStore (job persistence)
- **Files:** `ytchannel/web/jobs.py` (new)
- **Subtasks:**
  1. `Job` dataclass: `id, url, options, status (queued|running|done|failed|
     cancelled), created_at, report: RunReport|None`.
  2. `JobStore`: create/list/get/update; persist to JSON files (one per job) under
     a configurable dir (default `~/.local/share/ytchannel/jobs`). Simple, atomic
     writes (temp + replace, mirroring `Manifest.save`).
  3. In-memory index for fast list queries.
- **Acceptance:** jobs survive a server restart (reloaded from disk on boot).
- **Verify:** unit test create→persist→reload round-trip.

### W0.4 WebReporter + EventBus
- **Files:** `ytchannel/web/reporter.py` (new), `ytchannel/web/events.py` (new)
- **Subtasks:**
  1. `EventBus`: `subscribe(job_id) -> queue`, `publish(job_id, event)`; one
     queue per subscriber, dropped if no consumer.
  2. `WebReporter(DownloadReporter)`: implements `video_start/progress/finish`
     and `stop` by publishing structured events
     (`{type:"video_start",title}`, `{type:"progress",...}`, `{type:"done",...}`)
     to the bus for the active job.
  3. **(m1 fix)** On a new WebSocket subscription, first publish a **current-state
     snapshot** read from the manifest (overall progress, per-video statuses) so a
     late-connecting viewer sees correct progress even if early events were missed.
- **Acceptance:** events emitted by `Archiver` flow through `WebReporter` to the bus;
  a subscriber connecting mid-run receives a correct snapshot immediately.
- **Verify:** unit test — fake `Archiver` run with `WebReporter` yields expected events;
  test that a late subscriber gets a snapshot.

### W0.5 Run Archiver in background
- **Files:** `ytchannel/web/service.py` (new)
- **Subtasks:**
  1. `start_job(job, options)`: build a `Config` (defaults + request options),
     run `Archiver(config).run(url, playlist=, dry_run=)` via `asyncio.to_thread`,
     injecting `WebReporter(job.id)`; on completion, store `RunReport` in `JobStore`.
  2. **(M1 fix)** Guard with the service-owned **per-target run lock** (not the
     roadmap Phase 4.1 lock): only one active download per target manifest at a time.
     Reject (409) or queue concurrent same-target runs. This lock lives entirely in
     `service.py` and is available from W0, independent of later concurrency work.
- **Acceptance:** a job started via the service downloads through the real engine,
  records a `RunReport`, and a second same-target start is rejected/queued.
- **Verify:** integration test with a fake `Downloader` injected into `Archiver`;
  assert concurrent same-target start is rejected.

---

## Phase W1 — API contract (frontend consumes this; UI per DESIGN.md)

### W1.1 POST /api/jobs — create + start a download
- Body: `{url, playlist?:bool, quality?, audio_only?, limit?, after?, before?,
  write_thumbnail?, write_description?, write_subs?, cookies?, delay?, dry_run?}`.
- Validates URL via `resolver.normalize_*` (reuse existing logic) before accepting.
- Returns `{job_id, status}`. 409 if same target already actively downloading.
- **(M6 fix)** Requires a valid same-origin `Origin` (see W0.1) and, if issued, the CSRF token.

### W1.2 GET /api/jobs, GET /api/jobs/{id}
- List all jobs (newest first) with status + summary; detail returns full `RunReport`.

### W1.3 WS /api/jobs/{id}/progress
- Streams `WebReporter` events for that job until done/cancelled; first sends a
  current-state snapshot (m1); then streams live events; finally sends a
  `{type:"complete", report}` and closes.
- **(M6 fix)** WebSocket handshake rejects a non-local `Origin`.

### W1.4 GET /api/targets — manifest browser
- Lists known targets (channels/playlists) and per-target state
  (completed/failed/pending counts) by scanning manifests in the output dir.

### W1.5 POST /api/jobs/{id}/cancel
- Requests cancellation; backend sets a flag `Archiver` checks between videos
  (KeyboardInterrupt-style graceful stop), saves manifest, marks job `cancelled`.
- **(M6 fix)** Same-origin/CSRF protected.

### W1.6 POST /api/index — metadata-only export via UI
- Mirrors the `index` command: returns video count / triggers a JSON/CSV export
  download without downloading media.
- **(m4 fix)** When returning a file, use a `FileResponse` with a sensible
  `filename` (e.g. `<target>.json` / `<target>.csv`) and `media_type`; the
  frontend offers it as a download.

- **Acceptance (W1):** every endpoint covered by `TestClient` tests (including the
  cross-origin rejection); WebSocket progress verified with a fake downloader.
- **Verify:** `pytest tests/web/test_api.py`.

---

## Phase W2 — Frontend (markup per DESIGN.md)

### W2.1 Scaffold SPA in `ytchannel/web/static/`
- Single-page app implementing the **split-action** layout (variant 3): two panes.
- `index.html` + `app.js` + `styles.css`. Framework per DESIGN.md (vanilla JS is
  sufficient and keeps the dependency footprint small; a light framework is fine if
  DESIGN.md calls for it). Served by the backend; no separate dev server at runtime.

### W2.2 Left pane — URL input + options form
- Input for channel/playlist URL; options matching the API fields (quality,
  audio-only, limit, date filters, thumbnails/subs/description, cookies, delay).
- "Start" button → `POST /api/jobs`; "Cancel" button → `POST /api/jobs/{id}/cancel`.
- "Dry run" preview button → calls `POST /api/jobs` with `dry_run:true`, shows the
  plan in the right pane instead of starting a download.

### W2.3 Right pane — live progress
- Connects to `WS /api/jobs/{id}/progress` and renders: channel name, overall
  progress bar, `done / total` counter, a status pill (idle / downloading /
  complete / cancelled), and a streaming per-video list (reusing the rich-style
  summary as a simple list). Matches the variant-3 mockup behavior. Receives the
  initial snapshot (m1) before live events.

### W2.4 Target/manifest browser
- `GET /api/targets` rendered as a table of channels/playlists with
  completed/failed/pending counts; click to start a new job for that target.

### W2.5 Empty / error / loading states
- Friendly messages for: no jobs yet, invalid URL, all-complete (nothing to do),
  and backend errors. Per DESIGN.md visual language.

- **Acceptance (W2):** a non-technical user can paste a URL, click Download, and
  watch progress without touching a terminal.
- **Verify:** manual browser test (Playwright) — full flow URL→download→done;
  plus a cancelled-run test; plus a cross-origin request is blocked by the browser.

---

## Phase W3 — Packaging & DX

### W3.1 Bundle static assets in the package
- **Files:** `pyproject.toml` (`[project.optional-dependencies] web = [...]`,
  `package_data`/`include` for `ytchannel/web/static/**`)
- Ship `fastapi`+`uvicorn` only under `[web]` extra so core CLI stays light.

### W3.2 `serve` robustness
- Port-conflict auto-increment; clear startup banner with the exact URL;
  `--no-browser` for headless/SSH use.

### W3.3 Docs
- README section: "Web UI — `pip install quantum-downloader[web]` then
  `ytchannel serve`". Note localhost-only; document `--host` warning; document the
  same-origin protection (M6).

### W3.4 (optional, later) Docker
- A Docker image that runs `ytchannel serve` with ffmpeg baked in (reuse existing
  Dockerfile pattern). Out of scope for v1 of the UI.

---

## Phase W4 — Hardening (light, single-user)

### W4.1 Input validation + origin enforcement
- All URL inputs normalized/rejected via existing `resolver` guards; reject
  non-YouTube URLs at the API boundary.
- **(M6 fix)** Enforce the `Origin`/CSRF check from W0.1 on every state-changing
  route and the WebSocket handshake (defense in depth — don't rely on W0.1 alone).

### W4.2 Concurrency policy
- One active download per target manifest via the service-owned per-target lock
  (W0.5). Cross-target concurrent jobs allowed only if roadmap Phase 4.1 lands;
  otherwise serialize.

### W4.3 Graceful shutdown
- On SIGINT/SIGTERM: stop accepting new jobs, let the active run finish its
  current video, save manifests, then exit. `EventBus` closes cleanly.

### W4.4 Cancel correctness
- `cancel` saves the manifest and returns a partial `RunReport` with
  `interrupted=True` (reuses Phase 1 KeyboardInterrupt handling).

- **Acceptance (W4):** shutdown mid-run leaves a resumable manifest; cancel
  stops cleanly and the next run resumes; cross-origin requests are blocked in all
  states.
- **Verify:** tests for cancel + shutdown with a fake downloader; test that a
  cross-origin request is rejected post-shutdown.

---

## Dependency & ordering

```
roadmap.md Phase 0 (CI/hardening)
        │
        ▼
roadmap.md Phase 1 (Archiver + Planner refactor)   <-- REQUIRED before W0
        │
        ▼
Web UI Phase W0 → W1 → W2 → W3 → W4
```

The web UI **cannot start** until `Archiver` exists and accepts an injected
reporter + downloader (roadmap Phase 1.1–1.2). Build that first; the W-phases
then compose on top.

## Open items to confirm with DESIGN.md / Architect
- **Layout: DECIDED** — split-action (variant 3). Remaining open items are visual
  only or backend policy, not layout.
- Frontend framework choice (vanilla vs Preact/HTMX) — affects W2.1 build only.
- Exact progress visualization styling (bar width, per-video row density) — W2.3.
- Security model: the M6 origin/CSRF mitigation is specified; confirm token approach
  with Architect/DESIGN.md before W0.
