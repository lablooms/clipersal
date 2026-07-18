#!/bin/sh
# AppImage entry point for the trigger script. See AppRun.clipersal.
HERE="$(dirname "$(readlink -f "${0}")")"
exec "${HERE}/usr/bin/Clipersal-Trigger" "$@"
