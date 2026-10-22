# Add-ons: what SwitchAgent installs, and how

The Add-ons tab is `switchagent/web/addons/catalog.yaml`, curated by hand.
The rules are in `switchagent/addons.py`, the installer in
`switchagent/web/addons_service.py`; the working notes are in CLAUDE.md
("Add-ons: a curated catalog SwitchAgent installs from").

## Choosing what goes in

Checked against kefir (rashevskyv/kefir, release 919, 2026-09-25) -- the
Russian-speaking scene's reference pack -- and each project's own releases.

In: Ultrahand Overlay (with nx-ovlloader), emuiibo, Status Monitor
(ppkantorski's maintained fork), FPSLocker + SaltyNX, ReverseNX-RT, Fizeau,
QuickNTP, JKSV, Moonlight.

Added from the 2026-09-26 research (docs/ADDONS-CANDIDATES.md) -- apps and
overlays only, each release downloaded and matched by the catalog: Amiigo,
NXThemes Installer + Themezer-NX, Checkpoint, Goldleaf, sphaira (as an app,
never as the Homebrew Menu), SimpleModManager + SimpleModDownloader, EdiZon
Overlay, Sysmodules (ovl-sysmodules), SaltyNX-Tool, NX Activity Log,
NX-Update-Checker, NXMP, NooDS, Turnips.

Waiting, from the same research: system modules whose firmware support is
not proven -- MissionControl (states up to 22.5.0), sys-con 2.0.0 (a day
old), SysDVR, ldn_mitm, PNGShot (firmware-bound patches), NSParentalControl
(30 stars); ftpsrv (no published SHA-256); Switchfin (65 MB, narrow);
sys-patch (sigpatches) -- the owner's call.

Out, and why:

- Atmosphère / Kefirosphere, hekate, payloads (Lockpick, TegraExplorer),
  Daybreak: the boot chain. Writing it over MTP while the console runs from
  it is how a console stops booting.
- DBI: it is the MTP responder SwitchAgent talks through.
- Tesla Menu, WerWolv's nx-ovlloader and ovl-sysmodules: unmaintained since
  2023; Ultrahand and ppkantorski's forks replaced them (kefir did too).
- NX-FanControl (2024): not maintained for current firmware.
- sys-clk-OC / Horizon-OC: need a patched loader (`atmosphere/kips/`) -- boot
  chain. The original sys-clk (retronx-team, release 2.0.1 "21fix") is in, at
  the owner's request, on the understanding that its firmware support is not proven.
- Torrent catalogs: not what SwitchAgent is for.

## The releases, as they are laid out (2026-09-26)

| Add-on | Asset | What is in it |
|---|---|---|
| Ultrahand 2.5.3 | `sdout.zip` | `switch/.overlays/ovlmenu.ovl`, `switch/Ultrahand-Reload/`, `switch/.packages/`, `config/ultrahand/` (languages, sounds, themes), nx-ovlloader (`atmosphere/contents/420000000007E51A` + `…E51B`), `atmosphere/exefs_patches/audio_mastervolume/` |
| SaltyNX 1.9.3 | `SaltyNX.zip` | `atmosphere/contents/0000000000534C56/`, `atmosphere/exefs_patches/SaltyNX_Fixes/`, `SaltySD/` (`exceptions.txt` is the user's) |
| Fizeau 2.8.3 | `Fizeau-<ver>-<hash>.zip` | `atmosphere/contents/0100000000000F12/`, `atmosphere/exefs_patches/nvnflinger_cmu/`, `config/Fizeau/config.ini` (the user's), `switch/Fizeau/Fizeau.nro`, `switch/.overlays/Fizeau.ovl` |
| QuickNTP 1.6.0 | `sdout.zip` | `switch/.overlays/QuickNTP.ovl`, `config/quickntp.ini` (the user's) |
| FPSLocker 3.3.2 | `FPSLocker.ovl` | one overlay, NACP name "FPSLocker" |
| Status Monitor 1.4.1+r4 | `Status-Monitor-Overlay.ovl` | one overlay, NACP name "Status Monitor" |
| ReverseNX-RT 2.2.1 | `ReverseNX-RT-ovl.ovl` | one overlay, NACP name "ReverseNX-RT" |
| JKSV | `JKSV.nro` | one app (tag `12/02/2025`), NACP name "JKSV" |
| Moonlight 1.5.0 | `Moonlight-Switch.nro` | one app (17.7 MB), NACP name "Moonlight" |

nx-ovlloader's own `nx-ovlloader.zip` is not an add-on of the catalog: it is
inside Ultrahand's release, and on its own it is not recognised as anything
the tab installs.

## What a console read adds

After emuiibo's part, the worker lists each folder a catalog `sign` file
lives in (once per folder), notes which are there, reads back the NACP of
each `version_from` overlay/app of at most 4 MB -- only when its size
changed since the last read -- and checks for
`switch/kefir-updater/kefir-updater.nro`. Stored in `device_addons`.
