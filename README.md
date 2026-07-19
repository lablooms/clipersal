# Clipersal

*Catch the moment you bloomed.*

A small cross-platform instant-replay / rolling screen-capture buffer tool, in the spirit of
NVIDIA Instant Replay or OBS's Replay Buffer — continuously capture the screen in the
background, and save the last N seconds to a file on demand. Part of **Lablooms**, a
studio of open-source apps ("One lab, everything blooms.").

See `ARCHITECTURE.md` for design rationale and `CHANGELOG.md` for a summary of what's
shipped so far.

## Status

**Beta (0.1.0-beta)**. Continuous segmented capture + rolling buffer + concat-to-save,
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
  see `ARCHITECTURE.md` for the full reasoning, and what happens if it's missing)

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
**Pause**/**Resume**, **Save last 30s**, and **Save now** buttons, plus a strip of your
most recent clips with thumbnails.

## System tray

A tray icon shows recording status (green = recording, grey = paused). Clicking the icon
itself opens/focuses the main window (like OBS); right-click for the full menu: **Open
Clipersal**, **Save now**, **Save last 30s**, **View clips**, **Open clips folder**,
**Pause capture** / **Resume capture**, **Settings**, **View logs**, and **Quit**. Disable
the icon with `--no-tray` (e.g. headless, or when scripting via IPC only) — the main
window still works via `clipersal-trigger show` / `settings` / `gallery` / `logs` even
with the tray disabled.

## Settings

The **Settings** tab (main window, or `clipersal-trigger settings`) is a two-column
PySide6 panel with a warm gold (Pollen Gold) accent, grouped into cards:

- **Capture** — buffer length; a **Capture target** picker (Desktop / Monitor / Window —
  Monitor only appears on a genuine multi-monitor setup, Window lists currently open
  windows with a Refresh button); a **Microphone** picker (mixed in alongside system
  audio, only shown if a real input device was found); and a **Quality preset**
  (Performance / Balanced / Quality, or Custom to reveal the raw bitrate slider)
- **Save & hotkey** — clips folder (Browse button), the hotkey (click **Record** and
  press your combo, or type it directly — both stay available), and **Launch on startup**
- **Encoder** — an Auto-detect toggle that reveals a manual NVENC/VAAPI/QSV/libx264
  picker when switched off
- **Clip management** — a filename template (`{date}`, `{time}`, `{datetime}`
  placeholders) and a "keep clips for" retention slider (0 = forever)

Saving shows a brief inline "Settings saved." confirmation and writes to a config file
(printed in the startup banner as `settings:`, normally
`%APPDATA%\Lablooms\Clipersal\config.json` on Windows or
`~/.config/Lablooms/Clipersal/config.json` on Linux) and applies immediately where
possible: buffer length, clips folder, filename template, and retention all take effect
right away; a hotkey change rebinds the global hotkey; a bitrate, quality preset,
encoder, capture target, or microphone change briefly restarts capture (the buffer is
preserved). CLI flags always override a saved value if both are given.

If ffmpeg itself dies unexpectedly mid-capture (a driver hiccup, etc.), Clipersal
restarts it automatically, up to 5 times within a minute — beyond that it gives up and
the Home tab's status dot shows "Capture stopped -- see Logs" until you click
**Resume** (or run `clipersal-trigger resume`) to try again.

## Clip gallery, trimmed saves, and the save toast

The **Clips** tab (tray's **View clips**, or `clipersal-trigger gallery`) lists every
saved clip with a thumbnail, saved-at date, and size, plus Open / Reveal-in-folder /
Rename / Delete actions. Thumbnails are generated on demand with a single ffmpeg
frame-grab per clip and cached in `clips_dir/.thumbnails`.

Every successful save shows a small toast in the bottom-right corner with a thumbnail
preview — click it to jump straight to the clips folder, or ignore it and it dismisses
itself after a few seconds.

To save just part of the buffer instead of all of it, use `--trim` (or the tray's
**Save last 30s**):

```sh
clipersal-trigger save --trim 30   # save just the last 30 seconds
```

Saved clips older than the **clip retention** setting (`--clip-retention-days`, default
`0` = keep forever) are swept away automatically after each save.

## First run, logs, and launching automatically

The very first time Clipersal runs (no config file yet), a small wizard walks you
through picking a clips folder and a save hotkey instead of silently applying defaults.
Skipping it is fine -- it still saves your current settings so it won't ask again.

Launching a second copy while one is already running shows a friendly "already running"
message instead of a confusing socket error, and never starts a second capture session.

The **Logs** tab (tray's **View logs**, or `clipersal-trigger logs`) shows the tail of the
log file, which rotates next to the config file (printed in the startup banner as
`logs:`) -- useful since a packaged, windowed build has no console to print to.

**Settings → Save & hotkey** has a **Launch on startup** toggle (Windows: a per-user
`Run` registry entry; Linux: a `~/.config/autostart/*.desktop` file). Not yet available
on macOS.

## Updates

Once at startup, Clipersal can check GitHub Releases for a newer version and show a
dismissible banner on the Home tab if one's found — notify-only, it never downloads or
installs anything for you. Toggle it off any time in **Settings → Save & hotkey → Check
for updates automatically**.
