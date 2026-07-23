# PyInstaller spec for Clipersal (see ARCHITECTURE.md's "Packaging & distribution"
# section for the full rationale). Build with:
#
#   pip install -e ".[build]"
#   pyinstaller packaging/clipersal.spec --clean
#
# Clipersal.exe is built **onedir** (a folder, not a single file) -- onefile
# mode re-extracts the entire bundle to a temp directory on *every launch*,
# which was the actual cause of a real "runs from the start every time"
# complaint. Clipersal-Trigger.exe stays onefile: it has zero Qt dependency
# and was never the one with a slow-startup problem, so there's no reason
# to give up the single-file convenience for it.
#
# Requires `pyinstaller-hooks-contrib` (pulled in by the `build` extra) for
# pynput's hook (its platform backend needs to be picked up via a
# data-file/hidden-import mechanism plain static analysis can't see).
# PySide6 does NOT need it -- modern PyInstaller (>=6, already required)
# ships first-party PySide6 hooks internally.

from pathlib import Path

block_cipher = None
repo_root = Path(SPECPATH).resolve().parent
icon_path = str(repo_root / "assets" / "icon.ico")

# Qt submodules this app never uses -- PySide6's full wheel includes
# WebEngine, QML/Quick, 3D, etc., and PyInstaller will happily bundle all of
# it if not told otherwise. Excluding the big, definitely-unused ones keeps
# the frozen bundle from growing far larger than it needs to; still expect
# real growth over the pre-PySide6 ~20MB bundle regardless.
# QtMultimedia / QtMultimediaWidgets are deliberately NOT excluded:
# player_qt.py's in-app clip player (QMediaPlayer + QVideoWidget) needs them
# in the frozen build -- the size cost is accepted, see ARCHITECTURE.md.
QT_EXCLUDES = [
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtNetwork",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtPdf",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtRemoteObjects",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DExtras",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtHelp",
    "PySide6.QtDesigner",
    "PySide6.QtXml",
]

# ---------------------------------------------------------------------------
# Clipersal.exe -- the main app: capture, IPC, hotkey, tray, main window.
# Windowed (no console) since it's a background/tray tool; see cli.py's
# _show_startup_error and entry_clipersal.py's top-level catch for how
# startup failures still get surfaced without a console to print to.
# ---------------------------------------------------------------------------
app_analysis = Analysis(
    [str(repo_root / "packaging" / "entry_clipersal.py")],
    pathex=[str(repo_root / "src")],
    # Ship the icon as data so brand.app_icon() can load it at runtime from
    # sys._MEIPASS/assets/ -- the exe's own icon resource (icon= below) only
    # covers Explorer / the taskbar, not Qt's window title bars.
    datas=[
        (str(repo_root / "assets" / "icon.png"), "assets"),
        (str(repo_root / "assets" / "icon.ico"), "assets"),
    ],
    hiddenimports=[],
    hookspath=[],
    excludes=QT_EXCLUDES,
    noarchive=False,
)
app_pyz = PYZ(app_analysis.pure)
# exclude_binaries=True + a separate COLLECT() is what actually makes this
# onedir -- PyInstaller has no simple onefile=True/False switch on EXE()
# itself; onefile-vs-onedir is expressed structurally like this.
app_exe = EXE(
    app_pyz,
    app_analysis.scripts,
    [],
    exclude_binaries=True,
    name="Clipersal",
    icon=icon_path,
    console=False,
)
app_coll = COLLECT(
    app_exe,
    app_analysis.binaries,
    app_analysis.zipfiles,
    app_analysis.datas,
    strip=False,
    upx=False,
    name="Clipersal",
)

# ---------------------------------------------------------------------------
# Clipersal-Trigger.exe -- the tiny IPC trigger script (Wayland/DE
# keybinding fallback; also used for scripting). Its import graph only
# touches ipc.py/ipc_client.py (stdlib socket/socketserver), so this stays
# small regardless of how large the main app's bundle gets. Console=True:
# unlike the main app, this is meant to be invoked from a shell/keybinding
# and print a response / return an exit code. Kept onefile -- no Qt
# dependency, so no slow-startup-extraction problem to fix.
# ---------------------------------------------------------------------------
trigger_analysis = Analysis(
    [str(repo_root / "packaging" / "entry_trigger.py")],
    pathex=[str(repo_root / "src")],
    hiddenimports=[],
    hookspath=[],
    noarchive=False,
)
trigger_pyz = PYZ(trigger_analysis.pure)
trigger_exe = EXE(
    trigger_pyz,
    trigger_analysis.scripts,
    trigger_analysis.binaries,
    trigger_analysis.zipfiles,
    trigger_analysis.datas,
    name="Clipersal-Trigger",
    icon=icon_path,
    console=True,
)
