# Plan Validation Report — Quantum-Downloader

Validated by: manual review (arch-validator subagent unavailable; author performed
the validation). Scope: `plans/roadmap.md` + `plans/web-ui.md`, cross-checked against
the existing codebase (`ytchannel/*`) and `plans/archiver.md` / `plans/planner.md`.

**Verdict: NEEDS WORK.** No Critical blocker. 6 Major + 6 Minor findings. All Majors
are localized and cheap to fix.

## Critical
None.

## Major (fix before build)

### M1 — web-ui W0.5 lock doesn't exist yet (sequencing bug)
- W0.5 says the concurrency guard "inherits a shared manifest lock from Phase 1/4
  work," but that lock only appears in roadmap Phase 4.1, which lands *after* the web
  UI. At web-UI build time there's no lock → concurrent same-target runs could corrupt
  the JSON manifest.
- Fix: the web `service.py` owns its own per-target `asyncio.Lock` / single active
  slot, independent of Phase 4.1.

### M2 — roadmap 0.2 unresolved approach
- 0.2 offers "setuptools-scm OR attr:" — mutually exclusive; setuptools-scm needs git
  tags, breaking the current hand-built dist/exe flow.
- Fix: pick `attr: ytchannel.__version__` (no git dependency).

### M3 — roadmap 0.3 ffmpeg acceptance likely false
- Claims "plain `best` download without ffmpeg still works," but `downloader.py` says
  ffmpeg is required to merge video+audio and `best` usually needs merging.
- Fix: always *warn* when ffmpeg missing; *hard-fail* only for `--audio-only`/explicit
  merge.

### M4 — roadmap 0.1 can't build the exe
- "build Windows exe via PyInstaller" but no spec/command exists.
- Fix: add a `pyinstaller` spec/`build_exe.py` subtask invoking `ytchannel.cli:main`.

### M5 — roadmap 4.2 under-specifies refactor ripple
- Adding a `ManifestBackend` protocol touches every call site (cli, archiver, web
  service), but the plan only lists `manifest.py` changes.
- Fix: add a subtask for a `Manifest` ABC + `Manifest.open(path, backend=auto)` factory
  and update all call sites.

### M6 — web-ui localhost has no origin/CSRF protection (security)
- Even on `127.0.0.1` with no auth, any open browser tab can POST/WS to the server and
  trigger arbitrary-url downloads.
- Fix: validate `Origin` on state-changing POSTs + WS handshake (reject non-localhost
  origin); optionally a same-origin CSRF token. Must land before W1.

## Minor
- m1: WS subscriber can miss pre-connect events → send a state snapshot on connect.
- m2: roadmap 0.5 count fix must be carried into `Archiver`/`Planner` during Phase 1.
- m3: Phase 5 items lack Acceptance/Verify lines the plan's own convention requires.
- m4: W1.6 export should specify a `FileResponse` + filename.
- m5: 4.1 rate-limit → pick global-by-default.
- m6: 5.3 truncation can collide filenames → keep a short hash suffix.

## Per-file
- `roadmap.md`: phase ordering/deps solid; Majors localized (0.1/0.2/0.3/4.2).
- `web-ui.md`: architecture sound; only M1 and M6 are real defects, fixable without
  changing the split-action UI. No media-server creep.
