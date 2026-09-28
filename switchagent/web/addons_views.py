"""The Add-ons tab: the hand-curated catalog of open-source Switch
utilities (switchagent/addons.py, web/addons/catalog.yaml) -- what each one
is, how it works, how it gets onto a console, how to use it -- and, for the
chosen Switch, which of them it has, as of its last read.

A read of the catalog file and of the database only (what the worker last
read off a console -- web/emuiibo_service.py): no COM, no network, safe from
any request thread. The one action on the page, Install, is
web/addons_service.AddonInstaller: the add-on and whatever it requires that
this console does not have, from GitHub, then the ordinary queue.
"""

from __future__ import annotations

from typing import Optional

from .. import db, emuiibo
from ..addons import (  # noqa: F401 -- the catalog's names, where the page and its tests look for them
    BUILTIN_INSTALLERS, CATALOG_PATH, STATIC_DIR, STATUS_CHECKS, Addon, CatalogError, ConsoleAddons, Tip,
    install_order, load_catalog, parse_catalog, rich,
)
from .. import addons as addons_mod


def emuiibo_status(state: Optional[dict], latest: Optional[str] = None) -> Optional[dict]:
    """{state, label, version} of emuiibo on the console, as of its last
    read (web/emuiibo_service.py); "unknown" when never read. `latest` is
    GitHub's current release when known (ReleaseChecker), else the one
    this SwitchAgent was built knowing."""
    if state is None:
        return {"state": "unknown", "label": "not read yet", "version": None}
    components = state.get("components") or {}
    have = [bool(components.get("sysmodule")), bool(components.get("overlay"))]
    version = state.get("overlay_version")
    if all(have):
        current = latest or emuiibo.LATEST_KNOWN_VERSION
        outdated = addons_mod.is_newer(current, version) if latest else emuiibo.is_outdated(version)
        label = f"installed{' — ' + version if version else ''}"
        if outdated:
            label += f" (outdated, {current} is current)"
        return {"state": "outdated" if outdated else "installed", "label": label, "version": version}
    if any(have):
        missing = "the overlay" if have[0] else "the module"
        return {"state": "partial", "label": f"incomplete — {missing} is missing", "version": version}
    return {"state": "missing", "label": "not installed", "version": None}


def addon_status(addon: Addon, console: Optional[ConsoleAddons], emuiibo_state: Optional[dict],
                 latest: Optional[str] = None) -> Optional[dict]:
    if addon.status == "emuiibo" or addon.install == "emuiibo":
        return emuiibo_status(emuiibo_state, latest)
    return addons_mod.status(addon, console)


def installed_by_us(conn, device_id: str, addon: Addon) -> Optional[str]:
    """The version SwitchAgent itself last installed of this add-on on this
    console -- what a console read cannot say for a program too big to read
    back (JKSV, Moonlight) or one with no version inside (SaltyNX)."""
    return installed_record(conn, device_id, addon)[0]


def installed_record(conn, device_id: str, addon: Addon) -> tuple[Optional[str], Optional[str]]:
    """(version, when) of SwitchAgent's own last install of this add-on on
    this console -- (None, None) when it never installed it there."""
    import json

    from ..model import ContentType

    rows = conn.execute(
        "SELECT li.content_type, li.details_json, j.finished_at FROM jobs j "
        "JOIN library_items li ON li.id = j.library_item_id "
        "WHERE j.target_device_id = ? AND j.status IN ('DONE', 'DONE_UNVERIFIED') AND li.content_type IN (?, ?) "
        "ORDER BY j.finished_at DESC",
        (device_id, ContentType.ADDON.value, ContentType.EMUIIBO.value),
    ).fetchall()
    for row in rows:
        try:
            details = json.loads(row["details_json"] or "{}")
        except ValueError:
            continue
        if addon.install == "emuiibo" and row["content_type"] == ContentType.EMUIIBO.value:
            return (details.get("emuiibo") or {}).get("version"), row["finished_at"]
        found = details.get("addon") or {}
        if row["content_type"] == ContentType.ADDON.value and found.get("id") == addon.id:
            return found.get("version"), row["finished_at"]
    return None, None


def update_offer(conn, device_id: Optional[str], addon: Addon, status: Optional[dict],
                 latest: Optional[str]) -> Optional[dict]:
    """{"to", "installed"} when an installed add-on has a newer release on
    GitHub -- "installed" None when nobody can say which version is on the
    console (then the offer is to reinstall the latest)."""
    if not latest or not status or status["state"] not in ("installed", "outdated") or device_id is None:
        return None
    installed = status.get("version") or installed_by_us(conn, device_id, addon)
    if installed is None:
        return {"to": latest, "installed": None}
    return {"to": latest, "installed": installed} if addons_mod.is_newer(latest, installed) else None


