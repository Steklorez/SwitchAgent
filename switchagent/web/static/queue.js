// Queue page: retry/cancel buttons + worker pause/resume + live polling.
//
// Previously this only reloaded the whole page every 6s, and only if an
// "active-looking" status was present in the page AT LOAD TIME. For a
// short job (a small file can go CONFIRMED -> RUNNING -> DONE_UNVERIFIED
// in well under 15s total -- confirmed by a real install,
// docs/REAL-HARDWARE-TEST-*.md) that meant the page was a stale snapshot
// for up to 6s at a stretch, and the terminal state was never shown at
// all -- DONE_UNVERIFIED/DONE jobs are deliberately excluded from
// /api/queue (that's History's job), so a fast job could go from
// "just appeared" to "gone" between two reloads with nothing in between.
//
// Fix: genuine periodic polling of /api/queue (in-place DOM patch, no
// full reload) plus a toast when a job that WAS in the active list
// disappears -- looked up via /api/jobs/{id} so the toast shows its real
// terminal outcome, not a guess.
(function () {
  "use strict";

  // What a row installs, as a badge after its name. The same four role
  // names and the same colour per kind as the install-confirmation dialog
  // (library.js's CONFIRM_ROLE_LABELS, .confirm-tag-* in style.css), so an
  // item looks the same in the dialog that queued it and in the Queue that
  // runs it. No role -> no badge: the backend returns null whenever it
  // cannot actually tell, and a guessed kind would be worse than none.
  const ROLE_LABELS = { base: "Game", update: "Update", dlc: "DLC", mod: "Mod", sd: "SD files",
                        amiibo: "Amiibo", emuiibo: "emuiibo", addon: "Add-on" };

  function buildRoleTag(role) {
    const label = ROLE_LABELS[role];
    if (!label) return null;
    const tag = document.createElement("span");
    tag.className = "job-tag job-tag-" + role;
    tag.textContent = "[" + label + "]";
    return tag;
  }

  // Writes a row's name and (optionally) its badge, replacing whatever was
  // there -- the live poll re-renders rows in place, so this has to be
  // idempotent rather than appending.
  function setRowName(holder, name, role) {
    holder.replaceChildren();
    const text = document.createElement("span");
    text.className = "job-name-text";
    text.textContent = name;
    holder.appendChild(text);
    const tag = buildRoleTag(role);
    if (tag) holder.appendChild(tag);
  }

  // Every row, a job's or a preparation item's, ends in a segmented bar
  // (the same .seg-bar the queue dock uses) -- its blocks run while bytes
  // are moving (.is-running), or all along it while there is no byte count
  // yet (.is-sweeping).
  function addBar(row, barMode) {
    const bar = document.createElement("div");
    bar.className = "seg-bar job-bar";
    if (barMode) bar.classList.add(barMode);
    const fill = document.createElement("span");
    fill.className = "seg-bar-fill";
    // Rows are rebuilt on every poll; starting the run's animation this far
    // into its cycle keeps it where the old row's was, instead of jumping
    // back to the start every two seconds.
    fill.style.setProperty("--seg-phase", `-${Math.round(performance.now() % 1300)}ms`);
    bar.appendChild(fill);
    row.appendChild(bar);
  }

  // One card per game: its cover once, then one row per package it
  // installs. Grouped by the game's cover id (its base TITLE_ID), in the
  // order each game first appears; a row with none is a card of its own.
  // Same rule as services.group_jobs_by_game(), which queue.html uses for
  // the first paint. `entries` are {coverId, name, row, jobId?, itemId?,
  // removable, busy}.
  //
  // Under the cover: "Remove from queue", for the whole game -- every part
  // not yet sending, whether it is still waiting, being unpacked or next in
  // line. Not offered while a part is being sent: stopping that is Abort's.
  function appendGameGroups(container, entries, asList) {
    const groups = new Map();
    entries.forEach((entry, index) => {
      const key = entry.coverId || `row-${index}`;
      if (!groups.has(key)) {
        groups.set(key, { coverId: entry.coverId, name: entry.name, rows: [], jobIds: [], itemIds: [], removable: false, busy: false });
      }
      const group = groups.get(key);
      group.rows.push(entry.row);
      if (entry.jobId != null) group.jobIds.push(entry.jobId);
      if (entry.itemId != null) group.itemIds.push(entry.itemId);
      group.removable = group.removable || entry.removable;
      group.busy = group.busy || entry.busy;
    });
    for (const group of groups.values()) {
      const card = document.createElement(asList ? "li" : "div");
      card.className = "game-group";
      const side = document.createElement("div");
      side.className = "game-group-side";
      const thumb = window.SwitchAgentGameThumb
        ? window.SwitchAgentGameThumb(group.coverId, group.name) : document.createElement("span");
      thumb.classList.add("game-group-thumb");
      side.appendChild(thumb);
      if (group.removable && !group.busy) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "btn btn-small game-remove-btn";
        remove.textContent = "Remove";
        remove.title = "Remove this game from the queue";
        remove.dataset.removeJobs = group.jobIds.join(",");
        remove.dataset.removeItems = group.itemIds.join(",");
        remove.dataset.removeName = group.name;
        side.appendChild(remove);
      }
      const rows = document.createElement(asList ? "ol" : "div");
      rows.className = "game-group-jobs";
      rows.append(...group.rows);
      card.append(side, rows);
      container.appendChild(card);
    }
  }

  // Can this job still be taken out of the queue (it has not started
  // sending)? Kept in sync with services._CANCELABLE_JOB_STATUSES.
  const REMOVABLE_JOB_STATUSES = [
    "PENDING_CONFIRM", "CONFIRMED", "DEVICE_UNAVAILABLE", "WAITING_FOR_BASE", "WAITING_FOR_DEVICE",
    "FAILED", "INTERRUPTED", "SOURCE_CHANGED", "BLOCKED_BY_DEPENDENCY",
  ];
  const REMOVABLE_PHASES = ["Waiting", "Analyzing", "Extracting", "Preparing", "Prepared", "Installing"];
  function jobEntry(j, extra) {
    return Object.assign({
      coverId: j.cover_id, name: j.display_name, jobId: j.id,
      removable: !j.abandoned && REMOVABLE_JOB_STATUSES.includes(j.status),
      busy: ["RUNNING", "VERIFYING"].includes(j.status),
    }, extra || {});
  }

  // Paused: nothing new starts. Known from the page at load, then from
  // GET /api/worker/status on every tick (pollWorkerStatus below).
  let paused = document.getElementById("job-list")?.dataset.paused === "1";
  function renderPaused() {
    const banner = document.getElementById("paused-banner");
    if (banner) banner.hidden = !paused;
  }

  const jobList = document.getElementById("job-list");
  const emptyState = document.getElementById("empty-state");
  const toastContainer = document.getElementById("toast-container");
  if (!jobList) return; // not on the Queue page

  const POLL_INTERVAL_MS = 2000;
  // Keep in sync with queue.html's retry-button condition and
  // services._RETRYABLE_JOB_STATUSES (retry creates a brand-new job --
  // see services.retry_job docstring -- so this list is "terminal states
  // a user can reasonably re-attempt from", not "resumable states").
  const RETRYABLE_STATUSES = [
    "FAILED", "INTERRUPTED", "DEVICE_UNAVAILABLE",
    "SOURCE_CHANGED", "BLOCKED_BY_DEPENDENCY",
  ];
  let knownActiveIds = new Set(
    Array.from(jobList.querySelectorAll(".job-row")).map((el) => el.dataset.jobId)
  );

  // In-theme replacement for window.confirm() -- the browser's own native
  // dialog looks like an unrelated system/security prompt (unstyled white
  // box, generic OS chrome) sitting on top of this app's dark UI, jarring
  // enough to read as suspicious rather than as part of the app. Resolves
  // true/false exactly like a confirm() call would, so the two call sites
  // below only needed `await` added in front of them.
  const actionConfirmModal = document.getElementById("confirm-action-modal");
  const actionConfirmMessage = document.getElementById("confirm-action-message");
  const actionConfirmOkBtn = document.getElementById("confirm-action-ok-btn");
  const actionConfirmCancelBtn = document.getElementById("confirm-action-cancel-btn");
  const actionConfirmTitle = document.getElementById("confirm-action-title");
  // `labels` ({title, ok, cancel}) words one particular question; whatever
  // it leaves out keeps the dialog's own defaults, which are put back after
  // every answer so the next question never inherits this one's buttons.
  function confirmAction(message, labels) {
    if (!actionConfirmModal) return Promise.resolve(window.confirm(message)); // pages without the dialog markup
    const defaults = {
      title: actionConfirmTitle ? actionConfirmTitle.textContent : "",
      ok: actionConfirmOkBtn.textContent, cancel: actionConfirmCancelBtn.textContent,
    };
    const wording = Object.assign({}, defaults, labels || {});
    return new Promise((resolve) => {
      actionConfirmMessage.textContent = message;
      if (actionConfirmTitle) actionConfirmTitle.textContent = wording.title;
      actionConfirmOkBtn.textContent = wording.ok;
      actionConfirmCancelBtn.textContent = wording.cancel;
      const finish = (result) => {
        if (actionConfirmTitle) actionConfirmTitle.textContent = defaults.title;
        actionConfirmOkBtn.textContent = defaults.ok;
        actionConfirmCancelBtn.textContent = defaults.cancel;
        actionConfirmOkBtn.removeEventListener("click", onOk);
        actionConfirmCancelBtn.removeEventListener("click", onCancel);
        actionConfirmModal.removeEventListener("cancel", onCancel);
        actionConfirmModal.close();
        resolve(result);
      };
      const onOk = () => finish(true);
      const onCancel = () => finish(false);
      actionConfirmOkBtn.addEventListener("click", onOk);
      actionConfirmCancelBtn.addEventListener("click", onCancel);
      actionConfirmModal.addEventListener("cancel", onCancel); // Esc key
      actionConfirmModal.showModal();
    });
  }

  function statusClass(status) {
    return "status-" + status.toLowerCase();
  }

  // RUNNING only ever means "actively sending file bytes over MTP" (see
  // queue_worker._run_job_transfer) -- it never means DBI has started
  // installing, and this app has no MTP-visible way to observe that
  // separately (see docs/STAGE5B-REAL-MTP.md). SD_INSTALL is DBI's own
  // install destination though, so bytes arriving there really is
  // "Installing" from the user's point of view; SD_CARD (mods) is a
  // plain file copy, never run through DBI's installer at all -- so the
  // two get distinct, honest labels instead of one opaque "RUNNING".
  function statusLabel(j) {
    if (j.status === "RUNNING") {
      return j.target_storage === "SD_INSTALL" ? "Installing" : "Copying to device";
    }
    if (j.status === "CONFIRMED" && paused) return "Paused";
    return j.status_label || j.status.replace(/_/g, " ");
  }

  // A preparation item that has no job yet, in the same words as a job.
  const PHASE_LABELS = {
    Waiting: "Queued", Prepared: "Up next", Analyzing: "Checking", Extracting: "Unpacking",
    Preparing: "Preparing", Installing: "Starting", Cleaning: "Cleaning up", Ready: "Done", Failed: "Failed",
  };

  function renderGroup(g) {
    const li = document.createElement("li");
    li.className = "batch-group";
    li.dataset.batchId = g.batch_id === null || g.batch_id === undefined ? "" : g.batch_id;

    let headerHtml = "";
    if (g.total > 1) {
      headerHtml = `
        <div class="batch-header">
          <span class="batch-title"></span>
          <span class="batch-progress"></span>
        </div>`;
    }
    li.innerHTML = `${headerHtml}<ol class="batch-jobs"></ol>`;
    if (g.total > 1) {
      li.querySelector(".batch-title").textContent = g.display_name;
      li.querySelector(".batch-progress").textContent = `${g.finished} / ${g.total} finished`;
    }
    const jobsList = li.querySelector(".batch-jobs");
    appendGameGroups(jobsList, g.jobs.map((j) => jobEntry(j, { row: renderJobRow(j) })), true);
    return li;
  }

  function renderJobRow(j) {
    const li = document.createElement("li");
    li.className = "job-row";
    li.dataset.status = j.status;
    const done = ["DONE", "DONE_UNVERIFIED"].includes(j.status);
    const fraction = j.bytes_total > 0 ? Math.min(100, 100 * j.bytes_done / j.bytes_total) : 0;
    li.style.setProperty("--progress", `${done ? 100 : fraction}%`);
    li.classList.toggle("progress-active", j.status === "RUNNING");
    li.dataset.jobId = j.id;

    const spinner = j.status === "RUNNING" ? '<span class="spinner" aria-label="in progress"></span>' : "";
    // A plain spinner with no numbers reads as "frozen" -- which is exactly
    // how a game install used to look for its entire duration. SD_INSTALL
    // was deliberately excluded here while the Shell copy engine was the
    // only transport: it could not report anything mid-copy, and the bytes
    // recorded afterwards could legitimately read 0 even on a physically
    // confirmed success. The WPD transport (switchagent/mtp/wpd.py) counts
    // the bytes it writes itself and reports them about once a second, so
    // these numbers are now real for every storage. On the Shell fallback
    // the bar simply steps per completed file, as it always did for mods.
    const progressTextHtml = (j.status === "RUNNING" && j.bytes_total > 0)
      ? `<div class="job-progress-text"></div>` : "";
    const waitingForDevice = j.status === "WAITING_FOR_DEVICE";
    // A finished job can still carry a sentence -- an amiibo collection
    // saying how many were already on the console. That is a note, not a
    // failure, and is not painted as one.
    const errorClass = j.status === "DONE" ? "history-note" : "job-error";
    const errorHtml = (j.error && !waitingForDevice) ? `<div class="${errorClass}"></div>` : "";
    const stallHtml = j.possibly_stalled ? `<div class="job-stall-warning"></div>` : "";
    // A stopped row keeps Retry. Taking something out of the queue is the
    // game card's "Remove" (appendGameGroups): a per-row Cancel only ever
    // showed on the one package that was next in line -- never on the
    // games still waiting behind it -- and read as if it stopped the queue.
    const retryBtn = !j.abandoned && RETRYABLE_STATUSES.includes(j.status)
      ? `<button class="btn btn-small" data-job-action="retry" data-job-id="${j.id}">Retry installation</button>` : "";
    const cancelBtn = "";

    li.innerHTML = `
      <div class="job-main">
        <div class="job-name"><span class="job-name-text"></span></div>
        <div class="job-meta"></div>
        ${progressTextHtml}
        ${errorHtml}
        ${stallHtml}
      </div>
      <div class="job-status">
        <span class="status-pill ${statusClass(j.status)}${j.status === "CONFIRMED" && paused ? " status-paused" : ""}"></span>
        ${spinner}
      </div>
      <div class="job-actions">${retryBtn}${cancelBtn}</div>
    `;
    setRowName(li.querySelector(".job-name"), j.display_name, j.variant_role);
    li.querySelector(".job-meta").textContent = waitingForDevice
      ? ((j.auto_resume && j.error) || `Waiting for ${j.target_device_label} to reconnect — resumes automatically`)
      : `${j.target_device_label} · ${j.target_storage} · attempt ${j.attempt_count} · created ${j.created_at}`;
    li.querySelector(".status-pill").textContent = statusLabel(j);
    if (progressTextHtml) {
      li.querySelector(".job-progress-text").textContent =
        `${formatBytes(j.bytes_done)} / ${formatBytes(j.bytes_total)} (${Math.round(fraction)}%)`;
    }
    if (j.possibly_stalled) {
      const minutes = Math.floor(j.stall_seconds / 60);
      li.querySelector(".job-stall-warning").textContent =
        `Possibly stalled — no observable progress for ${minutes} min. ` +
        "Large MTP transfers may legitimately take a long time; this is not treated as a failure.";
    }
    if (j.error && !waitingForDevice) {
      const errorEl = li.querySelector("." + errorClass);
      errorEl.textContent = j.error;
      errorEl.title = j.error; // UX-001: long/technical errors are CSS-clamped -- full text via hover tooltip
    }
    addBar(li, j.status === "RUNNING" ? "is-running" : null);
    return li;
  }

  function showToast(text, kind) {
    const el = document.createElement("div");
    el.className = "toast toast-" + (kind || "info");
    el.textContent = text;
    toastContainer.appendChild(el);
    setTimeout(() => el.remove(), 7000);
  }

  async function handleDisappearedJob(jobId) {
    try {
      const res = await fetch(`/api/jobs/${jobId}`);
      if (!res.ok) return;
      const job = await res.json();
      // Removed or Aborted: the user just did that, it is not news -- and
      // a cancelled job is stored as FAILED, which would read as an error.
      if (job.abandoned) return;
      if (job.status === "DONE") {
        showToast(`✓ ${job.display_name} — Installed`, "ok");
      } else if (job.status === "DONE_UNVERIFIED") {
        showToast(`${job.display_name} — Installed (unverified, see History)`, "warn");
      } else if (["FAILED", "INTERRUPTED", "SOURCE_CHANGED", "DESTINATION_CONFLICT", "BLOCKED_BY_DEPENDENCY"].includes(job.status)) {
        showToast(`${job.display_name} — ${job.status_label || job.status.replace(/_/g, " ")}`, "error");
      }
      // Any other status (still active) means it just wasn't on this
      // particular poll's page/order -- say nothing, avoid a false toast.
    } catch (e) {
      // Network hiccup -- skip silently, the next poll will retry.
    }
  }

  async function poll() {
    let groups;
    try {
      const res = await fetch("/api/queue/grouped");
      if (!res.ok) return;
      groups = await res.json();
    } catch (e) {
      return; // transient network error -- try again next tick
    }

    const jobs = groups.flatMap((g) => g.jobs);
    const currentIds = new Set(jobs.map((j) => String(j.id)));
    for (const id of knownActiveIds) {
      if (!currentIds.has(id)) handleDisappearedJob(id);
    }
    knownActiveIds = currentIds;

    const represented = await pollPreparation();
    jobList.innerHTML = "";
    for (const g of groups) {
      const remaining = g.jobs.filter(j => !represented.has(String(j.id)));
      if (remaining.length) jobList.appendChild(renderGroup({...g, jobs: remaining}));
    }
    emptyState.hidden = groups.length > 0 || document.getElementById('preparation-list').children.length > 0;
  }

  async function pollPreparation() {
    const panel = document.getElementById("preparation-list");
    const represented = new Set();
    try {
      const response = await fetch("/api/preparations");
      if (!response.ok) return represented;
      const states = await response.json();
      panel.replaceChildren();
      for (const state of states) {
        // Prepared jobs now live in the main list; never duplicate them.
        const box = document.createElement("div");
        box.className = "batch-group";
        const items = Object.entries(state.items)
          .map(([id, item]) => Object.assign({ id: Number(id) }, item))
          .sort((a, b) => a.order - b.order);
        const ready = items.filter((item) => item.phase === "Ready").length;
        const heading = document.createElement("h3");
        heading.textContent = (state.phase === "Preparing" ? "Sequential installation" : state.phase) +
          ` · ${ready} / ${items.length} finished · ${state.elapsed}s`;
        box.appendChild(heading);
        const entries = [];
        for (const item of items) {
          if (item.jobs && item.jobs.length) {
            for (const job of item.jobs) {
              represented.add(String(job.id));
              const row = renderJobRow(job);
              setRowName(row.querySelector('.job-name'),
                         item.jobs.length === 1 ? item.name : job.display_name,
                         job.variant_role || item.role);
              if (item.phase === 'Cleaning') row.querySelector('.status-pill').textContent = 'Cleaning temporary files';
              entries.push(jobEntry(job, { coverId: job.cover_id || item.cover_id, name: item.name, row, itemId: item.id }));
            }
            continue;
          }
          const line = document.createElement("div");
          line.className = "job-row preparation-row";
          // Once the whole sequential run has already stopped (state.phase
          // "Failed"), an item still sitting at "Waiting" never got its own
          // job created at all and never will -- this run is over, not
          // paused. Relabelled so that reads as "abandoned", not "about to
          // start any second" (see preparation.py's own stop condition --
          // the run genuinely never resumes on its own past this point).
          const neverStarted = state.phase === "Failed" && item.phase === "Waiting";
          const waiting = ["Waiting", "Prepared"].includes(item.phase);
          const displayPhase = neverStarted ? "Not started"
            : (paused && waiting ? "Paused" : (PHASE_LABELS[item.phase] || item.phase));
          line.dataset.status = neverStarted ? "Failed" : item.phase;
          const active = ["Extracting", "Analyzing", "Installing", "Cleaning"].includes(item.phase);
          line.classList.toggle("progress-active", active);
          line.style.setProperty("--progress", item.phase === "Ready" ? "100%" : "0%");
          // Built out of elements rather than one textContent write, so the
          // badge can sit between the name and the phase -- same position
          // as on a real job row above.
          const main = document.createElement("div");
          main.className = "job-main";
          const name = document.createElement("div");
          name.className = "job-name";
          setRowName(name, item.name, item.role);
          main.appendChild(name);
          if (item.error || neverStarted) {
            const error = document.createElement("div");
            error.className = "job-error";
            error.textContent = item.error || "The installation stopped before this one's turn.";
            error.title = error.textContent;
            main.appendChild(error);
          }
          // The phase as a pill, where a job row has its status.
          const status = document.createElement("div");
          status.className = "job-status";
          const pill = document.createElement("span");
          pill.className = "status-pill" + (paused && waiting ? " status-paused" : "");
          pill.textContent = displayPhase;
          status.appendChild(pill);
          line.append(main, status);
          addBar(line, active ? "is-sweeping" : null);
          entries.push({
            coverId: item.cover_id, name: item.name, row: line, itemId: item.id,
            removable: !neverStarted && REMOVABLE_PHASES.includes(item.phase), busy: false,
          });
        }
        appendGameGroups(box, entries, false);
        // state.error is a snapshot of why the sequential run stopped,
        // frozen at the moment it happened -- it does NOT know that the
        // item which stopped it may since have been resolved (Retry
        // creates a brand-new job the panel now follows forward to, see
        // _resolve_latest_retry). Without this check, retrying a failure
        // and watching its new job actively copy still showed
        // "Installation stopped" right next to a live, moving progress
        // bar -- flatly wrong, nothing is stopped. Only keep showing it
        // while there's a job still stuck in one of the same statuses
        // that originally halted the run, and it wasn't itself just
        // abandoned -- i.e. something actually still needs a
        // click. An item that never got a job created at all ("Not
        // started (installation stopped)") does NOT keep this banner
        // alive on its own: that item's own row already says exactly
        // that, permanently (see `neverStarted` above) -- repeating the
        // identical sentence in a banner below it forever, long after
        // the one thing a user COULD act on has already been resolved,
        // is just noise.
        const stillBlocked = items.some((item) =>
          item.jobs && item.jobs.some((job) =>
            [...RETRYABLE_STATUSES, "WAITING_FOR_BASE"].includes(job.status) && !job.abandoned));
        const errors = state.result ? state.result.errors.map((error) => error.error) : [];
        if (state.error && stillBlocked) errors.push(state.error);
        if (errors.length) {
          const error = document.createElement("p");
          error.className = "job-error";
          error.textContent = "Installation stopped: " + errors.join("; ");
          box.appendChild(error);
        }
        panel.appendChild(box);
      }
      if (states.some((state) => ["Waiting", "Preparing"].includes(state.phase))) emptyState.hidden = true;
    } catch (_) { /* the next poll retries a transient connection failure */ }
    return represented;
  }
  // -- Abort (header controls) --------------------------------------------
  //
  // Shown only while there is something to abort. Once pressed, the row
  // that is still transferring says "Stopping" until the worker has really
  // stopped it (GET /api/worker/status): over WPD that is about a second,
  // but a file the console is still finalising -- or any file on the Shell
  // fallback, which cannot be cut off mid-copy -- finishes first, and the
  // page says so instead of pretending it already stopped.
  const abortBtn = document.getElementById("abort-btn");
  const abortStatus = document.getElementById("abort-status");
  const SLOW_STOP_SECONDS = 5;
  let stopping = false;

  async function pollWorkerStatus() {
    if (!abortBtn) return;
    let status;
    try {
      const res = await fetch("/api/worker/status");
      if (!res.ok) return;
      status = await res.json();
    } catch (_) { return; } // the next tick retries
    stopping = status.stopping;
    if (status.worker_paused !== paused) {
      paused = status.worker_paused;
      renderPaused();
    }
    abortBtn.hidden = !status.abortable;
    abortBtn.disabled = status.stopping;
    abortStatus.hidden = !status.stopping;
    if (status.stopping) {
      abortStatus.textContent = status.stopping_seconds >= SLOW_STOP_SECONDS
        ? "Stopping… the file being sent can't be cut off, so it finishes first"
        : "Stopping…";
    }
    markStoppingRows();
  }

  // poll() redraws every row from scratch, so this runs after each redraw.
  function markStoppingRows() {
    if (!stopping) return;
    for (const pill of document.querySelectorAll('.job-row[data-status="RUNNING"] .status-pill')) {
      pill.textContent = "Stopping…";
    }
  }

  if (abortBtn) {
    abortBtn.addEventListener("click", async () => {
      const ok = await confirmAction(
        "Stop the install in progress and cancel everything waiting in the queue? " +
        "The file being sent right now is left unfinished — Retry on its row sends it again. " +
        "Items that already failed keep their Retry.",
        { title: "Abort installation?", ok: "Abort", cancel: "Keep installing" },
      );
      if (!ok) return;
      abortBtn.disabled = true;
      try {
        const res = await fetch("/api/queue/abort", { method: "POST" });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          alert(`Could not abort: ${data.detail || res.status}`);
          abortBtn.disabled = false;
          return;
        }
        const result = await res.json();
        const cancelled = result.cancelled_job_ids.length;
        showToast(
          (result.stopping_job_ids.length ? "Stopping the install in progress. " : "") +
          (cancelled ? `Cancelled ${cancelled} waiting item${cancelled === 1 ? "" : "s"}.` : "Nothing else was waiting."),
          "info",
        );
      } catch (e) {
        alert("Request failed: " + e);
        abortBtn.disabled = false;
        return;
      }
      await pollWorkerStatus();
      await poll();
      markStoppingRows();
    });
  }

  async function tick() {
    try {
      await pollWorkerStatus();
      await poll();
      markStoppingRows();
    } finally { setTimeout(tick, POLL_INTERVAL_MS); }
  }
  renderPaused();
  tick();

  // -- actions (event delegation -- works for rows replaced by poll()) ----

  document.addEventListener("click", async (evt) => {
    const removeBtn = evt.target.closest(".game-remove-btn");
    if (removeBtn) {
      const ids = (value) => (value || "").split(",").filter(Boolean).map(Number);
      // The game, not one of its files: extension and [tags] dropped, the
      // way Library names it.
      const game = (removeBtn.dataset.removeName || "This game")
        .replace(/\.(nsp|nsz|xci|xcz|zip|7z|rar)$/i, "").replace(/\s*\[[^\]]*\]/g, "").trim();
      const ok = await confirmAction(
        `${game} will not be installed. Parts of it already on the Switch stay there.`,
        { title: "Remove from queue?", ok: "Remove", cancel: "Keep it" },
      );
      if (!ok) return;
      removeBtn.disabled = true;
      try {
        const res = await fetch("/api/queue/remove", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ job_ids: ids(removeBtn.dataset.removeJobs), library_item_ids: ids(removeBtn.dataset.removeItems) }),
        });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          showToast(data.detail || `Could not remove (${res.status})`, "error");
          removeBtn.disabled = false;
          return;
        }
        poll();
      } catch (e) {
        showToast("Request failed: " + e, "error");
        removeBtn.disabled = false;
      }
      return;
    }

    const btn = evt.target.closest("[data-job-action]");
    if (btn) {
      const action = btn.dataset.jobAction;
      const jobId = btn.dataset.jobId;
      if (action === "cancel" && !(await confirmAction("Abandon this attempt and release its temporary files when no other job needs them?"))) return;
      btn.disabled = true;
      try {
        const res = await fetch(`/api/jobs/${jobId}/${action}`, { method: "POST" });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          alert(`Could not ${action}: ${data.detail || res.status}`);
          btn.disabled = false;
          return;
        }
        poll();
      } catch (e) {
        alert("Request failed: " + e);
        btn.disabled = false;
      }
      return;
    }

    const workerBtn = evt.target.closest("[data-worker-action]");
    if (workerBtn) {
      workerBtn.disabled = true;
      await fetch("/api/worker/" + workerBtn.dataset.workerAction, { method: "POST" });
      window.location.reload(); // worker state text lives outside job-list -- a full reload here is fine, it's user-triggered, not a live-progress path
    }
  });

  // Global conflict policy -- same setting as Settings' own select (both
  // read/write config.conflict_policy through the one endpoint), just
  // reachable without leaving the page you're watching an install on.
  const conflictToggle = document.getElementById("conflict-policy-toggle");
  const conflictLabel = document.getElementById("conflict-policy-label");
  if (conflictToggle) {
    conflictToggle.addEventListener("change", async () => {
      const policy = conflictToggle.checked ? "override" : "skip";
      conflictToggle.disabled = true;
      try {
        const res = await fetch("/api/settings/conflict-policy", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ policy }),
        });
        if (!res.ok) {
          conflictToggle.checked = !conflictToggle.checked; // revert
          alert("Could not save: " + res.status);
          return;
        }
        conflictLabel.textContent = policy === "override" ? "Replace" : "Skip";
      } catch (e) {
        conflictToggle.checked = !conflictToggle.checked;
        alert("Request failed: " + e);
      } finally {
        conflictToggle.disabled = false;
      }
    });
  }
})();
