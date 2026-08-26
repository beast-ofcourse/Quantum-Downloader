# Quantum-Downloader

> Resumable, idempotent archiving of entire YouTube channels from the command line.

**Quantum-Downloader** (Python package: `ytchannel`) is a command-line tool that
downloads every video on a YouTube channel to local storage — organized,
resumable, and safe to re-run. It is a thin orchestration layer on top of
[`yt-dlp`](https://github.com/yt-dlp/yt-dlp), which performs the actual
extraction and downloading; this tool adds channel-level planning, a manifest
of download state, polite rate limiting, and a clean CLI.

![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Built with yt-dlp](https://img.shields.io/badge/built%20with-yt--dlp-red.svg)

---

> **Scope & legal disclaimer.** This tool is intended for archiving content you
> have the right to download — your own channel, Creative Commons / public-domain
> material, or content you are otherwise licensed to archive. It is **not**
> intended for redistributing or pirating copyrighted content you do not have
> rights to. Respect YouTube's Terms of Service and applicable law. Age-restricted
> or members-only content is only accessible with cookies from a session you are
> legitimately logged into (see `--cookies`).

## Features

- **Whole-channel downloads** — point it at a channel URL and walk away.
- **Resumable** — progress is saved to a manifest after every video; kill the
  process or lose the connection and re-run to continue exactly where you left off.
- **Idempotent** — re-running the same command never re-downloads completed work.
- **Multiple URL forms** — `@handle`, `/c/name`, `/channel/UC…`, `/user/name`
  are all normalized to the channel's `/videos` tab automatically.
- **Metadata export** — `index` writes the full video list to JSON or CSV with no
  downloads.
- **Polite & resilient** — configurable delay between downloads, exponential
  backoff on transient errors, and permanent failures (private/deleted/region-
  blocked) are recorded and skipped rather than retried forever.
- **One bad video doesn't abort the batch** — failures are collected and reported
  at the end.
- **Extras** — optional thumbnails, descriptions, subtitles, and cookies for
  members-only / age-restricted content.
- **Config file** — persist common defaults; CLI flags always win.

## Requirements

- Python **3.9+**
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) (installed automatically)
- [`ffmpeg`](https://ffmpeg.org/) — **required** to merge separate video+audio
  streams and for `--audio-only` (mp3) extraction. Install it and put it on your
  `PATH`.

> **Note:** For explicit quality selection (e.g. `--quality 1080p`), yt-dlp
> benefits from a JavaScript runtime (deno or node) to read the full format list.
> The default `best` quality works without one.

## Installation

From a clone of this repository:

```bash
git clone https://github.com/beast-ofcourse/Quantum-Downloader.git
cd Quantum-Downloader
pip install -e .
ytchannel --version
```

Or install from a built wheel:

```bash
pip install dist/ytchannel-1.0.0-py3-none-any.whl
```

## Quick start

List a channel's videos and export metadata (no downloads):

```bash
ytchannel index "https://www.youtube.com/@handle" -o channel.json
ytchannel index "https://www.youtube.com/@handle" -o channel.csv
```

Download the whole channel, resumably:

```bash
ytchannel download "https://www.youtube.com/@handle"
```

Re-running the same command is a no-op for already-downloaded videos and resumes
cleanly after an interruption.

## Usage

### `ytchannel index <url>`

Resolves a channel and writes its video list to a file.

| Flag | Description |
|------|-------------|
| `-o, --output` | Output path; `.csv` exports CSV, anything else exports JSON. |

### `ytchannel download <url>`

Downloads (filtered) videos from a channel.

| Flag | Description |
|------|-------------|
| `-o, --output` | Base download directory (default `./downloads`). |
| `--quality` | `best` (default), `worst`, or a height like `1080p`. |
| `--audio-only` | Download audio only, converted to mp3 (needs ffmpeg). |
| `--dry-run` | Print the plan (count, date range) and exit without downloading. |
| `--limit N` | Stop after `N` videos. |
| `--after DATE` / `--before DATE` | Filter by upload date (`YYYYMMDD`). *See limitations.* |
| `--write-thumbnail` | Save the thumbnail alongside the video. |
| `--write-description` | Save the video description as a `.txt` file. |
| `--write-subs` | Download available subtitles/captions. |
| `--cookies <path>` | Path to a cookies file (members-only / age-restricted). |
| `--delay SECONDS` | Seconds to wait between downloads (default 2) to avoid throttling. |
| `--config <path>` | Path to a TOML config file. |

## Output structure

```
<output_dir>/<channel_name>/<upload_date>_<video_title>.<ext>
```

State is tracked in a manifest file (`<channel_name>.manifest.json`) next to the
output directory. The manifest — not the filesystem — is the source of truth for
what has been downloaded, so a crash or Ctrl-C never forces a restart from
scratch, and renaming or title edits don't cause re-downloads.

## Configuration

Common defaults can be persisted in `~/.config/ytchannel/config.toml`:

```toml
[defaults]
output_dir = "~/Videos/archive"
quality = "1080p"
delay = 5
write_thumbnail = true
```

Precedence is **CLI flags > config file > built-in defaults**.

## How resumability works

1. **Resolve** the channel URL into a canonical ID and a full video list.
2. **Plan** by reconciling that list against the manifest: new videos become
   `pending`, completed videos are left alone, and any video left `downloading`
   from a crashed run is reset to `pending`.
3. **Execute** the pending videos sequentially, updating the manifest after each
   success or failure.
4. **Report** a summary of downloaded / skipped / failed.

A partial `.part` file from an interrupted download is resumed automatically by
yt-dlp on the next run.

## Resilience & rate limiting

- **Resumable:** state is saved after every video.
- **Polite:** a short delay between downloads (configurable) reduces the chance
  of rate-limiting.
- **Retry with backoff:** transient errors (network blips, HTTP 429/5xx) are
  retried with exponential backoff. Permanent failures (private, deleted,
  region-blocked) are recorded and skipped, never retried forever.
- **One bad video doesn't abort the batch:** failures are reported at the end.

## Limitations

- **Upload dates:** the fast "flat" extraction yt-dlp uses typically does **not**
  include `upload_date`, so `--after` / `--before` and the dry-run date range may
  be unavailable for some channels. When dates are present they are honored.
- **Estimated size:** flat metadata does not include file sizes, so the dry-run
  estimated size is always reported as unknown.
- **Concurrency:** v1 downloads sequentially by default. Limited concurrency is a
  post-v1 nice-to-have.

## Development

```bash
pip install -e ".[dev]"
pytest          # run the test suite
ruff check ytchannel   # lint
```

## License

MIT — see [LICENSE](LICENSE).
