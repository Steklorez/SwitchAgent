// Settings page: "Copy diagnostics" button. The full safe report text is
// server-rendered into a hidden <textarea> (see settings.html) so we never
// have to worry about JSON/attribute escaping of a large multi-line blob --
// this script only ever reads it back out.
(function () {
  "use strict";

  const btn = document.getElementById("copy-diagnostics-btn");
  const status = document.getElementById("copy-diagnostics-status");
  const textArea = document.getElementById("diagnostics-text");
  if (!btn || !textArea) return;

  async function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (e) {
        // fall through to the legacy fallback below
      }
    }
    // Graceful fallback for browsers/contexts without Clipboard API access
    // (e.g. no secure context): select the hidden textarea and use the
    // older execCommand copy path.
    const original = textArea.value;
    textArea.value = text;
    try {
      textArea.hidden = false;
      textArea.focus();
      textArea.select();
      const ok = document.execCommand("copy");
      textArea.hidden = true;
      return ok;
    } catch (e) {
      textArea.hidden = true;
      return false;
    } finally {
      textArea.value = original;
    }
  }

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    status.textContent = "";
    try {
      const ok = await copyText(textArea.value);
      status.textContent = ok ? "Copied to clipboard." : "Could not copy automatically -- select the text manually.";
    } finally {
      btn.disabled = false;
    }
  });

  // "Copy logs": fetched on click, not rendered into the page -- the log
  // is far bigger than the diagnostics text and changes by the second.
  const logsBtn = document.getElementById("copy-logs-btn");
  if (logsBtn) {
    logsBtn.addEventListener("click", async () => {
      logsBtn.disabled = true;
      status.textContent = "";
      try {
        const res = await fetch("/api/logs");
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const text = await res.text();
        if (!text) {
          status.textContent = "The log is empty.";
          return;
        }
        const ok = await copyText(text);
        status.textContent = ok ? "Logs copied to clipboard." : "Could not copy automatically.";
      } catch (e) {
        status.textContent = "Could not read the log: " + (e.message || e);
      } finally {
        logsBtn.disabled = false;
      }
    });
  }

  // -- UI-004: work directory cleanup (preview-then-confirm, never silent) --

  const cleanupBtn = document.getElementById("cleanup-btn");
  const cleanupSummary = document.getElementById("cleanup-summary");
  const cleanupStatus = document.getElementById("cleanup-status");

  function formatBytes(n) {
    if (n == null) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let size = n, i = 0;
    while (size >= 1024 && i < units.length - 1) { size /= 1024; i += 1; }
    return `${size.toFixed(1)} ${units[i]}`;
  }

  if (cleanupBtn) {
    cleanupBtn.addEventListener("click", async () => {
      cleanupBtn.disabled = true;
      cleanupStatus.textContent = "";
      try {
        // Always re-check right before confirming -- the server-rendered
        // numbers on page load may be stale by the time the user clicks.
        const previewRes = await fetch("/api/work-cleanup/preview");
        const preview = await previewRes.json();
        if (preview.eligible_job_count === 0) {
          cleanupSummary.textContent = "Nothing eligible for cleanup right now.";
          return; // stays disabled -- nothing to do, matches the summary
        }
        const confirmed = confirm(
          `${preview.eligible_job_count} completed job(s), ` +
          `${formatBytes(preview.eligible_size_bytes)} will be removed. Continue?`
        );
        if (!confirmed) {
          cleanupBtn.disabled = false;
          return;
        }

        const res = await fetch("/api/work-cleanup", { method: "POST" });
        if (!res.ok) {
          cleanupStatus.textContent = "Cleanup failed.";
          cleanupBtn.disabled = false;
          return;
        }
        const result = await res.json();
        cleanupStatus.textContent =
          `Cleaned ${result.cleaned_job_count} job(s), freed ${formatBytes(result.freed_bytes)}.`;
        cleanupSummary.textContent = "Nothing eligible for cleanup right now.";
        // stays disabled -- nothing left to clean, matches the summary
      } catch (e) {
        cleanupStatus.textContent = "Request failed: " + e;
        cleanupBtn.disabled = false;
      }
    });
  }

  // -- W3-002: Library folders -- a list you add to and remove from
  // directly (each add is still validated server-side before it's
  // persisted; a remove is just dropping an already-known-good entry, so
  // it saves immediately -- no separate Validate/Save step to remember). -

  const libraryList = document.getElementById("library-dir-list");
  const libraryEmpty = document.getElementById("library-dir-empty");
  const libraryFeedback = document.getElementById("library-dir-feedback");
  const libraryPickBtn = document.getElementById("library-dir-pick-btn");
  const libraryManualInput = document.getElementById("library-dir-manual-input");
  const libraryManualAddBtn = document.getElementById("library-dir-manual-add-btn");

  if (libraryList) {
    let currentDirs = Array.from(libraryList.querySelectorAll(".library-dir-row")).map(li => li.dataset.path);

    function renderLibraryDirs(dirs) {
      currentDirs = dirs;
      libraryList.innerHTML = "";
      for (const dir of dirs) {
        const li = document.createElement("li");
        li.className = "library-dir-row";
        li.dataset.path = dir;
        const span = document.createElement("span");
        span.className = "library-dir-path";
        span.textContent = dir;
        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "library-dir-remove-btn";
        removeBtn.title = "Remove";
        removeBtn.setAttribute("aria-label", `Remove ${dir}`);
        removeBtn.textContent = "×";
        removeBtn.addEventListener("click", () => removeLibraryDir(dir));
        li.append(span, removeBtn);
        libraryList.appendChild(li);
      }
      if (libraryEmpty) libraryEmpty.hidden = dirs.length > 0;
    }

    // Returns null on success, or a plain-string error message on failure
    // -- never throws, so callers can decide what to do next (retry, give
    // up) instead of only being able to report the failure.
    async function saveLibraryDirs(dirs, busyMessage) {
      // An empty list is a real request -- "stop looking at anything" --
      // and posting it is the whole point: the server clears the config and
      // the rescan it triggers retires the now-orphaned index. This used to
      // be refused here, which left no way to stop SwitchAgent watching a
      // folder without first finding another folder to offer it.
      libraryFeedback.textContent = busyMessage;
      try {
        const res = await fetch("/api/settings/library-dir", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: dirs[0] || "", paths: dirs }),
        });
        const body = await res.json();
        if (!res.ok) return body.detail || "Could not save.";
        renderLibraryDirs(body.library_dirs);
        libraryFeedback.textContent = dirs.length
          ? "Saved -- rescanning the library now."
          : "All folders removed. SwitchAgent is no longer watching anything; your files are untouched.";
        return null;
      } catch (e) {
        return String(e.message || e);
      }
    }

    async function removeLibraryDir(path) {
      const next = currentDirs.filter((d) => d !== path);
      const error = await saveLibraryDirs(next, "Removing…");
      if (error) libraryFeedback.textContent = error;
    }

    async function addLibraryDir(path) {
      if (!path) return;
      if (currentDirs.includes(path)) {
        libraryFeedback.textContent = "That folder is already in the list.";
        return;
      }
      libraryFeedback.textContent = "Checking…";
      try {
        const res = await fetch("/api/settings/library-dir/validate", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path }),
        });
        const body = await res.json();
        if (!res.ok || !body.valid) throw new Error(body.reason || body.detail || "Not a usable folder.");
      } catch (e) {
        libraryFeedback.textContent = String(e.message || e);
        return;
      }

      const baseline = currentDirs;
      let error = await saveLibraryDirs([...baseline, path], "Adding…");
      if (error) {
        // The list is validated as a whole (services.set_library_dir) --
        // an ALREADY-listed folder that's since gone stale (drive
        // unplugged, renamed) blocks adding this new, perfectly good one
        // too, and the user would otherwise be stuck: they can't remove
        // the stale entry either while it's the only one (the server
        // refuses an empty list). If the error names one of the folders
        // already in the list (not the one just typed/picked), drop it
        // and retry once rather than leaving the user to hunt it down.
        const stale = baseline.find((d) => error.includes(d));
        if (stale) {
          const retryError = await saveLibraryDirs(
            [...baseline.filter((d) => d !== stale), path],
            "Removing no-longer-valid folder and adding the new one…",
          );
          if (retryError) {
            libraryFeedback.textContent = retryError;
          } else {
            libraryFeedback.textContent = `Removed "${stale}" (no longer valid) and added "${path}".`;
          }
          return;
        }
        libraryFeedback.textContent = error;
      }
    }

    for (const btn of libraryList.querySelectorAll(".library-dir-remove-btn")) {
      btn.addEventListener("click", () => removeLibraryDir(btn.dataset.path));
    }

    if (libraryManualAddBtn) {
      const submitManualPath = async () => {
        const path = libraryManualInput.value.trim();
        if (!path) { libraryFeedback.textContent = "Enter a path first."; return; }
        libraryManualAddBtn.disabled = true;
        await addLibraryDir(path);
        libraryManualAddBtn.disabled = false;
        libraryManualInput.value = "";
      };
      libraryManualAddBtn.addEventListener("click", submitManualPath);
      libraryManualInput.addEventListener("keydown", (evt) => {
        if (evt.key === "Enter") { evt.preventDefault(); submitManualPath(); }
      });
    }

    if (libraryPickBtn) {
      libraryPickBtn.disabled = !["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
      libraryPickBtn.addEventListener("click", async () => {
        libraryPickBtn.disabled = true;
        libraryFeedback.textContent = "Choose a folder in the Windows dialog that just opened…";
        try {
          const res = await fetch("/api/settings/library-dir/pick", { method: "POST" });
          if (!(res.headers.get("content-type") || "").includes("application/json")) {
            throw new Error(`Folder picker returned an unexpected response (HTTP ${res.status}). Restart SwitchAgent after updating; details are in the application log.`);
          }
          const body = await res.json();
          if (!res.ok) throw new Error(body.detail || "Could not open the folder picker");
          if (body.path) {
            await addLibraryDir(body.path);
          } else {
            libraryFeedback.textContent = "Selection cancelled.";
          }
        } catch (e) {
          libraryFeedback.textContent = String(e.message || e);
        } finally {
          libraryPickBtn.disabled = false;
        }
      });
    }
  }

  // -- W3-008: update availability notification (never an auto-updater --
  // fetched asynchronously so a slow/offline check can never delay this
  // page's own initial render) ---------------------------------------

  const updateStatus = document.getElementById("update-check-status");
  const updateLink = document.getElementById("update-check-link");
  const updateNowBtn = document.getElementById("update-check-now-btn");

  function renderUpdateCheck(body) {
    if (body.error && !body.latest_version) {
      updateStatus.textContent = "Could not check for updates.";
      updateLink.hidden = true;
      return;
    }
    if (body.update_available) {
      updateStatus.textContent = `Version ${body.latest_version} available.`;
      updateLink.href = body.release_url;
      updateLink.hidden = false;
    } else {
      updateStatus.textContent = "You are on the latest version.";
      updateLink.hidden = true;
    }
  }

  async function loadUpdateCheck(force) {
    try {
      const res = await fetch("/api/update-check", { method: force ? "POST" : "GET" });
      const body = await res.json();
      renderUpdateCheck(body);
    } catch (e) {
      updateStatus.textContent = "Could not check for updates.";
    }
  }

  if (updateStatus) {
    loadUpdateCheck(false);
    updateNowBtn.addEventListener("click", async () => {
      updateNowBtn.disabled = true;
      updateStatus.textContent = "Checking…";
      try {
        await loadUpdateCheck(true);
      } finally {
        updateNowBtn.disabled = false;
      }
    });
  }

  // -- UI-006: "Restart worker when safe" --------------------------------

  const restartBtn = document.getElementById("restart-worker-btn");
  const restartStatus = document.getElementById("restart-status");

  if (restartBtn) {
    restartBtn.addEventListener("click", async () => {
      restartBtn.disabled = true;
      try {
        const res = await fetch("/api/worker/restart", { method: "POST" });
        if (!res.ok) {
          restartStatus.textContent = "Could not request a restart.";
          return;
        }
        restartStatus.textContent = "Restart queued — will apply at the worker's next safe point.";
      } catch (e) {
        restartStatus.textContent = "Request failed: " + e;
      } finally {
        restartBtn.disabled = false;
      }
    });
  }

  // -- Conflict policy: what to do when a file already exists on the Switch --

  const conflictPolicySelect = document.getElementById("conflict-policy-select");
  const conflictPolicyStatus = document.getElementById("conflict-policy-status");

  if (conflictPolicySelect) {
    conflictPolicySelect.addEventListener("change", async () => {
      const policy = conflictPolicySelect.value;
      conflictPolicySelect.disabled = true;
      conflictPolicyStatus.textContent = "";
      try {
        const res = await fetch("/api/settings/conflict-policy", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ policy }),
        });
        if (!res.ok) {
          conflictPolicyStatus.textContent = "Could not save.";
          return;
        }
        conflictPolicyStatus.textContent = "Saved.";
        setTimeout(() => { conflictPolicyStatus.textContent = ""; }, 2000);
      } catch (e) {
        conflictPolicyStatus.textContent = "Request failed: " + e;
      } finally {
        conflictPolicySelect.disabled = false;
      }
    });
  }

  // -- Start with Windows (autostart.py; the registry is the setting) ------
  const autostartSection = document.getElementById("autostart");
  const autostartToggle = document.getElementById("autostart-toggle");
  const autostartHint = document.getElementById("autostart-hint");

  function renderAutostart(s) {
    if (!s.available) return;  // a dev run has no exe to register
    autostartSection.hidden = false;
    autostartToggle.checked = s.enabled;
    autostartHint.textContent = s.other_copy
      ? `Windows currently starts another copy: ${s.other_copy}. Turning this on starts this one instead.`
      : "";
  }

  if (autostartSection) {
    fetch("/api/autostart").then((r) => r.json()).then(renderAutostart).catch(() => {});
    autostartToggle.addEventListener("change", async () => {
      autostartToggle.disabled = true;
      try {
        const res = await fetch("/api/autostart", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: autostartToggle.checked }),
        });
        const body = await res.json();
        if (!res.ok) {
          autostartToggle.checked = !autostartToggle.checked;
          autostartHint.textContent = "Could not change it: " + (body.detail || res.status);
          return;
        }
        renderAutostart(body);
      } catch (e) {
        autostartToggle.checked = !autostartToggle.checked;
        autostartHint.textContent = "Request failed: " + e;
      } finally {
        autostartToggle.disabled = false;
      }
    });
  }
})();
