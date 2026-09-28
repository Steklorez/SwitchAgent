// Library page: multi-select + sticky confirmation bar + install
// confirmation modal + game detail modal + non-blocking rescan.
//
// Hard rule this file exists to enforce client-side (mirrored server-side
// in switchagent/web/services.py -- this is UX, not the real guard):
// selecting a card NEVER creates a job. A job is only ever created after
// the user explicitly presses "Confirm Install" in the modal below.
(function () {
  "use strict";

  // Inline trash-bin icon used on every confirm-list remove button (group-
  // level and child-level) -- replaces the old plain "✕" glyph. No user data
  // ever goes into this constant, so it's safe to assign via innerHTML;
  // item NAMES must still only ever go through .textContent/.title below.
  const TRASH_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' +
    '<path d="M3 6h18"/>' +
    '<path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/>' +
    '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>' +
    '<path d="M10 11v6M14 11v6"/></svg>';

  const selected = new Map(); // id -> {name, size, destination, role, familyName, family, coverId}

  const selectionBar = document.getElementById("selection-bar");
  const selectionCount = document.getElementById("selection-count");
  const selectionSize = document.getElementById("selection-size");
  // Reassigned by refreshSelectionTarget() below whenever the connected-
  // device set changes -- the <select> this points at only exists in the
  // DOM for the "exactly one device connected" branch, so it comes and
  // goes as devices connect/disconnect, never a stable element to keep a
  // single reference to.
  let targetSelect = document.getElementById("target-device");
  const installBtn = document.getElementById("install-selected-btn");

  function updateSelectionBar() {
    const n = selected.size;
    selectionCount.textContent = String(n);
    let totalSize = 0;
    let newSpaceSize = 0;
    selected.forEach((v) => {
      totalSize += v.size;
      // An item with an existing copy at its destination (confirmed on
      // device, or SwitchAgent's own record shows a completed transfer)
      // -- installs never overwrite an existing file (see the confirm
      // modal's own "existing files are never replaced" notice), so
      // (re)selecting it is a no-op transfer-wise and shouldn't count
      // toward "space this selection is about to add", even though it
      // still counts toward the plain total shown just below.
      if (!v.existingCopy) newSpaceSize += v.size;
    });
    selectionSize.textContent = formatBytes(totalSize);
    selectionBar.hidden = n === 0;
    updateCardSizes();
    // Lets app.js's header SD card space bar show the estimated space the
    // current selection would take up, without this file needing to know
    // anything about that bar's markup or rendering.
    document.dispatchEvent(new CustomEvent("storage:selection-changed", { detail: { bytes: newSpaceSize } }));
  }
  // A game card's size: the whole game while none of its parts is ticked,
  // the sum of the ticked ones once some are -- so the card agrees with the
  // selection bar and the storage estimate above it.
  function updateCardSizes() {
    document.querySelectorAll(".card-size[data-card-family]").forEach((label) => {
      const boxes = document.querySelectorAll(`.select-box[data-family="${CSS.escape(label.dataset.cardFamily)}"]`);
      let ticked = 0;
      let sum = 0;
      boxes.forEach((box) => {
        if (!box.checked) return;
        ticked += 1;
        sum += parseInt(box.dataset.size, 10) || 0;
      });
      label.textContent = formatBytes(ticked ? sum : parseInt(label.dataset.total, 10) || 0);
    });
  }

  updateSelectionBar();

  // Auto-select the target Switch when exactly one is currently connected
  // (point: never guess when 2+ are connected, never invent one when 0
  // are). Runs once, at page load -- if the user later explicitly picks a
  // (or a different) device, nothing here ever overrides that choice
  // again; and if their chosen device disconnects, the target is never
  // silently switched to another one (see the disabled `option` for a
  // disconnected device above -- the browser just leaves the select on
  // its already-chosen value, which becomes a proper validation error at
  // confirm time instead of a silent redirect).
  if (targetSelect && targetSelect.dataset.connectedCount === "1") {
    const onlyOption = Array.from(targetSelect.options).find((o) => o.value && !o.disabled);
    if (onlyOption) targetSelect.value = onlyOption.value;
  }

  // UI-008: the topbar device-pill (app.js) polls /api/devices live, but
  // until now nothing else on this page did -- plugging the Switch in
  // AFTER the page had already loaded left this whole area stuck on "No
  // Switch connected" with Install permanently disabled, no matter what
  // the pill said, until a manual reload. This mirrors the three branches
  // _install_selection.html renders server-side, rebuilding only the
  // #selection-target-area + Install button's disabled state -- never
  // touches the user's current game selection.
  const selectionTargetArea = document.getElementById("selection-target-area");
  let lastConnectedSignature = null;

  function buildTargetSelect(devices, connectedCount) {
    const select = document.createElement("select");
    select.id = "target-device";
    select.dataset.connectedCount = String(connectedCount);
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Select Switch…";
    select.appendChild(placeholder);
    for (const d of devices) {
      const opt = document.createElement("option");
      opt.value = d.device_fingerprint;
      opt.disabled = !d.connected;
      opt.textContent = d.display_name + (d.connected ? "" : " (disconnected)");
      select.appendChild(opt);
    }
    return select;
  }

  async function refreshSelectionTarget() {
    if (!selectionTargetArea) return; // not on a page that includes this partial
    let devices;
    try {
      const res = await fetch("/api/devices");
      if (!res.ok) return;
      devices = await res.json();
    } catch (e) {
      return; // network hiccup or server briefly restarting -- try again next tick
    }
    const connected = devices.filter((d) => d.connected);
    // Only touch the DOM when the connected SET actually changed -- never
    // rebuild (and so never reset the user's already-chosen target) just
    // because a poll tick happened to land.
    const signature = connected.map((d) => d.device_fingerprint).sort().join(",");
    if (signature === lastConnectedSignature) return;
    lastConnectedSignature = signature;

    selectionTargetArea.innerHTML = "";
    if (connected.length === 0) {
      const warn = document.createElement("div");
      warn.className = "selection-target no-device-warning";
      warn.textContent = "No Switch connected";
      selectionTargetArea.appendChild(warn);
    } else if (connected.length > 1) {
      const warn = document.createElement("div");
      warn.className = "selection-target no-device-warning";
      warn.textContent = "Multiple Switches connected — disconnect all but one to install";
      selectionTargetArea.appendChild(warn);
    } else {
      const wrap = document.createElement("div");
      wrap.className = "selection-target";
      const label = document.createElement("label");
      label.setAttribute("for", "target-device");
      label.textContent = "Target:";
      const select = buildTargetSelect(devices, connected.length);
      wrap.appendChild(label);
      wrap.appendChild(select);
      selectionTargetArea.appendChild(wrap);
      targetSelect = select;
      // Same auto-select as the page-load case above: exactly one
      // connected device picks itself, never guessed with 0 or 2+.
      const onlyOption = Array.from(select.options).find((o) => o.value && !o.disabled);
      if (onlyOption) select.value = onlyOption.value;
    }
    if (connected.length !== 1) targetSelect = null;
    installBtn.disabled = connected.length !== 1;
  }

  refreshSelectionTarget();
  setInterval(refreshSelectionTarget, 5000);

  function bindSelection() {
  document.querySelectorAll(".select-box:not([data-selection-bound])").forEach((box) => {
    box.dataset.selectionBound = 'true';
    box.addEventListener("change", () => {
      const id = box.value;
      if (box.checked) {
        selected.set(id, {
          name: box.dataset.name,
          size: parseInt(box.dataset.size, 10) || 0,
          destination: box.dataset.destination || "SD Card install",
          role: box.dataset.role || null,
          familyName: box.dataset.familyName || null,
          family: box.dataset.family || null,
          coverId: box.dataset.coverId || box.dataset.family || "",
          existingCopy: box.dataset.existingCopy === "1",
        });
      } else {
        selected.delete(id);
      }
      updateSelectionBar();
    });
  });

  // Selecting a game's Base Game checkbox auto-selects every related
  // Update/DLC/Mod (same TITLE_ID family, see data-family) -- the default
  // "select a game" action is "select the whole installable set". The user
  // can still uncheck individual siblings afterward; unchecking a sibling
  // never affects the base or its other siblings. Unchecking the base
  // itself mirrors the selection back off, so nothing is left selected
  // "invisibly" once the game card itself looks unselected. Never reaches
  // into a DIFFERENT TITLE_ID family (data-family scopes the query).
  document.querySelectorAll('.select-box[data-role="base"]:not([data-family-bound])').forEach((baseBox) => {
    baseBox.dataset.familyBound = 'true';
    baseBox.addEventListener("change", () => {
      const family = baseBox.dataset.family;
      if (!family) return;
      document.querySelectorAll(`.select-box[data-family="${CSS.escape(family)}"]`).forEach((box) => {
        if (box === baseBox || box.disabled || box.checked === baseBox.checked) return;
        // Came with the game, not needed by it (Events Unlock beside
        // Animal Crossing): ticked by hand only -- but unticked with it.
        if (baseBox.checked && box.dataset.optional === "1") return;
        box.checked = baseBox.checked;
        box.dispatchEvent(new Event("change"));
      });
    });
  });

  }
  bindSelection();
  document.addEventListener('library-updated', bindSelection);
  // -- game detail modal --------------------------------------------------

  const detailModal = document.getElementById("detail-modal");
  const detailBody = document.getElementById("detail-modal-body");

  function bindDetails() {
  document.querySelectorAll(".card-body[data-detail-url]:not([data-detail-bound])").forEach((el) => {
    el.dataset.detailBound = 'true';
    el.addEventListener("click", async () => {
      const url = el.dataset.detailUrl;
      detailBody.innerHTML = "<p>Loading…</p>";
      detailModal.showModal();
      try {
        const res = await fetch(url);
        const item = await res.json();
        detailBody.innerHTML = renderDetail(item);
      } catch (e) {
        detailBody.innerHTML = "<p>Could not load details.</p>";
      }
    });
  });

  }
  bindDetails();
  document.addEventListener('library-updated', bindDetails);

  function renderDetail(item) {
    const rows = [
      ["Name", item.name],
      ["TITLE_ID", item.title_id || "unknown"],
      ["Format", item.package_format || item.content_type || item.file_type],
      ["Size", formatBytes(item.size)],
      ["SHA-256", item.content_hash || "—"],
      ["Source path", item.absolute_path],
      ["Content type", item.content_type || "—"],
      ["Status", item.status.replace(/_/g, " ")],
    ];
    if (item.job) {
      rows.push(["Last job", "#" + item.job.id + " — " + item.job.status.replace(/_/g, " ")]);
      rows.push(["Last target", item.job.target_device_label]);
      if (item.job.error) rows.push(["Last error", item.job.error]);
    }
    let html = "<h3>" + escapeHtml(item.name) + "</h3><table class='detail-table'>";
    for (const [label, value] of rows) {
      html += "<tr><th>" + escapeHtml(label) + "</th><td>" + escapeHtml(String(value)) + "</td></tr>";
    }
    html += "</table>";
    return html;
  }

  function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
  }

  // -- install confirmation modal -----------------------------------------

  const confirmModal = document.getElementById("confirm-modal");
  const confirmCount = document.getElementById("confirm-count");
  const confirmTarget = document.getElementById("confirm-target");
  const confirmDestination = document.getElementById("confirm-destination");
  const confirmSize = document.getElementById("confirm-size");
  const confirmList = document.getElementById("confirm-list");
  const confirmResult = document.getElementById("confirm-result");
  const confirmCancelBtn = document.getElementById("confirm-cancel-btn");
  const confirmInstallBtn = document.getElementById("confirm-install-btn");

  // role -> tag label shown on child/flat rows in the confirm list. "base"
  // and null are intentionally absent -- those rows never get a tag.
  const CONFIRM_ROLE_LABELS = { update: "Update", dlc: "DLC", mod: "Mod", sd: "SD files", amiibo: "Amiibo",
                                duplicate: "Other copy" };

  // Cover-art loader for the confirm-list thumbnails. covers.js's own loader
  // can't be reused here -- it hard-depends on #cover-status-text existing on
  // the page (the confirm modal has no such element) and it snapshots
  // [data-cover-id] once at page load rather than picking up rows added to
  // the DOM afterward. Same endpoint + uppercase-id convention covers.js uses
  // elsewhere on this page.
  function attachConfirmCover(img) {
    const id = (img.dataset.coverId || "").toUpperCase();
    if (!id) return;
    img.onload = () => { img.hidden = false; };
    img.onerror = () => { img.hidden = true; };
    img.src = `/api/covers/${encodeURIComponent(id)}`;
  }

  // Builds the <span class="confirm-thumb"> shared by group headers and
  // standalone flat rows (nested .confirm-child rows never get one -- only
  // top-level rows show cover art). Falls back to the first-letter
  // placeholder alone when there's no coverId to try.
  function buildConfirmThumb(name, coverId) {
    const thumb = document.createElement("span");
    thumb.className = "confirm-thumb";

    const placeholder = document.createElement("span");
    placeholder.className = "cover-placeholder";
    placeholder.setAttribute("aria-hidden", "true");
    placeholder.textContent = (name || "").charAt(0).toUpperCase();
    thumb.appendChild(placeholder);

    if (coverId) {
      const img = document.createElement("img");
      img.className = "game-cover";
      img.dataset.coverId = coverId;
      img.alt = "";
      img.hidden = true;
      thumb.appendChild(img);
      attachConfirmCover(img);
    }

    return thumb;
  }

  // Builds the <div class="confirm-text"> name+meta block shared by group
  // headers and standalone flat rows. Both name and meta are full,
  // untruncated strings -- CSS handles ellipsis, .title carries the full
  // name so hovering a truncated row still reveals it.
  function buildConfirmTextBlock(name, nameClass, metaText) {
    const wrap = document.createElement("div");
    wrap.className = "confirm-text";

    const nameSpan = document.createElement("span");
    nameSpan.className = nameClass;
    nameSpan.textContent = name;
    nameSpan.title = name;
    wrap.appendChild(nameSpan);

    const metaSpan = document.createElement("span");
    metaSpan.className = "confirm-meta";
    metaSpan.textContent = metaText;
    wrap.appendChild(metaSpan);

    return wrap;
  }

  // Builds one standalone flat row (a selected item with no 2+-member
  // family) as a <li class="confirm-list-item" data-id="...">, with cover
  // thumbnail + name/meta text block.
  function renderConfirmRow(id, v) {
    const li = document.createElement("li");
    li.className = "confirm-list-item";
    li.dataset.id = id;

    const label = v.role ? CONFIRM_ROLE_LABELS[v.role] : null;
    if (label) {
      const tagSpan = document.createElement("span");
      tagSpan.className = "confirm-tag confirm-tag-" + v.role;
      tagSpan.textContent = "[" + label + "]";
      li.appendChild(tagSpan);
    }

    li.appendChild(buildConfirmThumb(v.name, v.coverId));
    li.appendChild(buildConfirmTextBlock(v.name, "confirm-name", formatBytes(v.size) + " · " + v.destination));

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "confirm-item-remove";
    removeBtn.dataset.id = id;
    removeBtn.setAttribute("aria-label", "Remove " + v.name);
    removeBtn.innerHTML = TRASH_SVG;
    li.appendChild(removeBtn);

    return li;
  }

  // Builds one nested <li class="confirm-child" data-id="..."> inside a
  // group's <ul class="confirm-children"> -- no thumbnail (per the design,
  // only top-level rows show cover art), just tag + name + remove button.
  function renderConfirmChildRow(id, v) {
    const li = document.createElement("li");
    li.className = "confirm-child";
    li.dataset.id = id;

    const label = v.role ? CONFIRM_ROLE_LABELS[v.role] : null;
    if (label) {
      const tagSpan = document.createElement("span");
      tagSpan.className = "confirm-tag confirm-tag-" + v.role;
      tagSpan.textContent = "[" + label + "]";
      li.appendChild(tagSpan);
    }

    const nameSpan = document.createElement("span");
    nameSpan.className = "confirm-child-name";
    nameSpan.textContent = v.name;
    nameSpan.title = v.name;
    li.appendChild(nameSpan);

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "confirm-item-remove";
    removeBtn.dataset.id = id;
    removeBtn.setAttribute("aria-label", "Remove " + v.name);
    removeBtn.innerHTML = TRASH_SVG;
    li.appendChild(removeBtn);

    return li;
  }

  // Builds the group header + nested children list for one family with 2+
  // selected entries -- header carries the cover thumbnail, name/meta text
  // block and the per-group remove button; the <ul> right after it carries
  // one .confirm-child row per selected member of that family (in
  // `selected`'s insertion order). The grouping/dedup decision itself (which
  // families qualify for a group at all) is made by the caller, unchanged.
  function renderConfirmGroup(family, familyName) {
    const li = document.createElement("li");
    li.className = "confirm-group";

    // The group's OWN totals -- not the whole selection's -- so the meta
    // line always matches what's actually nested under this one group.
    let groupSize = 0;
    let itemCount = 0;
    const groupDestinations = new Set();
    selected.forEach((v) => {
      if (v.family !== family) return;
      groupSize += v.size;
      itemCount += 1;
      groupDestinations.add(v.destination);
    });
    const groupDestination = groupDestinations.size === 1
      ? Array.from(groupDestinations)[0]
      : groupDestinations.size + " different";

    // `family` is itself the base game's TITLE_ID / cover id (same value the
    // base row's own data-cover-id carries), so it doubles as the group
    // thumbnail's cover id with no extra lookup needed.
    li.appendChild(buildConfirmThumb(familyName || "", family));
    li.appendChild(buildConfirmTextBlock(
      familyName || "",
      "confirm-group-name",
      "Base game · " + itemCount + " item(s) · " + formatBytes(groupSize) + " · " + groupDestination
    ));

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "confirm-item-remove confirm-group-remove";
    removeBtn.dataset.family = family;
    removeBtn.setAttribute("aria-label", "Remove " + (familyName || "") + " and all its content");
    removeBtn.innerHTML = TRASH_SVG;
    li.appendChild(removeBtn);

    const ul = document.createElement("ul");
    ul.className = "confirm-children";
    selected.forEach((v, id) => {
      if (v.family !== family) return;
      ul.appendChild(renderConfirmChildRow(id, v));
    });
    li.appendChild(ul);

    return li;
  }

  function renderConfirmList() {
    confirmCount.textContent = String(selected.size);
    document.getElementById("confirm-count-2").textContent = String(selected.size);
    let totalSize = 0;
    const destinations = new Set();
    const familyCounts = new Map();
    selected.forEach((v) => {
      totalSize += v.size;
      destinations.add(v.destination);
      if (v.family) familyCounts.set(v.family, (familyCounts.get(v.family) || 0) + 1);
    });

    confirmList.innerHTML = "";
    const renderedFamilies = new Set();
    selected.forEach((v, id) => {
      // Only group families with 2+ selected members -- a lone pick from
      // an otherwise-family-eligible game (e.g. just its one DLC) stays a
      // plain flat row, not a 1-item group.
      if (v.family && familyCounts.get(v.family) >= 2) {
        if (renderedFamilies.has(v.family)) return;
        renderedFamilies.add(v.family);
        confirmList.appendChild(renderConfirmGroup(v.family, v.familyName));
        return;
      }
      confirmList.appendChild(renderConfirmRow(id, v));
    });

    confirmSize.textContent = formatBytes(totalSize);
    // A batch can legitimately mix destinations (a game install + an
    // atmosphere mod merge) -- never claim a single destination that
    // isn't true for every item; the per-item list above always has the
    // exact answer regardless.
    confirmDestination.textContent = destinations.size === 1
      ? Array.from(destinations)[0]
      : destinations.size + " different (see list below)";
    confirmInstallBtn.disabled = selected.size === 0;
    if (selected.size === 0) confirmModal.close();
    // Dropping the last amiibo from the list drops the emuiibo offer too.
    if (!selectionHasAmiibo()) emuiiboBox.hidden = true;
  }

  // -- emuiibo, offered with a game's amiibo --------------------------------
  //
  // Amiibo do nothing on a Switch without emuiibo. When the selection holds
  // some and the target Switch does not have it (as of its last read --
  // GET /api/amiibo/emuiibo/offer), the confirmation offers its download
  // from GitHub and install in the same click: POST /api/amiibo/emuiibo/download
  // queues it, and the selection is queued once that has succeeded.
  const emuiiboBox = document.getElementById("confirm-emuiibo");
  const emuiiboCheck = document.getElementById("confirm-emuiibo-check");
  const emuiiboWhy = document.getElementById("confirm-emuiibo-why");
  const emuiiboTesla = document.getElementById("confirm-emuiibo-tesla");
  let offerRequest = 0;

  function selectionHasAmiibo() {
    return Array.from(selected.values()).some((v) => v.role === "amiibo");
  }

  async function refreshEmuiiboOffer(device) {
    const request = ++offerRequest;
    emuiiboBox.hidden = true;
    if (!device || !selectionHasAmiibo()) return;
    let offer;
    try {
      const res = await fetch("/api/amiibo/emuiibo/offer?device=" + encodeURIComponent(device));
      if (!res.ok) return;
      offer = await res.json();
    } catch (_) {
      return;
    }
    if (request !== offerRequest || !offer.offer || !selectionHasAmiibo()) return;
    const known = offer.state !== "unknown";
    emuiiboWhy.textContent = offer.state === "partial"
      ? "emuiibo is incomplete on this Switch, and these amiibo need it."
      : known ? "these amiibo need it, and this Switch does not have it."
      : "these amiibo need it. SwitchAgent has not checked this Switch for it yet.";
    // Checked when the Switch is known to lack it; left to the user when
    // nobody knows yet.
    emuiiboCheck.checked = known;
    // Its overlay menu (Ultrahand) comes first when the Switch lacks one.
    emuiiboTesla.hidden = !offer.also.length;
    emuiiboTesla.textContent = offer.also.length
      ? "Its overlay menu, " + offer.also.join(" and ") + ", is not on this Switch either — it is installed first."
      : "";
    emuiiboBox.hidden = false;
  }

  // Resolves with the download's final state ("done" / "failed").
  async function installEmuiibo(device) {
    const res = await fetch("/api/amiibo/emuiibo/download", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ device }),
    });
    let d = await res.json();
    if (!res.ok) return { state: "failed", error: d.detail || ("HTTP " + res.status) };
    while (d.state !== "done" && d.state !== "failed") {
      confirmResult.textContent = window.SwitchAgentAddons.describe(d);
      await new Promise((resolve) => setTimeout(resolve, 700));
      try {
        const poll = await fetch("/api/addons/activity");
        if (poll.ok) d = (await poll.json()).download || d;
      } catch (_) { /* keep waiting */ }
    }
    return d;
  }

  // Lets the user drop an individual Update/DLC/Mod (or anything else)
  // right at the confirmation step, not just before opening the modal --
  // unchecks the matching card checkbox too, so the two stay in sync if
  // the user cancels and looks at the page again. A per-group remove drops
  // every selected member of that family in one click; checked first since
  // it's the more specific target (its button is also a .confirm-item-remove).
  confirmList.addEventListener("click", (evt) => {
    const groupBtn = evt.target.closest(".confirm-group-remove");
    if (groupBtn) {
      const family = groupBtn.dataset.family;
      Array.from(selected.entries())
        .filter(([, v]) => v.family === family)
        .forEach(([id]) => {
          selected.delete(id);
          const box = document.querySelector(`.select-box[value="${CSS.escape(id)}"]`);
          if (box) box.checked = false;
        });
      updateSelectionBar();
      renderConfirmList();
      return;
    }

    const itemEl = evt.target.closest(".confirm-item-remove");
    if (!itemEl) return;
    const idHost = evt.target.closest("[data-id]");
    if (!idHost) return;
    const id = idHost.dataset.id;
    selected.delete(id);
    const box = document.querySelector(`.select-box[value="${CSS.escape(id)}"]`);
    if (box) box.checked = false;
    updateSelectionBar();
    renderConfirmList();
  });

  installBtn.addEventListener("click", () => {
    if (selected.size === 0) return;
    if (!targetSelect || !targetSelect.value) {
      alert("Choose a target Switch first.");
      return;
    }
    confirmTarget.textContent = targetSelect.options[targetSelect.selectedIndex].textContent.trim();
    renderConfirmList();
    confirmResult.hidden = true;
    confirmResult.textContent = "";
    refreshEmuiiboOffer(targetSelect.value);
    confirmModal.showModal();
  });

  confirmCancelBtn.addEventListener("click", () => confirmModal.close());

  confirmInstallBtn.addEventListener("click", async () => {
    if (selected.size === 0) return;
    // The device could have disconnected (or a second one connected)
    // while this modal was already open -- refreshSelectionTarget() nulls
    // targetSelect out from under it in that case; never send a stale or
    // missing target.
    if (!targetSelect || !targetSelect.value) {
      confirmModal.close();
      alert("The target Switch is no longer available -- choose one again.");
      return;
    }
    const originalBtnText = confirmInstallBtn.textContent;
    confirmInstallBtn.disabled = true;
    confirmInstallBtn.textContent = "Preparing…";
    if (!emuiiboBox.hidden && emuiiboCheck.checked && selectionHasAmiibo()) {
      confirmResult.hidden = false;
      emuiiboCheck.disabled = true;
      const d = await installEmuiibo(targetSelect.value);
      emuiiboCheck.disabled = false;
      if (d.state !== "done") {
        // Nothing is queued: installing the amiibo without emuiibo is the
        // user's call (untick it), never a silent fallback.
        confirmResult.textContent = "emuiibo could not be installed: " + (d.error || "unknown error") +
          ". Nothing was queued — try again, or untick “Also install emuiibo” to install without it.";
        confirmInstallBtn.disabled = false;
        confirmInstallBtn.textContent = originalBtnText;
        return;
      }
      emuiiboBox.hidden = true;
    }
    // Queue owns preparation progress; this request only accepts the batch.
    confirmResult.hidden = false;
    confirmResult.textContent =
      "Preparing " + (selected.size === 1 ? "installation" : "installations") +
      "… extracting archives if needed — large files can take a while, please wait.";
    const body = {
      library_item_ids: Array.from(selected.keys()).map((id) => parseInt(id, 10)),
      target_device_id: targetSelect.value,
    };
    try {
      const res = await fetch("/api/preparations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (res.ok && data.preparation_id) {
        window.location.href = "/queue";
        return;
      }
      confirmResult.hidden = false;
      if (data.created && data.created.length) {
        const n = data.created.length;
        confirmResult.textContent =
          n + (n === 1 ? " installation" : " installations") + " added to queue." +
          (data.errors && data.errors.length ? " " + data.errors.length + " failed -- see Library for details." : "");
        // Every created job already exists as its own persisted row the
        // instant this response comes back (services.create_and_confirm_jobs
        // creates+confirms each one before returning) -- the worker thread
        // hasn't necessarily started any of them yet. A fast job can still
        // finish (and leave Queue for History) before this redirect lands,
        // which is correct, not lost -- see Queue's own live view/History.
        setTimeout(() => { window.location.href = "/queue"; }, 1500);
      } else {
        confirmResult.textContent = "Nothing was queued: " + (data.errors || []).map((e) => e.error).join("; ");
        confirmInstallBtn.disabled = false;
        confirmInstallBtn.textContent = originalBtnText;
      }
    } catch (e) {
      confirmResult.hidden = false;
      confirmResult.textContent = "Request failed: " + e;
      confirmInstallBtn.disabled = false;
      confirmInstallBtn.textContent = originalBtnText;
    }
  });

  // -- rescan (non-blocking, point 16) -------------------------------------
  //
  // W3-007: more than one trigger can exist on the page (the toolbar's own
  // #rescan-btn, plus an onboarding banner's own Rescan button when the
  // library is empty -- see library.html/onboarding.py) -- every element
  // with the shared .rescan-trigger class is wired up identically. What the
  // scan is doing, its Stop button, and when the buttons come back are all
  // activity.js's: it shows every scan, not only one started from here --
  // and library_live.js refreshes the grid when it ends, without reloading
  // the page out from under a selection.

  document.querySelectorAll(".rescan-trigger").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await fetch(btn.dataset.scanUrl, { method: "POST" });
      } finally {
        document.dispatchEvent(new Event("activity-poke"));
      }
    });
  });
})();
