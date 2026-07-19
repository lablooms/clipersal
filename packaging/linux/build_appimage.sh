#!/usr/bin/env bash
set -euo pipefail

# Build two AppImages for Clipersal: the main app, and the tiny
# trigger script (needed standalone for Wayland/DE-keybinding use -- see
# ARCHITECTURE.md's Wayland caveat). See ARCHITECTURE.md's "Packaging & distribution"
# section for why AppImage was chosen over a .deb.
#
# Must be run ON Linux -- PyInstaller cannot cross-compile a Linux binary
# from Windows/macOS. This script (and everything under packaging/linux/)
# was written and reviewed but NOT executed in the environment that built
# the rest of this project (Windows-only) -- verify it end to end on a
# real Linux box before treating it as a release process. See the
# "Verifying this yourself" note in ARCHITECTURE.md for a checklist.
#
# Requirements on the build machine:
#   - Python 3.10+ with this project installed: pip install -e ".[build]"
#   - Runtime system packages the frozen binary will still need at
#     runtime (freezing doesn't remove this dependency, it's the same
#     caveats documented in ARCHITECTURE.md for the unpackaged app):
#       - Qt's own Linux platform plugin dependencies (X11/XCB libs, and
#         libxkbcommon, libfontconfig) -- PySide6's own wheel bundles most
#         of what it needs, but the underlying system libs it links
#         against at runtime must exist on the build machine for
#         PyInstaller to find and bundle them
#       - A StatusNotifierItem-capable tray host (most modern DEs) for
#         QSystemTrayIcon -- absence degrades to "tray disabled" the same
#         non-fatal way as on Windows, not a build failure
#       - python3-xlib's C dependencies (libX11) for pynput on X11
#   - ffmpeg is deliberately NOT bundled (see ARCHITECTURE.md) -- the end user
#     installs it via their distro's package manager
#   - `appimagetool` on PATH, or let this script download a pinned
#     version (requires network access; set APPIMAGETOOL to a local path
#     to skip the download)
#
# Usage: ./packaging/linux/build_appimage.sh

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD_DIR="${REPO_ROOT}/build/appimage"
DIST_DIR="${REPO_ROOT}/dist"
APPIMAGETOOL_VERSION="1.9.1"
ARCH="$(uname -m)"

rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}" "${DIST_DIR}"

echo "==> Running PyInstaller"
cd "${REPO_ROOT}"
pyinstaller packaging/clipersal.spec --clean --noconfirm

get_appimagetool() {
    if [ -n "${APPIMAGETOOL:-}" ]; then
        echo "${APPIMAGETOOL}"
        return
    fi
    if command -v appimagetool >/dev/null 2>&1; then
        command -v appimagetool
        return
    fi
    local cached="${BUILD_DIR}/appimagetool-${ARCH}.AppImage"
    if [ ! -f "${cached}" ]; then
        echo "==> Downloading appimagetool ${APPIMAGETOOL_VERSION} (not found on PATH)" >&2
        curl -fsSL -o "${cached}" \
            "https://github.com/AppImage/appimagetool/releases/download/${APPIMAGETOOL_VERSION}/appimagetool-${ARCH}.AppImage"
        chmod +x "${cached}"
    fi
    echo "${cached}"
}

APPIMAGETOOL="$(get_appimagetool)"

build_one() {
    local name="$1"          # AppDir name, e.g. clipersal
    local binary_name="$2"   # binary (onefile) or directory (onedir) in dist/, e.g. Clipersal
    local desktop_file="$3"
    local apprun_file="$4"
    local out_name="$5"      # output AppImage filename
    local layout="$6"        # "onefile" or "onedir"

    local appdir="${BUILD_DIR}/${name}.AppDir"
    mkdir -p "${appdir}/usr/bin"
    if [ "${layout}" = "onedir" ]; then
        # PyInstaller's onedir output is a *directory* (the executable plus
        # an _internal/ folder of bundled libraries) -- copy its whole
        # contents into usr/bin/ so the executable still ends up at
        # usr/bin/${binary_name}, exactly where AppRun expects it, with
        # _internal/ sitting alongside it rather than nested under an extra
        # subfolder.
        cp -r "${DIST_DIR}/${binary_name}/." "${appdir}/usr/bin/"
    else
        cp "${DIST_DIR}/${binary_name}" "${appdir}/usr/bin/${binary_name}"
    fi
    cp "${REPO_ROOT}/packaging/linux/${desktop_file}" "${appdir}/${name}.desktop"
    cp "${REPO_ROOT}/assets/icon.png" "${appdir}/clipersal.png"
    cp "${REPO_ROOT}/packaging/linux/${apprun_file}" "${appdir}/AppRun"
    chmod +x "${appdir}/AppRun" "${appdir}/usr/bin/${binary_name}"

    echo "==> Building ${out_name}"
    "${APPIMAGETOOL}" "${appdir}" "${DIST_DIR}/${out_name}"
}

build_one "clipersal" "Clipersal" "clipersal.desktop" "AppRun.clipersal" \
    "Clipersal-${ARCH}.AppImage" "onedir"
build_one "clipersal-trigger" "Clipersal-Trigger" "clipersal-trigger.desktop" "AppRun.trigger" \
    "Clipersal-Trigger-${ARCH}.AppImage" "onefile"

echo "==> Done. Output in ${DIST_DIR}:"
ls -la "${DIST_DIR}"/*.AppImage
