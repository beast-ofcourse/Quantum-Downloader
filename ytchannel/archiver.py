"""Download execution and progress reporting.

This module holds the side-effecting parts of the download orchestration that
used to live in ``cli.py``: the rich progress reporter and the
:func:`run_archiver` driver that loops over a :class:`~ytchannel.planner.DownloadPlan`,
applying rate limiting and recording outcomes.

The orchestration here is behavior-preserving: it mirrors the original
``cli.download`` loop exactly (same console messages, same KeyboardInterrupt
handling, same summary shape).
"""

from __future__ import annotations

import concurrent.futures as cf
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

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

from .config import Config
from .downloader import Downloader, DownloadReporter
from .manifest import BaseManifest
from .planner import DownloadPlan
from .utils.rate_limit import RateLimiter


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
        # Rich's live display is not inherently thread-safe; serialize updates
        # so a shared reporter can be driven from multiple worker threads.
        self._lock = threading.Lock()

    def video_start(self, title: str) -> None:
        with self._lock:
            self.progress.update(
                self.video_task,
                description=(title[:50] + ("…" if len(title) > 50 else "")),
                completed=0,
                total=None,
                visible=True,
            )

    def video_progress(self, data: Dict[str, Any]) -> None:
        with self._lock:
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
        with self._lock:
            self.progress.update(self.video_task, visible=False)
            self.progress.advance(self.overall, 1)

    def stop(self) -> None:
        with self._lock:
            self.progress.stop()


@dataclass
class Summary:
    downloaded: int
    already_complete: int
    failed: int
    failed_reasons: List[str]
    interrupted: bool = False


