# Clipersal

*Catch the moment you bloomed.*

A small cross-platform instant-replay / rolling screen-capture buffer tool, in the spirit of
NVIDIA Instant Replay or OBS's Replay Buffer — continuously capture the screen in the
background, and save the last N seconds to a file on demand. Part of **Lablooms**, a
studio of open-source apps ("One lab, everything blooms.").

See `ARCHITECTURE.md` for design rationale and `CHANGELOG.md` for a summary of what's
shipped so far.

## Status

**0.1.0**. Continuous segmented capture + rolling buffer + concat-to-save,
triggered over a local IPC socket via a real global hotkey (Windows / Linux-X11), the
`clipersal-trigger` script (Wayland fallback), or the system tray icon. Capture can
target the whole desktop, a specific monitor, or a single window, with an optional
microphone mixed in alongside best-effort system-audio loopback, a named quality preset
(Performance / Balanced / Quality / Custom), and automatic restart if ffmpeg crashes
unexpectedly. A persistent main window (Home / Clips / Settings / Logs, like OBS) hosts
everything — Settings (buffer length, clips folder, capture target, microphone, quality
preset, hotkey, encoder, filename template, clip retention, launch-on-startup, update
checks), an in-app clip gallery with open/reveal/rename/delete actions, and a log viewer.
Saves can be trimmed to just the last N seconds, and a save-notification toast pops up
with a thumbnail preview. A first-run wizard greets a fresh install, a second launch
detects the already-running instance instead of failing confusingly, logs rotate to a
file next to the config, the app can register itself to launch at login, and it can
optionally check GitHub Releases for a newer version at startup (notify-only — see
"Updates" below). Packaged as standalone executables for Windows (PyInstaller) and Linux
(AppImage) -- see "Packaging" below.

## Requirements

