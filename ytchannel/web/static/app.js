/*
 * ytchannel web UI — vanilla JS control panel.
 *
 * Talks to the backend over the fixed API contract (plans/web-ui.md W1):
 *   POST /api/jobs                 -> { job_id, status }
 *   GET  /api/jobs/{id}            -> full job incl. report
 *   WS   /api/jobs/{id}/progress   -> snapshot, then live events, then complete/cancelled
 *   GET  /api/targets              -> [{ key, target_name, completed, failed, pending }]
 *   POST /api/jobs/{id}/cancel     -> { cancelled: bool }
 *   GET  /api/health               -> { status: "ok" }
 *
 * No build step, no frameworks. Plain fetch + WebSocket.
 */

(function () {
  "use strict";

  // ---- DOM references -------------------------------------------------------
  const $ = (id) => document.getElementById(id);

  const form = $("job-form");
  const urlInput = $("url");
  const urlError = $("url-error");
  const startBtn = $("start-btn");
  const dryRunBtn = $("dryrun-btn");
  const cancelBtn = $("cancel-btn");
  const formMessage = $("form-message");

  const statusPill = $("status-pill");
  const targetName = $("target-name");
  const progressBar = $("progress-bar");
  const progressFill = $("progress-fill");
  const counter = $("counter");
  const percent = $("percent");
  const reportBox = $("report");
  const videoList = $("video-list");
  const videoListEmpty = $("video-list-empty");

  const targetsState = $("targets-state");
  const targetsTable = $("targets-table");
  const targetsBody = $("targets-body");

  const healthDot = $("health-dot");
  const healthText = $("health-text");

  // ---- Runtime state --------------------------------------------------------
  let currentJobId = null;     // active job id (download or dry run)
  let socket = null;           // active WebSocket for progress
  let dryRunTimer = null;      // polling timer for dry-run report
  let activeVideoEl = null;    // the <li> currently "in progress"
  let progress = { done: 0, total: 0 }; // overall counters

  // ===========================================================================
  // Helpers
  // ===========================================================================

  // Read the form into a JSON body matching the API contract.
  // Empty optional fields are omitted entirely (keeps payload clean).
  function buildJobBody(forceDryRun) {
    const val = (id) => $(id).value.trim();
    const checked = (id) => $(id).checked;
    const num = (id) => {
      const v = $(id).value.trim();
      if (v === "") return null;
      const n = Number(v);
      return Number.isFinite(n) ? n : null;
    };

    const body = { url: val("url") };

    // Booleans: only send true when checked.
    const bools = [
      "audio_only", "write_thumbnail", "write_description",
      "write_subs", "quiet", "verbose",
    ];
    bools.forEach((k) => { if (checked(k)) body[k] = true; });

    // Strings: send only when non-empty.
    // NOTE: template / log_file are intentionally omitted — the web server pins
    // the output directory and ignores client-supplied filesystem paths for
    // safety, so offering them here would be a silently-ignored footgun.
    const strings = [
      "quality", "cookies", "proxy",
      "manifest_backend", "cookies_from_browser",
    ];
    strings.forEach((k) => { const v = val(k); if (v) body[k] = v; });

    // Numbers: send only when provided.
    const numbers = ["limit", "delay", "concurrency", "after", "before"];
    numbers.forEach((k) => { const n = num(k); if (n !== null) body[k] = n; });

    // Dry run: explicit override wins, else the form checkbox.
    const dry = forceDryRun === true || checked("dry_run");
    if (dry) body.dry_run = true;

    return body;
  }

  function showFormMessage(text, kind) {
    formMessage.textContent = text;
    formMessage.className = "form-message" + (kind ? " " + kind : "");
    formMessage.hidden = !text;
  }

  function showUrlError(text) {
    urlError.textContent = text;
    urlError.hidden = !text;
    urlInput.setAttribute("aria-invalid", text ? "true" : "false");
  }

  // Status pill + aria.
  function setStatus(status) {
    const map = {
      idle: "pill-idle",
      downloading: "pill-downloading",
      complete: "pill-complete",
      cancelled: "pill-cancelled",
      error: "pill-error",
    };
    statusPill.className = "pill " + (map[status] || "pill-idle");
    statusPill.textContent = status;
  }

  function setHealth(ok, text) {
    healthDot.className = "dot " + (ok === null ? "dot-unknown" : ok ? "dot-ok" : "dot-bad");
    healthText.textContent = text;
  }

  // Update the overall progress bar + counter.
  function renderProgress() {
    const total = progress.total || 0;
    const done = Math.min(progress.done, total);
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    progressFill.style.width = pct + "%";
    counter.textContent = done + " / " + total;
    percent.textContent = pct + "%";
    progressBar.setAttribute("aria-valuenow", String(pct));
  }

  function resetProgressPane() {
    progress = { done: 0, total: 0 };
    videoList.innerHTML = "";
    videoListEmpty.hidden = false;
    videoListEmpty.textContent = "No videos yet. Start a download to see live progress here.";
    reportBox.hidden = true;
    reportBox.innerHTML = "";
    activeVideoEl = null;
    renderProgress();
  }

  // Append a per-video row when a video starts downloading.
  function addVideoRow(title) {
    videoListEmpty.hidden = true;
    const li = document.createElement("li");
    li.className = "video-item video-active";
    li.innerHTML =
      '<span class="video-state" aria-hidden="true">●</span>' +
      '<span class="video-title"></span>';
    li.querySelector(".video-title").textContent = title || "(untitled)";
    videoList.appendChild(li);
    activeVideoEl = li;
    // Keep the newest at the bottom in view.
    li.scrollIntoView({ block: "nearest" });
  }

  // Mark the active video row as finished.
  function finishVideoRow() {
    if (activeVideoEl) {
      activeVideoEl.className = "video-item video-done";
      const st = activeVideoEl.querySelector(".video-state");
      if (st) st.textContent = "✓";
      activeVideoEl = null;
    }
    progress.done += 1;
    renderProgress();
  }

  // ===========================================================================
  // Job creation
  // ===========================================================================

  async function createJob(dryRun) {
    const body = buildJobBody(dryRun);

    if (!body.url) {
      showUrlError("Please enter a channel or playlist URL.");
      urlInput.focus();
      return;
    }
    showUrlError("");

    // Optimistic UI: show we're working.
    setStatus(dryRun ? "downloading" : "downloading");
    if (!dryRun) {
      targetName.textContent = body.url;
      resetProgressPane();
    }
    showFormMessage(dryRun ? "Requesting dry-run preview…" : "Starting download…", "info");
    startBtn.disabled = true;
    dryRunBtn.disabled = true;
    cancelBtn.disabled = true;

    try {
      const resp = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      if (!resp.ok) {
        // Surface the backend's error text (e.g. 400 invalid URL).
        let msg = "Request failed (" + resp.status + ").";
        try {
          const data = await resp.json();
          if (data && data.detail) msg = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
          else if (data && data.error) msg = String(data.error);
        } catch (_) { /* non-JSON error body */ }
        throw new Error(msg);
      }

      const data = await resp.json();
      currentJobId = data.job_id;
      if (dryRun) {
        startDryRunPolling(currentJobId);
      } else {
        cancelBtn.disabled = false;
        openProgressSocket(currentJobId);
      }
    } catch (err) {
      setStatus("error");
      showFormMessage(err.message || "Could not reach the backend.", "error");
      startBtn.disabled = false;
      dryRunBtn.disabled = false;
      cancelBtn.disabled = true;
    }
  }

  // ===========================================================================
  // Live progress (WebSocket)
  // ===========================================================================

  function openProgressSocket(jobId) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = proto + "//" + location.host + "/api/jobs/" + encodeURIComponent(jobId) + "/progress";

    try {
      socket = new WebSocket(url);
    } catch (err) {
      setStatus("error");
      showFormMessage("Could not open progress connection: " + err.message, "error");
      return;
    }

    socket.onopen = () => {
      showFormMessage("", "");
    };

    socket.onmessage = (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (_) {
        return; // ignore non-JSON frames
      }
      handleProgressMessage(msg);
    };

    socket.onerror = () => {
      // The onclose handler does the user-visible state change.
    };

    socket.onclose = () => {
      // If we're still "downloading" with no terminal event, mark error.
      if (statusPill.textContent === "downloading") {
        setStatus("error");
        showFormMessage("Progress connection closed unexpectedly.", "error");
      }
      socket = null;
    };
  }

  function handleProgressMessage(msg) {
    if (!msg || typeof msg !== "object") return;
    const type = msg.type;

    if (type === undefined) {
      // First frame is the snapshot object (no "type" field).
      renderSnapshot(msg);
      return;
    }

    switch (type) {
      case "video_start":
        addVideoRow(msg.title);
        break;
      case "progress":
        // Optional granular progress; we keep the row active.
        break;
      case "video_finish":
        finishVideoRow();
        break;
      case "complete":
        onComplete(msg.report);
        break;
      case "cancelled":
        onCancelled();
        break;
      default:
        break;
    }
  }

  // Snapshot: { exists:true, completed, failed, pending } or { exists:false }
  function renderSnapshot(snap) {
    if (!snap || snap.exists === false) {
      // Nothing on the server yet; wait for live events.
      progress = { done: 0, total: 0 };
      renderProgress();
      return;
    }
    const completed = Number(snap.completed) || 0;
    const failed = Number(snap.failed) || 0;
    const pending = Number(snap.pending) || 0;
    progress = { done: completed, total: completed + failed + pending };
    renderProgress();
  }

  function onComplete(report) {
    setStatus("complete");
    cancelBtn.disabled = true;
    startBtn.disabled = false;
    dryRunBtn.disabled = false;
    if (socket) { try { socket.close(); } catch (_) {} socket = null; }

    if (report && typeof report === "object") {
      const downloaded = report.downloaded ?? report.completed ?? 0;
      const skipped = report.skipped ?? 0;
      const failed = report.failed ?? 0;
      reportBox.hidden = false;
      reportBox.innerHTML =
        '<div class="report-title">Done</div>' +
        '<div class="report-grid">' +
        '<div><span class="report-num">' + downloaded + '</span><span class="report-lbl">downloaded</span></div>' +
        '<div><span class="report-num">' + skipped + '</span><span class="report-lbl">skipped</span></div>' +
        '<div><span class="report-num">' + failed + '</span><span class="report-lbl">failed</span></div>' +
        "</div>";
    }
    showFormMessage("Download complete.", "info");
  }

  function onCancelled() {
    setStatus("cancelled");
    cancelBtn.disabled = true;
    startBtn.disabled = false;
    dryRunBtn.disabled = false;
    if (socket) { try { socket.close(); } catch (_) {} socket = null; }
    showFormMessage("Job cancelled.", "info");
  }

  // ===========================================================================
  // Dry run (poll GET /api/jobs/{id} until report present)
  // ===========================================================================

  function startDryRunPolling(jobId) {
    if (dryRunTimer) clearTimeout(dryRunTimer);
    const poll = async () => {
      try {
        const resp = await fetch("/api/jobs/" + encodeURIComponent(jobId));
        if (resp.ok) {
          const job = await resp.json();
          if (job && job.report) {
            renderDryRunReport(job.report);
            return; // stop polling
          }
        }
      } catch (_) { /* backend may still be planning; keep polling */ }
      dryRunTimer = setTimeout(poll, 1000);
    };
    poll();
  }

  function renderDryRunReport(report) {
    setStatus("complete");
    startBtn.disabled = false;
    dryRunBtn.disabled = false;
    cancelBtn.disabled = true;
    targetName.textContent = "Dry run preview";

    const count = report.count ?? report.total ?? report.videos ?? 0;
    const dateRange = Array.isArray(report.date_range)
      ? report.date_range.filter(Boolean).join(" → ")
      : (report.date_range || "—");
    const duration = report.total_duration_seconds ?? report.total_duration ?? report.duration ?? null;
    const durStr = duration != null ? formatDuration(duration) : "—";

    reportBox.hidden = false;
    reportBox.innerHTML =
      '<div class="report-title">Dry run — plan only, nothing downloaded</div>' +
      '<div class="report-grid">' +
      '<div><span class="report-num">' + count + '</span><span class="report-lbl">videos</span></div>' +
       '<div><span class="report-num report-small">' + escapeHtml(dateRange) + '</span><span class="report-lbl">date range</span></div>' +
      '<div><span class="report-num report-small">' + durStr + '</span><span class="report-lbl">total duration</span></div>' +
      "</div>";
    showFormMessage("Dry run complete.", "info");
  }

  function formatDuration(seconds) {
    const s = Number(seconds) || 0;
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = Math.floor(s % 60);
    if (h > 0) return h + "h " + m + "m";
    if (m > 0) return m + "m " + sec + "s";
    return sec + "s";
  }

  // ===========================================================================
  // Cancel
  // ===========================================================================

  async function cancelJob() {
    if (!currentJobId) return;
    cancelBtn.disabled = true;
    try {
      const resp = await fetch("/api/jobs/" + encodeURIComponent(currentJobId) + "/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (resp.ok) {
        // WS may also send "cancelled"; either way reflect it.
        onCancelled();
      } else {
        showFormMessage("Cancel request failed (" + resp.status + ").", "error");
        cancelBtn.disabled = false;
      }
    } catch (err) {
      showFormMessage("Cancel failed: " + err.message, "error");
      cancelBtn.disabled = false;
    }
  }

  // ===========================================================================
  // Targets table
  // ===========================================================================

  async function loadTargets() {
    targetsState.hidden = false;
    targetsState.textContent = "Loading targets…";
    targetsTable.hidden = true;
    try {
      const resp = await fetch("/api/targets");
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const targets = await resp.json();
      if (!Array.isArray(targets) || targets.length === 0) {
        targetsState.textContent = "No targets yet. Downloads you run will appear here.";
        return;
      }
      targetsBody.innerHTML = "";
      targets.forEach((t) => {
        const tr = document.createElement("tr");
        tr.tabIndex = 0;
        tr.setAttribute("role", "button");
        tr.setAttribute("aria-label", "Start a job for " + (t.target_name || t.key));
        tr.innerHTML =
          "<td>" + escapeHtml(t.target_name || t.key) + "</td>" +
          "<td>" + (t.completed ?? 0) + "</td>" +
          "<td>" + (t.failed ?? 0) + "</td>" +
          "<td>" + (t.pending ?? 0) + "</td>";
        const fill = () => {
          urlInput.value = t.key || "";
          showUrlError("");
          validateUrl();
          urlInput.focus();
        };
        tr.addEventListener("click", fill);
        tr.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fill(); }
        });
        targetsBody.appendChild(tr);
      });
      targetsState.hidden = true;
      targetsTable.hidden = false;
    } catch (err) {
      targetsState.textContent = "Could not load targets (backend offline?).";
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // ===========================================================================
  // Health check
  // ===========================================================================

  async function checkHealth() {
    try {
      const resp = await fetch("/api/health");
      if (resp.ok) {
        const data = await resp.json().catch(() => ({}));
        setHealth(true, data.status === "ok" ? "backend online" : "backend online");
      } else {
        setHealth(false, "backend error");
      }
    } catch (_) {
      setHealth(false, "backend offline");
    }
  }

  // ===========================================================================
  // Validation + wiring
  // ===========================================================================

  function validateUrl() {
    const has = urlInput.value.trim().length > 0;
    startBtn.disabled = !has;
    dryRunBtn.disabled = !has;
    if (!has) showUrlError("");
  }

  urlInput.addEventListener("input", () => {
    validateUrl();
    if (urlInput.value.trim()) showUrlError("");
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    createJob(false);
  });

  dryRunBtn.addEventListener("click", () => createJob(true));
  cancelBtn.addEventListener("click", cancelJob);

  // Initial load.
  validateUrl();
  setStatus("idle");
  resetProgressPane();
  checkHealth();
  loadTargets();
  // Re-check health periodically so the dot stays honest.
  setInterval(checkHealth, 15000);
})();