def run_archiver(
    config: Config,
    manifest: BaseManifest,
    plan: DownloadPlan,
    target_key: str,
    downloader_cls: type = Downloader,
    console: Optional[Console] = None,
    reporter: Optional[DownloadReporter] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> Summary:
    """Execute the downloads described by ``plan`` and return a summary.

    ``downloader_cls`` defaults to :class:`~ytchannel.downloader.Downloader`
    but is injectable so callers (and tests) can substitute a fake. The
    ``already_complete`` count comes from ``plan`` (computed before any
    download), never from the post-loop state.
    """
    console = console or Console()

    if not plan.pending:
        console.print("[green]All videos already downloaded. Nothing to do.[/]")
        return Summary(0, plan.already_complete, 0, [])

    console.print(
        f"[bold]Downloading[/] {len(plan.pending)} video(s) to "
        f"[bold]{config.output_dir}[/]"
    )

    # --- run log file (optional) ------------------------------------------
    log_f = None
    log_lock = threading.Lock()
    if config.log_file:
        log_f = open(config.log_file, "w", encoding="utf-8")  # noqa: SIM115

        def _log(line: str) -> None:
            with log_lock:
                log_f.write(f"{datetime.now().isoformat()} {line}\n")
                log_f.flush()

        _log(
            f"START target={target_key} planned={len(plan.pending)} "
            f"already_complete={plan.already_complete}"
        )
    else:

        def _log(line: str) -> None:  # noqa: ARG001 - no-op when disabled
            pass

    def _close_log() -> None:
        if log_f is not None:
            with log_lock:
                log_f.close()

    # --- reporter selection (quiet suppresses the rich UI) -----------------
    def _make_reporter() -> DownloadReporter:
        if config.quiet:
            return DownloadReporter()
        return RichReporter(total=len(plan.pending))

    if config.concurrency <= 1:
        downloader = downloader_cls(
            output_dir=config.output_dir,
            target_key=target_key,
            quality=config.quality,
            audio_only=config.audio_only,
            write_thumbnail=config.write_thumbnail,
            write_description=config.write_description,
            write_subs=config.write_subs,
            cookies=config.cookies,
            max_retries=config.max_retries,
            after=config.after,
            before=config.before,
            proxy=config.proxy,
            cookies_from_browser=config.cookies_from_browser,
            quiet=config.quiet,
            verbose=config.verbose,
            template=config.template,
        )

        reporter = reporter if reporter is not None else _make_reporter()
        rate_limiter = RateLimiter(base_delay=config.delay)
        downloaded = failed = 0
        failed_reasons: List[str] = []
        interrupted = False

        try:
            for idx, vid in enumerate(plan.pending):
                # Graceful cancellation: stop before starting the next video.
                if should_cancel is not None and should_cancel():
                    interrupted = True
                    break
                entry = manifest.entries[vid]
                # Delay between videos (not before the very first one).
                if idx > 0:
                    rate_limiter.delay()
                outcome = downloader.download(entry, manifest, reporter=reporter)
                if outcome["status"] == "complete":
                    downloaded += 1
                    _log(f"VIDEO {vid} status=complete")
                else:
                    failed += 1
                    reason = outcome.get("error", "unknown error")
                    failed_reasons.append(f"{entry.get('title', vid)}: {reason}")
                    _log(f"VIDEO {vid} status=failed error={reason}")
        except KeyboardInterrupt:
            reporter.stop()
            manifest.save()
            _close_log()
            console.print("\n[yellow]Interrupted.[/] Manifest saved; re-run to resume.")
            raise typer.Exit(code=130)

        if interrupted:
            manifest.save()
        reporter.stop()
        _log(f"END downloaded={downloaded} failed={failed}")
        _close_log()

        return Summary(
            downloaded,
            plan.already_complete,
            failed,
            failed_reasons,
            interrupted=interrupted,
        )

    # --- concurrent path (config.concurrency > 1) -------------------------
    # One downloader per worker (yt-dlp/Downloader is not thread-safe to share),
    # but a single shared reporter and a single shared rate limiter so politeness
    # stays global across all workers (roadmap m5 fix).
    dl_kwargs = dict(
        output_dir=config.output_dir,
        target_key=target_key,
        quality=config.quality,
        audio_only=config.audio_only,
        write_thumbnail=config.write_thumbnail,
        write_description=config.write_description,
        write_subs=config.write_subs,
        cookies=config.cookies,
        max_retries=config.max_retries,
        after=config.after,
        before=config.before,
        proxy=config.proxy,
        cookies_from_browser=config.cookies_from_browser,
        quiet=config.quiet,
        verbose=config.verbose,
        template=config.template,
    )

    reporter = reporter if reporter is not None else _make_reporter()
    rate_limiter = RateLimiter(base_delay=config.delay)

    def worker(vid: str) -> Dict[str, Any]:
        # Best-effort cancellation: skip this video if a cancel was requested.
        if should_cancel is not None and should_cancel():
            return {"video_id": vid, "status": "skipped", "error": "cancelled"}
        d = downloader_cls(**dl_kwargs)
        rate_limiter.delay()
        entry = manifest.entries[vid]
        outcome = d.download(entry, manifest, reporter=reporter)
        if outcome["status"] == "complete":
            _log(f"VIDEO {outcome['video_id']} status=complete")
        else:
            _log(
                f"VIDEO {outcome['video_id']} status=failed "
                f"error={outcome.get('error', 'unknown error')}"
            )
        return outcome

    ex = cf.ThreadPoolExecutor(max_workers=config.concurrency)
    futures = [ex.submit(worker, vid) for vid in plan.pending]
    outcomes: List[Dict[str, Any]] = []
    try:
        for fut in cf.as_completed(futures):
            outcomes.append(fut.result())
    except KeyboardInterrupt:
        ex.shutdown(wait=False, cancel_futures=True)
        reporter.stop()
        manifest.save()
        _close_log()
        console.print("\n[yellow]Interrupted.[/] Manifest saved; re-run to resume.")
        raise typer.Exit(code=130)

    ex.shutdown(wait=True)
    reporter.stop()

    downloaded = sum(1 for o in outcomes if o.get("status") == "complete")
    failed = sum(1 for o in outcomes if o.get("status") == "failed")
    failed_reasons = [
        f"{manifest.entries[o['video_id']].get('title', o['video_id'])}: "
        f"{o.get('error', 'unknown error')}"
        for o in outcomes
        if o.get("status") == "failed"
    ]
    _log(f"END downloaded={downloaded} failed={failed}")
    _close_log()

    # A worker that was cancelled before it started returns "skipped"; surface
    # that as an interrupted run so the job status/cancellation is honest.
    interrupted = any(o.get("status") == "skipped" for o in outcomes)
    return Summary(
        downloaded, plan.already_complete, failed, failed_reasons, interrupted=interrupted
    )
