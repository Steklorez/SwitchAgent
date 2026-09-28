# SwitchAgent — working notes

## Releases

A push to `main` **is** a release. `.github/workflows/release-windows.yml`
derives the tag from `pyproject.toml`'s `version`, builds the installer and
portable zip on a Windows runner, and publishes them. Pushing to `main`
with a version that already has a release **re-publishes** that release:
the old one is deleted and recreated, which resets its download count. Fine
for fixing a release page minutes after the fact; worth knowing before
pushing a doc typo to a release that has been up for a week.

To cut a new version: bump `version` in `pyproject.toml`, add its section
to `CHANGELOG.md`, commit, push.

## CHANGELOG.md is the release body

The workflow copies the `## v<X.Y.Z>` section verbatim into the GitHub
Release, then appends the auto-generated `**Full Changelog**` compare link.
There is nowhere else release notes are written. A tag with no section
falls back to generated notes only — a release is never blocked on prose.

### How to write an entry

**One sentence per change. Three to five lines for a normal release.**

The reader is someone on the releases page deciding whether to download
this. The only question they have is *what is different now*.

- **Only what a user would notice.** If a change cannot be seen from the
  UI, it does not belong here. Cache keys, refactors, a fixed internal
  race, test coverage — all real work, none of it a release note.
- **No reasoning, no cause, no history.** Not why it broke, not how it was
  fixed, not what it used to do. That belongs in the commit message, which
  is where anyone asking those questions will look.
- **Fold related fixes into one line.** Four separate bugs that all made
  the library show the wrong thing are one sentence, not four.
- **No bold lead-ins, no sub-bullets, no paragraphs.** A flat list of plain
  sentences.

Good:

```markdown
## v1.0.12

- The library shows what is actually on disk: no duplicate entries after a
  folder's path changes, no games missing after re-adding one.
- A scan can be stopped, and changing Library folders no longer waits for
  one to finish.
- The SD card space bar works with no Switch connected.
```

Bad — this is the same release, and it is an inventory of the work rather
than a summary of it:

```markdown
- **One file is one row again.** Pointing a Library folder at the same
  files by another path -- `D:\shared\Download` to `\\192.168.50.2\...`,
  a new drive letter, a renamed parent folder -- used to re-index
  everything as new and leave the old rows behind, so a single game
  counted as two ("Base game: present (2 copies)").
- **Static assets are versioned by content.** Two of the fixes above
  shipped briefly invisible for exactly that reason.
```

If a section runs past ~500 characters, it is describing the work instead
of the result.

## Commits

Commit as the repository owner alone. **No `Co-Authored-By` trailer** for
Claude or any other tool.

Commit messages are the opposite of changelog entries: that is where the
cause, the reasoning, the dead ends and the measurements go, at whatever
length the change earns.

## History is a journal, not a pulpit

**Nothing on the History page changes anything.** No install confirmation, no
Override / Skip / Send again, no dismiss-or-hide. Decisions live in Queue;
what is on a console lives in Devices. This is a product decision, not an
oversight — do not add a control there because it would be convenient.

The line, when it is unclear: *the page may change what you are looking at;
it may not change what happened, or what will.* Search, filters, day
grouping, a `<details>` fold, pagination and links to another page are all
fine. Anything that writes — including "mark as read" — is not: a journal
you can edit stops being a record.

The one link it is allowed is an **address, not a control**: a stuck row says
`Still waiting in Queue →`, rendered as a text link rather than a `.btn`,
worded as a state rather than an order, and shown only while there is
actually something in Queue to decide. Reporting a problem and staying silent
about where it gets resolved is worse than not reporting it.

### Why the confirmation prompt was deleted

It asked "did this install?" on every unverified row. In a real library that
was 55 questions and **0 answers, ever**. The answer lives on another device,
costs a walk to the console, and buys nothing once given — the row changes
one caption for another. Eighteen retries of one mod asked eighteen times
about one file. `POST /api/history/{id}/verification` and
`set_history_verification()` are still correct and still tested; they simply
have no caller in the UI.

### What the page owes the reader

Two questions, and it is not asked to do more:

1. *"I pressed Install — did it arrive?"* — most visits, minutes old.
2. *"Why is this game not on the Switch?"* — the stuck ones.

One row per **title**, not per transfer: a worker that retried the same mod
18 times is one fact to a person, not eighteen (86 rows for 19 things is what
this replaced). A row states its latest outcome, and carries what it took to
get there — dropping the earlier refusals would hide exactly what someone
came to find out.

Local time, never the stored UTC. Human wording from `classify_activity()`,
never a raw enum. The same name Library shows, via the same
`strip_release_tags` — one object must not read as two different things
depending on which page you are on.

## A game is its folder: SD files

