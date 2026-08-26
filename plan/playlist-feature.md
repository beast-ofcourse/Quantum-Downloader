# Feature Plan — Playlist Downloads

**Mode:** yolo (all decisions made by Architect; no questions asked)
**Goal:** Add the ability to download an *entire YouTube playlist* by its URL, reusing the existing channel-download machinery (resolver → manifest → downloader → indexer → CLI).

This is an additive feature on top of the shipped v1.0.0 tool. It does **not** change the on-disk manifest schema or the download engine — it adds a second *resolver entry point* and a CLI switch.

---

## Assumptions (yolo decisions)

1. **Interface:** add a `--playlist` boolean flag to the existing `index` and `download` commands. When present, the URL is treated as a playlist and `resolve_playlist` is used instead of `resolve_channel`. This matches the v1 plan's stated nice-to-have ("`--playlist <url>` mode … different resolver entry point") and avoids a second command to learn.
2. **URL forms accepted:** `https://www.youtube.com/playlist?list=PLxxxx`, `https://www.youtube.com/watch?v=VID&list=PLxxxx` (use the playlist, ignore the single video), and a bare playlist id `PLxxxx` (we prepend the canonical playlist URL). Anything else → clear error.
3. **Result schema:** standardize the resolver output dict to `target_type` (`"channel"`|`"playlist"`), `target_name`, `target_id`, `url`, `videos`. This is a small, safe refactor of the existing `channel_name` key so channels and playlists share one shape. `indexer` and `cli` are updated to read the new keys (with a fallback to `channel_name` so old callers/tests don't break mid-refactor).
4. **Manifest & output layout:** the on-disk identity is a **stable storage key** = `<target_type>_<target_id>` (e.g. `channel_UCxxxx` or `playlist_PLxxxx`), not the mutable display title. The manifest file is named `<sanitized storage_key>.manifest.json` and videos land in `<output_dir>/<sanitized storage_key>/`. `target_name` is retained only for display (console messages, manifest's stored name). This keeps resume/idempotency intact if a channel or playlist renames itself. Each playlist gets its own manifest (so the same video appearing in two playlists is tracked independently — correct).
5. **Limitations inherited from channels:** flat extraction typically omits `upload_date`, so `--after`/`--before` and the dry-run date range may be unavailable for playlists too; estimated size stays "unknown". These are documented, not fixed here.
6. **Auth:** mixes / "liked" / "created by you" playlists may require `--cookies`; that path already exists and is reused as-is.
7. **No new dependencies.** Everything is built on the existing `yt-dlp` + `typer` + `rich` stack.

---

## Design / architecture changes

```
URL (playlist) ──► normalize_playlist_url() ──► resolve_playlist()
                                                    │  (flat extraction, target_type="playlist")
                                                    ▼
                                            result { target_type, target_name, target_id, url, videos[] }
                                                    │
                    index ──────────────────────────┼───────────────────────── download
                            │                                                  │
                    indexer.export_*()                              manifest.reconcile(target_name)
                                                                           │
                                                                   Downloader.download()  (unchanged engine)
                                                                           │
                                                                   <output_dir>/<target_name>/...
                                                                   <target_name>.manifest.json
```

Modules touched:
- `ytchannel/resolver.py` — add `normalize_playlist_url`, `resolve_playlist`, and a shared `_flat_extract` helper; refactor `resolve_channel` result to the new schema.
- `ytchannel/indexer.py` — read `target_name`/`target_type` (fallback to `channel_name`).
- `ytchannel/cli.py` — add `--playlist` flag to `index`/`download`; route to `resolve_playlist`; use `target_name` for dir/manifest/messages.
- `tests/` — new + updated unit tests (mocked yt-dlp).
- `README.md` — document the flag and playlist URL forms.

---

## Spec checklist coverage (this feature)

| Area | Decision |
|---|---|
| Product | Download whole playlists, resumably/idempotently — same value prop as channels. |
| Users | Same persona as channel mode (archivists, researchers, rights-holders). |
| Platform | CLI only (no GUI change). |
| Features | `index --playlist`, `download --playlist`; same filters (`--limit`, `--after/--before`, `--quality`, etc.) apply. |
| Stack | Unchanged (Python, yt-dlp, typer, rich). |
| Data | Manifest keyed by video ID, per-playlist file; no schema change. |
| Auth/security | Reuses existing `--cookies`; no new secrets handling. |
| Scale | yt-dlp paginates playlists; same memory/throughput profile as channels. |
| Integrations | None new (yt-dlp only). |
| Design | Consistent with existing CLI/README style. |
| Deployment/ops | No new ops surface; ships in same package. |

---

## Task list

### Phase A — Result-schema refactor (prep, low risk)

- **P-A1** Refactor `resolve_channel` to return `target_type="channel"`, `target_name`, `target_id` (keep `url`, `videos`).
  - *Build:* `ytchannel/resolver.py` (`resolve_channel`).
  - *Accept:* existing `test_resolver` still passes; returned dict has `target_type=="channel"` and `target_name`.
  - *Verify:* `python -m pytest tests/test_resolver.py -q`
- **P-A2** Update `indexer.export_json/export_csv/dry_run_summary` to read `target_name`/`target_type` with fallback to `channel_name`.
  - *Build:* `ytchannel/indexer.py`.
  - *Accept:* JSON/CSV output uses the new key; old `channel_name` still accepted.
  - *Verify:* `python -m pytest tests/test_indexer.py -q`
- **P-A3** Update `cli.py` to read `target_name`/`target_type` for output dir, manifest path, and progress/summary messages.
  - *Build:* `ytchannel/cli.py`.
  - *Accept:* `ytchannel download "<channel-url>"` still works end-to-end (re-run a 1-video live check or existing mocked path).
  - *Verify:* `python -m pytest -q`
- **P-A4** Update affected tests (`test_indexer` SAMPLE key) and run ruff.
  - *Build:* `tests/`, repo.
  - *Accept:* `pytest` green, `ruff check ytchannel` clean.
  - *Verify:* `python -m pytest -q && python -m ruff check ytchannel`

### Phase B — Playlist resolver

- **P-B1** Implement `normalize_playlist_url(url)`: accept `playlist?list=`, `watch?v=…&list=`, or bare `PL…` id; construct/validate; raise `ValueError` on anything else.
  - *Build:* `ytchannel/resolver.py`.
  - *Accept:* `playlist?list=PLx` → `https://www.youtube.com/playlist?list=PLx`; `watch?v=V&list=PLx` → same playlist URL; bare `PLx` → canonical; junk → raises.
  - *Verify:* `python -m pytest tests/test_resolver.py -q`
- **P-B2** Implement `resolve_playlist(url)`: flat-extract, return `target_type="playlist"`, `target_name` = playlist title, `target_id` = list id, `videos[]`; raise `ResolutionError` if empty or not a playlist.
  - *Build:* `ytchannel/resolver.py`.
  - *Accept:* mocked yt-dlp returning playlist entries yields correct `videos` and `target_type`; empty → error.
  - *Verify:* `python -m pytest tests/test_resolver.py -q`
- **P-B3** Extract a shared `_flat_extract(url, quiet)` helper used by both resolvers (remove duplication).
  - *Build:* `ytchannel/resolver.py`.
  - *Accept:* both `resolve_channel` and `resolve_playlist` use it; tests green.
  - *Verify:* `python -m pytest -q`

### Phase C — CLI wiring

- **P-C1** Add `--playlist` flag to `index` and `download`; when set, call `resolve_playlist` instead of `resolve_channel`.
  - *Build:* `ytchannel/cli.py`.
  - *Accept:* `ytchannel index "<playlist-url>" --playlist` resolves; `download --playlist` downloads.
  - *Verify:* `python -m pytest -q`
- **P-C2** Use `target_name` for manifest filename + output dir; messages say "playlist" vs "channel" based on `target_type`.
  - *Build:* `ytchannel/cli.py`.
  - *Accept:* playlist run creates `<output_dir>/<playlist_name>/` and `<playlist_name>.manifest.json`.
  - *Verify:* `python -m pytest -q`

### Phase D — Tests

- **P-D1** Unit tests for `normalize_playlist_url` (valid/invalid forms).
  - *Build:* `tests/test_resolver.py`.
  - *Accept:* covers playlist?list=, watch&list=, bare id, invalid.
  - *Verify:* `python -m pytest tests/test_resolver.py -q`
- **P-D2** Unit tests for `resolve_playlist` with mocked yt-dlp (entries → videos; empty → error; non-playlist → error).
  - *Build:* `tests/test_resolver.py`.
  - *Accept:* pytest green.
  - *Verify:* `python -m pytest tests/test_resolver.py -q`
- **P-D3** Mocked end-to-end: `Downloader.download` with a playlist-derived video entry writes a manifest named by the playlist.
  - *Build:* `tests/test_downloader.py`.
  - *Accept:* pytest green.
  - *Verify:* `python -m pytest -q`

### Phase E — Docs & final verification

- **P-E1** Update README: document `--playlist` flag, accepted playlist URL forms, and the note that mixes/liked/created-by-you playlists may need `--cookies`.
  - *Build:* `README.md`.
  - *Accept:* accurate, renders, no invented claims.
  - *Verify:* visual review.
- **P-E2** Final gate: full `pytest`, `ruff check`, and an optional live smoke (`download --limit 1 --playlist` on a small public playlist).
  - *Build:* repo.
  - *Accept:* 39+ prior tests still green + new playlist tests green; ruff clean; live smoke downloads 1 playlist video and records it in the playlist manifest.
  - *Verify:* `python -m pytest -q && python -m ruff check ytchannel`

---

## Risks / unknowns

- **`upload_date` absence** in flat extraction applies to playlists too → date filters may be ineffective (documented, inherited).
- **`watch?v=V&list=L`** must resolve to playlist `L`, not video `V` — `normalize_playlist_url` strips to the playlist (covered by P-B1 tests).
- **Auth-gated playlists** (mixes, liked, "created by you") need `--cookies`; existing path reused, just documented.
- **Huge playlists** (1000s) rely on yt-dlp pagination; memory profile same as channels — acceptable for v1.

## Definition of done

- `ytchannel download "<playlist-url>" --playlist` downloads every video in the playlist to `./downloads/<playlist_name>/`, resumably and idempotently, tracked in `<playlist_name>.manifest.json`.
- `ytchannel index "<playlist-url>" --playlist` exports metadata without downloading.
- All filters/flags (`--limit`, `--quality`, `--audio-only`, `--write-*`, `--cookies`, `--delay`, `--config`) work identically to channel mode.
- Unit tests for normalization, resolution (mocked), and download (mocked) pass; `ruff` clean; README documents the feature.
- (Optional) one live `--limit 1 --playlist` smoke test succeeds.