- Python >= 3.10 (not needed if you're running one of the packaged executables below)
- [ffmpeg](https://ffmpeg.org/) installed and on `PATH` (not bundled, to keep the
  download small and avoid pulling in ffmpeg's GPL/nonfree licensing obligations --
  see `ARCHITECTURE.md` for the full reasoning, and what happens if it's missing).
  The Windows installer offers to install FFmpeg for you during setup (via `winget`,
  as an opt-out task on the "Additional setup" page; skipped entirely when FFmpeg is
  already present) — otherwise the ffmpeg site linked above has Windows builds.
- **Linux Wayland sessions only**: GStreamer with the PipeWire plugin
  (`gstreamer1.0-pipewire` on Debian/Ubuntu, `gstreamer1-plugin-pipewire` on Fedora) —
  Wayland capture goes through xdg-desktop-portal + PipeWire, and since no released
  ffmpeg can read PipeWire, frames reach ffmpeg through a GStreamer bridge. X11 and
  Windows don't need it. Wayland support is experimental (see `ARCHITECTURE.md`'s
  Wayland caveat); a global hotkey still isn't possible there — use
  `clipersal-trigger` bound to a desktop-environment shortcut, as below.

## Packaging

Prebuilt executables need only ffmpeg on `PATH` -- no Python required:

- **Windows**: a portable `Clipersal.exe` + `Clipersal-Trigger.exe`, or `ClipersalSetup.exe`
  — a normal installer (Start Menu shortcut, optional desktop icon, "Add or Remove
  Programs" entry) for anyone who'd rather not manage a folder by hand
- **Linux**: `Clipersal-x86_64.AppImage` + `Clipersal-Trigger-x86_64.AppImage`
  (`chmod +x` and run, no install step)

To build them yourself:

```sh
pip install -e ".[build]"
pyinstaller packaging/clipersal.spec --clean        # Windows (portable build)
iscc packaging/clipersal_installer.iss              # Windows installer (needs Inno Setup)
./packaging/linux/build_appimage.sh                 # Linux
```

## Running from source

```sh
pip install -e .
clipersal --clips-dir ~/Videos/Clipersal --buffer-seconds 60
```

Press **Ctrl+Alt+R** (default, override with `--hotkey`, or set it in Settings by
clicking Record and pressing your own combo) to save the current buffer as a clip. Press
**Ctrl+C** to quit.

On Wayland, where no app can register a real global hotkey, bind a desktop-environment
custom keyboard shortcut to `clipersal-trigger save` instead (see `ARCHITECTURE.md`'s
Wayland caveat). The same command works everywhere for scripting or testing a save
without touching the keyboard:

```sh
clipersal-trigger save              # trigger a save
clipersal-trigger save --trim 30    # save just the last 30 seconds of the buffer
clipersal-trigger pause             # pause capture
clipersal-trigger resume            # resume capture
clipersal-trigger status            # RECORDING or PAUSED
clipersal-trigger show              # open/focus the main window
clipersal-trigger settings          # open the main window's Settings tab
clipersal-trigger gallery           # open the main window's Clips tab
clipersal-trigger logs              # open the main window's Logs tab
clipersal-trigger ping              # health check
clipersal-trigger quit              # ask a running instance to shut down
```

## Main window

Clipersal has one persistent main window, the same shape as OBS: a sidebar with **Home**,
**Clips**, **Settings**, and **Logs**. It opens on launch, and closing it (the ✕ button)
just hides it back to the tray rather than quitting — use the tray's **Quit** (or
`clipersal-trigger quit`) to actually exit. If you disable the tray (`--no-tray`), closing
the window quits instead, since there'd be no way to bring it back.

**Home** shows a status card (recording/paused, buffer length, clips folder) with
**Pause capture**/**Resume capture** (the button follows the real state) and **Save
now** buttons, a live stats line (uptime, buffer fill,
buffer size on disk, active encoder, free disk space), plus a strip of your most
recent clips with thumbnails — click one to play it in the built-in player, or use
the strip's **Refresh** button to re-read the folder. If ffmpeg's auto-restart budget
runs out, a banner offers one-click **Restart capture**; another banner warns when the
clips drive drops below 1 GiB free. The window title carries the state too
("Clipersal — Paused" / "Clipersal — Capture stopped"). The sidebar footer links out
to **Lablooms** (the studio) and **♥ Support** (the project's GitHub repo).

Window keyboard shortcuts: **Ctrl+S** save now, **Ctrl+Shift+S** save last 30 s,
**Ctrl+P** pause/resume, **F5** refresh the gallery, **Ctrl+,** open Settings,
**Ctrl+1–4** switch tabs.

## System tray

A tray icon shows recording status (green = recording, grey = paused). Clicking the icon
itself opens/focuses the main window (like OBS); right-click for the full menu: **Open
Clipersal**, **Save now**, **Save last 30s**, **Take screenshot**, **View clips**,
**Open clips folder**, **Pause capture** / **Resume capture**, **Settings**,
**View logs**, and **Quit**. Disable
the icon with `--no-tray` (e.g. headless, or when scripting via IPC only) — the main
window still works via `clipersal-trigger show` / `settings` / `gallery` / `logs` even
with the tray disabled.

## Settings

The **Settings** tab (main window, or `clipersal-trigger settings`) is organized into
tabs with a warm gold (Pollen Gold) accent:

- **Capture** — buffer length; a **Capture target** picker (Desktop / Monitor / Window —
  Monitor only appears on a genuine multi-monitor setup, Window lists currently open
  windows with a Refresh button); a **Microphone** picker (mixed in alongside system
  audio, only shown if a real input device was found); desktop/microphone volume
  sliders; a **Quality preset** (Performance / Balanced / Quality, or Custom to reveal
  the raw bitrate slider); a **Frame rate** picker (15/24/30/60); and a **Resolution
  scale** (Native/1080p/720p) for smaller files at the cost of sharpness
- **Saving** — clips folder (Browse button), the hotkey (click **Record** and
  press your combo, or type it directly — both stay available), two **quick-save
  hotkeys** with their own durations (save just the last N seconds with one press),
  an optional **screenshot hotkey**, an optional **save sound** (a beep on every
  successful save — useful when saving via hotkey with the window hidden), and
  **Launch on startup**
- **Encoder** — an Auto-detect toggle that reveals a manual NVENC/VAAPI/QSV/libx264
  picker when switched off
- **Clips** — a filename template (`{window}`, `{date}`, `{time}`, `{datetime}`
  placeholders — the default `{window}-{date}-{time}` names clips after the app you
  were using), a "keep clips for" retention slider (0 = forever), and a **max folder
  size** cap (0 = unlimited; oldest non-favorite clips are swept first)
- **Appearance** — theme (System / Light / Dark; System follows your OS dark-mode
  setting and is the default)
- **About** — version, license, the update checker (with a **Check now** button and
  last-checked time), and project links

Combo boxes, spin boxes, and sliders never change value when you scroll past them
(the scroll wheel never adjusts them — keyboard arrows still work when focused) —
and a **Reset to defaults** button in the footer restores every setting after a
confirmation.

There's no Save button — **every change saves automatically** (a short debounce lets
sliders and typing settle first), confirmed by a brief inline "Saved ✓". If a change
can't be applied, the error shows inline and the field returns to its previous value.
Saving writes to a config file
(printed in the startup banner as `settings:`, normally
`%APPDATA%\Lablooms\Clipersal\config.json` on Windows or
`~/.config/Lablooms/Clipersal/config.json` on Linux) and applies immediately where
possible: buffer length, clips folder, filename template, and retention all take effect
right away; a hotkey change rebinds the whole global-hotkey map; a bitrate, quality
preset, encoder, capture target, microphone, frame rate, or resolution-scale change
briefly restarts capture (the buffer is preserved). CLI flags always override a saved
value if both are given.

If ffmpeg itself dies unexpectedly mid-capture (a driver hiccup, etc.), Clipersal
restarts it automatically, up to 5 times within a minute — beyond that it gives up and
shows a "Capture stopped" state with a one-click **Restart capture** banner, plus a
dialog offering to **send a crash report** (a pre-filled GitHub issue opens in your
browser — you review and edit everything before it's submitted; nothing is ever sent
automatically) or to export the diagnostics zip instead.

## Clip gallery, player, exports, and the save toast

The **Clips** tab (tray's **View clips**, or `clipersal-trigger gallery`) lists every
saved clip with a thumbnail, saved-at date, size, and duration. A **List/Grid** switch
in the header swaps the classic rows for a thumbnail grid — every feature below works
identically in both. Each row/card shows a favorite **♥** and a **⋯** button (rows
also get **Play**) — the ⋯ (or a right-click anywhere on the clip) opens the full
action menu: Play / Open / Reveal in folder / Favorite /
Details / Rename / Trim / Export as GIF / Compress / Copy path / Copy filename / Delete.
A search box filters by name; a **window filter** groups clips by the app they were
saved from; clips can be
sorted by date, name, size, or window; a selection mode adds checkboxes for confirmed
batch deletes; the footer tally shows the clip count, disk usage, and favorites count;
and clips can be
**dragged straight out** of the gallery into Explorer, a chat, or an editor.
Thumbnails are generated on demand with a single ffmpeg frame-grab per clip and cached
in `clips_dir/.thumbnails`.

**Double-click a clip to play it in-app** (or single-click a recent clip on Home) — a
real player with seek, volume, and
0.5×–2× speed, plus a built-in trim export (mark start/end from the playhead, export
a lossless stream-copy cut; the menu's **Trim…** opens the same player). From the
right-click menu you can also **Export as GIF…**
(start/duration/frame rate/width — great for sharing moments) and **Compress…**
(re-encode to a smaller file with optional 720p/480p downscale). The original clip is
always kept. **Details…** shows the clip's metadata and lets you attach a **note**
(shown as a tooltip in the list).

Clips can be marked as **favorites** (♡/♥ per row, or from the context menu) — a
"Favorites first" toggle floats them to the top, and favorites are exempt from both
the retention sweep and the folder size cap. Favorites and notes live in a small
sidecar file, `clips_dir/.clipmeta.json`, not in the app config.

Every successful save shows a small toast in the bottom-right corner with a thumbnail
preview, the clip's duration and size, and **Open** (play with the default app) /
**Show in folder** buttons — consecutive saves stack upward instead of overlapping.
Clicking elsewhere on the toast jumps straight to the clips folder, or ignore it and
it dismisses itself after a few seconds.

**Screenshots**: **Take screenshot** (tray menu, an optional global
hotkey, or `clipersal-trigger screenshot`) grabs the most recent frame from the
capture buffer into `screenshot-<date>-<time>.png` next to your clips — no second
capture device is opened, so it works everywhere capture does, and the frame is at
most a couple of seconds old.

To save just part of the buffer instead of all of it, use `--trim` (or the tray's
**Save last 30s**, the **Ctrl+Shift+S** shortcut, or a quick-save hotkey):

```sh
clipersal-trigger save --trim 30   # save just the last 30 seconds
```

Saved clips older than the **clip retention** setting (`--clip-retention-days`, default
`0` = keep forever) are swept away automatically after each save — favorited clips are
always kept. You can also cap the **total folder size** (Settings → Clips,
1–50 GB, default unlimited): when a save pushes the folder over the cap, the oldest
non-favorite clips are swept until it fits again.

## First run, logs, and launching automatically

The very first time Clipersal runs (no config file yet), a small wizard walks you
through picking a clips folder and a save hotkey instead of silently applying defaults.
Skipping it is fine -- it still saves your current settings so it won't ask again.

Launching a second copy while one is already running shows a friendly "already running"
message instead of a confusing socket error, and never starts a second capture session.

The **Logs** tab (tray's **View logs**, or `clipersal-trigger logs`) shows the tail of
the log file, which rotates next to the config file (printed in the startup banner as
`logs:`) -- useful since a packaged, windowed build has no console to print to. It has
search and level filters, an auto-scroll toggle, a copy button, and an **Export
diagnostics…** button that bundles the app logs, the ffmpeg log, the config file, and
a system summary (OS, session type, versions, encoder, monitors) into a single zip for
bug reports.

**Settings → Saving** has a **Launch on startup** toggle (Windows: a per-user
`Run` registry entry; Linux: a `~/.config/autostart/*.desktop` file). Not yet available
on macOS.

## Updates

Once at startup, Clipersal can check GitHub Releases for a newer version and show a
dismissible banner on the Home tab if one's found — notify-only, it never downloads or
installs anything for you. The Settings row also shows when the last check happened
and has a **Check now** button for an immediate re-check. Toggle it off any time in
**Settings → About → Check for updates automatically**.

## License

Clipersal is free software licensed under the
[GNU General Public License v3.0](LICENSE) (GPL-3.0-only).