A homebrew port ships as a small **forwarder** `.nsp` (installed through DBI,
it only puts an icon on the home menu) plus a **`switch/` folder** that has to
land on the SD card verbatim — the forwarder launches e.g.
`sdmc:/switch/zumaportable/dbc17o.nro`, stored in plain text inside it. The
`switch/` folder comes as an archive (`switch.7z`) or already unpacked
(`Homebrew (1.0.0)/switch/...`), and a port can need both (Mega Man X
Regenesis: data in the archive, the `.nro` in the unpacked folder). All of it
is `ContentType.SD_FILES`; the rules live in `switchagent/sd_files.py`.

What must stay true — `tests/test_sd_files.py` encodes each of these against
the real Zuma / Mega Man layouts, so change a test on purpose or not at all:

- **It belongs to its game, never a card called "switch".** Owner, in order:
  a `[TITLE_ID]` in its own name; a forwarder in the library that launches an
  `.nro` inside it; the nearest folder above it holding any game — if that
  is exactly one game, or else the one game whose package lies directly in
  that folder (a release with a companion app in a sub-folder: Animal
  Crossing and its Island Transfer Tool). That last case is the game's but
  not needed by it (`title_id_source` "release"): ticked by hand, never
  along with the game. Two games side by side: it stays its own card
  (named after the folder), never guessed onto either.
- **A forwarder that gives nothing away is still its port.** Some ports'
  `.nsp` has no `[TITLE_ID]` in its name and every NCA encrypted (Need for
  Speed: Most Wanted), so neither its TITLE_ID nor its launch path can be
  read. One forwarder-sized package like that, beside a `switch/` folder or
  archive no game claims, alone in its release folder with no other game:
  that is a port (`sd_files.SEALED_FORWARDER` / `PORT_PART`) -- one card,
  installable, ticked together. Two such packages, or a real game beside
  them: NEEDS_REVIEW as before, never guessed.
- **Only `switch/`, only verbatim.** Every destination is `switch/<path as in
  the release>`; nothing else of a release (a README beside it) is copied to
  the card. A `switch` folder holding packages is a downloads category, one
  under `atmosphere/` belongs to a mod, one deeper than a single wrapper folder
  inside an archive is game data — none of them is SD content.
- **Nothing an archive ships silently stays behind.** A package or mod archive
  that also carries `switch/` gets one more job for it.
- **Bump `scanner.CLASSIFICATION_REVISION` whenever classification learns to
  recognise something new.** Unchanged files are otherwise never looked at
  again, and the change would not reach the very libraries it was made for.
- The forwarder's launch path is checked against the game's own SD files; a
  missing `.nro` is said on the card, not discovered on the console.
- DBI's installed list cannot confirm SD files (like mods): no "On Switch" for
  them, and DBI itself only refreshes that list when reopened on the console.
- **Thousands of small files over MTP must survive a console that stalls.**
  One timed-out write reopens the WPD session and retries that file; it does
  not move the rest of the job to the Shell (25x slower). Retry continues
  from what the previous attempt delivered, and a file already on the card
  with exactly the same bytes (read back, hashed) is done, not a conflict.

## A virtual amiibo is its folder: emuiibo

emuiibo (`atmosphere/contents/0100000000000352` + `switch/.overlays/emuiibo.ovl`)
answers games' amiibo requests from `emuiibo/amiibo/`, where a virtual amiibo
is a folder holding `amiibo.json` and `amiibo.flag`. The knowledge lives in
`switchagent/emuiibo.py`, the full picture in `docs/EMUIIBO.md`;
`tests/test_emuiibo.py` encodes each rule below against the real 766-amiibo
pack's layout.

- **Recognised by structure, never by name.** A release is its sysmodule's
  `exefs.nsp`; its version is the overlay's own NACP. An `.nsp` inside
  `atmosphere/contents/<id>/` is never a package to install.
- **Copied by what it is, nowhere else.** An AMIIBO manifest can only name
  `emuiibo/amiibo/...`; an EMUIIBO one only emuiibo's own three places.
  Checked when the manifest is built.
- **Decided per amiibo, never per file.** emuiibo rewrites an amiibo as soon
  as it is used, so bytes cannot say "already there": the same figure and
  UUID at the same place is the same amiibo, and it is left as it is, save
  data and all. A different one in that folder is left alone unless the
  conflict policy says override -- then its folder goes first, whole.
- **A collection that came with a game is part of it**: an "N amiibo" tag on
  the game's card, installed after the game when the game is selected. Owner
  by `[TITLE_ID]` in its name, else the one game of its release folder -- or,
  with several there, the one whose package lies directly in that folder.
  Otherwise nobody, never a guess. Unowned collections are not games: they
  are on the Amiibo tab, counted in one line under the grid. emuiibo's own
  release files and its PC tools are off the grid and shown nowhere -- the
  Amiibo page offers one button to install or update emuiibo, never a list
  of release files to pick from.
- **No Amiibo tab before emuiibo.** It exists once some console's last read
  has emuiibo, or SwitchAgent delivered it there since
  (`amiibo_views.amiibo_tab_visible`); until then `/amiibo` redirects to
  Add-ons, where emuiibo is installed. Installing amiibo onto a console
  without it offers emuiibo's download and install in the same confirmation
  -- ticked when the console is known to lack it, unticked when it was never
  read. A failed download queues nothing; installing without emuiibo is the
  user's untick, never a silent fallback.
