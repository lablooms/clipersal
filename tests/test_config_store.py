import json
import logging
from pathlib import Path

from clipersal.config_store import PERSISTED_KEYS, load_overrides, save_overrides


def test_load_overrides_returns_empty_dict_when_file_missing(tmp_path: Path) -> None:
    assert load_overrides(tmp_path / "does-not-exist.json") == {}


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    values = {
        "buffer_seconds": 90,
        "clips_dir": "/home/user/Videos",
        "hotkey_combo": "<ctrl>+<alt>+r",
        "video_bitrate": "12M",
        "encoder_override": "libx264",
        "filename_template": "clip-{date}-{time}",
        "clip_retention_days": 14,
        "launch_on_startup": True,
        "check_for_updates": True,
    }

    save_overrides(values, path)
    loaded = load_overrides(path)

    assert loaded == values


def test_save_overrides_ignores_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.json"

    save_overrides({"buffer_seconds": 30, "bogus_key": "nope"}, path)
    loaded = load_overrides(path)

    assert loaded == {"buffer_seconds": 30}


def test_load_overrides_ignores_unknown_keys_in_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"buffer_seconds": 45, "something_else": true}', encoding="utf-8")

    assert load_overrides(path) == {"buffer_seconds": 45}


def test_load_overrides_recovers_from_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not valid json", encoding="utf-8")

    assert load_overrides(path) == {}


def test_load_overrides_recovers_from_non_object_json(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    assert load_overrides(path) == {}


def test_save_overrides_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "config.json"

    save_overrides({"buffer_seconds": 60}, path)

    assert path.exists()
    assert load_overrides(path) == {"buffer_seconds": 60}


def test_save_overrides_encoder_override_none_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "config.json"

    save_overrides({"encoder_override": None}, path)

    assert load_overrides(path) == {"encoder_override": None}


def test_persisted_keys_include_audio_volumes() -> None:
    assert "desktop_volume" in PERSISTED_KEYS
    assert "mic_volume" in PERSISTED_KEYS


def test_persisted_keys_include_dark_mode() -> None:
    assert "dark_mode" in PERSISTED_KEYS


def test_save_then_load_round_trips_dark_mode(tmp_path: Path) -> None:
    path = tmp_path / "config.json"

    save_overrides({"dark_mode": True}, path)

    assert load_overrides(path) == {"dark_mode": True}


def test_save_then_load_round_trips_audio_volumes(tmp_path: Path) -> None:
    path = tmp_path / "config.json"

    save_overrides({"desktop_volume": 150, "mic_volume": 50}, path)

    assert load_overrides(path) == {"desktop_volume": 150, "mic_volume": 50}


# ---- wrong-typed values from a hand-edited config ----------------------------


def test_load_overrides_drops_wrong_typed_values(tmp_path: Path) -> None:
    # The exact hand-edit typos that used to crash startup: {"buffer_seconds":
    # "abc"} reached argparse as a str default and SystemExit'd when converted
    # through type=int; {"clips_dir": 123} raised TypeError in Path(). A
    # corrupt-typed config must now behave like corrupt JSON: ignored.
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "buffer_seconds": "abc",
                "clips_dir": 123,
                "launch_on_startup": "yes",
                "desktop_volume": 1.5,
                "hotkey_combo": 42,
            }
        ),
        encoding="utf-8",
    )

    assert load_overrides(path) == {}


def test_load_overrides_keeps_valid_values_while_dropping_wrong_typed_ones(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "buffer_seconds": 90,
                "clips_dir": "/home/user/Videos",
                "encoder_override": None,
                "mic_device": None,
                "dark_mode": True,
                "clip_retention_days": "14",
            }
        ),
        encoding="utf-8",
    )

    assert load_overrides(path) == {
        "buffer_seconds": 90,
        "clips_dir": "/home/user/Videos",
        "encoder_override": None,
        "mic_device": None,
        "dark_mode": True,
    }


def test_load_overrides_rejects_bool_for_int_and_int_for_bool(tmp_path: Path) -> None:
    # bool is a subclass of int, but {"buffer_seconds": true} is a typo, not a
    # length -- and {"launch_on_startup": 1} is a number, not a JSON bool.
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "buffer_seconds": True,
                "desktop_volume": False,
                "launch_on_startup": 1,
                "check_for_updates": 0,
            }
        ),
        encoding="utf-8",
    )

    assert load_overrides(path) == {}


def test_load_overrides_logs_a_warning_for_dropped_values(tmp_path: Path, caplog) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"buffer_seconds": "abc"}', encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="clipersal.config_store"):
        assert load_overrides(path) == {}

    assert "buffer_seconds" in caplog.text
