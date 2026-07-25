# Changelog

All notable changes to Clipersal are documented here. See `ARCHITECTURE.md` for the
full design rationale behind each entry.

The format loosely follows [Keep a Changelog](https://keepachangelog.com/); this project
does not yet follow strict semantic versioning (still pre-1.0).

## [0.1.0-lab.2] - 2026-07-25

### Added

- **The player is a real editor now**: the in-app player's trim section gains
  a **range-highlighted seek bar** (the selected region paints in accent with
  markers at both marks), **editable start/end time fields** (type exact times
  or step them, synced both ways with the playhead marks), **frame previews**
  at both cut points (grabbed off the UI thread, debounced), and a **Clear**
  button for the marks, on top of the existing one-click stream-copy export.
  "Trim…" opens this view paused and emphasized (accent-bordered trim card,
  `Trim — <filename>` window title); play actions are untouched.

### Fixed

- Opening the in-app player can never silently eat a click again: if the
  QtMultimedia backend fails to construct the dialog on some machine, the
  gallery falls back to the OS default player (and logs why) instead of doing
  nothing.

## [0.1.0-lab.1] - 2026-07-23

First public Lablooms release ("-lab" is the studio's pre-release series, like
"-dev", but ours). Windows and Linux are supported: X11 fully, Wayland
experimentally (see "Known limitations" below). Everything Clipersal is, in one
entry: the capture platform first, then the experience-and-creator feature set.

### Added

- **The core platform**: continuous segmented screen capture with a rolling buffer
  and save-on-demand (concat, stream-copy, no re-encode), for Windows
  (`ddagrab`/`gdigrab`) and Linux X11 (`x11grab`), with best-effort system-audio
  loopback where a compatible virtual audio device is present. Automatic
  hardware-encoder selection (NVENC → VAAPI/QSV → libx264), verified by a real
  smoke-encode. A loopback-only IPC socket + global hotkey (Windows/Linux-X11)
  for save/pause/resume, plus `clipersal-trigger`, a standalone CLI/DE-keybinding
  fallback for Wayland (or scripting). Automatic restart if the ffmpeg capture
  process dies (bounded retry budget), surfaced as "Capture stopped" rather than
  looking paused. A first-run setup wizard, single-instance detection, rotating
  log files, and launch-on-startup (Windows Run key / Linux XDG autostart).
- **The app shell**: a persistent, OBS-style main window (Home / Clips /
  Settings, with Logs as a Settings sub-tab) and a system tray icon; closing
  the window hides to the tray rather than quitting. Capture target picker
  (Desktop / a specific Monitor / a single Window), a microphone picker mixed
  in alongside system audio, and named quality presets (Performance / Balanced /
  Quality, or Custom for a raw bitrate slider). A from-scratch visual identity
  ("Pollen Gold" palette, a hand-painted seed-puff brand mark, botanical empty
  states) built around the Lablooms studio's flower theme.
- **Packaging**: standalone executables (`Clipersal.exe`/`Clipersal-Trigger.exe`
  on Windows, AppImages on Linux; ffmpeg itself is a system dependency, never
  bundled), a per-user Windows installer (no admin needed, proper uninstaller),
  and tag-driven GitHub Actions workflows that build everything on CI and
  publish releases with this changelog's section as their notes.

- **In-app video player**: double-click a clip (or right-click → **Play**) to watch it
  without leaving Clipersal: play/pause, seek, volume, and 0.5×/1×/1.5×/2× speed,
  built on QtMultimedia (shipped in the packaged builds; if the backend is
  unavailable the gallery falls back to the OS default player). The player has a
  built-in **trim export**: mark start/end from the playhead and export a stream-copy
  cut (instant, zero quality loss; the original is always kept, and the trimmed copy
  gets a unique `-trimmed` name). Also opens from the Home tab's recent-clips strip.
- Right-click any clip → **Export as GIF…**: pick start, duration
  (up to 30 s), frame rate, and width; a two-pass palette render produces a
  shareable `.gif` next to the clip.
- Right-click → **Compress…** re-encodes a clip to a smaller file
  (2.5-8 Mbps, optional 720p/480p downscale) using the same hardware encoder the
  capture uses, keeping the original untouched.
- **Gallery overhaul**: search-as-you-type filtering, sorting (newest/oldest/
  name/size, plus **Window A-Z** grouping), a **window filter** that groups clips
  by the app they were saved from (with per-window clip counts), a favorites-first
  option, a selection mode with select-all/none and confirmed batch delete, a
  right-click context menu (also behind a per-row "⋯" button) with every action
  (play, open, reveal, favorite, details, rename, GIF, compress, copy path,
  **copy filename**, delete), clip durations in the meta line (probed off the UI
  thread), per-clip **details and notes** (notes show as tooltips), drag-and-drop
  of clips straight out of the window, and a footer with the total clip count,
  size, and favorites count.
- **Gallery grid view**: a List/Grid switch in the Clips tab header swaps the
  classic rows for a thumbnail grid (cards with the same heart/⋯/context menu,
  double-click to play, selection checkboxes, and drag-out). Search, window
  filter, sorting, favorites-first, and the selection all apply identically in
  both views.
- **Favorites**: star a clip (♡/♥) to keep it. Favorites persist across launches in
  a sidecar file (`clips_dir/.clipmeta.json`, never config.json) and are exempt from
  both the retention sweep and the folder size cap.
- **Clips folder size cap**: Settings → Clips can cap the folder at 1-50 GB; after
  each save the oldest non-favorite clips are swept until the folder fits (the clip
  you just saved and all favorites are always protected).
- **Home dashboard**: the status card shows live session stats (uptime, buffer fill,
  buffer size on disk, the active encoder, and free space on the clips drive) fed by
  a new IPC `STATS` command (also available as `clipersal-trigger stats` for
  scripting). The pause button follows the real capture state, and the window title
  carries it too (`Clipersal — Paused` / `Clipersal — Capture stopped`).
- **Crash recovery + crash reports**: when ffmpeg burns through its restart budget
  (the "Capture stopped" state), a banner offers one-click "Restart capture", and a
  dialog asks whether you'd like to **send a crash report**: it opens a pre-filled
  GitHub issue in your browser (log tails + system facts) for you to review and edit
  before submitting. Nothing is ever sent automatically; an "Export zip" option
  (the diagnostics bundle) is right there too.
- **Low-disk warning**: a dismissible banner appears when the clips drive drops
  below 1 GiB free and clears itself above 1.5 GiB (hysteresis, no flapping).
- **Quick-save hotkeys**: two extra configurable global hotkeys, each with its own
  save duration (5-300 s, defaults 30 s and 60 s, disabled until bound), alongside
  the main save hotkey. One listener binds the whole combo map, and duplicates or
  invalid combos are rejected in Settings with a clear error.
- **Screenshot capture**: a `SCREENSHOT` IPC command (tray item, optional global
  hotkey, and `clipersal-trigger screenshot`) grabs the last frame of the newest
  finalized buffer segment into `screenshot-<date>-<time>.png` next to your clips.
  It deliberately reads the buffer instead of opening a second capture device: on
  Windows a concurrent ddagrab/gdigrab grab is platform-dependent, and on Wayland
  it's impossible without a new portal consent prompt; the frame is at most a couple
  of segment-lengths old, which is exactly the instant-replay model.
- **`{window}` filename templates**: clips can be named after the app you were using.
  The default template is now `{window}-{date}-{time}` (e.g.
  `Valorant-20260717-011351.mp4`). The active window title is read at save time
  (Windows/X11; sanitized, 40-char cap), falls back to `clip` when unavailable, and
  uses the captured window's own title in window-capture mode. Existing configs keep
  their saved template.
- **Frame rate and resolution-scale settings**: an FPS picker (15/24/30/60; the
  frame rate is now persisted, previously CLI-only) and a resolution scale
  (Native/1080p/720p) inserted into the ffmpeg filter chain. Both apply via the
  validate-then-restart capture path; at the Native/30 defaults the capture command
  is byte-identical to before.
- **Toast upgrade**: save toasts have "Open" (plays the clip with the OS default
  app) and "Show in folder" buttons, a duration+size meta line, and stacking.
  Consecutive saves pile upward instead of overlapping, and remaining toasts reflow
  when one closes.
- **Logs tab**: search and level filtering (All/INFO/WARNING/ERROR) over a deeper
  500-line tail, an auto-scroll toggle, a copy-to-clipboard button, and a one-click
  **diagnostics export**: a zip of the app logs, the ffmpeg log, the config, and a
  `system.txt` (OS, session type, versions, encoder, monitors) for bug reports.
- **Tab-based Settings**: Capture / Saving / Encoder / Clips / Appearance / About
  tabs replace the single scrolling panel. Every option is easier to find and
  nothing crowds at small window sizes. Combo boxes, spin boxes, and sliders no
  longer change value when you scroll past them (the scroll wheel never adjusts
  them; keyboard arrows still work when focused), and a **Reset to defaults**
  button restores every setting after a confirmation that spells out what resets.
- **"Check now" for updates**: the About tab shows the last check time and can force
  an immediate update check (bypassing the 24 h throttle, still notify-only).
- **Follow-system theme**: the appearance setting is now **System / Light / Dark**,
  defaulting to **System**: Clipersal starts in dark mode when your OS is in dark
  mode and vice versa (Windows `AppsUseLightTheme`, GNOME `color-scheme`; best-effort,
  other desktops fall back to light). An explicit dark choice in an older config is
  honored; the Pollen Gold dark variant itself is unchanged (warm espresso-brown,
  same gold accent family, applies live without a restart).
- **Desktop and microphone volume sliders** (0-200 %) in Settings → Capture,
  baked into the capture's audio mix as per-source `volume=` filter stages.
  Changes restart capture; at the 100 % defaults the ffmpeg command is
  byte-identical to before. Sliders disable with a hint when their source
  doesn't exist.
- **Experimental Wayland screen capture** (Linux): capture now works on Wayland
  sessions via xdg-desktop-portal ScreenCast + PipeWire: the desktop's own consent
  dialog asks which screen (or window) to share on first launch, the choice is
  remembered with a rotating restore token so re-launches and crash restarts are
  silent, and revoking the share from the desktop's indicator stops capture cleanly.
  Frames reach ffmpeg as rawvideo through a GStreamer bridge
  (`gst-launch-1.0` + `gstreamer1.0-pipewire` required on Wayland only; no released
  ffmpeg can read PipeWire directly). Unit-tested end-to-end against fakes; pending
  verification on a real Wayland session (checklist in `ARCHITECTURE.md`). The global
  hotkey still doesn't exist on Wayland; `clipersal-trigger` + a DE keybinding
  remains the save trigger. New runtime dependency: `jeepney` (pure-Python D-Bus).
- **Window keyboard shortcuts**: Ctrl+S save now, Ctrl+Shift+S save last 30 s,
  Ctrl+P pause/resume, F5 refresh gallery, Ctrl+, open Settings, Ctrl+1-4 switch
  tabs. (Global triggers still all go through the IPC boundary.)
- Optional **save sound**: a beep on every successful save (Settings → Saving),
  audible confirmation when you're saving via hotkey with the window hidden.
- **Lablooms identity + Support**: the sidebar footer links to the studio and has a
  "♥ Support" button (opens the project page; star it if Clipersal helps you).
- **Window and taskbar icon**: the main window (and every dialog) now carries the
  Clipersal icon in the title bar and taskbar, including a proper Windows App User
  Model ID so taskbar grouping/pinning works.
- The Windows installer now offers to install FFmpeg (opt-out, recommended) via
  winget at the end of setup. The long-standing "ffmpeg is never bundled"
  licensing rule is untouched, since it's your own package manager doing the
  install with your consent. Without winget it points you at the download page
  instead.

### Changed

- **License: MIT → GPL-3.0-only.** See `LICENSE`.
- **Settings now save automatically**: the Save button is gone. Every field change
  applies after a short debounce (sliders apply on release, never mid-drag; hotkeys
  apply when the recorder finishes), confirmed by an inline "Saved ✓". A change that
  fails to apply shows the error and returns the field to its previous value.
- The default filename template changed from `clip-{date}-{time}` to
  `{window}-{date}-{time}`; a persisted copy of the old default (what everyone
  who never customized it has) is migrated automatically on launch; custom
  templates are untouched.
- Gallery rows are slimmer: Play, ♥, and a "⋯" menu instead of five text buttons.
  Every action is one click further at most, and rows stay readable at small sizes.
  The old separate trim dialog is gone; the in-app player's playhead trim replaces it.

### Fixed

- **UI polish**: labels no longer paint a visible background box behind their
  text; backgrounds are scoped to the containers that own a surface (the main
  window, dialogs, menus, cards, inputs) in both light and dark mode. A follow-up
  theming audit removed the remaining unthemed boxes (scroll viewports, the player
  surface, hardcoded tray/status colors), unified font sizes on a proper type
  scale, and themed the last native-looking controls (spin-box steppers, combo
  drop-downs, checkbox indicators, segmented-control tracks). The first-run
  wizard's dead bottom space is gone too.
- **No more alert sounds**: message boxes (delete confirms, rename warnings,
  reset confirmation, the crash prompt) no longer play the Windows system alert
  sound, and GIF/compress exports report success inline inside their dialog
  instead of closing it with a "dudun~" popup.
- The Home recent-clips strip now refreshes itself when clips are deleted, renamed,
  or created from the gallery (and has its own Refresh button).
- Buttons and rows no longer truncate or crowd at the minimum window size; the
  Home status line elides the clips path from the middle instead of clipping it.
- Failed or timed-out saves/trims no longer leave a partial, broken `.mp4` in
  the gallery.
- A hand-edited config file with wrong-typed values (e.g. text where a number
  belongs) is now ignored entry-by-entry instead of crashing startup.
- Filename templates rendering to a Windows reserved device name (`NUL`,
  `CON`, …) fall back to `clip` instead of "successfully" saving into the void.
- The update checker no longer suppresses retries for 24 h after one transient
  network error, and no longer lets the banner flicker off for a launch.
- IPC error responses with embedded newlines no longer arrive truncated
  client-side; a foreign non-UTF-8 service on the port can't crash the
  single-instance probe.
- Linux: window/monitor enumeration can't crash on non-ASCII titles under
  C locales; minimized windows are excluded from the picker (they captured as
  black frames); sticky ("always on visible workspace") windows are no longer
  dropped from the picker along with the desktop background.
- Windows: all `user32` ctypes calls use proper 64-bit prototypes (a latent
  handle-truncation landmine).
- The tray re-syncs its pause state when its menu opens: pausing from the
  window, another trigger, or a crash no longer leaves it showing a stale
  "Recording" state.
- Closing the first-run wizard or hiding the window while recording a hotkey
  no longer leaks a live OS-wide keyboard listener for the rest of the process.
- Clip lists and thumbnails no longer error when a clip vanishes mid-listing;
  thumbnail writes are atomic (a corrupt JPEG can't get permanently cached);
  the status dot no longer accumulates dead animation objects per save.
- Settings: out-of-range config values display clamped consistently (what's
  shown is what's saved), and the launch-on-startup toggle reconciles with the
  real registration state instead of a stale persisted belief.

### Fixed (post-release)

- **Logs moved into Settings**: the top-level Logs page is now the Settings
  tab widget's last sub-tab (… / Appearance / About / Logs), so the sidebar
  reads just Home / Clips / Settings. Every old entry point still lands on the
  log viewer: Ctrl+4, the crash banner's and the Settings footer's "View
  logs", the tray's View logs, and `clipersal-trigger logs` all open
  Settings → Logs now.
- **Selection mode no longer pushes the clip list down**: the selection action
  bar ("N selected", All/None, Delete selected, Done) takes over the Clips
  tab's fixed footer strip in place of the usual count/size line, instead of
  appearing above the list and shoving every row down when it opened.
- **Typography normalized on one scale** (documented at the top of
  `theme.py`): H1 18 page titles, H2 14 card/section titles, BODY 12 for all
  interactive text (installed as the application font; everything sat at the
  platform default before), HINT 11 for secondary text, MONO 11 for code-ish
  readouts. Bold is reserved for page/card titles, the status word, clip
  names, and primary buttons; settings field labels, nav buttons, segmented
  controls, and value badges no longer invent their own weights/sizes.
- **Spin-box steppers and scrollbars remade**: the up/down steppers are now
  custom themed ▲/▼ buttons built into the inputs (no more native grey
  arrows), and scrollbars are slim 8px pills on a transparent track (no
  buttons, no page fill, accent while dragging) in both orientations.
- **"Trim…" opens the player paused**: it used to start playing immediately,
  which read as "just a video player" and made the trim marks impossible to
  land on a moving playhead. Play actions still autoplay.
- **Screenshots can't return a ghost path**: ffmpeg can exit 0 without
  decoding a single frame (seen on QSV-encoded segments with `-sseof`), so the
  grab now verifies the PNG actually exists and falls back to the next seek
  strategy instead of "successfully" writing nothing. Found by the
  end-to-end smoke test (record → save → trim → screenshot against a real
  ffmpeg, 11/11 checks).
- **Deep-review bug pass** (each with a regression test):
  - cancelling a hotkey recording with a key held no longer leaves a partial
    combo behind: the autosave was rebinding the save hotkey to a bare
    `<ctrl>` that fired on every ctrl press;
  - a failed launch-on-startup registration now blocks the whole settings
    apply (nothing mutated, nothing persisted) instead of applying everything
    else and then erroring, which made the autosave roll back genuinely-saved
    values;
  - IPC commands landing before the main window exists no longer die on a
    `NameError`; they answer "main window unavailable" properly;
  - the tray's pause/resume no longer blocks the GUI thread on a multi-second
    IPC call (now a worker, like every other trigger), and can't be inverted
    by a concurrent status re-sync;
  - hotkey-triggered saves/screenshots use the 70 s save timeout instead of
    the 5 s default, so a slow-but-successful save no longer logs as failed;
  - a capture restart that fails during settings apply (e.g. a cancelled
    Wayland share prompt) rolls the config back and revives the old session
    instead of leaving capture dead with config half-mutated; applying
    capture settings while CRASHED now actually restarts capture;
  - a read-only config dir reports "could not save settings" (settings tab
    and first-run wizard) instead of raising an uncaught exception.

### Known limitations

- **Wayland**: capture works via xdg-desktop-portal ScreenCast + PipeWire
  (unit-tested, pending real-session verification; checklist in
  `ARCHITECTURE.md`). The global hotkey doesn't exist there; use
  `clipersal-trigger` + a DE keybinding as the save trigger.
- **macOS**: not implemented yet (capture, launch-on-startup, and packaging are
  all Windows/Linux only so far).
- **Linux AppImage packaging**: the build is verified end-to-end on CI, but
  runtime behavior on an actual desktop session (tray icon, a full
  capture/save cycle) hasn't been; see `ARCHITECTURE.md`.
- No Linux installer/`.deb` package yet; the AppImage already covers
  "download and run" with no install step.
