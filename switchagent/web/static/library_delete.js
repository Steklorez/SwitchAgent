/* Deleting Library files from the PC. Works on whatever is ticked on the
   page (the same checkboxes Install uses): "Delete…" in the selection bar asks
   the server what each ticked item is on disk (POST /api/library/delete/plan)
   and shows exactly that -- the real file, its size, and whether the Recycle
   Bin can take it -- before anything is touched. The Recycle Bin is the
   default; permanent deletion is a separate choice with its own tick. */
(() => {
  "use strict";
  const btn = document.getElementById("delete-selected-btn");
  const modal = document.getElementById("delete-modal");
  if (!btn || !modal) return;
  const $ = (id) => document.getElementById(id);
  const list = $("delete-list"), count = $("delete-count"), total = $("delete-total");
  const note = $("delete-note"), sure = $("delete-sure"), sureCheck = $("delete-sure-check");
  const result = $("delete-result"), confirmBtn = $("delete-confirm-btn"), cancelBtn = $("delete-cancel-btn");
  const modeBox = $("delete-mode"), lead = $("delete-lead");
  let plan = [];
  let done = false;

  const size = (n) => {
    if (!n) return "0 B";
    const u = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
    return (n / Math.pow(1024, i)).toFixed(i ? 1 : 0) + " " + u[i];
  };
  const mode = () => modeBox.querySelector("input:checked").value;
  const post = (url, body) => fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

  function names() {
    const out = new Map();
    document.querySelectorAll(".select-box:checked").forEach((b) => out.set(parseInt(b.value, 10), b.dataset.name || ""));
    return out;
  }

  function render() {
    const radios = modeBox.querySelectorAll("input");
    const deletable = plan.filter((e) => !e.blocked);
    const noBin = deletable.filter((e) => e.exists && !e.recoverable);
    const m = mode();
    const acting = m === "recycle" ? deletable.filter((e) => !e.exists || e.recoverable) : deletable;
    const bytes = acting.reduce((a, e) => a + e.size, 0);
    count.textContent = plan.length === 1 ? "1 item" : plan.length + " items";
    list.querySelectorAll("li").forEach((li) => {
      const e = plan.find((x) => String(x.id) === li.dataset.id);
      const skipped = m === "recycle" && !e.blocked && e.exists && !e.recoverable;
      li.querySelector(".d-tag").textContent =
        e.blocked ? "Not deleted: " + e.blocked :
        !e.exists ? "Already gone from disk — only the entry is removed" :
        skipped ? "Skipped: no Recycle Bin here" :
        m === "recycle" ? "→ Recycle Bin" :
        e.recoverable ? "Deleted permanently" : "Deleted permanently (no Recycle Bin here)";
    });
    note.hidden = !(noBin.length && m === "recycle");
    if (!note.hidden) {
      note.textContent = noBin.length + (noBin.length === 1 ? " file is" : " files are") +
        " on a network share or removable drive, where Windows has no Recycle Bin — " +
        "they are skipped here. Choose “Delete permanently” to remove them.";
    }
    sure.hidden = m !== "permanent";
    const verb = m === "recycle" ? "Move to Recycle Bin" : "Delete permanently";
    confirmBtn.textContent = acting.length ? `${verb} · ${acting.length}` : verb;
    total.textContent = acting.length ? `${acting.length} to remove · ${size(bytes)}` : "Nothing to remove";
    confirmBtn.disabled = done || !acting.length || (m === "permanent" && !sureCheck.checked);
    radios.forEach((r) => { r.disabled = done; });
  }

  btn.addEventListener("click", async () => {
    const chosen = names();
    if (!chosen.size) return;
    done = false;
    result.hidden = true; result.textContent = "";
    sureCheck.checked = false;
    modeBox.querySelector('input[value="recycle"]').checked = true;
    lead.hidden = false;
    confirmBtn.disabled = true; confirmBtn.textContent = "Checking…";
    list.textContent = "";
    modal.showModal();
    try {
      const r = await post("/api/library/delete/plan", { library_item_ids: [...chosen.keys()] });
      plan = (await r.json()).items || [];
    } catch (e) {
      result.hidden = false; result.textContent = "Could not check these files: " + e;
      return;
    }
    for (const e of plan) {
      const li = document.createElement("li");
      li.dataset.id = String(e.id);
      li.classList.toggle("is-blocked", !!e.blocked);
      li.classList.toggle("is-nobin", !e.blocked && e.exists && !e.recoverable);
      const mk = (cls, text) => { const s = document.createElement("span"); s.className = cls; s.textContent = text; return s; };
      const path = mk("d-path", e.path);
      if (e.also && e.also.length) path.textContent += `  (+ ${e.also.length} more entr${e.also.length === 1 ? "y" : "ies"} in it)`;
      li.append(mk("d-name", chosen.get(e.id) || e.file), mk("d-size", e.exists ? size(e.size) : ""), path, mk("d-tag", ""));
      list.appendChild(li);
    }
    render();
  });

  modeBox.addEventListener("change", render);
  sureCheck.addEventListener("change", render);
  cancelBtn.addEventListener("click", () => {
    modal.close();
    if (done) window.location.reload();
  });
  modal.addEventListener("cancel", () => { if (done) window.location.reload(); });

  confirmBtn.addEventListener("click", async () => {
    confirmBtn.disabled = true;
    confirmBtn.textContent = "Deleting…";
    const m = mode();
    let data;
    try {
      const r = await post("/api/library/delete", { library_item_ids: plan.map((e) => e.id), mode: m });
      data = await r.json();
      if (!r.ok) throw new Error(data.detail || r.status);
    } catch (e) {
      result.hidden = false; result.textContent = "Nothing was deleted: " + e;
      render();
      return;
    }
    done = true;
    const out = [`${data.deleted.length} ${m === "recycle" ? "moved to the Recycle Bin" : "deleted"} · ${size(data.bytes)} freed.`];
    for (const f of data.failed || []) {
      const e = plan.find((x) => x.id === f.id);
      out.push(`Not deleted — ${e ? e.file : f.id}: ${f.error}`);
    }
    result.hidden = false;
    result.textContent = out.join("\n");
    result.style.whiteSpace = "pre-line";
    lead.hidden = true;
    confirmBtn.hidden = true;
    cancelBtn.textContent = "Close";
    list.querySelectorAll("li").forEach((li) => {
      if (data.deleted.includes(parseInt(li.dataset.id, 10))) li.style.textDecoration = "line-through";
    });
  });

  modal.addEventListener("close", () => { confirmBtn.hidden = false; cancelBtn.textContent = "Cancel"; });
})();
