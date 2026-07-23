"""Per-clip metadata sidecar: favorites (and notes) for saved clips.

Lives in ``clips_dir/.clipmeta.json`` -- a sibling cache file, NOT inside
config.json: config_store.py's PERSISTED_KEYS allowlist reserves the main
config for Settings-window fields, and per-clip annotations are data about
the clips folder's contents, so they belong next to those clips. This is
the same "separate cache" convention update_check.py's cache file and
thumbnails.py's .thumbnails/ directory already follow; keeping it inside
clips_dir also means the metadata travels with the clips if the user moves
the folder.

Keys in the file are full clip filenames ("clip-20260722-130000.mp4"),
never stems. A rename therefore naturally *orphans* the old entry instead
of silently attaching one clip's favorite/note to a different clip that
happens to reuse the stem -- and prune() drops the orphan on the next
gallery refresh.

Schema: ``{"version": 1, "clips": {"<filename>": {"favorite": true,
"note": "..."}}}``. Entries carrying neither flag nor text (favorite=false
AND note="") are dropped from the file entirely rather than accumulating
as tombstones.

Every function here is best-effort, like the rest of the app's probes and
caches: a missing clips_dir, an unreadable/corrupt sidecar, or a failed
write logs at most and never raises -- a broken metadata file must never
take down a save, a gallery refresh, or the retention sweep that reads
favorites() to decide what to protect.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_SIDECAR_FILENAME = ".clipmeta.json"
_SCHEMA_VERSION = 1


def _sidecar_path(clips_dir: Path) -> Path:
    return clips_dir / _SIDECAR_FILENAME


def load_metadata(clips_dir: Path) -> dict:
    """The sidecar's "clips" mapping: filename -> {"favorite": bool, "note": str}.

    Returns {} for a missing file, a missing clips_dir, corrupt JSON, or a
    file whose top-level shape isn't the expected schema -- a bad sidecar
    reads as "no metadata yet", never as an error the caller has to handle.
    Entries are normalized (favorite coerced to bool, note to str) so
    callers never see whatever a hand-edited file left behind.
    """
    try:
        with open(_sidecar_path(clips_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read clip metadata sidecar: %s", exc)
        return {}
    clips = data.get("clips") if isinstance(data, dict) else None
    if not isinstance(clips, dict):
        return {}
    normalized = {}
    for name, entry in clips.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        note = entry.get("note", "")
        normalized[name] = {
            "favorite": bool(entry.get("favorite", False)),
            "note": note if isinstance(note, str) else "",
        }
    return normalized


def _save_metadata(clips_dir: Path, clips: dict) -> None:
    """Write the sidecar atomically: tmp file + Path.replace(), the same
    pattern as config_store.save_overrides -- a crash mid-write must never
    leave a half-written, unparseable file behind, because every read
    treats corrupt JSON as "no metadata" and would silently drop everyone's
    favorites.
    """
    payload = {"version": _SCHEMA_VERSION, "clips": {}}
    for name, entry in clips.items():
        favorite = bool(entry.get("favorite", False))
        note = entry.get("note", "")
        note = note if isinstance(note, str) else ""
        # An entry with neither flag nor text is dead weight -- drop it
        # from the file entirely instead of filling the sidecar with
        # {"favorite": false, "note": ""} tombstones.
        if not favorite and not note:
            continue
        out = {"favorite": favorite}
        if note:
            out["note"] = note
        payload["clips"][name] = out
    path = _sidecar_path(clips_dir)
    tmp_path = path.with_suffix(".json.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        tmp_path.replace(path)
    except OSError as exc:
        # Missing clips_dir, read-only folder, disk full -- losing one
        # metadata write is not worth crashing the action that caused it.
        log.warning("Could not write clip metadata sidecar: %s", exc)


def is_favorite(clips_dir: Path, name: str) -> bool:
    return load_metadata(clips_dir).get(name, {}).get("favorite", False)


def favorites(clips_dir: Path) -> set[str]:
    """Filenames (full names, extension included -- never stems) currently
    marked favorite. Read by the retention sweep on every save, so it stays
    cheap: one small JSON read, no probing.
    """
    return {name for name, entry in load_metadata(clips_dir).items() if entry["favorite"]}


def set_favorite(clips_dir: Path, name: str, favorite: bool) -> None:
    clips = load_metadata(clips_dir)
    entry = clips.setdefault(name, {"favorite": False, "note": ""})
    entry["favorite"] = bool(favorite)
    _save_metadata(clips_dir, clips)


def note_for(clips_dir: Path, name: str) -> str:
    return load_metadata(clips_dir).get(name, {}).get("note", "")


def set_note(clips_dir: Path, name: str, note: str) -> None:
    """Attach a free-text note to a clip. An empty note clears the key --
    the entry itself survives when the clip is still a favorite, otherwise
    the whole entry is dropped from the file (see _save_metadata).
    """
    clips = load_metadata(clips_dir)
    entry = clips.setdefault(name, {"favorite": False, "note": ""})
    entry["note"] = note
    _save_metadata(clips_dir, clips)


def prune(clips_dir: Path, existing_names: set[str]) -> None:
    """Drop metadata for filenames no longer present in clips_dir -- called
    on gallery refresh so renames and deletions (which orphan entries, keys
    being full filenames) don't accumulate forever. Writes only when
    something actually changed: refresh runs often and a no-op rewrite
    would still be a pointless disk touch.
    """
    clips = load_metadata(clips_dir)
    kept = {name: entry for name, entry in clips.items() if name in existing_names}
    if len(kept) != len(clips):
        _save_metadata(clips_dir, kept)
