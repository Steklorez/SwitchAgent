/* The Library page's activity panel: everything that runs on its own in the
   background, live, and one line that says whether what is on screen is
   everything yet.

   - the library scan: at startup, after a Rescan, and whenever a Library
     folder changes (the watcher). Until this existed only a Rescan click
     ever showed one; an automatic pass ran silently and the grid simply
     changed under the user some minutes later;
   - cover downloads;
   - the console's "Installed games" read, which "On Switch" badges wait
     for -- it can take a while on a console with many titles.

   One read, GET /api/activity, every second while anything is running and
   every few seconds otherwise (a watcher scan or a newly plugged-in console
   can start at any time). */
(() => {
  "use strict";
  const panel = document.getElementById("activity");
  if (!panel) return;
  const topline = document.getElementById("activity-topline");
  const toplineFill = topline && topline.querySelector("span");
  const summaryText = panel.querySelector(".activity-summary-text");
  const summaryFacts = panel.querySelector(".activity-summary-facts");
  const rows = {
    scan: panel.querySelector('[data-task="scan"]'),
    covers: panel.querySelector('[data-task="covers"]'),
    device: panel.querySelector('[data-task="device"]'),
  };
  const cancelBtn = document.getElementById("scan-cancel-btn");
  const retryBtn = document.getElementById("cover-retry");
  const rescanBtns = document.querySelectorAll(".rescan-trigger");
  const number = new Intl.NumberFormat();
  const pageLoadedAt = Date.now();

  let timer = null;
  let lastFinished;          // the scan finished_at already accounted for
  let scanResultUntil = 0;   // keep showing a just-finished scan's result until then
  let coverImageErrors = 0;

  // -- small helpers ----------------------------------------------------------

  function duration(seconds) {
    if (seconds == null) return "";
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return `${s} s`;
    return `${Math.floor(s / 60)} min ${s % 60} s`;
  }

  function ago(iso) {
    const ms = Date.now() - Date.parse(iso);
    if (!(ms >= 0)) return "";
    const minutes = Math.round(ms / 60000);
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes} min ago`;
    return `${Math.round(minutes / 60)} h ago`;
  }

  function tail(path) {
    return (path || "").split(/[\\/]/).filter(Boolean).slice(-2).join(" / ");
  }

  function plural(n, one, many) {
    return `${number.format(n)} ${n === 1 ? one : many}`;
  }

  function show(row, { state, title, count = "", detail = "", progress = null }) {
    if (row.hidden) {
      row.hidden = false;
      row.classList.remove("is-new");
      void row.offsetWidth;  // restart the entrance animation
      row.classList.add("is-new");
    }
    row.dataset.state = state;
    row.querySelector(".activity-title").textContent = title;
    row.querySelector(".activity-count").textContent = count;
    row.querySelector(".activity-detail").textContent = detail;
    // No line of its own -- the strip stays one line -- but never lost:
    // hovering the chip says which file, how long, or what went wrong.
    row.title = [title, count, detail].filter(Boolean).join(" · ");
    const bar = row.querySelector(".activity-bar");
    const fill = row.querySelector(".activity-bar-fill");
    const indeterminate = progress == null;
    bar.classList.toggle("is-indeterminate", indeterminate);
    fill.style.width = indeterminate ? "" : `${(Math.min(1, Math.max(0, progress)) * 100).toFixed(1)}%`;
    bar.setAttribute("role", "progressbar");
    if (indeterminate) bar.removeAttribute("aria-valuenow");
    else bar.setAttribute("aria-valuenow", String(Math.round(progress * 100)));
  }

  function hide(row) { row.hidden = true; }

  // -- the three kinds of work --------------------------------------------------

  function renderScan(scan) {
    rescanBtns.forEach((btn) => { btn.disabled = scan.running; });
    if (scan.running) {
      scanResultUntil = 0;
      const stopping = scan.cancel_requested;
      const where = tail(scan.current_filename);
      const took = duration(scan.elapsed_seconds);
      if (scan.phase === "indexing" && scan.total) {
        show(rows.scan, {
          state: "running", title: stopping ? "Stopping…" : "Scanning",
          count: `${number.format(scan.done)} / ${number.format(scan.total)}`,
          detail: [where, took].filter(Boolean).join(" · "), progress: scan.done / scan.total,
        });
      } else if (scan.phase === "finishing") {
        show(rows.scan, { state: "running", title: "Scanning", count: "almost done",
                          detail: took, progress: 1 });
      } else {
        show(rows.scan, {
          state: "running", title: stopping ? "Stopping…" : "Looking through folders",
          count: scan.done ? plural(scan.done, "entry", "entries") : "",
          detail: [where, took].filter(Boolean).join(" · "),
        });
      }
      cancelBtn.hidden = false;
      cancelBtn.disabled = stopping;
      cancelBtn.textContent = "Stop";
      return true;
    }

    cancelBtn.hidden = true;
    if (scan.finished_at && scan.finished_at !== lastFinished) {
      // A pass has finished since the last look -- or just before this page
      // loaded, which is worth a moment on screen too.
      const recent = lastFinished !== undefined || Date.parse(scan.finished_at) > pageLoadedAt - 20000;
      lastFinished = scan.finished_at;
      if (recent) scanResultUntil = Date.now() + 6000;
    }
    if (scan.status === "failed" && scan.error) {
      show(rows.scan, { state: "error", title: "Scan failed", detail: scan.error, progress: 1 });
    } else if (Date.now() < scanResultUntil) {
      if (scan.cancelled) {
        show(rows.scan, { state: "info", title: "Scan stopped",
                          detail: "What it had already indexed was kept.", progress: 1 });
      } else {
        const s = scan.summary || {};
        const parts = [`${s.new || 0} new`, `${s.updated || 0} updated`, `${s.removed || 0} removed`];
        if (s.relocated) parts.push(`${s.relocated} matched to a new path`);
        show(rows.scan, { state: "done", title: "Scanned", count: parts.join(" · "),
                          detail: duration(scan.elapsed_seconds), progress: 1 });
      }
    } else {
      hide(rows.scan);
    }
    return false;
  }

  function renderCovers(covers) {
    if (!covers.enabled) { hide(rows.covers); retryBtn.hidden = true; return false; }
    if (covers.running) {
      retryBtn.hidden = true;
      const downloading = covers.phase === "Downloading covers" && covers.total;
      show(rows.covers, {
        state: "running", title: "Covers",
        count: covers.total ? `${number.format(covers.ready)} / ${number.format(covers.total)}` : "",
        detail: downloading ? "" : covers.phase, progress: downloading ? covers.ready / covers.total : null,
      });
      return true;
    }
    if (covers.error || coverImageErrors) {
      retryBtn.hidden = false;
      show(rows.covers, {
        state: "error", title: "Covers",
        count: covers.total ? `${number.format(covers.ready)} / ${number.format(covers.total)}` : "",
        detail: covers.error || "Some cover images could not be displayed.",
        progress: covers.total ? covers.ready / covers.total : 1,
      });
    } else {
      retryBtn.hidden = true;
      hide(rows.covers);
    }
    return false;
  }

  function renderDevices(devices) {
    const busy = devices.find((d) => d.phase);
    if (!busy) { hide(rows.device); return false; }
    const reading = busy.phase === "Reading installed games";
    show(rows.device, {
      state: "running",
      title: busy.label,
      count: `${reading ? "reading games" : "checking"} · ${duration(busy.busy_seconds)}`,
      detail: reading ? "“On Switch” badges appear once this is done." : "",
    });
    return true;
  }

  function renderSummary(active, data) {
    panel.dataset.state = active ? "busy" : "idle";
    const scan = data.scan;
    const navLibrary = document.querySelector('.main-nav a[href="/"]');
    if (navLibrary) navLibrary.classList.toggle("is-scanning", !!active);
    if (scan.running) {
      // Say plainly that the list below may be incomplete, not just "Loading".
      const checked = scan.phase === "indexing" && scan.total
        ? ` ${number.format(scan.done)} of ${number.format(scan.total)} files checked.` : "";
      summaryText.textContent = `Scanning your library…${checked} Games may still be missing.`;
    } else if (active) {
      summaryText.textContent = "Loading…";
    } else if (Date.now() < scanResultUntil) {
      summaryText.textContent = scan.cancelled
        ? "Scan stopped — the list may be incomplete"
        : "Scan finished — your library is up to date";
    } else {
      summaryText.textContent = "Everything is loaded";
    }
    // A library that is empty only because the scan has not got there yet
    // must not read as "you have no games".
    document.querySelectorAll(".empty-state[data-scan-aware]").forEach((el) => {
      if (!el.dataset.idleText) el.dataset.idleText = el.textContent.trim();
      el.textContent = scan.running
        ? "Scanning your library… your games will appear here as soon as the scan finishes."
        : el.dataset.idleText;
    });

    const facts = [];
    const cards = document.querySelectorAll(".library-tiles > .game-group, .library-tiles > .card").length;
    facts.push(plural(cards, "game", "games"));
    const c = data.covers;
    if (!c.enabled) facts.push("covers off");
    else if (c.total) {
      facts.push(`${number.format(c.ready)}/${number.format(c.total)} covers`
        + (!c.running && c.not_found ? ` (${c.not_found} not in TitleDB)` : ""));
    }
    if (!data.devices.length) facts.push("no Switch connected");
    for (const d of data.devices) {
      if (d.installed_count != null) {
        facts.push(`${d.label}: ${plural(d.installed_count, "title", "titles")} on the console`
          + (d.installed_read_at ? `, read ${ago(d.installed_read_at)}` : ""));
      } else if (!d.phase) {
        facts.push(`${d.label} connected`);
      }
    }
    summaryFacts.textContent = facts.join(" · ");
    summaryFacts.title = summaryFacts.textContent;  // cut short on a narrow screen

    if (!topline) return;
    topline.hidden = !active;
    const determinate = scan.running && scan.phase === "indexing" && scan.total;
    topline.classList.toggle("is-indeterminate", !determinate);
    toplineFill.style.width = determinate ? `${(scan.done / scan.total * 100).toFixed(1)}%` : "";
  }

  // -- polling ----------------------------------------------------------------

  async function poll() {
    clearTimeout(timer);
    let active = false;
    let unreachable = false;
    try {
      const response = await fetch("/api/activity", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const scanning = renderScan(data.scan);
      const covering = renderCovers(data.covers);
      const reading = renderDevices(data.devices);
      active = scanning || covering || reading;
      renderSummary(active, data);
    } catch (error) {
      panel.dataset.state = "error";
      summaryText.textContent = "Could not check background activity";
      summaryFacts.textContent = error.message;
      unreachable = true;
    }
    const soon = active || Date.now() < scanResultUntil;
    // An app that is not answering (stopped, restarting) is asked again
    // gently rather than every second.
    timer = setTimeout(poll, document.hidden || unreachable ? 10000 : soon ? 1000 : 4000);
  }

  cancelBtn.addEventListener("click", async () => {
    cancelBtn.disabled = true;
    cancelBtn.textContent = "Stopping…";
    // Cooperative: the scan unwinds at its next checkpoint; the next poll
    // is what confirms it has stopped.
    try { await fetch("/api/scan/cancel", { method: "POST" }); } finally { poll(); }
  });
  document.addEventListener("activity-poke", poll);
  document.addEventListener("library-updated", poll);
  document.addEventListener("cover-image-error", () => { coverImageErrors += 1; poll(); });
  document.addEventListener("cover-retry-started", () => { coverImageErrors = 0; });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  poll();
})();