- **The one thing ever removed from a console** is an amiibo folder under
  `emuiibo/amiibo/`, explicitly, from the Amiibo page: checked path by path,
  one object at a time, each deletion proven by a fresh listing, save data
  only with an explicit yes. Nothing else anywhere deletes anything.
- **Every MTP call from the worker thread, in slices.** Reading a console is
  a generator the worker advances between jobs; an install never waits
  behind a read of 800 folders.
- emuiibo's current release can be fetched from GitHub on an explicit click
  (`emuiibo_download.py`): only `emuiibo.zip` of XorTroll/emuiibo, only from
  GitHub's hosts, size and published SHA-256 checked, emuiibo by structure,
  into a Library folder -- then the ordinary queue. Nothing fetched is run.
- Its overlay menu is the Add-ons catalog's Ultrahand entry (it carries
  nx-ovlloader): installed with emuiibo when the console lacks a menu. A
  Tesla Menu already there does the same job and is never replaced behind
  anybody's back -- only an explicit Install of Ultrahand replaces it.

## Add-ons: a curated catalog SwitchAgent installs from

The Add-ons tab lists open-source Switch utilities from
`switchagent/web/addons/catalog.yaml`, in file order -- what each is, how it
works, how it gets onto a console, how to use it -- and installs them.
Entries are added by hand; the file's header lists every field, and
`tests/test_addons.py` refuses an entry the tab could not show truthfully,
or an install that could write where it must not. The rules live in
`switchagent/addons.py`, the installer in `web/addons_service.py`.

- **A beta feature, off by default.** Settings -> "Beta features" turns
  it on, after a dialog saying it is experimental (`preferences.json`
  `beta`). Off: no Add-ons tab, `/addons` goes to Settings, and
  `/api/addons/install` refuses everything but emuiibo and Ultrahand --
  emuiibo and amiibo are not beta; they are installed from the Amiibo page
  and Library's install confirmation instead.
- **Only what goes on the SD card as plain files, over MTP, while DBI
  runs.** Never the boot chain: an install block cannot even name
  `bootloader/`, Atmosphère's own files and config, payloads, `Nintendo/`,
  `emummc/`, DBI itself, or a whole shared folder (`switch/`,
  `atmosphere/contents/`). Old or unmaintained projects stay out too.
- **Written only to its `places`.** A release is the catalog's add-on when
  it holds all its `sign` files and nothing outside its places (an archive),
  or when the NACP inside names it (one .ovl/.nro) -- never by file name.
  The destination is asked of the catalog again when the manifest is built.
  An archive not laid out like the SD card says where its folders go
  (`layout:` -- NXMP's `nxmp/`, NooDS's bare `.nro`); what a Mac or
  Explorer adds (`__MACOSX/`, `.DS_Store`, `Thumbs.db`) is never copied.
- **For every console, not only kefir.** `kefir: true` only keeps
  SwitchAgent off what kefir updates on a console that runs kefir; on any
  other CFW setup the entry installs and updates as usual.
- **Apps and overlays first; system modules only when proven.** A module
  that does not suit the firmware can stop the console from booting, and
  firmware support cannot be read over MTP -- MissionControl, sys-con 2.x,
  SysDVR, ldn_mitm, PNGShot wait (docs/ADDONS-CANDIDATES.md).
- **A person's settings are never replaced** (`keep:` -- Fizeau's
  config.ini, SaltyNX's exceptions.txt): written only where missing.
  Everything else of the add-on is its own and is overwritten on update.
- **Install brings what it needs, all or nothing.** The add-on and every
  requirement the console lacks (FPSLocker -> Ultrahand, SaltyNX), from each
  project's own GitHub release (size, published SHA-256, recognised by
  structure), into the Library, then the ordinary queue together. One
  failed download queues nothing.
- **kefir keeps its own.** On a console running kefir (its updater app is
  on the card), what kefir ships (`kefir: true`) is shown but not installed
  from here -- two updaters would take turns overwriting it.
- **Updates: GitHub is asked at start and once a day** (`ReleaseChecker`,
  one plain GET per entry, nothing about the user in it; answers cached in
  `addon_releases.json`) -- only with the beta features on (off, nothing
  goes to the network), not again at a start within an hour of the last
  check, and stars only once a week: GitHub allows an unauthenticated
  client 60 requests an hour, shared with the app's own update check. An installed add-on older than its latest release
  gets "Update to X" -- its version from the console, or, where the console
  cannot say (no NACP, too big to read back), from what SwitchAgent itself
  last installed there; neither known: "Reinstall latest". What kefir
  updates gets no Update button. A check never downloads anything.
- A status line comes from the console's last read (file presence, the
  version inside each overlay/app) -- never a guess. A downloaded release
  is an ADDON Library item: off the game grid, counted in one line there.
- **Bump `scanner.CLASSIFICATION_REVISION`** when an install block that
  existing Library files could match is added or changed.
