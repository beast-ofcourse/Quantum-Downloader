"""Command-line interface for ytchannel.

Commands:
  index     List a channel's videos and export metadata (no downloads).
  download  Download (filtered) videos from a channel, resumably.

Global flags include --version. Configuration precedence is
CLI flags > config file > built-in defaults.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .archiver import run_archiver
from .config import DEFAULT_CONFIG_PATH, Config
from .downloader import Downloader
from .indexer import dry_run_summary, export_csv, export_json
from .manifest import Manifest, ManifestError
from .planner import filter_videos, plan_downloads
from .resolver import ResolutionError, resolve_channel, resolve_playlist
from .utils.organize import sanitize_segment

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


def _fail(message: str, code: int = 1) -> "typer.Exit":
    err_console.print(f"[bold red]Error:[/] {message}")
    return typer.Exit(code=code)


def _check_ffmpeg(require: bool) -> None:
    """Warn (or hard-fail) when ffmpeg is missing.

    ffmpeg is required to merge separate video+audio streams and for
    --audio-only. A plain `best` download may still need ffmpeg at merge time,
    so we only *warn* in that case and *fail* when a merge is explicitly needed.
    """
    if shutil.which("ffmpeg"):
        return
    if require:
        raise _fail(
            "ffmpeg not found on PATH. Install from https://ffmpeg.org "
            "(required for --audio-only / merged formats)."
        )
    err_console.print(
        "[yellow]Warning:[/] ffmpeg not found on PATH; video/audio merging may "
        "fail. Install from https://ffmpeg.org if downloads error out."
    )


def _storage_key(result: Dict[str, Any]) -> str:
    """Stable on-disk identity for a target: type + id.

    Uses the immutable YouTube id, not the display title, so a channel or
    playlist renaming itself does not orphan the manifest or download folder.
    """
    ttype = result.get("target_type", "target")
    tid = result.get("target_id") or result.get("target_name") or "unknown"
    return f"{ttype}_{tid}"


def _manifest_path(output_dir: str, key: str) -> str:
    """Path to the manifest file for a given target (channel or playlist).

    Keyed by the stable storage key (target_type + target_id), not the display
    name, so title renames don't break resume/idempotency.
    """
    return str(Path(output_dir) / (sanitize_segment(key) + ".manifest.json"))


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
    except (ResolutionError, ValueError) as e:
        raise _fail(str(e)) from e

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
    proxy: Optional[str] = typer.Option(None, "--proxy", help="HTTP/HTTPS proxy URL (e.g. http://host:port) passed to yt-dlp."),
    cookies_from_browser: Optional[str] = typer.Option(None, "--cookies-from-browser", help="Browser to read cookies from (chrome, firefox, edge, ...). Conflicts with --cookies."),
    delay: Optional[float] = typer.Option(None, "--delay", help="Seconds to wait between downloads (default 2)."),
    retries: Optional[int] = typer.Option(None, "--retries", help="Max retries per video on transient errors (default 3)."),
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
        "max_retries": retries,
        "proxy": proxy,
        "cookies_from_browser": cookies_from_browser,
    }
    cfg.merge_cli(cli_opts)

    if cfg.cookies and cfg.cookies_from_browser:
        raise _fail("Cannot use both --cookies and --cookies-from-browser.")

    try:
        if playlist:
            result = resolve_playlist(url, quiet=True)
        else:
            result = resolve_channel(url, quiet=True)
    except (ResolutionError, ValueError) as e:
        raise _fail(str(e)) from e

    target_name = result.get("target_name") or result.get("channel_name") or "target"
    target_type = result.get("target_type", "channel")
    storage_key = _storage_key(result)
    videos = result["videos"]

    # Apply filters at the reconciliation step (Phase 4).
    if cfg.limit is not None and cfg.limit < 0:
        raise _fail("--limit must be non-negative")
    videos = filter_videos(videos, cfg)

    if dry_run:
        dry_summary = dry_run_summary({**result, "videos": videos})
        label = "Playlist" if target_type == "playlist" else "Channel"
        console.print(f"[bold]{label}:[/] {target_name}")
        console.print(f"[bold]Videos to download:[/] {dry_summary['count']}")
        if dry_summary["date_range"]:
            console.print(
                f"[bold]Date range:[/] {dry_summary['date_range'][0]} .. {dry_summary['date_range'][1]}"
            )
        else:
            console.print("[bold]Date range:[/] unknown (flat metadata lacks upload dates)")
        console.print(
            "[bold]Estimated size:[/] unknown (flat metadata does not include file sizes)"
        )
        return

    manifest_path = _manifest_path(cfg.output_dir, storage_key)
    try:
        manifest = Manifest(manifest_path)
    except ManifestError as e:
        raise _fail(str(e))

    manifest.reconcile(videos, target_name)

    plan = plan_downloads(videos, manifest, cfg)
    _check_ffmpeg(require=bool(cfg.audio_only))
    summary = run_archiver(
        cfg,
        manifest,
        plan,
        target_key=storage_key,
        downloader_cls=Downloader,  # references cli-module Downloader so tests can monkeypatch cli_mod.Downloader
        console=console,
    )

    # Summary table.
    table = Table(title="Download summary")
    table.add_column("Result", style="bold")
    table.add_column("Count", justify="right")
    table.add_row("Downloaded", str(summary.downloaded))
    table.add_row("Skipped (already complete)", str(summary.already_complete))
    table.add_row("Failed", str(summary.failed))
    console.print(table)

    if summary.failed_reasons:
        console.print("[red]Failures:[/]")
        for r in summary.failed_reasons[:20]:
            console.print(f"  - {r}")
        if len(summary.failed_reasons) > 20:
            console.print(f"  ... and {len(summary.failed_reasons) - 20} more")


@app.command()
def verify(
    target: str = typer.Argument(..., help="Channel/playlist URL, or a path to a *.manifest.json file."),
    playlist: Optional[bool] = typer.Option(None, "--playlist", is_flag=True, help="Treat the URL as a playlist."),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Base download directory (default ./downloads)."),
    delete_orphans: bool = typer.Option(False, "--delete-orphans", help="Delete orphan media files on disk (asks for confirmation)."),
) -> None:
    """Verify that downloaded files match the manifest and surface orphans."""
    output_dir = output or "./downloads"
    if target.endswith(".manifest.json"):
        manifest_path = target
        if output is None:
            output_dir = str(Path(target).parent)
    else:
        try:
            if playlist:
                result = resolve_playlist(target, quiet=True)
            else:
                result = resolve_channel(target, quiet=True)
        except (ResolutionError, ValueError) as e:
            raise _fail(str(e)) from e
        storage_key = _storage_key(result)
        manifest_path = _manifest_path(output_dir, storage_key)

    try:
        manifest = Manifest(manifest_path)
    except ManifestError as e:
        raise _fail(str(e))

    report = manifest.check_files(output_dir)

    table = Table(title="Verification")
    table.add_column("Result", style="bold")
    table.add_column("Count", justify="right")
    table.add_row("Complete (file present)", str(len(report["complete_present"])))
    table.add_row("Complete (file missing)", str(len(report["complete_missing"])))
    table.add_row("Orphan on disk", str(len(report["orphan_on_disk"])))
    console.print(table)

    if report["complete_missing"]:
        console.print("[red]Missing files:[/]")
        for entry in report["complete_missing"][:20]:
            console.print(f"  - {entry.get('file_path') or entry.get('video_id')}")
        if len(report["complete_missing"]) > 20:
            console.print(f"  ... and {len(report['complete_missing']) - 20} more")

    if report["orphan_on_disk"]:
        console.print("[yellow]Orphan files (on disk, not in manifest):[/]")
        for path in report["orphan_on_disk"][:20]:
            console.print(f"  - {path}")
        if len(report["orphan_on_disk"]) > 20:
            console.print(f"  ... and {len(report['orphan_on_disk']) - 20} more")

    if delete_orphans and report["orphan_on_disk"]:
        if typer.confirm(f"Delete {len(report['orphan_on_disk'])} orphan file(s)?"):
            count = 0
            for path in report["orphan_on_disk"]:
                os.remove(path)
                count += 1
            console.print(f"[green]Deleted {count} orphan file(s).[/]")
        else:
            console.print("Cancelled; no orphan files were deleted.")


@app.command()
def update(
    url: str = typer.Argument(..., help="Channel or playlist URL to re-index."),
    playlist: Optional[bool] = typer.Option(None, "--playlist", is_flag=True, help="Treat the URL as a playlist."),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Base download directory (default ./downloads)."),
    config: Optional[str] = typer.Option(None, "--config", help="Path to a TOML config file."),
) -> None:
    """Re-index a channel/playlist, adding new videos to the manifest."""
    cfg = Config.from_file(config or DEFAULT_CONFIG_PATH)
    cfg.merge_cli({"output_dir": output})  # -o overrides config default
    output_dir = cfg.output_dir or "./downloads"
    try:
        if playlist:
            result = resolve_playlist(url, quiet=True)
        else:
            result = resolve_channel(url, quiet=True)
    except (ResolutionError, ValueError) as e:
        raise _fail(str(e)) from e

    storage_key = _storage_key(result)
    manifest_path = _manifest_path(output_dir, storage_key)

    try:
        manifest = Manifest(manifest_path)
    except ManifestError as e:
        raise _fail(str(e))

    prior_ids = set(manifest.entries.keys())
    manifest.reconcile(
        result["videos"],
        result.get("target_name") or result.get("channel_name"),
    )
    added = set(manifest.entries.keys()) - prior_ids
    removed = prior_ids - set(manifest.entries.keys())
    name = result.get("target_name") or result.get("channel_name") or "target"
    console.print(
        f"[green]Re-indexed[/] '{name}': added {len(added)} new video(s), "
        f"{len(removed)} no longer in source (kept in manifest)."
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
