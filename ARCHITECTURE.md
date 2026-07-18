# Architecture

Design notes for Clipersal's internals -- the "why" behind decisions that
aren't obvious just from reading the code. See `README.md` for usage and
`CHANGELOG.md` for what's shipped.

## Capture engine

```
                 ┌──────────────────────────┐
                 │   ffmpeg (one process)    │
                 │  continuous capture, GPU  │
                 │  or CPU encode, writes    │
                 │  short segment files      │
                 └────────────┬─────────────┘
                              │ writes
                              ▼
                 buffer_dir/seg-<timestamp>.ts   (rolling window)
                              │
              background cleanup thread deletes
              segments older than buffer_seconds
                              │
                    on save trigger:
                              ▼
              concat demuxer, stream-copy (-c copy)
                              │
                              ▼
                 clips_dir/clip-<timestamp>.mp4
```

ffmpeg runs **once**, continuously, for the life of the capture session. Saving a clip
never stops or restarts it -- it just reads whatever segment files are currently on disk
at that moment and remuxes them into an output file. This is what makes "keep capturing
uninterrupted after saving" cheap and reliable, and it's the same fundamental design OBS
uses for its Replay Buffer.

### Why segments + a cleanup thread, instead of `-segment_wrap`

ffmpeg's segment muxer has a `-segment_wrap N` option that cycles segment filenames after
N segments, which looks tempting for a fixed-size ring buffer. It's deliberately not used
here: it ties the buffer size to a *segment count*, not to *seconds*, so changing the
user-facing "buffer length" setting would mean recomputing and restarting the ffmpeg
process. Instead, segments use `-strftime 1` filenames (monotonically sortable, never
reused) and are written indefinitely; a separate Python thread (`capture._cleanup_loop`)
deletes any segment file whose mtime is older than `now - buffer_seconds` on a short
interval. This lets `buffer_seconds` be adjusted independently of the capture process
(Settings can change it live without restarting ffmpeg), and keeps the "how much do we
retain" policy in plain, testable Python rather than ffmpeg's own state.

### `-segment_time` needs forced keyframes, or it's a lie

`-segment_time N` is only a *minimum* -- the segment muxer cuts at the next available
keyframe at or after N seconds, not at exactly N seconds. Left to an encoder's default
GOP length (several seconds for libx264/NVENC/QSV alike), real segment length can run
3-4x longer than configured -- caught during verification: segments were rolling every
8-9s instead of the configured 2s, which made `save_clip` spuriously report "not enough
captured yet" on a fresh buffer. The fix, applied in `capture._build_command`, is
`-force_key_frames "expr:gte(t,n_forced*{segment_seconds})"`, which forces a keyframe on
exactly that cadence regardless of encoder GOP settings. This applies uniformly to
libx264 and the hardware encoders (NVENC/VAAPI/QSV all honor `-force_key_frames`). Don't
drop this flag when touching the capture command -- segment cadence silently degrades
back to "whenever the encoder feels like it" without it.

### Why MPEG-TS segments, not fragmented MP4

Segments are muxed as `.ts` (MPEG-TS), not `.mp4`. The concat demuxer's stream-copy mode
(`-f concat -c copy`) joins TS segments cleanly; joining MP4 segments the same way is
prone to moov-atom/duration edge cases at segment boundaries. The final saved clip is
still remuxed to `.mp4` (via stream copy -- no re-encode) for playback compatibility.

### Why concat is a stream copy, not a re-encode

The GPU/CPU encode happens exactly once, continuously, while segments are being written.
Saving a clip is a `-c copy` remux -- it just repackages already-encoded bytes, so it's
fast and its cost doesn't scale with buffer length or quality settings.

## Encoder selection

Priority order, auto-detected at startup (`ffmpeg_utils.pick_encoder`):

1. `h264_nvenc` (NVIDIA NVENC)
2. `h264_vaapi` (Linux VAAPI) / `h264_qsv` (Intel Quick Sync)
3. `libx264` (CPU software fallback)

Detection is **two-step**, not just a presence check:

1. Is the encoder listed in `ffmpeg -encoders`? (Compile-time availability.)
2. A cheap smoke test: encode a couple of frames from a `lavfi` test source through that
   encoder to a null output and check the exit code. This matters because an encoder can
   be compiled into ffmpeg without the hardware/driver behind it actually working -- e.g.
   a stock Windows ffmpeg build always lists `h264_nvenc` regardless of whether the
   machine has an NVIDIA GPU. Skipping step 2 means "supported encoder" and "encoder that
   actually runs on this machine" get silently conflated.

The result is cached for the process lifetime. An optional forced-encoder override lets
Settings plug in a user choice without changing `capture.py`.

## Platform capture sources

| Platform | Session type | Video source | Status |
|---|---|---|---|
| Windows | -- | `ddagrab` (lavfi source, Desktop Duplication API) | implemented, falls back to `gdigrab` if `ddagrab` unavailable in the local ffmpeg build |
| Linux | X11 (`XDG_SESSION_TYPE=x11`) | `x11grab` on `$DISPLAY` | implemented |
| Linux | Wayland (`XDG_SESSION_TYPE=wayland`) | xdg-desktop-portal `ScreenCast` -> PipeWire | **not implemented** -- see caveat below |
| macOS | -- | `avfoundation` | not yet started |

### Wayland caveat

