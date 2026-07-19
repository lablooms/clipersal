# Changelog

All notable changes to Clipersal are documented here. See `ARCHITECTURE.md` for the
full design rationale behind each entry.

The format loosely follows [Keep a Changelog](https://keepachangelog.com/); this project
does not yet follow strict semantic versioning (still pre-1.0).

## [0.1.0] — 2026-07-19

### Added

- A tag-driven release workflow (`.github/workflows/release.yml`): pushing a `v*` tag
  builds the Windows installer + portable zip and both AppImages, then publishes the
  GitHub Release with this changelog's section for the tag as its notes. Tags with a
  pre-release suffix are published as pre-releases.

### Changed

- The update checker's version comparison now treats a stable release as newer than
  the same-numeric beta (`0.1.0` > `0.1.0-beta`, per semver), so beta installs are
  notified about the stable promotion.

### Fixed

- The update checker now queries the releases list instead of `/releases/latest`, which
  excludes pre-releases — it would never have found a release while only beta
  pre-releases exist.
- Windows: recovering from an exhausted auto-restart budget (RESUME after "Capture
  stopped") no longer fails with a `PermissionError` on `ffmpeg.log`.
- Windows: a second instance can no longer slip past single-instance detection when the
  PING check races — the IPC socket now binds exclusively (`SO_EXCLUSIVEADDRUSE`) before
  any slow startup work.
- Capture resilience: the background cleanup thread now survives unexpected errors
  (e.g. ffmpeg failing to relaunch) instead of silently stopping segment aging, and
  re-starting capture no longer spawns duplicate cleanup threads.
- Saving a nearly-full buffer no longer sporadically fails when the cleanup thread
  deletes the oldest segment mid-save; two near-simultaneous saves no longer race into
  the same output file.
- Saving from the main window or tray no longer freezes the UI for the remux duration
  or falsely reports failure after 5 s — saves run on a worker thread with a timeout
  matching the server, and real failures are surfaced in the window.
- Settings: a failing capture restart (e.g. a bogus forced encoder) no longer leaves
  capture silently stopped while STATUS reports RECORDING — the new setup is validated
  before the old session is touched, and the old capture keeps running on failure.
- Settings / first-run wizard: an invalid or half-recorded hotkey combo is rejected
  instead of silently leaving the hotkey dead on every launch.
- A hung or unrunnable ffmpeg during encoder/filter probing no longer crashes startup
  or escapes out of the Settings apply path.
- The auto-created temp buffer dir is deleted on exit — every run previously leaked up
  to a full buffer (~60 MB at defaults) into the system temp dir.
- Changing the clips folder now takes effect in the Clips tab, the Home recent-clips
  strip, and the tray's "Open clips folder" immediately, not only after a restart.
- Gallery: renaming a clip onto an existing clip's name is refused instead of silently
  overwriting it on Linux; names containing path separators are rejected instead of
  raising an error.
- Save toasts are destroyed after closing instead of accumulating one hidden widget
  per save.
- Linux: autostart `.desktop` files now quote the Exec line per the Desktop Entry
  spec — login autostart silently failed for install paths containing spaces.
- Linux: windows at negative coordinates (other viewports) no longer vanish from the
  window picker.

## [0.1.0-beta] — 2026-07-19

First public beta. Windows and Linux (X11) are supported; Wayland and macOS are not yet
(see "Known limitations" below).

### Added

- Continuous segmented screen capture with a rolling buffer and save-on-demand
  (concat, stream-copy — no re-encode), for Windows (`ddagrab`/`gdigrab`) and Linux X11
  (`x11grab`). Best-effort system-audio loopback capture where a compatible virtual
  audio device is present.
- Automatic hardware-encoder selection (NVENC → VAAPI/QSV → libx264 software fallback),
  verified with a real smoke-encode rather than a compile-time presence check alone.
- Local IPC socket (loopback-only) + a real global hotkey (Windows/Linux-X11) to trigger
  a save, pause/resume, or open the app — plus `clipersal-trigger`, a standalone CLI/DE-
  keybinding fallback for Wayland (or scripting on any platform).
- A persistent, OBS-style main window (Home / Clips / Settings / Logs) with a system
  tray icon; closing the window hides it to the tray rather than quitting.
- Capture target picker (Desktop / a specific Monitor / a single Window), a microphone
  picker mixed in alongside system audio, and named quality presets (Performance /
  Balanced / Quality, or Custom for a raw bitrate slider).
- Automatic restart if the ffmpeg capture process dies unexpectedly (bounded retry
  budget), surfaced as a distinct "Capture stopped" status rather than looking paused.
- An in-app clip gallery (thumbnails, rename/delete/reveal-in-folder), configurable
  filename templates, trim-before-save (save just the last N seconds), a clip-retention
  sweep, and a save-notification toast with a thumbnail preview and a bloom-style
  entrance animation.
- A first-run setup wizard, single-instance detection, rotating log files, and a
  launch-on-startup toggle (Windows Run key / Linux XDG autostart).
- A notify-only auto-update checker: checks GitHub Releases once at startup (rate-limited,
  toggleable in Settings) and shows a dismissible Home tab banner if a newer version is
  found — never downloads or installs anything automatically.
- A from-scratch visual identity ("Pollen Gold" palette, a hand-painted seed-puff/
  dandelion brand mark, decorative botanical accents on empty states) reflecting the
  Lablooms studio's flower theme.
- Packaged as standalone executables: `Clipersal.exe`/`Clipersal-Trigger.exe` (Windows,
  PyInstaller, onedir) and `Clipersal-x86_64.AppImage`/`Clipersal-Trigger-x86_64.AppImage`
  (Linux). ffmpeg itself is a system dependency, not bundled (see `ARCHITECTURE.md`
  for why).
- A Windows installer (`ClipersalSetup-<version>.exe`, built with Inno Setup): license
  page, per-user install with no admin elevation required, Start Menu shortcut, optional
  desktop icon, and a proper "Add or Remove Programs" entry with a working uninstaller.
- A GitHub Actions workflow that builds both Linux AppImages on a real Ubuntu runner
  (`.github/workflows/build-appimage.yml`), and a CI fix (`tests.yml` was watching
  pushes to a branch named "main" that doesn't exist on this repo, so its push trigger
  had never actually fired).

### Changed

- The entire GUI was rewritten from CustomTkinter/pystray to PySide6/Qt mid-development
  (general dissatisfaction with the old toolkit's look, plus a onefile-vs-onedir
  packaging fix for slow startup).
- **Dark mode was removed.** The app is light-themed only now — the sidebar's dark/light
  toggle is gone, and every color constant in `theme.py` collapsed from a `(light, dark)`
  tuple to a single flat value.

### Known limitations

- **Wayland**: no screen capture yet (needs xdg-desktop-portal + PipeWire — see
  `ARCHITECTURE.md`'s Wayland caveat). Detected and reported clearly, not silently broken.
  `clipersal-trigger` + a DE keybinding is the documented save-trigger workaround in the
  meantime, since a global hotkey can't be grabbed there either.
- **macOS**: not implemented yet (capture, launch-on-startup, and packaging are all
  Windows/Linux only so far).
- **Linux AppImage packaging**: the build is now verified end-to-end on real Linux CI,
  but runtime behavior on an actual desktop session (tray icon, a full capture/save
  cycle) hasn't been (see `ARCHITECTURE.md`'s "Packaging & distribution" section).
- No Linux installer/`.deb` package yet — the AppImage already covers "download and
  run" there with no install step needed.
