import json
from pathlib import Path

import clipersal.clip_metadata as clip_metadata
from clipersal.clip_metadata import (
    favorites,
    is_favorite,
    load_metadata,
    note_for,
    prune,
    set_favorite,
    set_note,
)


def _sidecar(clips_dir: Path) -> Path:
    return clips_dir / ".clipmeta.json"


def _read_sidecar(clips_dir: Path) -> dict:
    return json.loads(_sidecar(clips_dir).read_text(encoding="utf-8"))


# ---- load_metadata tolerance -------------------------------------------------


def test_load_metadata_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert load_metadata(tmp_path) == {}


def test_load_metadata_returns_empty_when_clips_dir_missing(tmp_path: Path) -> None:
    assert load_metadata(tmp_path / "no-such-dir") == {}


def test_load_metadata_returns_empty_for_corrupt_json(tmp_path: Path) -> None:
    _sidecar(tmp_path).write_text("{not valid json", encoding="utf-8")

    assert load_metadata(tmp_path) == {}


def test_load_metadata_returns_empty_for_wrong_shape(tmp_path: Path) -> None:
    # A top-level list, or a "clips" that isn't a mapping, reads as "no
    # metadata yet" -- never as an error the caller has to handle.
    _sidecar(tmp_path).write_text('["clip-a.mp4"]', encoding="utf-8")
    assert load_metadata(tmp_path) == {}

    _sidecar(tmp_path).write_text('{"version": 1, "clips": ["clip-a.mp4"]}', encoding="utf-8")
    assert load_metadata(tmp_path) == {}


def test_load_metadata_skips_malformed_entries_but_keeps_valid_ones(tmp_path: Path) -> None:
    _sidecar(tmp_path).write_text(
        json.dumps(
            {
                "version": 1,
                "clips": {
                    "clip-a.mp4": {"favorite": True, "note": "keep"},
                    "clip-b.mp4": "not-a-dict",
                    "clip-c.mp4": {"favorite": "yes", "note": 42},
                },
            }
        ),
        encoding="utf-8",
    )

    assert load_metadata(tmp_path) == {
        "clip-a.mp4": {"favorite": True, "note": "keep"},
        "clip-c.mp4": {"favorite": True, "note": ""},  # coerced, not trusted
    }


# ---- favorites ---------------------------------------------------------------


def test_favorite_round_trip_uses_full_filenames_not_stems(tmp_path: Path) -> None:
    set_favorite(tmp_path, "clip-a.mp4", True)
    set_favorite(tmp_path, "clip-b.mp4", True)

    assert is_favorite(tmp_path, "clip-a.mp4") is True
    assert not is_favorite(tmp_path, "clip-c.mp4")
    # Full filenames, extension included -- never stems.
    assert favorites(tmp_path) == {"clip-a.mp4", "clip-b.mp4"}

    set_favorite(tmp_path, "clip-a.mp4", False)

    assert is_favorite(tmp_path, "clip-a.mp4") is False
    assert favorites(tmp_path) == {"clip-b.mp4"}


def test_unfavorite_without_note_drops_entry_from_file(tmp_path: Path) -> None:
    # Entries with neither flag nor text are dropped entirely, so the
    # sidecar doesn't fill up with {"favorite": false, "note": ""}
    # tombstones every time someone toggles a star off.
    set_favorite(tmp_path, "clip-a.mp4", True)
    set_favorite(tmp_path, "clip-a.mp4", False)

    assert _read_sidecar(tmp_path)["clips"] == {}


def test_favorites_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    assert favorites(tmp_path / "no-such-dir") == set()
    assert is_favorite(tmp_path / "no-such-dir", "clip-a.mp4") is False


# ---- notes -------------------------------------------------------------------


def test_note_round_trip(tmp_path: Path) -> None:
    assert note_for(tmp_path, "clip-a.mp4") == ""

    set_note(tmp_path, "clip-a.mp4", "the good one")

    assert note_for(tmp_path, "clip-a.mp4") == "the good one"
    assert _read_sidecar(tmp_path)["clips"]["clip-a.mp4"] == {"favorite": False, "note": "the good one"}


