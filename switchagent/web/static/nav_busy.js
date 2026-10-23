/* A small spinner on a sidebar tab while that page's data is still being
   worked on in the background -- a library scan, the console being read, an
   install on its way, an add-on download -- so a tab that looks empty or
   says "not read yet" is not mistaken for a finished one.
   One light read, GET /api/busy (memory only), every 2 s while anything is
   busy and every 5 s otherwise; nothing while the tab is hidden. */
(() => {
  "use strict";
  const links = [...document.querySelectorAll(".main-nav a[data-busy]")];
  if (!links.length) return;
  const spins = {};
  for (const a of links) {
    const el = document.createElement("span");
    el.className = "nav-spin";
    el.setAttribute("role", "status");
    el.setAttribute("aria-label", "working");
    el.hidden = true;
    a.appendChild(el);
    spins[a.dataset.busy] = el;
  }
  let timer = null;

  async function tick() {
    let busy = {};
    try {
      const r = await fetch("/api/busy", { cache: "no-store" });
      if (r.ok) busy = await r.json();
    } catch (e) { /* the app is restarting or gone: show nothing */ }
    let any = false;
    for (const key of Object.keys(spins)) {
      const on = !!busy[key];
      spins[key].hidden = !on;
      any = any || on;
    }
    schedule(any ? 2000 : 5000);
  }

  function schedule(ms) {
    clearTimeout(timer);
    if (document.hidden) return;
    timer = setTimeout(tick, ms);
  }

  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
  tick();
})();
