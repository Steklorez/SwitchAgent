(function () {
  const btn = document.getElementById("about-check-btn");
  if (!btn) return;
  const status = document.getElementById("about-update-status");
  const link = document.getElementById("about-update-link");

  // Same endpoint Settings' "Check now" uses: one plain GET to GitHub's
  // Releases API, nothing about the user in it.
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    link.hidden = true;
    status.textContent = "Checking…";
    try {
      const res = await fetch("/api/update-check", { method: "POST" });
      const body = await res.json();
      if (body.error && !body.latest_version) {
        status.textContent = "Could not check for updates. Are you online?";
      } else if (body.update_available) {
        status.textContent = `Version ${body.latest_version} is available.`;
        link.href = body.release_url;
        link.hidden = false;
      } else {
        status.textContent = "You are on the latest version.";
      }
    } catch (e) {
      status.textContent = "Could not check for updates.";
    } finally {
      btn.disabled = false;
    }
  });
})();
