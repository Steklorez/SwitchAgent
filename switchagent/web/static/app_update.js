// The update strip in the top bar: says a newer SwitchAgent is out and, on
// click, downloads and installs it (switchagent/app_update.py picks the
// installer or the portable zip by how this copy runs). A dev run cannot
// install anything, so there the strip just opens the release page.
(function () {
  const strip = document.getElementById("app-update");
  if (!strip) return;
  let pollTimer = null;

  function percent(s) {
    return s.total > 0 ? ` ${Math.floor(100 * s.done / s.total)}%` : "";
  }

  function render(s) {
    strip.classList.remove("is-failed", "is-progress");
    strip.disabled = false;
    strip.title = "";
    switch (s.phase) {
      case "downloading":
        strip.textContent = `Downloading SwitchAgent ${s.version || s.latest_version || ""}…${percent(s)}`;
        // The button itself fills up as the download goes.
        strip.classList.add("is-progress");
        strip.style.setProperty("--progress", s.total > 0 ? `${100 * s.done / s.total}%` : "0%");
        strip.disabled = true;
        break;
      case "waiting":
        strip.textContent = `SwitchAgent ${s.version} is ready — installs once the current transfer finishes`;
        strip.disabled = true;
        break;
      case "installing":
        strip.textContent = `Installing SwitchAgent ${s.version} — it restarts in a moment…`;
        strip.disabled = true;
        break;
      case "failed":
        strip.textContent = "Update failed — try again";
        strip.title = s.error || "";
        strip.classList.add("is-failed");
        break;
      default:
        if (!s.update_available) { strip.hidden = true; return; }
        strip.textContent = `SwitchAgent ${s.latest_version} is available — Update`;
        strip.title = s.can_install
          ? `Downloads the ${s.mode === "portable" ? "portable build" : "installer"} and installs it; SwitchAgent restarts.`
          : "Opens the release page.";
    }
    strip.hidden = false;
  }

  async function load() {
    try {
      const res = await fetch("/api/app-update");
      if (!res.ok) return null;
      const s = await res.json();
      render(s);
      return s;
    } catch (e) {
      return null;
    }
  }

  // Once the installer runs, this server goes away; when it answers again
  // it is the new version -- reload into it.
  function waitForRestart(oldVersion) {
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      try {
        const res = await fetch("/api/app-update", { cache: "no-store" });
        if (!res.ok) return;
        const s = await res.json();
        if (s.current_version !== oldVersion) location.reload();
      } catch (e) { /* still restarting */ }
    }, 3000);
  }

  function follow(oldVersion) {
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      const s = await load();
      if (!s) { waitForRestart(oldVersion); return; }
      if (s.phase === "installing") waitForRestart(oldVersion);
      else if (!["downloading", "waiting"].includes(s.phase)) clearInterval(pollTimer);
    }, 1000);
  }

  strip.addEventListener("click", async () => {
    const s = await load();
    if (!s) return;
    if (!s.can_install) {
      if (s.release_url) window.open(s.release_url, "_blank", "noopener");
      return;
    }
    strip.disabled = true;
    try {
      const res = await fetch("/api/app-update", { method: "POST" });
      const body = await res.json();
      if (!res.ok) {
        render({ phase: "failed", error: body.detail || "" });
        return;
      }
      render(Object.assign({}, s, body));
      follow(s.current_version);
    } catch (e) {
      render({ phase: "failed", error: String(e) });
    }
  });

  load().then((s) => {
    if (s && ["downloading", "waiting", "installing"].includes(s.phase)) follow(s.current_version);
  });
})();
