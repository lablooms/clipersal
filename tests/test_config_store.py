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


def test_save_then_load_round_trips_audio_volumes(tmp_path: Path) -> None:
    path = tmp_path / "config.json"

    save_overrides({"desktop_volume": 150, "mic_volume": 50}, path)

    assert load_overrides(path) == {"desktop_volume": 150, "mic_volume": 50}
