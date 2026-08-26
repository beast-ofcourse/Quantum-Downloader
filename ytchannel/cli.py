"""Command-line interface for ytchannel.

Commands:
  index     List a channel's videos and export metadata (no downloads).
  download  Download (filtered) videos from a channel, resumably.

Global flags include --version. Configuration precedence is
CLI flags > config file > built-in defaults.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from . import __version__
from .config import DEFAULT_CONFIG_PATH, Config
from .downloader import Downloader, DownloadReporter
from .indexer import dry_run_summary, export_csv, export_json
from .manifest import Manifest, ManifestError
from .resolver import ResolutionError, resolve_channel, resolve_playlist
from .utils.organize import sanitize_segment
from .utils.rate_limit import RateLimiter

app = typer.Typer(
    help="Archive entire YouTube channels with resumable, idempotent downloads.",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"ytchannel {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """ytchannel — YouTube channel archiver."""


class RichReporter(DownloadReporter):
    """Rich-based progress reporter: overall count + per-video download bar."""

    def __init__(self, total: int) -> None:
        self.progress = Progress(
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
        )
        self.overall = self.progress.add_task("[green]Overall", total=total)
        self.video_task = self.progress.add_task("Video", total=None, visible=False)
        self.progress.start()

    def video_start(self, title: str) -> None:
        self.progress.update(
            self.video_task,
            description=(title[:50] + ("…" if len(title) > 50 else "")),
            completed=0,
            total=None,
            visible=True,
        )

    def video_progress(self, data: Dict[str, Any]) -> None:
        if data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes", 0)
            if total:
                self.progress.update(self.video_task, completed=downloaded, total=total)
            else:
                self.progress.update(self.video_task, completed=downloaded)
        elif data.get("status") == "finished":
            self.progress.update(self.video_task, visible=False)

    def video_finish(self) -> None:
        self.progress.update(self.video_task, visible=False)
        self.progress.advance(self.overall, 1)

    def stop(self) -> None:
        self.progress.stop()


def _filter_dates(
    videos: List[Dict[str, Any]],
    after: Optional[str],
    before: Optional[str],
) -> List[Dict[str, Any]]:
    def parse(d: Optional[str]):
        if not d:
            return None
        try:
            return datetime.strptime(d, "%Y%m%d").date()
        except ValueError:
            return None

    after_d = parse(after)
    before_d = parse(before)
    if not after_d and not before_d:
        return videos

    out: List[Dict[str, Any]] = []
    for v in videos:
        ud = v.get("upload_date")
        if not ud:
            out.append(v)  # keep videos whose date is unknown
            continue
        try:
            d = datetime.strptime(ud, "%Y%m%d").date()
        except (ValueError, TypeError):
            out.append(v)
            continue
        if after_d and d < after_d:
            continue
        if before_d and d > before_d:
            continue
        out.append(v)
    return out


def _fail(message: str, code: int = 1) -> "typer.Exit":
    err_console.print(f"[bold red]Error:[/] {message}")
    return typer.Exit(code=code)


def _manifest_path(output_dir: str, target_name: str) -> str:
    """Path to the manifest file for a given target (channel or playlist)."""
    return str(Path(output_dir) / (sanitize_segment(target_name) + ".manifest.json"))


@app.command()
def index(
    url: str = typer.Argument(..., help="Channel or playlist URL."),
    output: str = typer.Option("channel.json", "--output", "-o", help="Output file (.json or .csv)."),
    playlist: Optional[bool] = typer.Option(None, "--playlist", is_flag=True, help="Treat the URL as a playlist instead of a channel."),
) -> None:
    """List a channel's (or playlist's) videos and export metadata (no downloads)."""
    try:
        if playlist:
            result = resolve_playlist(url, quiet=True)
        else:
            result = resolve_channel(url, quiet=True)
    except ResolutionError as e:
        raise _fail(str(e))

    if output.endswith(".csv"):
        export_csv(result, output)
    else:
        export_json(result, output)

    name = result.get("target_name") or result.get("channel_name") or "target"
    console.print(
        f"[green]Exported[/] {len(result['videos'])} video(s) from "
        f"'{name}' to [bold]{output}[/]"
    )


@app.command()
def download(
    url: str = typer.Argument(..., help="Channel or playlist URL."),
    playlist: Optional[bool] = typer.Option(None, "--playlist", is_flag=True, help="Treat the URL as a playlist instead of a channel."),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Base download directory (default ./downloads)."),
    quality: Optional[str] = typer.Option(None, "--quality", help="e.g. 1080p, best, worst (default best)."),
    audio_only: Optional[bool] = typer.Option(None, "--audio-only", is_flag=True, help="Download audio only (mp3)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan and exit without downloading."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Stop after N videos."),
    after: Optional[str] = typer.Option(None, "--after", help="Only videos uploaded on/after DATE (YYYYMMDD)."),
    before: Optional[str] = typer.Option(None, "--before", help="Only videos uploaded on/before DATE (YYYYMMDD)."),
    write_thumbnail: Optional[bool] = typer.Option(None, "--write-thumbnail", is_flag=True, help="Save thumbnail alongside video."),
    write_description: Optional[bool] = typer.Option(None, "--write-description", is_flag=True, help="Save video description as .txt."),
    write_subs: Optional[bool] = typer.Option(None, "--write-subs", is_flag=True, help="Download available subtitles/captions."),
    cookies: Optional[str] = typer.Option(None, "--cookies", help="Path to a cookies file (members-only / age-restricted)."),
    delay: Optional[float] = typer.Option(None, "--delay", help="Seconds to wait between downloads (default 2)."),
    config: Optional[str] = typer.Option(None, "--config", help="Path to a TOML config file."),
) -> None:
    """Download (filtered) videos from a channel or playlist, resumably."""
    cfg = Config.from_file(config or DEFAULT_CONFIG_PATH)
    cli_opts = {
        "output_dir": output,
        "quality": quality,
        "audio_only": audio_only,
        "limit": limit,
        "after": after,
        "before": before,
        "write_thumbnail": write_thumbnail,
        "write_description": write_description,
        "write_subs": write_subs,
        "cookies": cookies,
        "delay": delay,
    }
    cfg.merge_cli(cli_opts)

    try:
        if playlist:
            result = resolve_playlist(url, quiet=True)
        else:
            result = resolve_channel(url, quiet=True)
    except ResolutionError as e:
        raise _fail(str(e))

    target_name = result.get("target_name") or result.get("channel_name") or "target"
    target_type = result.get("target_type", "channel")
    videos = result["videos"]

    # Apply filters at the reconciliation step (Phase 4).
    videos = _filter_dates(videos, cfg.after, cfg.before)
    if cfg.limit is not None:
        if cfg.limit < 0:
            raise _fail("--limit must be non-negative")
        videos = videos[: cfg.limit]

    if dry_run:
        summary = dry_run_summary({**result, "videos": videos})
        label = "Playlist" if target_type == "playlist" else "Channel"
        console.print(f"[bold]{label}:[/] {target_name}")
        console.print(f"[bold]Videos to download:[/] {summary['count']}")
        if summary["date_range"]:
            console.print(
                f"[bold]Date range:[/] {summary['date_range'][0]} .. {summary['date_range'][1]}"
            )
        else:
            console.print("[bold]Date range:[/] unknown (flat metadata lacks upload dates)")
        console.print(
            "[bold]Estimated size:[/] unknown (flat metadata does not include file sizes)"
        )
        return

    manifest_path = _manifest_path(cfg.output_dir, target_name)
    try:
        manifest = Manifest(manifest_path)
    except ManifestError as e:
        raise _fail(str(e))

    manifest.reconcile(videos, target_name)

    # Apply the filters to the pending set: only videos in the (filtered) list
    # that still need work are downloaded this run. This keeps --limit / date
    # filters authoritative even when the manifest has older pending/failed
    # entries from previous runs.
    pending_ids = set(manifest.get_pending())
    pending = [v["video_id"] for v in videos if v["video_id"] in pending_ids]
    if not pending:
        console.print("[green]All videos already downloaded. Nothing to do.[/]")
        return

    console.print(
        f"[bold]Downloading[/] {len(pending)} video(s) from '{target_name}' to "
        f"[bold]{cfg.output_dir}[/]"
    )

    downloader = Downloader(
        output_dir=cfg.output_dir,
        target_name=target_name,
        quality=cfg.quality,
        audio_only=cfg.audio_only,
        write_thumbnail=cfg.write_thumbnail,
        write_description=cfg.write_description,
        write_subs=cfg.write_subs,
        cookies=cfg.cookies,
    )

    reporter = RichReporter(total=len(pending))
    rate_limiter = RateLimiter(base_delay=cfg.delay)
    downloaded = failed = 0
    failed_reasons: List[str] = []

    try:
        for idx, vid in enumerate(pending):
            entry = manifest.entries[vid]
            # Delay between videos (not before the very first one).
            if idx > 0:
                rate_limiter.delay()
            outcome = downloader.download(entry, manifest, reporter=reporter)
            if outcome["status"] == "complete":
                downloaded += 1
            else:
                failed += 1
                reason = outcome.get("error", "unknown error")
                failed_reasons.append(f"{entry.get('title', vid)}: {reason}")
    except KeyboardInterrupt:
        reporter.stop()
        manifest.save()
        console.print("\n[yellow]Interrupted.[/] Manifest saved; re-run to resume.")
        raise typer.Exit(code=130)

    reporter.stop()

    # Summary table.
    already_complete = len(videos) - len(pending)
    table = Table(title="Download summary")
    table.add_column("Result", style="bold")
    table.add_column("Count", justify="right")
    table.add_row("Downloaded", str(downloaded))
    table.add_row("Skipped (already complete)", str(already_complete))
    table.add_row("Failed", str(failed))
    console.print(table)

    if failed_reasons:
        console.print("[red]Failures:[/]")
        for r in failed_reasons[:20]:
            console.print(f"  - {r}")
        if len(failed_reasons) > 20:
            console.print(f"  ... and {len(failed_reasons) - 20} more")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
