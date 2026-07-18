# Changelog

All notable changes to Clipersal are documented here. See `ARCHITECTURE.md` for the
full design rationale behind each entry.

The format loosely follows [Keep a Changelog](https://keepachangelog.com/); this project
does not yet follow strict semantic versioning (still pre-1.0).

## [0.1.0-beta] — Unreleased

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
- **Linux AppImage packaging**: written to documented conventions and reviewed by hand,
  but not yet run end-to-end on a real Linux machine — treat it as unverified before
  trusting it as a release process (see `ARCHITECTURE.md`'s "Packaging & distribution"
  section).
- No installer (Windows or Linux) and no `.deb` package yet — a portable exe/AppImage plus
  the first-run wizard was judged sufficient for a beta of a small background tool.