def console_state(conn, device_id: Optional[str]) -> tuple[Optional[ConsoleAddons], Optional[dict], Optional[str]]:
    """(add-ons, emuiibo state, read time) of one console's last read."""
    if device_id is None:
        return None, None, None
    stored = db.get_device_addons(conn, device_id)
    em = db.get_device_emuiibo(conn, device_id)
    console = ConsoleAddons.from_dict(stored[0]) if stored else None
    return console, (em[0] if em else None), (stored[1] if stored else (em[2] if em else None))


def _satisfied(addon: Addon, console: Optional[ConsoleAddons], emuiibo_state: Optional[dict]) -> bool:
    """For a requirement: is it there, in a form that does its job? Another
    overlay menu where Ultrahand's goes (Tesla Menu) does the same job, so
    it is not replaced behind anybody's back -- only an explicit Install of
    Ultrahand replaces it."""
    found = addon_status(addon, console, emuiibo_state)
    if found and found["state"] in ("installed", "outdated", "other", "elsewhere"):
        return True
    if (console is None or (found and found["state"] == "unknown")) and addon.id == "ultrahand" and emuiibo_state:
        # Not (yet) looked for by an add-ons read: emuiibo's own read knows
        # the overlay menu and its loader.
        components = emuiibo_state.get("components") or {}
        return bool(components.get("ovlloader") and components.get("tesla_menu"))
    return False


def plan_install(conn, device_id: Optional[str], addon_id: str) -> list[Addon]:
    """What one Install click of `addon_id` downloads, in order: every
    requirement this console does not have yet, then the add-on itself."""
    console, emuiibo_state, _read_at = console_state(conn, device_id)
    chain = install_order([addon_id])
    return [a for a in chain if a.id == addon_id or not _satisfied(a, console, emuiibo_state)]


def page(conn, ctx, fingerprint: Optional[str] = None) -> dict:
    from . import services

    devices = services.list_devices(conn, ctx)
    chosen = next((d for d in devices if d["device_fingerprint"] == fingerprint), None) if fingerprint else None
    chosen = chosen or next((d for d in devices if d["connected"]), None) or (devices[0] if devices else None)
    console, emuiibo_state, read_at = console_state(conn, chosen["device_id"] if chosen else None)
    catalog = load_catalog()
    by_id = {a.id: a for a in catalog}
    connected = bool(chosen and chosen["connected"])
    releases = ctx.addon_releases.snapshot()
    entries = []
    for addon in catalog:
        latest = (releases["releases"].get(addon.id) or {}).get("version")
        status = addon_status(addon, console, emuiibo_state, latest)
        state = status["state"] if status else None
        managed = bool(status and status.get("kefir"))
        update = None if managed else update_offer(conn, chosen["device_id"] if chosen else None,
                                                   addon, status, latest)
        if update and status and state != "outdated":  # "outdated" already says which is current
            status = {**status, "label": status["label"] + (
                f" · {update['to']} is available" if update["installed"] else
                f" · version not known, {update['to']} is the latest")}
        elif latest and status and state == "installed" and status.get("version")                 and not addons_mod.is_newer(latest, status["version"]):
            # Confirmed by GitHub, not just "no news": said so.
            status = {**status, "label": status["label"] + " · latest"}
        record = (installed_record(conn, chosen["device_id"], addon)
                  if chosen and state in ("installed", "outdated", "partial", "other", "elsewhere") else (None, None))
        if update:
            button = f"Update to {update['to']}" if update["installed"] else "Reinstall latest"
        else:
            button = ("Installed" if state in ("installed", "elsewhere") else
                      "Update" if state in ("outdated", "partial") else
                      "Replace" if state == "other" else "Install")
        entries.append({
            "addon": addon,
            "status": status,
            "requires": [by_id[r] for r in addon.requires],
            "latest": latest,
            "update": update,
            # Installed from here unless kefir keeps it updated on this
            # console (two updaters would take turns overwriting it).
            "can_install": bool(addon.installable and connected and not managed
                                and state != "elsewhere" and (state != "installed" or update)),
            "button": button,
            "managed": managed,
            "stars": releases["stars"].get(addon.id),
            # For sorting (addons.js): 3 update waiting, 2 installed, 1
            # partly there / someone else's in its place, 0 not there.
            "rank": (3 if update and update["installed"] else
                     2 if state in ("installed", "outdated", "elsewhere") else
                     1 if state in ("partial", "other") else 0),
            "installed_at": record[1],
            "with": ([r.name for r in plan_install(conn, chosen["device_id"], addon.id) if r.id != addon.id]
                     if addon.installable and chosen else []),
        })
    return {
        "entries": entries,
        "devices": [{"fingerprint": d["device_fingerprint"], "name": d["display_name"], "connected": d["connected"]}
                    for d in devices],
        "device": ({"fingerprint": chosen["device_fingerprint"], "name": chosen["display_name"],
                    "connected": chosen["connected"]} if chosen else None),
        "read_at": read_at,
        "releases_checked_at": releases["checked_at"],
        "releases_checking": releases["checking"],
        "kefir": bool(console and console.kefir),
        "download": ctx.emuiibo_downloads.snapshot(),
    }