def test_empty_note_removes_key_but_keeps_favorite_entry(tmp_path: Path) -> None:
    set_favorite(tmp_path, "clip-a.mp4", True)
    set_note(tmp_path, "clip-a.mp4", "the good one")
    set_note(tmp_path, "clip-a.mp4", "")

    entry = _read_sidecar(tmp_path)["clips"]["clip-a.mp4"]
    assert entry == {"favorite": True}  # note key gone, entry kept for the star
    assert note_for(tmp_path, "clip-a.mp4") == ""


def test_empty_note_without_favorite_drops_entry_from_file(tmp_path: Path) -> None:
    set_note(tmp_path, "clip-a.mp4", "the good one")
    set_note(tmp_path, "clip-a.mp4", "")

    assert _read_sidecar(tmp_path)["clips"] == {}


# ---- prune -------------------------------------------------------------------


def test_prune_drops_entries_for_clips_no_longer_present(tmp_path: Path) -> None:
    # Renames/deletions orphan entries because keys are full filenames;
    # prune() is what garbage-collects them on gallery refresh.
    set_favorite(tmp_path, "clip-still-here.mp4", True)
    set_note(tmp_path, "clip-renamed-away.mp4", "orphaned")

    prune(tmp_path, {"clip-still-here.mp4"})

    assert _read_sidecar(tmp_path)["clips"] == {"clip-still-here.mp4": {"favorite": True}}
    assert favorites(tmp_path) == {"clip-still-here.mp4"}


def test_prune_skips_write_when_nothing_changed(tmp_path: Path, monkeypatch) -> None:
    # Refresh runs often; a no-op prune must not even touch the disk.
    set_favorite(tmp_path, "clip-a.mp4", True)
    writes = []
    real_save = clip_metadata._save_metadata

    def recording_save(clips_dir, clips):
        writes.append(True)
        return real_save(clips_dir, clips)

    monkeypatch.setattr(clip_metadata, "_save_metadata", recording_save)

    prune(tmp_path, {"clip-a.mp4", "clip-b.mp4"})

    assert writes == []


# ---- atomic writes + OSError tolerance ----------------------------------------


def test_write_is_tmp_file_then_replace(tmp_path: Path, monkeypatch) -> None:
    replaced = []
    real_replace = Path.replace

    def recording_replace(self: Path, target: Path):
        replaced.append((self, target))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", recording_replace)

    set_favorite(tmp_path, "clip-a.mp4", True)

    # The real path is only ever swapped in via replace() -- a crash
    # mid-write can strand a .tmp file but never a half-written sidecar.
    assert replaced == [(tmp_path / ".clipmeta.json.tmp", _sidecar(tmp_path))]
    assert not (tmp_path / ".clipmeta.json.tmp").exists()
    assert _read_sidecar(tmp_path)["version"] == 1


def test_failed_write_leaves_existing_sidecar_intact_and_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    set_favorite(tmp_path, "clip-a.mp4", True)
    before = _sidecar(tmp_path).read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(clip_metadata.json, "dump", boom)

    set_favorite(tmp_path, "clip-b.mp4", True)  # must not raise

    assert _sidecar(tmp_path).read_text(encoding="utf-8") == before
    assert favorites(tmp_path) == {"clip-a.mp4"}  # the lost write is simply absent


def test_all_functions_tolerate_a_clips_dir_that_is_not_a_directory(tmp_path: Path) -> None:
    # Opening "<file>/.clipmeta.json" raises NotADirectoryError (an OSError)
    # on every platform -- the read side and the write side both swallow it.
    not_a_dir = tmp_path / "clip-a.mp4"
    not_a_dir.write_bytes(b"fake mp4 data")

    assert load_metadata(not_a_dir) == {}
    set_favorite(not_a_dir, "clip-a.mp4", True)  # must not raise
    set_note(not_a_dir, "clip-a.mp4", "note")  # must not raise
    prune(not_a_dir, set())  # must not raise
    assert favorites(not_a_dir) == set()
