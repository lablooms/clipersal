# AGENTS.md

Guidance for coding agents working in this repository. Assumes no prior knowledge
of the project. Deeper design rationale lives in `ARCHITECTURE.md` (the "why" behind
non-obvious decisions); `README.md` covers user-facing usage and `CHANGELOG.md` tracks
what has shipped.
Keep those files in sync when you change behavior they describe.

## Project overview

**Clipersal** — "Catch the moment you bloomed." Part of **Lablooms**, a studio of
open-source apps. A cross-platform instant-replay / rolling screen-capture buffer tool
(same idea as NVIDIA Instant Replay or OBS's Replay Buffer): one long-running ffmpeg
process continuously captures the screen into short segment files, a background thread
ages out segments older than the configured buffer length, and a save trigger remuxes
the current segments into an `.mp4` clip without interrupting capture.

- Package name: `clipersal` (src layout, `src/clipersal/`). Version `0.1.0-beta`
  (`src/clipersal/__init__.py`, kept in sync with `pyproject.toml` and
  `packaging/clipersal_installer.iss` by hand).
- Status: beta. Supported targets are **Windows** and **Linux X11**. Wayland screen
  capture is deliberately not implemented (detected, logged, and exited cleanly —
  needs the xdg-desktop-portal ScreenCast + PipeWire path, a future phase). macOS
  capture is not started.
- Runtime requirement beyond Python: **ffmpeg (and ffprobe) installed and on `PATH`**.
  ffmpeg is a system dependency — never bundled, never pip-installed — for size,
  licensing (GPL/nonfree builds), and maintenance reasons. `ffmpeg_utils.find_ffmpeg()`
  fails fast with an actionable message when it's missing.

## Tech stack

- **Python >= 3.10**, hatchling build backend (`pyproject.toml`).
- Only two third-party runtime dependencies, both declared in `pyproject.toml`:
  - `PySide6` — the entire GUI layer (main window, Settings, clip gallery, save toast,
    tray icon via `QSystemTrayIcon`, first-run wizard).
  - `pynput` — global hotkey binding (Windows/X11) and the press-the-combo hotkey
    recorder.
- Everything else is intentionally **stdlib-only**: `json` config persistence,
  `socket`/`socketserver` IPC, `ctypes`/`winreg`/`subprocess` platform probing
  (monitor/window enumeration, autostart registration), `urllib` for the update check.
  Do not add a new third-party dependency for something the stdlib plus an OS API can
  do — that is an established, deliberate convention here. A later Wayland phase is expected to add `dbus-next`/`jeepney`.
- GUI modules carry a `_qt` suffix; the old CustomTkinter/pystray modules were deleted
  in the PySide6 migration, not kept side by side.

## Setup, build, and test commands

```sh
pip install -e .                 # editable install for development
pip install -e ".[test]"         # + pytest
pip install -e ".[build]"        # + pyinstaller / pyinstaller-hooks-contrib

pytest                                     # full suite (testpaths = tests)
pytest tests/test_capture.py               # one file
pytest tests/test_capture.py -k name       # one test

clipersal --clips-dir ~/Videos/Clipersal --buffer-seconds 60   # run from source
clipersal-trigger save|pause|resume|status|show|settings|gallery|logs|ping|quit

pyinstaller packaging/clipersal.spec --clean   # Windows: dist/Clipersal/ + dist/Clipersal-Trigger.exe
iscc packaging/clipersal_installer.iss         # Windows installer (Inno Setup 6) -> dist_installer/
./packaging/linux/build_appimage.sh            # Linux AppImages (must run on Linux)
```

There is **no configured linter or formatter** (no ruff/flake8/black/mypy config in the
repo). Don't invent lint commands that don't exist; match the surrounding code style
instead.

## Architecture in brief

```
ffmpeg (one continuous process) --> buffer_dir/seg-<timestamp>.ts  (rolling window)
   ^ background cleanup thread deletes segments older than buffer_seconds
   | on save trigger:
   +--> concat demuxer, stream copy (-c copy) --> clips_dir/clip-<...>.mp4
```

- **Capture** (`capture.py`, `SegmentedCapture`): runs ffmpeg once for the whole
  session; saving never stops it. Segments are MPEG-TS with `-strftime 1` timestamped
  names; a cleanup thread enforces `buffer_seconds` in plain Python (do not switch to
  ffmpeg's `-segment_wrap` — see `ARCHITECTURE.md`). Also auto-restarts ffmpeg on
  unexpected exit, bounded to 5 restarts / 60 s, after which IPC `STATUS` reports
  `CRASHED`.
- **Save** (`concat.py`, `save_clip`): stream-copy remux of currently-finalized
  segments (the newest, possibly-still-being-written segment is excluded) into a
  `.mp4`; handles filename templates (`{date}`/`{time}`/`{datetime}`), `--trim N`
  last-N-seconds saves, name-collision suffixes, and the saved-clip retention sweep.
- **Encoder selection** (`ffmpeg_utils.pick_encoder`): NVENC → VAAPI/QSV → libx264,
  verified by a real smoke-encode, not just `ffmpeg -encoders` listing. System-audio
  loopback and microphone discovery are best-effort probes; video-only capture with a
  loud warning is the intended fallback, not a bug.
- **IPC boundary** (`ipc.py`, `ipc_client.py`): a loopback-only (`127.0.0.1`) TCP
  server, default port `51525`, trivial line protocol: `SAVE [seconds]`, `PAUSE`,
  `RESUME`, `STATUS`, `SHOW`, `SETTINGS`, `GALLERY`, `LOGS`, `PING`, `QUIT` →
  `OK <result>` / `ERROR <message>`. Handlers are registered callbacks; the server
  knows nothing about capture. **Every trigger goes through this boundary** — the
  pynput hotkey callback, the tray menu, and the main window's buttons all call
  `ipc_client.send_command(...)` rather than touching `capture`/`concat` directly.
  Keep it that way.
- **Entry points**: `cli.py` (`clipersal` console script — single-instance PING check,
  first-run wizard, IPC server, hotkey, tray, main window, update check) and
  `trigger.py` (`clipersal-trigger` — one-shot IPC client used as the Wayland/DE-
  keybinding fallback and for scripting; its import graph is deliberately
  stdlib-only so its frozen exe stays tiny).
- **GUI** (`main_window_qt.py` + `settings_window_qt.py`, `gallery_window_qt.py`,
  `toast_qt.py`, `tray_qt.py`, `first_run_qt.py`, ...): one persistent OBS-style main
  window (sidebar: Home / Clips / Settings / Logs) built once at startup; closing it
  hides to the tray instead of quitting (unless `--no-tray`, where closing quits).
  Cross-thread updates go through `signals.py`'s `AppSignals` Qt signals, never
  through direct calls or polling. Theme is light-only (`theme.py`, Pollen Gold
  palette) — dark mode was deliberately removed; don't reintroduce it.

## Module map (`src/clipersal/`)

| Module | Role |
|---|---|
| `cli.py` | main entry point; wires IPC handlers, hotkey, tray, main window, logging, autostart, update check |
| `trigger.py` | `clipersal-trigger` one-shot IPC client |
| `ipc.py` / `ipc_client.py` | loopback TCP command server / client |
| `capture.py` | `SegmentedCapture`: ffmpeg process, rolling-buffer cleanup, crash auto-restart |
| `concat.py` | `save_clip`, filename templates, trim-before-save, retention sweep |
| `ffmpeg_utils.py` | ffmpeg/ffprobe discovery, encoder probing, capture/audio/mic source args, quality presets |
| `config.py` | `Config` dataclass; argparse parser whose defaults come from `config_store` |
| `config_store.py` | JSON config load/save, `PERSISTED_KEYS` allowlist, config/log path conventions |
| `platform_detect.py` | OS + X11/Wayland session detection |
| `monitors.py` / `window_capture.py` | monitor / window enumeration (`ctypes` on Windows, `xrandr`/`wmctrl` on Linux) |
| `hotkey.py` / `hotkey_widget_qt.py` | pynput global hotkey / press-the-combo recorder widget |
| `main_window_qt.py` | persistent main window (Home / Clips / Settings / Logs) |
| `settings_window_qt.py` / `gallery_window_qt.py` | frame-builders embedded as the Settings / Clips tabs |
| `tray_qt.py` / `tray.py` | `QSystemTrayIcon` tray / `open_folder()` helper (the only survivor of `tray.py`) |
| `toast_qt.py` | save-notification toast with thumbnail |
| `first_run_qt.py` | first-launch wizard (clips folder + hotkey) |
| `signals.py` | `AppSignals` Qt-signal bridge for all cross-thread UI updates |
| `theme.py` / `qt_widgets.py` / `brand.py` / `status_dot.py` | palette + QSS / `SegmentedControl` + `ToggleSwitch` / brand glyphs / animated status dot |
| `thumbnails.py` | cached ffmpeg frame-grab thumbnails (`clips_dir/.thumbnails`) + duration probing |
| `autostart.py` | launch-on-startup (Windows Run key / Linux `~/.config/autostart` .desktop) |
| `update_check.py` | notify-only GitHub Releases update check |
| `subprocess_utils.py` | `NO_WINDOW_KWARGS` — see invariants below |

`packaging/` holds the PyInstaller spec + entry points, the Inno Setup script, the
Linux AppImage tooling, and `generate_icon.py` (one-off Pillow script that produced
`assets/icon.*` — run by hand, not part of the build).

## Conventions and invariants (things that will bite you)

- **Config precedence**: hardcoded default < config file < CLI flag. `config.py`
  loads persisted values as argparse defaults, so explicit CLI flags always win.
  Persisted config lives at `%APPDATA%\Lablooms\Clipersal\config.json` (Windows) or
  `$XDG_CONFIG_HOME`/`~/.config/Lablooms/Clipersal/config.json` (Linux); writes are
  atomic (`.tmp` + `Path.replace()`). Only Settings-tab fields belong in
  `config_store.PERSISTED_KEYS` — runtime flags (`ipc_port`, `tray_enabled`, ...) stay
  CLI-only, and unrelated caches get their own sibling files (see
  `update_check.py`'s cache, `thumbnails.py`'s `.thumbnails/`).
- **New `Config` fields must default to the pre-existing behavior** so an old config
  file produces byte-identical ffmpeg commands. This is a stated, tested rule from
  Phase 8.
- **Never drop `-force_key_frames "expr:gte(t,n_forced*{segment_seconds})"`** from
  `capture._build_command` — without it `-segment_time` silently degrades to
  "whenever the encoder's GOP feels like it".
- **Segments stay MPEG-TS**, not MP4/fragmented MP4 — the concat stream-copy and the
  hard `terminate()` shutdown path both rely on it.
- **Every ffmpeg/ffprobe subprocess call must spread `**NO_WINDOW_KWARGS`**
  (`subprocess_utils.py`) — otherwise a windowed packaged build flashes a console for
  every child process. `gallery_window_qt.py`'s `explorer /select,` call is the one
  documented exception.
- **Never call a `QApplication` method directly from a non-GUI thread** — not even
  `quit()`, which Qt's docs call thread-safe but which hangs in practice here. Emit an
  `AppSignals` signal instead (`signals.py`'s docstring documents this).
- **Settings apply paths differ per field** (`cli.py`'s `apply_settings`): buffer
  length / clips folder / filename template / retention are live-mutated; a hotkey
  change rebinds the listener; bitrate / quality preset / encoder / capture target /
  mic changes fully restart capture via `restart_capture()` (buffer preserved). IPC
  handlers read `state.session`/`state.setup` through the `_AppState` container for
  exactly this reason.
- **Degrade quietly, never crash the app over a probe**: monitor/window/mic/audio
  enumeration, tray construction, main-window construction, the update check, and the
  first-run wizard all log-and-continue on failure. Best-effort is the house style.
- Comment style: the codebase explains *why*, often at length, referencing the design
  docs. Match that when touching non-obvious logic; keep obvious code uncommented.

## Testing

- Plain **pytest** only (`pyproject.toml`: `testpaths = ["tests"]`); no fixtures
  framework beyond plain helper classes/fakes (e.g. `tests/test_capture.py`'s
  `_FakeProcess` standing in for `subprocess.Popen`). ffmpeg-dependent logic is
  tested against fakes/mocks, not a real ffmpeg, unless the test genuinely needs one.
- Qt test files follow a fixed preamble: `pytest.importorskip("PySide6")`, then
  `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")` **before** importing
  PySide6. Tests drive widgets via `.click()` and direct method calls.
- **Never inject real synthetic keyboard input in tests** (`pynput.keyboard.Controller`
  sends system-wide keystrokes that can leak into whatever window has focus). Test the
  pure key-mapping functions directly and drive the Record/Cancel state machine with a
  fake `pynput.keyboard.Listener`.
- Screenshot-style verification uses `QWidget.grab()` → `QPixmap.save()`, which works
  headlessly under the offscreen platform.
- CI (`.github/workflows/tests.yml`): matrix of `ubuntu-latest` + `windows-latest` ×
  Python 3.10/3.12 on pushes to `master` and PRs. Linux installs ffmpeg, the Qt
  offscreen platform's system libs (`libegl1`, `libgl1`, `libxkbcommon0`,
  `libxkbcommon-x11-0`, `libdbus-1-3`, `libfontconfig1`) and **xvfb** — pynput opens a
  real X connection at import time, so Linux tests run as `xvfb-run -a pytest -v`.
  Windows installs ffmpeg via `choco install ffmpeg -y`.

## Packaging & deployment

- All packaging flows from `packaging/clipersal.spec` (see its header comment):
  **Clipersal.exe** is built **onedir + windowed** (onefile re-extracted on every
  launch — a real fixed bug); **Clipersal-Trigger.exe** stays onefile + console. The
  spec excludes unused PySide6 submodules (QtQml/QtQuick/QtWebEngine/QtMultimedia/...)
  to control bundle size; `pyinstaller-hooks-contrib` is required for pynput's hook.
- Windows additionally ships an Inno Setup installer:
  `iscc packaging/clipersal_installer.iss` → `dist_installer/ClipersalSetup-<version>.exe`.
  Its `MyAppVersion` must be bumped by hand with `pyproject.toml`; its `AppId` GUID
  must never change (Windows uses it to recognize upgrades).
- Linux ships two AppImages built by `packaging/linux/build_appimage.sh` (auto-
  downloads a pinned `appimagetool`). Per the script's own header, it was written and
  reviewed on a Windows-only environment — **verify it
  end-to-end on a real Linux box before trusting it as a release process** (there is a
  checklist in `ARCHITECTURE.md`). `.github/workflows/build-appimage.yml` runs it on
  `workflow_dispatch` and `v*` tags and uploads the AppImages as artifacts.
- AppImage was a deliberate choice over `.deb` (distro-agnostic, no install step); a
  `.deb` remains a documented future addition. `README.md` is the current source of
  truth on what ships where.
- Historical packaging bugs worth knowing before touching this area (details in
  `ARCHITECTURE.md`): `CTRL_BREAK_EVENT` doesn't work from a windowed build (plain
  `terminate()` is used everywhere), and PyInstaller sets `sys.stdout`/`sys.stderr` to
  `None` in windowed mode (logging and startup-error paths are written for that).

## Security considerations

- The IPC server binds **loopback only** (`127.0.0.1`) and has no authentication —
  any local process can issue SAVE/PAUSE/QUIT commands. This is a deliberate
  simplicity trade-off; do not expose the socket beyond loopback, and keep
  `ipc.py` free of anything that executes shell commands from client input.
- The global hotkey (`pynput`) and the hotkey-recording widget run OS-level keyboard
  hooks — treat any change there as privacy-sensitive and keep the test rule above
  (no real synthetic input) intact.
- `update_check.py` is the **only outbound network call** in the project: one GET to
  the GitHub Releases API per 24 h at most, notify-only (never downloads or installs
  anything), gated by a `check_for_updates` setting and by the hardcoded `GITHUB_REPO`
  constant (`"lablooms/clipersal"`; an empty value no-ops the feature). It queries the
  `/releases` list endpoint and takes the newest non-draft entry — deliberately not
  `/releases/latest`, which excludes pre-releases and would 404 while every published
  release is a beta pre-release. Every failure path is swallowed by design. Don't add
  other network calls without flagging it as a design change.
- ffmpeg is deliberately **not bundled**: the verified Windows build is GPL+nonfree,
  and redistributing it would create licensing obligations. Keep detection-with-clear-
  error as the only path.
- Autostart registration writes to the per-user Run key (`HKCU`) or
  `~/.config/autostart/` only — never system-wide locations, and the generated command
  is a bare argv list (no shell wrapper) so it can't flash a console at login.