Plain `x11grab` **does not work under Wayland** -- there is no global framebuffer a
process can just grab, by design (Wayland's security model). The only sanctioned way to
capture the screen is:

1. Call `org.freedesktop.portal.ScreenCast` over D-Bus to open a screencast session. This
   triggers a compositor-drawn picker/consent dialog -- the user explicitly chooses what
   to share, every time (or a remembered choice, if the portal backend supports it).
2. The portal hands back a PipeWire **node id** and, over the D-Bus connection, a file
   descriptor for the PipeWire socket (fd passing, not a plain string).
3. ffmpeg needs to be built with PipeWire support (`--enable-libpipewire`, an `avdevice`
   pipewire input merged around ffmpeg 6.1) to consume that fd/node id directly as a
   `-f pipewire` input; if the local ffmpeg build lacks that, capture would have to go
   through a GStreamer intermediary instead.

This is a meaningfully different code path from X11 (async D-Bus session negotiation,
user-consent UI, fd handoff, a PipeWire-capable ffmpeg build) -- enough surface area that
it's tracked as its own future project, not part of the initial skeleton. Today, on
Wayland, the tool detects the session type and logs a clear "Wayland capture not
implemented yet" message and exits rather than attempting (and silently failing at)
`x11grab`. Likely libraries for that work: `dbus-next` or `jeepney` for the D-Bus
portal calls.

## Audio capture caveat

There is **no native WASAPI loopback demuxer in stock ffmpeg on Windows**. Verified
directly: `ffmpeg -formats` has no `wasapi` entry, and `-f dshow` only sees
loopback-style audio if a virtual audio driver is installed and exposes itself as a
`dshow` device (e.g. `virtual-audio-capturer` from screen-capture-recorder, or
VB-Cable's `CABLE Output`), or if the user has enabled Windows' built-in "Stereo Mix"
recording device. None of these are guaranteed present on a fresh install.

On Linux, system audio similarly isn't a normal input device -- it requires a
PulseAudio/PipeWire **monitor source** (`pactl list short sources`, looking for a name
ending in `.monitor`), which is virtually always present under Pulse/PipeWire audio
stacks but still needs to be discovered by name rather than assumed.

Given this, audio capture is **best-effort**: at startup, `ffmpeg_utils` probes for a
known loopback/monitor-capable device by name. If one is found, it's added as a second
ffmpeg input and muxed into the segments alongside video. If none is found, capture
proceeds **video-only** with a loud warning logged -- silently dropping audio would be
worse than a clear "no system audio source found" message. This is a real platform gap
outside a virtual-driver install, not a code defect.

## Capture feature set

### Monitor selection

`monitors.py` (`list_monitors(os_) -> list[MonitorInfo]`) enumerates physical displays:
`ctypes` + `EnumDisplayMonitors`/`GetMonitorInfoW` on Windows, `xrandr --query` parsing on
Linux. Enumeration failures (no xrandr, a ctypes call failing) return `[]` rather than
raising -- Settings just hides the monitor picker when there's nothing useful to show.

`ddagrab`'s `output_idx` (hardcoded to `0` originally) is **not** "the whole desktop" --
Desktop Duplication API captures one specific adapter output per instance, so a
multi-monitor Windows machine using `ddagrab` would silently only capture monitor 0
without this feature. `monitor_index` numbering deliberately matches `output_idx`'s own
numbering (index 0 = whatever Windows/X11 itself calls monitor 0) specifically so
`monitor_index=0` (the default) needs no new logic in
`ffmpeg_utils.build_video_capture_source` to keep behaving exactly as before -- a
non-zero index is the only thing that adds `-offset_x -offset_y -video_size` (gdigrab) or
a `+X,Y` display suffix + `-video_size` (x11grab) crop. If the requested monitor isn't
found (unplugged since last Settings save), it falls back to the full desktop/display
with a warning log rather than crashing.

### Single-window capture mode

`window_capture.py` (`list_windows(os_) -> list[WindowInfo]`) mirrors `monitors.py`'s
shape: `EnumWindows`/`GetWindowTextW`/`GetWindowRect` via `ctypes` on Windows (filtered
to visible, non-empty-title, non-degenerate-size windows), `wmctrl -l -G` parsing on
Linux (skipping desktop==-1 sticky/background entries and blank titles).

Windows are matched **by title**, not a stable handle, because that's what ffmpeg's own
`gdigrab -i title=<title>` capture mode takes -- there's no handle-based alternative in
stock ffmpeg. This is also why window capture always forces `gdigrab` on Windows even
when `ddagrab` is otherwise available and preferred: Desktop Duplication API (`ddagrab`)
captures a whole adapter output, never a single window, so there's no hardware-accelerated
path for this mode. Two windows sharing an exact title are ambiguous (ffmpeg picks
whichever it finds first) -- an accepted limitation; most windows (a browser tab's page
title, an editor's open filename) don't collide in practice. Linux resolves a window
capture the same way monitor capture resolves a non-zero index: look up the window's
geometry once at capture-start time and crop `x11grab` to that rect via `-video_size` +
a `+X,Y` display suffix. A window that moves after capture starts keeps the original crop
rect -- re-resolving on the fly is a possible future refinement.

### Microphone input

`ffmpeg_utils.list_microphones(ffmpeg_path, os_)` enumerates real (non-loopback) input
devices -- the same dshow device list Windows loopback discovery already parses,
filtered to *exclude* names matching the loopback-device hints (`virtual-audio-capturer`,
`CABLE Output`, `Stereo Mix`) so those never show up disguised as a "microphone" choice.
Linux mirrors this against `pactl list short sources`, excluding `.monitor` sources.

When both a loopback source and a configured mic are present, `capture._build_command`
mixes them with `-filter_complex "[1:a][2:a]amix=inputs=2:duration=first
:dropout_transition=0[aout]"` and maps `[aout]` as the sole audio output track --
loopback is always input 1, mic input 2, so the filtergraph's stream indices are fixed.
Mic-only (no loopback found) falls back to the same direct `-map 1:a:0` path the
loopback-only case uses, and no mic + no loopback stays video-only.

### Auto-restart on ffmpeg crash

`_cleanup_loop` (the same thread that already sweeps stale segments, on the same cadence)
also calls `_check_process_health()`, which restarts the process in place when `poll()`
shows it exited unexpectedly. Restarting reuses the same `buffer_dir` with no collision
risk (segment filenames are strftime-timestamped), and a fresh `start()` truncates
`ffmpeg.log` while a restart *appends* to it -- so the crash diagnostics that explain
*why* ffmpeg died are still there immediately after the auto-restart replaces the
process, instead of being clobbered by the new process's own log output.

A budget (`_MAX_RESTARTS_PER_WINDOW = 5` within `_RESTART_WINDOW_SECONDS = 60`) stops a
persistently broken setup (bad encoder args, genuinely dead hardware) from
spin-restarting forever. Once exhausted, `gave_up_restarting()` returns `True` and the
IPC `STATUS` command reports `CRASHED` instead of `PAUSED`/`RECORDING` -- surfaced on the
Home tab's status dot ("Capture stopped -- see Logs") rather than invented as a fourth
ad-hoc state, since it's functionally "stopped, but not because the user paused it."
Manual recovery reuses the existing **Pause/Resume** button and the `RESUME` IPC
command: `handle_resume` also checks whether the session is actually running (not just
`state.paused`), and if not, calls `session.start()`, which resets the restart budget.

### Quality presets

`ffmpeg_utils.QUALITY_PRESETS` maps three named presets (Performance / Balanced /
Quality) to a bitrate + a per-encoder speed/preset-string override (NVENC's `p1`-`p7`,
libx264's named presets, QSV's named presets; VAAPI has no speed knob and ignores it).
`resolve_quality_preset(preset, encoder, custom_bitrate)` is the one place that decides
between "look up a preset" and "use the raw bitrate" -- `"custom"` (the default, and the
fallback for any unrecognized preset name, e.g. a hand-edited config typo) always takes
the second path.

## IPC / hotkey boundary

A hotkey trigger is a local socket/IPC command under the hood, decoupled from any
specific hotkey-binding library:

- **`ipc.py`** runs a loopback-only (`127.0.0.1`) TCP server (`IpcServer`, default port
  `51525`) with a trivial line protocol: a client sends a command word (`SAVE`, `PAUSE`,
  `RESUME`, `STATUS`, `SHOW`, `SETTINGS`, `GALLERY`, `LOGS`, `PING`, or `QUIT`), optionally
  followed by one whitespace-separated argument, and gets back `OK <result>` or
  `ERROR <message>`. `SHOW`/`SETTINGS`/`GALLERY`/`LOGS` all show/focus the one persistent
  main window, differing only in which tab they switch to (or none, for `SHOW`). The
  only command that takes an argument is `SAVE`, e.g. `SAVE 30` to save just the last 30
  seconds instead of the whole buffer; every handler is registered as
  `Callable[[str | None], str | None]` so adding an argument to a command never means
  touching the server itself. `cli.py` registers each against a plain closure
  (`concat.save_clip`, `session.stop`/`session.start` for pause/resume, a shared
  `stop_event` for quit) -- the server itself has no idea what a "save" or "pause" even is,
  it just dispatches command strings to whatever callback was registered.
- A loopback TCP socket (rather than a Windows named pipe + a Unix domain socket, one
  per platform) is a deliberate simplification: identical code and behavior on Windows
  and Linux, and binding to `127.0.0.1` keeps it unreachable from outside the machine.
  `port=0` lets the OS pick a free port, which the test suite uses to avoid port
  collisions between runs.
- **`hotkey.py`** binds a real global hotkey via `pynput.keyboard.GlobalHotKeys` -- this
  works on Windows and Linux/X11 (both are backed by an OS/X11-level global key-grab
  API that pynput wraps) but **not on Wayland**, for the same reason Wayland screen
  capture needs the portal: no cross-desktop-environment API exists for a random
  process to grab a global hotkey. `hotkey.is_supported(os_, session_type)` gates this;
  on Wayland (or if pynput fails to bind for any other reason) `cli.py` logs a message
  pointing at the trigger script below instead of crashing. Default combo:
  `<ctrl>+<alt>+r`, overridable via `--hotkey`.
- Critically, the hotkey callback in `cli.py` (`_trigger_save_via_ipc`) does **not**
  call `concat.save_clip` directly -- it calls `ipc_client.send_command("SAVE", ...)`,
  going back out through the same IPC boundary a remote/CLI trigger would use. This is
  what keeps the hotkey-binding layer genuinely swappable: it could move into a
  separate sidecar process with a one-line change, because it was never wired directly
  to the capture engine.
- **`trigger.py`** (console script `clipersal-trigger`) is the documented Wayland
  fallback: bind a compositor/DE-level custom keyboard shortcut directly to
  `clipersal-trigger save` (e.g. GNOME Settings -> Keyboard -> Custom Shortcuts).
  It's the same IPC client the hotkey listener uses internally, just invoked as a
  one-shot process instead of a callback -- and it works identically on Windows/X11
  too, for scripting or testing a save without touching a keyboard.

`concat.save_clip` itself has zero knowledge of any of this -- it's a plain function
taking `(ffmpeg_path, buffer_dir, clips_dir)`, called from an IPC handler closure in
`cli.py`. The tray icon's "Save now" sends the same `SAVE` command rather than reaching
into `capture`/`concat` directly.

Pause/resume has no dedicated state in `capture.py` -- `SegmentedCapture.start()`/`stop()`
already tolerate being called repeatedly (idempotent `stop()`, `start()` resets its own
stop-event), so "pause" is just `session.stop()` and "resume" is just `session.start()`
again, with the paused/not-paused bookkeeping living in `cli.py`'s closure state
(`paused` dict + a lock) rather than inside `SegmentedCapture`. Resuming starts a new
ffmpeg process and a fresh run of timestamped segment filenames into the same
`buffer_dir` -- no collision risk, and any pre-pause segments already in the buffer are
still valid and still age out normally.

## GUI layer (PySide6/Qt)

**Cross-thread updates**: `signals.py`'s `AppSignals` (`show_requested`,
`toast_requested`, `save_completed`, `quit_requested`) is constructed once on the GUI
thread in `cli.py`. A signal emitted from any other thread (an IPC handler, the hotkey
listener) is delivered to a GUI-thread slot automatically via Qt's `QueuedConnection` --
no polling timer needed for any of these. The one poll kept as a real `QTimer` is
`main_window_qt.py`'s STATUS check (~1.5s): it's a genuine "did some other trigger change
capture state" check with no natural push notification, not a thread-safety workaround.

**A subtle bug found only by driving the real app over real IPC, not by unit tests**:
`handle_quit` originally called `QApplication.instance().quit()` directly from the IPC
handler's thread. Despite Qt's own docs describing `quit()`/`exit()` as safe to call
from any thread, this hung forever in practice (confirmed with a minimal reproduction:
a bare `QApplication`, a background thread sleeping then calling `app.quit()`, and
`app.exec()` on the main thread that never returned). Routing the same request through
`AppSignals.quit_requested` (emit from the worker thread, `connect(app.quit)` on the GUI
thread) fixed it immediately. Lesson: never call a `QApplication` method directly from a
non-GUI thread in this codebase, even ones Qt calls thread-safe -- always go through a
signal.

**Widget equivalents with no native Qt counterpart** are built once in `qt_widgets.py`
and reused everywhere: `SegmentedControl` (exclusive checkable `QPushButton`s in a
`QFrame` track, used for capture target, quality preset, and encoder) and `ToggleSwitch`
(a `QPainter`-painted pill+knob, animated with `QPropertyAnimation`, used for
auto-detect and launch-on-startup). Both read colors directly from `theme.py`'s
constants rather than through the global stylesheet.

**Theming**: `theme.py`'s hex constants, with `build_stylesheet()` generating the QSS
equivalent, applied once via `QApplication.setStyleSheet(...)`. Object names (`#card`,
`#cardTitle`, `#hint`, `#primary`, `#segmentedButton`, `#recordButton`, `#statusLabel`,
`#sidebar`, `#navButton`, `#brandMark`, `#valueBadge`, `#thumbPlaceholder`) drive
per-widget styling. A few states need a dynamic Qt property + `style().unpolish()/.polish()`
re-evaluation rather than a plain QSS pseudo-class, because they're not simple
hover/checked states -- the Record button's recording/idle color swap and the Settings
status label's error/success color both work this way.

**Layout**: `QHBoxLayout`/`QVBoxLayout` position widgets by explicit
`addWidget()`/`insertWidget()` call order and index, which avoids a whole class of
packing-order bugs that show up in toolkits that allocate space by pack order instead.

**Save toast bloom animation** (`toast_qt.py`): a `QPropertyAnimation` on `geometry` (a
small point at the toast's final center, expanding outward) runs in parallel with one on
`windowOpacity` (0 -> 1), using `QEasingCurve.OutBack` so the toast overshoots slightly
past its final size before settling back. One bug caught only by running this for real:
`SaveToast.__init__` calls `setFixedWidth(_TOAST_WIDTH)`, which pins
`minimumWidth == maximumWidth`, and Qt re-clamps a widget's size to that range on *every*
`setGeometry()` call -- so a "shrink to a point" starting state was being silently
clamped straight back to full width every frame, and the geometry animation had nothing
real to animate. Fixed by lifting the min/max constraint for the animation's duration and
only calling `setFixedSize()` again once the entrance animation's `finished` signal
fires. A second bug: `_final_geometry()` originally computed its result from
`self.width()`/`self.height()` at call time, correct only once, before the widget was
first shrunk for the animation -- fixed by capturing the result once into
`self._final_rect` right after layout, before any shrinking happens.

**Status-dot seed-scatter pulse** (`status_dot.py`): a `StatusDot(QWidget)` that briefly
scatters three small satellite dots outward from the resting dot and fades them on save.
Two bugs, both caught only by rendering actual pixels: (1) the widget was originally
sized exactly to the resting dot's own diameter -- since Qt clips all painting to a
widget's own bounds, any satellite traveling outward even one pixel past the dot's edge
was invisibly clipped away. Fixed by giving `StatusDot` a bounding box (36px) strictly
larger than its visible dot diameter (14px). (2) even with room to render into,
satellites were still invisible: the resting dot is painted *last* (on top), and the
scatter's travel distance started at 0 (dead-center) -- at low progress a satellite's
entire disc sat well inside the opaque resting dot's own radius and was completely
painted over. Fixed by starting the travel distance just outside the resting dot's
radius instead of at the center.

## Hotkey capture

`hotkey_widget_qt.py` (`HotkeyField(QWidget)`) is used by both the Settings tab and the
first-run wizard: a **Record** button next to a manual `QLineEdit`, so a user can either
press the actual key combination they want or type pynput's `<ctrl>+<alt>+r` format
directly. Clicking Record starts a `pynput.keyboard.Listener` on a background thread; its
`on_press`/`on_release` callbacks emit `_ListenerBridge`'s `pressed`/`released` Qt signals,
delivered to the GUI thread automatically. The entry field shows "Press keys..." and
live-updates as modifiers are held, and the combo finalizes once every key has been
released *and* at least one non-modifier key was part of it.

pynput reports specific left/right key variants on physical keypress (`ctrl_l`, `ctrl_r`,
`alt_gr`, ...), but the stored combo format (and `hotkey.DEFAULT_COMBO`) uses the generic
modifier name so either physical key works -- `_token_for_key`'s `_MODIFIER_ALIASES` table
normalizes `ctrl_l`/`ctrl_r` -> `ctrl`, `alt_l`/`alt_r`/`alt_gr` -> `alt`, etc., before
building the combo string.

**Verifying this without simulating real keypresses**: `pynput.keyboard.Controller`
sends genuinely system-wide synthetic input, not input confined to a test window --
using it in an automated test risks leaking a keystroke into whatever window actually
has OS focus. The pure key-mapping logic (`_token_for_key`/`_format_combo`) is
unit-tested directly against real `pynput.keyboard.Key`/`KeyCode` objects instead; the
Record/Cancel UI state machine is exercised via `.click()` with a fake
`pynput.keyboard.Listener` substituted in (never a real global hook), and the full
press/release state machine is driven directly through
`_on_key_press`/`_on_key_release` rather than a real `Listener` too.

## Settings persistence

**Persistence** (`config_store.py`): a small JSON file at `%APPDATA%\Lablooms\Clipersal\
config.json` (Windows) or `$XDG_CONFIG_HOME/Lablooms/Clipersal/config.json` (Linux,
falling back to `~/.config`) -- nested under `Lablooms/<AppName>/` since Clipersal is one
app in the Lablooms lineup. Holds exactly the Settings-tab fields (`buffer_seconds`,
`clips_dir`, `hotkey_combo`, `video_bitrate`, `encoder_override`, `filename_template`,
`clip_retention_days`, `launch_on_startup`, `quality_preset`, `capture_mode`,
`monitor_index`, `window_title`, `mic_device`, `check_for_updates`) -- nothing else (not
`ipc_port`, `hotkey_enabled`, `tray_enabled`, etc; those stay CLI-only flags). JSON over
TOML is a deliberate choice: stdlib `json` covers both read and write with no extra
dependency, whereas writing TOML would need one (`tomllib` is read-only). Writes are
atomic (write to `.json.tmp`, then `Path.replace()`) so a crash mid-write can't corrupt
the file into something unparseable.

**Precedence**: hardcoded default < config file < CLI flag. `config.build_arg_parser()`
loads the persisted overrides once and uses them as argparse *defaults* -- so a value the
user explicitly passes on the command line always wins, but otherwise a saved Settings
value beats the hardcoded fallback.

**Applying a change** (`cli.py`'s `apply_settings`, called from the Save button) differs
per field, because not everything can change without touching the running ffmpeg process:

- **Buffer length**, **clips folder**, **filename template**, and **retention days** are
  all live-mutable: `capture._cleanup_loop` reads `config.buffer_seconds` fresh every pass,
  and `concat.save_clip`/`concat.enforce_clip_retention` read `config.clips_dir`,
  `config.filename_template`, and `config.clip_retention_days` fresh on every save, so just
  mutating the shared `Config` object's attributes takes effect immediately with no restart.
- **Hotkey combo** requires unbinding and rebinding `HotkeyListener` (`rebind_hotkey()`) --
  a bind failure (e.g. the combo is already grabbed by another app) is logged as a warning
  but doesn't block saving the new value.
- **Video bitrate**, **quality preset**, **encoder override**, and **capture
  mode/monitor index/window title/mic device** are all baked into the ffmpeg command line
  at capture-start time, so applying any of them means fully restarting capture: stop the
  running `SegmentedCapture`, call `capture.resolve_setup(config)` again (re-running the
  two-step encoder detection against the *new* `encoder_override`), and swap in a fresh
  `SegmentedCapture` built from the new setup (`restart_capture()`). If the forced encoder
  turns out not to work, `resolve_setup` raises `NoWorkingEncoderError`, which
  `apply_settings` turns into an error message shown inline in the Settings tab instead
  of quietly leaving capture broken. The restart preserves whichever paused/running state
  was already in effect and reuses the same `buffer_dir` (segment filenames are
  timestamped, so there's no collision with segments from before the restart).

Because encoder/bitrate changes replace `state.session` and `state.setup` outright,
`cli.py` keeps them behind a small `_AppState` container that IPC handler closures read
indirectly (`state.session`, `state.setup`) rather than closing over the original local
variables.

**PySide6-missing caveat**: `PySide6` needs to actually be installed for any GUI to exist
at all. Missing `PySide6`, or an exception raised while constructing `MainWindow` (e.g. a
missing Qt platform plugin with no display) is handled non-fatally: `cli.py` logs a
warning naming the config file path directly, `main_window` stays `None`, and the app
falls back to a plain wait loop -- the hotkey, IPC, and tray keep working,
`SHOW`/`SETTINGS`/`GALLERY`/`LOGS` raise a clear "main window unavailable" IPC error
instead of silently doing nothing, and a user can always hand-edit the JSON config file
directly.

## Clip management

**Filename templates** (`concat.render_filename`): `Config.filename_template` (default
`"clip-{date}-{time}"`) supports `{date}`, `{time}`, and `{datetime}` placeholders.
Rendering sanitizes anything that isn't a valid filename character to `_` and falls back
to the literal name `"clip"` if the result would otherwise be empty (e.g. a template of
just `"..."`) -- a bad hand-edited template should never crash a save.
`concat._unique_output_path` appends `-1`, `-2`, ... on a name collision (a template
without `{time}`, or two saves within the same second, would otherwise silently
overwrite a previous clip).

**Clip retention** (`concat.enforce_clip_retention`): a separate policy from the rolling
*buffer*'s `buffer_seconds` (which ages out *unsaved* segments) -- this ages out already
*saved* clips in `clips_dir`. `Config.clip_retention_days` defaults to `0` (keep forever,
so a fresh install never surprises anyone by deleting something), and when set, sweeps
every `*.mp4` in `clips_dir` by mtime -- not just ones this run of Clipersal wrote, since
there's no manifest tracking that; this assumes `clips_dir` is a dedicated folder, the
same assumption the Settings folder-picker already makes. The sweep runs after every
successful save and again whenever Settings applies a new retention value.

**Trim-before-save**: `concat._finalized_segments` accepts an optional `trim_seconds`
that further restricts the segment list to those with `mtime >= now - trim_seconds`,
letting `save_clip` save just the last N seconds of the buffer instead of the whole
thing. This threads all the way out through the IPC layer's optional argument: `SAVE 30`
saves the last 30 seconds. Exposed two ways: the tray's **Save last 30s** menu item, and
`clipersal-trigger save --trim 30`.

**Thumbnails** (`thumbnails.py`): one ffmpeg frame-grab per clip (`-ss 0.5`, falling back
to `-ss 0` for a clip shorter than that), cached in `clips_dir/.thumbnails` keyed by
`{stem}.{mtime_ns}.jpg` -- keying on mtime means a renamed/replaced/re-saved clip
naturally gets a fresh thumbnail with no explicit cache-invalidation logic needed.
`find_ffprobe` looks next to `ffmpeg_path` first, then falls back to `PATH`; a missing
`ffprobe` degrades to no duration shown rather than failing the gallery. Thumbnail
generation always happens on a background thread in whatever window needs it, never on
the GUI thread -- a frame-grab takes a few hundred ms, long enough to freeze the whole
app if done synchronously.

**Save toast** (`toast_qt.py`): a borderless, auto-dismissing popup (a frameless,
translucent-background `QWidget` with an opaque rounded `#card` `QFrame` inside it --
`Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool`, positioned bottom-right via
`QGuiApplication.primaryScreen().availableGeometry()`, which natively excludes the
taskbar) shown after every successful save, with a thumbnail preview (background-thread
generation, delivered via a signal) and a "click to open folder" action; auto-dismisses
after 4.5s via `QTimer.singleShot`. This is the one place in the app that deliberately
uses a frameless window -- the main window explicitly avoids it (a real interactive
window needs native minimize/close/drag, which frameless breaks), but a toast is
supposed to be chromeless by nature.

## Feels-like-a-real-app polish

**Rotating log file** (`cli.py`'s `_configure_logging`): a `RotatingFileHandler` (1MB, 3
backups) next to the config file, plus a `StreamHandler` -- but only when
`sys.stderr is not None`. That guard matters specifically for a `--windowed` PyInstaller
build: PyInstaller sets `sys.stdout`/`sys.stderr` to `None` (not just closed) in that
mode, and constructing a bare `logging.StreamHandler()` in that situation captures `None`
as its stream at construction time, which would raise the moment anything tried to log.

**Single-instance detection** (`cli.py`'s `_another_instance_running`): before doing any
of the slow startup work (encoder detection, starting ffmpeg), `main()` sends a `PING` to
the configured IPC port with a short timeout. If something answers, that's another
running instance -- `_show_already_running_message` shows a friendly dialog naming the
port and pointing at the tray/hotkey/`clipersal-trigger`, and `main()` returns `0`
immediately. Otherwise a second launch would fully spin up its own encoder detection and
ffmpeg process, only to fail later when `IpcServer` couldn't bind the already-taken port.

**Launch on startup** (`autostart.py`): Windows registers a value under the per-user Run
key (`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`), which the OS runs silently at
login; Linux writes a `.desktop` file to `~/.config/autostart/`, the XDG autostart
convention. `autostart.launch_command()` resolves to the frozen exe's own path
(`sys.frozen`/`sys.executable`, set by the PyInstaller bootloader) when packaged, or
`python -m clipersal.cli` when running from source. macOS isn't supported yet, since
capture itself isn't implemented there either.

**First-run setup wizard** (`first_run_qt.py`): shown once, before capture ever starts,
when `config_store.default_config_path()` doesn't exist yet. Lets a new user pick a clips
folder and a save hotkey instead of silently applying hardcoded defaults. Both "Get
Started" and "Skip for now" persist the current config values -- skipping still writes a
config file, so the wizard doesn't nag on every subsequent launch; only a config file
that's never been written at all counts as "first run."

## Auto-update checker

Notify-only: checks GitHub Releases once at startup and, if something newer is found,
shows a dismissible banner on the Home tab -- never downloads or installs anything.

**`update_check.py`** (stdlib-only -- `urllib.request`/`json`/`re`/`time`, no new
dependency): `GITHUB_REPO` is set to `"lablooms/clipersal"`. `fetch_latest_release(repo,
fetch=...)` hits `https://api.github.com/repos/{repo}/releases/latest` (GitHub 403s any
request missing a `User-Agent` header) and returns `None` on any failure whatsoever --
offline, GitHub down, malformed JSON, missing fields. `is_newer(candidate, current)`
strips a leading `v` and a `-suffix`/`+build`, then compares dotted-integer tuples;
returns `False` (never raises, never true-by-default) for anything unparseable. One
documented, accepted simplification: stripping the suffix means `"0.1.0"` compares equal
to (not newer than) running `"0.1.0-beta"`, so a beta-to-stable promotion of the same
numeric version won't trigger a banner.

**Cache, separate from config.json**: `default_cache_path()` returns
`config_store.default_config_path().parent / "update_check_cache.json"` -- a sibling of
`config.json`, not a key inside it, since `last_checked`/`available_version`/
`available_url`/`dismissed_version` aren't Settings-window-exposed fields.

**Throttling that doesn't hide a real update**: `check_for_update_once()` limits the
actual network call to at most once every 24h, but a found-and-undismissed update is
still re-derived from the cache and returned even when the network call itself is
throttled -- otherwise a banner the user saw once, then didn't act on, would silently
vanish on a same-day relaunch before they ever got to dismiss it.

## Packaging & distribution

Windows ships as two PyInstaller-frozen executables; Linux ships as two AppImages built
from the same PyInstaller output.

### Why `Clipersal.exe` is onedir, not onefile

A onefile build re-extracts its entire bundled Python runtime + libraries to a temp
directory on **every launch**, not just the first -- noticeably slow with PySide6 in the
bundle. `Clipersal.exe` is built **onedir** (a folder containing the launcher exe + an
`_internal/` folder of bundled libraries). `Clipersal-Trigger.exe` stays onefile -- it
has zero Qt dependency and was never the one with a slow-startup problem.

### Why two executables, not one

`Clipersal.exe`/`Clipersal-x86_64.AppImage` is the main app (capture + IPC + hotkey +
tray + settings), built **windowed** (no console) since it's a background tool.
`Clipersal-Trigger.exe`/`Clipersal-Trigger-x86_64.AppImage` is the tiny IPC trigger
script. It exists standalone, not just as a subcommand of the main exe, for the Wayland
fallback: a user binds a DE custom keyboard shortcut directly to a command, and that
command needs to be invocable on its own without also being the always-running
background app. Because `trigger.py`'s entire import graph is `ipc.py`/`ipc_client.py`
(stdlib `socket`/`socketserver` only), PyInstaller's per-entry-point dependency analysis
naturally keeps that executable small (~8MB) regardless of how large the main app's
bundle gets.

### Why ffmpeg is not bundled

1. **Size.** An ffmpeg build with NVENC/VAAPI/QSV support and the codec set this project
   needs runs 100-150+ MB on its own -- multiple times the size of the rest of the
   packaged app. That's a poor fit for something meant to stay "a small standalone tool."
2. **Licensing.** A full-featured Windows ffmpeg build (gyan.dev's "full" build) is
   GPLv3 + nonfree (NVENC/QSV linking requires the nonfree flag). Redistributing that
   binary inside our own installer/AppImage would pull this project under GPL
   redistribution obligations. A user's own distro-packaged or winget-installed ffmpeg
   carries none of that obligation.
3. **Maintenance.** A bundled ffmpeg becomes this project's responsibility to update for
   security patches; a system-provided one is the OS/package manager's.

### Windows: PyInstaller

```sh
pip install -e ".[build]"
pyinstaller packaging/clipersal.spec --clean
```

Produces `dist/Clipersal/Clipersal.exe` (onedir, windowed, `assets/icon.ico`) and
`dist/Clipersal-Trigger.exe` (onefile, console). The `build` extra installs
`pyinstaller-hooks-contrib`, needed for `pynput`'s hook. PySide6 doesn't need it --
modern PyInstaller (>=6) ships first-party PySide6 hooks internally.

### A real bug packaging found: `CTRL_BREAK_EVENT` needs a console

`capture.SegmentedCapture._stop_process` used to send `signal.CTRL_BREAK_EVENT` to stop
ffmpeg on Windows, on the theory that it was a more graceful shutdown than a hard
`terminate()`. That relies on `GenerateConsoleCtrlEvent`, a Win32 API that operates on
the *calling process's own console* -- which a `--windowed` PyInstaller build (correctly,
by design) doesn't have. The result, only visible once actually running the packaged
exe: `PAUSE` (and full shutdown) failed with `OSError: [WinError 6] The handle is
invalid`, and both the app and the child `ffmpeg.exe` were left orphaned because the
exception broke out of the cleanup sequence before it finished.

Fixed by dropping `CTRL_BREAK_EVENT` entirely in favor of a plain `terminate()` on every
platform. This is safe specifically because segments are MPEG-TS, not MP4 -- there's no
container trailer/index that needs a graceful shutdown to finalize -- and `concat.py`
already excludes the newest (possibly still-being-written) segment from every save
regardless, so an abruptly-terminated last segment was never going to be read either way.

### ffmpeg subprocess console flash

`console=False` in `packaging/clipersal.spec` only controls **Clipersal's own** PE
subsystem, not its **children's**. ffmpeg and ffprobe are console-subsystem executables,
and spawning one via `subprocess.run`/`subprocess.Popen` from a windowed Python process
still makes Windows allocate (and briefly flash) a brand new console window for that
child process by default -- independent of whether the parent is windowed. Every ffmpeg
subprocess call does this: starting capture, every `concat.save_clip`, every
encoder-detection smoke test, every thumbnail generation, every `ffprobe` duration query.

Fixed in `subprocess_utils.py`: a single `NO_WINDOW_KWARGS` dict
(`{"creationflags": subprocess.CREATE_NO_WINDOW}` on Windows, `{}` everywhere else)
spread (`**NO_WINDOW_KWARGS`) into every `subprocess.run`/`subprocess.Popen` call that
touches ffmpeg/ffprobe.

### Startup errors in a windowed build

`cli.py`'s `_show_startup_error` prints to stderr (works if there's a console) and also
best-effort shows a native `QMessageBox` dialog, used for both the ffmpeg-missing path
and an IPC-port-already-in-use failure. `entry_clipersal.py` (the PyInstaller entry
point) wraps `main()` in one more catch-all that shows a dialog for a genuinely
unexpected exception too, deliberately still `tkinter.messagebox` here, not `QMessageBox`:
this is the last-resort safety net for a failure so early or unexpected that PySide6
itself might be the thing that broke, and `tkinter` ships with the standard Python
installer regardless of what the app's own GUI stack is.

### Linux: AppImage, not .deb

**AppImage** was chosen:

- **Distro-agnostic.** A `.deb` only helps Debian/Ubuntu-family users; an AppImage runs
  on any modern Linux distro without rebuilding.
- **Matches the "single exe" shape of the Windows build.** No install step, no root/sudo,
  chmod +x and run.

The concrete cost: a `.deb` could declare `Recommends: ffmpeg`, so `apt install` would
proactively offer to install it. An AppImage has zero package-manager awareness and can't
do anything like that -- on Linux, exactly as on Windows, the clear-error-on-first-run
path is the only safety net for a missing ffmpeg.

**Caveat, stated plainly**: this Linux packaging was written to documented AppImage
conventions and manually reviewed, but this project's entire history so far was built
and verified on a Windows machine with no Linux available -- so, unlike the Windows
build, it has not been executed or run end-to-end. Before trusting it as a release
process: run `build_appimage.sh` on a real Linux box, confirm both AppImages launch, that
the tray icon actually renders under whatever desktop environment you're targeting
(StatusNotifierItem/AppIndicator support for `QSystemTrayIcon` varies between Linux
desktop environments), and that a full save/pause/resume/quit cycle works the same way
it was verified to on Windows.

### Why no installer (yet)

Decided **not yet** -- a single portable exe plus the first-run wizard is the idiomatic
pattern for a small background utility like this, the same way OBS itself ships
portable-friendly. An installer mainly earns its keep for Start Menu integration,
uninstall registration, or auto-update -- none of which Clipersal has yet.

## Testing approach

`QWidget.grab()` -> `QPixmap.save(path, "PNG")` works under
`QT_QPA_PLATFORM=offscreen` -- i.e. headlessly, without a real desktop session. Every
GUI widget/tab has both real unit tests (state machines, signal wiring, validation) and
at least one screenshot pass. The `pynput.keyboard.Controller` real-synthetic-input
caveat means tests never inject a real key event; the Record/Cancel state machine is
exercised via `.click()` instead. Some UI bugs (the toast's shrink-animation clamp, the
status dot's satellites being painted over) only showed up when rendering actual pixels
and checking them, not from asserting on internal widget state alone -- worth keeping in
mind when adding new animated/painted widgets.

## Known limitations

- **Wayland**: no screen capture yet (needs xdg-desktop-portal + PipeWire support).
  `clipersal-trigger` + a DE keybinding is the documented save-trigger workaround in the
  meantime, since a global hotkey can't be grabbed there either.
- **macOS**: not implemented yet (capture, launch-on-startup, and packaging are all
  Windows/Linux only so far).
- **Linux AppImage packaging**: written to documented conventions and reviewed by hand,
  but not yet run end-to-end on a real Linux machine -- treat it as unverified before
  trusting it as a release process.
- No installer (Windows or Linux) and no `.deb` package yet.
- Other ideas not yet scoped in detail: privacy auto-pause (blocklisted apps/windows),
  disk-space-based buffer retention as an alternative to `buffer_seconds`, a sound cue on
  save, a portable mode (config/clips relative to the executable), localization, and
  cloud upload/share integration. An in-game overlay is a deliberate non-goal -- a
  fundamentally larger engineering effort (DirectX/Vulkan/OpenGL hooking) that doesn't
  fit "a small standalone tool."
