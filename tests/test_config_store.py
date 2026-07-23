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
        "filename_template": "my-clip-{datetime}",
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


def test_persisted_keys_include_theme_mode() -> None:
    assert "theme_mode" in PERSISTED_KEYS


def test_save_then_load_round_trips_theme_mode(tmp_path: Path) -> None:
    path = tmp_path / "config.json"

    save_overrides({"theme_mode": "dark"}, path)

    assert load_overrides(path) == {"theme_mode": "dark"}


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
                "theme_mode": "dark",
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
        "theme_mode": "dark",
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


def test_persisted_keys_include_framerate_and_resolution_scale() -> None:
    assert "framerate" in PERSISTED_KEYS
    assert "resolution_scale" in PERSISTED_KEYS


def test_persisted_keys_include_quick_save_and_screenshot_hotkeys() -> None:
    for key in (
        "quick_save_hotkey_1",
        "quick_save_seconds_1",
        "quick_save_hotkey_2",
        "quick_save_seconds_2",
        "screenshot_hotkey",
    ):
        assert key in PERSISTED_KEYS


def test_save_then_load_round_trips_wave2_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    values = {
        "framerate": 60,
        "resolution_scale": "1080p",
        "quick_save_hotkey_1": "<ctrl>+1",
        "quick_save_seconds_1": 15,
        "quick_save_hotkey_2": "",
        "quick_save_seconds_2": 60,
        "screenshot_hotkey": "<ctrl>+<f12>",
    }

    save_overrides(values, path)

    assert load_overrides(path) == values


def test_load_overrides_drops_wrong_typed_wave2_values(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "framerate": "60",
                "resolution_scale": 1080,
                "quick_save_seconds_1": "15",
                "screenshot_hotkey": 12,
            }
        ),
        encoding="utf-8",
    )

    assert load_overrides(path) == {}


# ---- 0.1.4 keys: clips size cap / save sound ---------------------------------------


def test_persisted_keys_include_wave5_keys() -> None:
    for key in ("clips_max_gb", "save_sound_enabled"):
        assert key in PERSISTED_KEYS


def test_save_then_load_round_trips_wave5_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    values = {
        "clips_max_gb": 10,
        "save_sound_enabled": True,
    }

    save_overrides(values, path)

    assert load_overrides(path) == values


def test_load_overrides_drops_wrong_typed_wave5_values(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "clips_max_gb": "10",
                "save_sound_enabled": "yes",
            }
        ),
        encoding="utf-8",
    )

    assert load_overrides(path) == {}


def test_load_overrides_ignores_dropped_overlay_keys_from_old_configs(tmp_path: Path) -> None:
    # Config files written before the overlay feature was removed still carry
    # overlay_* keys -- load_overrides only reads PERSISTED_KEYS, so they're
    # ignored like any other unknown key, not an error.
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "buffer_seconds": 90,
                "overlay_enabled": True,
                "overlay_corner": "bottom-left",
            }
        ),
        encoding="utf-8",
    )

    assert load_overrides(path) == {"buffer_seconds": 90}


def test_load_overrides_migrates_legacy_default_filename_template(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"filename_template": "clip-{date}-{time}"}', encoding="utf-8")

    assert load_overrides(path) == {"filename_template": "{window}-{date}-{time}"}


def test_load_overrides_keeps_custom_filename_template(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"filename_template": "my-clip-{datetime}"}', encoding="utf-8")

    assert load_overrides(path) == {"filename_template": "my-clip-{datetime}"}


def test_load_overrides_keeps_new_default_filename_template(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"filename_template": "{window}-{date}-{time}"}', encoding="utf-8")

    assert load_overrides(path) == {"filename_template": "{window}-{date}-{time}"}


def test_load_overrides_migrates_dark_mode_true_to_theme_mode_dark(tmp_path: Path) -> None:
    # true was an explicit choice under the old boolean setting, so it is
    # honored as a forced dark theme.
    path = tmp_path / "config.json"
    path.write_text('{"dark_mode": true}', encoding="utf-8")

    assert load_overrides(path) == {"theme_mode": "dark"}


def test_load_overrides_migrates_dark_mode_false_to_theme_mode_system(tmp_path: Path) -> None:
    # false was the old default for everyone, so it maps to the NEW default:
    # follow the OS dark-mode setting.
    path = tmp_path / "config.json"
    path.write_text('{"dark_mode": false}', encoding="utf-8")

    assert load_overrides(path) == {"theme_mode": "system"}


def test_load_overrides_does_not_migrate_when_theme_mode_already_present(tmp_path: Path) -> None:
    # A post-migration config file may still carry the stale boolean next to
    # the real key -- the real key wins and the boolean is ignored.
    path = tmp_path / "config.json"
    path.write_text('{"theme_mode": "light", "dark_mode": true}', encoding="utf-8")

    assert load_overrides(path) == {"theme_mode": "light"}


def test_load_overrides_ignores_lone_dark_mode_junk_value(tmp_path: Path) -> None:
    # A non-bool dark_mode was already junk; it migrates to "system" rather
    # than crashing or leaking the raw value through.
    path = tmp_path / "config.json"
    path.write_text('{"dark_mode": "yes"}', encoding="utf-8")

    assert load_overrides(path) == {"theme_mode": "system"}
