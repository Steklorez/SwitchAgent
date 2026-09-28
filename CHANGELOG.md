# Changelog

Each `## vX.Y.Z` section becomes the body of that version's
[GitHub Release](../../releases).

Entries are one sentence per change, three to five lines per release, and
only what a user would notice — no internals, no reasoning, no history.
Those belong in the commit message. See [CLAUDE.md](CLAUDE.md#how-to-write-an-entry).

## v1.0.24

- Homebrew ports get a cover too: the icon inside the port itself, or the game's Steam art when the name matches.
- Games with no TITLE_ID show their cover on the card.

## v1.0.23

- A homebrew port whose package does not say which game it is (Need for Speed: Most Wanted) shows as one game with its SD files and installs like any other.
- A port's SD files install before the game itself, so its icon appears only once everything it needs is on the card, and Queue lists them in that order.
- A game's card shows the size of the whole game, or of just the parts you have ticked.

## v1.0.22

- The Library only shows something as installed while the Switch it went to is connected.
- Homebrew and other files copied to the SD card show "On Switch" when they are actually on the card, and stop showing as installed once they are gone from it.

## v1.0.21

- A new Amiibo tab, once emuiibo is on a console: install virtual amiibo collections, see and remove what is on the Switch.
- A game's amiibo show on its card and install with it, with an offer to install emuiibo where the Switch lacks it.
- Beta, in Settings: an Add-ons tab that installs and updates homebrew such as Ultrahand, FPSLocker, Status Monitor and JKSV from their GitHub releases.
- Tools that come with a game, like Animal Crossing's Events Unlock, appear in its card.

## v1.0.20

- A homebrew app on its own installs: a lone .nro, or a zip with one inside, goes to `switch/<name>/` on the SD card, named after the app itself.
- When the Switch stops responding mid-install, the install waits instead of hanging, and continues where it stopped once the Switch is reconnected or MTP is restarted in DBI.

## v1.0.19

- A panel along the bottom of every page shows what is being installed, one line per game with its progress.
- Queue shows one card per game, with its cover and a bar for every package it installs.
- Abort stops the install in progress and everything waiting; Remove takes one game out of the queue at any stage.
- Pause now shows on the page, and statuses read as words instead of codes.

## v1.0.18

- The tray icon's menu opens the program, data and logs folders.
- Settings has a Copy logs button that puts the recent application log on the clipboard, ready to paste into a bug report.

## v1.0.17

- The Library says whether it is still loading: one line above the games shows a running scan, cover downloads and the console's installed-games read, each with its own count.
- A thin bar along the top of the window moves while anything is still loading.
- Games that TitleDB has no cover for are no longer shown as a cover error.

## v1.0.16

- A console that stops answering for a moment no longer fails a big install: that file is simply sent again.
- Retry continues where the failed attempt stopped instead of starting over.
- Files already on the SD card with exactly the same contents are recognised and not sent again.
- Override replaces only the files that are actually there, instead of slowing the whole install down.

## v1.0.15

- A homebrew port is one game now: its `switch/` folder, archived or unpacked, is part of the game's card and is copied to the SD card on install.
- If a game's icon would start a file none of its files provide, its card says so.
- Homebrew ports with a TITLE_ID of their own show as games, not as DLC of a missing game.
- Card tags wrap onto a second line instead of scrolling sideways.
- Archives packed by a recent 7-Zip install instead of failing with "unsupported compression algorithm".

## v1.0.14

- Installing over a file already on the Switch now follows a Settings choice — skip it or replace it, the same way every time — instead of a Queue card waiting to be clicked.
- A game installed while the Switch stayed connected now shows as On Switch in Library right away, instead of only after reconnecting.

## v1.0.13

- History is now the same list Queue shows, after the fact: every transfer in the order it happened, with the same Game / Update / DLC / Mod badge and how it ended.
- It asks you to confirm nothing and changes nothing — the buttons are gone, and times are shown in your own timezone at last.

## v1.0.12

- The library shows what is actually on disk: no duplicate entries after a folder's path changes, no games missing after re-adding one.
- A scan can be stopped, and changing Library folders no longer waits for one to finish.
- A fresh install no longer scans your Downloads folder unasked.
- The SD card space bar works with no Switch connected.

## v1.0.11

- Installs are much faster: a 123 MiB NSP went from 68.5s to 5.97s, and games show real progress while they transfer.

---

Releases before 1.0.11 predate this file — see the
[releases page](../../releases).
