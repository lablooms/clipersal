from pathlib import Path

from clipersal.config import build_arg_parser, config_from_args
from clipersal.config_store import load_overrides


def test_hardcoded_defaults_used_when_nothing_persisted_and_no_cli_flags() -> None:
    args = build_arg_parser(persisted={}).parse_args([])

    assert args.buffer_seconds == 60
    assert args.bitrate == "8M"
    assert args.encoder is None
    assert args.filename_template == "{window}-{date}-{time}"
    assert args.clip_retention_days == 0
    assert args.quality_preset == "custom"


def test_persisted_values_override_hardcoded_defaults() -> None:
    persisted = {
        "buffer_seconds": 90,
        "clips_dir": "/tmp/clips",
        "hotkey_combo": "<ctrl>+<shift>+s",
        "video_bitrate": "12M",
        "encoder_override": "libx264",
    }

    args = build_arg_parser(persisted=persisted).parse_args([])

    assert args.buffer_seconds == 90
    assert args.clips_dir == Path("/tmp/clips")
    assert args.hotkey == "<ctrl>+<shift>+s"
    assert args.bitrate == "12M"
    assert args.encoder == "libx264"


def test_cli_flags_override_persisted_values() -> None:
    persisted = {"buffer_seconds": 90, "video_bitrate": "12M"}

    args = build_arg_parser(persisted=persisted).parse_args(["--buffer-seconds", "45", "--bitrate", "4M"])

    assert args.buffer_seconds == 45
    assert args.bitrate == "4M"


def test_config_from_args_reflects_persisted_defaults() -> None:
    persisted = {"buffer_seconds": 120, "encoder_override": "h264_nvenc"}
    args = build_arg_parser(persisted=persisted).parse_args([])

    config = config_from_args(args)

    assert config.buffer_seconds == 120
    assert config.encoder_override == "h264_nvenc"


def test_filename_template_and_retention_persisted_and_overridable() -> None:
    persisted = {"filename_template": "{datetime}-recording", "clip_retention_days": 14}

    args = build_arg_parser(persisted=persisted).parse_args(["--clip-retention-days", "30"])
    config = config_from_args(args)

    assert config.filename_template == "{datetime}-recording"
    assert config.clip_retention_days == 30


def test_filename_template_default_is_window_but_a_persisted_template_still_wins() -> None:
    # The hardcoded default changed to the {window} template, but an
    # existing user's persisted template keeps applying -- argparse defaults
    # are seeded from the config file, so only a fresh install sees the new
    # default.
    args = build_arg_parser(persisted={}).parse_args([])
    assert config_from_args(args).filename_template == "{window}-{date}-{time}"

    args = build_arg_parser(persisted={"filename_template": "clip-{date}-{time}"}).parse_args([])
    assert config_from_args(args).filename_template == "clip-{date}-{time}"


def test_launch_on_startup_defaults_false() -> None:
    args = build_arg_parser(persisted={}).parse_args([])

    assert args.launch_on_startup is False


def test_launch_on_startup_persisted_value_used() -> None:
    args = build_arg_parser(persisted={"launch_on_startup": True}).parse_args([])
    config = config_from_args(args)

    assert config.launch_on_startup is True


def test_launch_on_startup_cli_flag_overrides_persisted() -> None:
    args = build_arg_parser(persisted={"launch_on_startup": True}).parse_args(["--no-launch-on-startup"])
    config = config_from_args(args)

    assert config.launch_on_startup is False


def test_check_for_updates_defaults_true() -> None:
    args = build_arg_parser(persisted={}).parse_args([])

    assert args.check_for_updates is True


def test_check_for_updates_persisted_value_used() -> None:
    args = build_arg_parser(persisted={"check_for_updates": False}).parse_args([])
    config = config_from_args(args)

    assert config.check_for_updates is False


def test_check_for_updates_cli_flag_overrides_persisted() -> None:
    args = build_arg_parser(persisted={"check_for_updates": True}).parse_args(["--no-check-for-updates"])
    config = config_from_args(args)

    assert config.check_for_updates is False


def test_theme_mode_defaults_system() -> None:
    # "system" = follow the OS dark-mode setting -- the new default. An old
    # config file (no theme_mode key) gets here via the persisted fallback,
    # and the old boolean's false migrates to the same value (config_store).
    args = build_arg_parser(persisted={}).parse_args([])
    config = config_from_args(args)

    assert config.theme_mode == "system"


def test_theme_mode_persisted_value_used() -> None:
    args = build_arg_parser(persisted={"theme_mode": "dark"}).parse_args([])
    config = config_from_args(args)

    assert config.theme_mode == "dark"


def test_theme_mode_cli_flag_overrides_persisted() -> None:
    args = build_arg_parser(persisted={"theme_mode": "dark"}).parse_args(["--theme-mode", "light"])
    config = config_from_args(args)

    assert config.theme_mode == "light"


def test_dark_mode_flag_is_a_deprecated_alias_for_dark() -> None:
    # Kept so old launch scripts don't break; --theme-mode is the real flag.
    args = build_arg_parser(persisted={}).parse_args(["--dark-mode"])
    config = config_from_args(args)

    assert config.theme_mode == "dark"


def test_quality_preset_persisted_value_used() -> None:
    args = build_arg_parser(persisted={"quality_preset": "quality"}).parse_args([])
    config = config_from_args(args)

    assert config.quality_preset == "quality"


def test_quality_preset_cli_flag_overrides_persisted() -> None:
    args = build_arg_parser(persisted={"quality_preset": "quality"}).parse_args(
        ["--quality-preset", "performance"]
    )
    config = config_from_args(args)

    assert config.quality_preset == "performance"


def test_audio_volumes_default_to_100() -> None:
    args = build_arg_parser(persisted={}).parse_args([])
    config = config_from_args(args)

    assert config.desktop_volume == 100
    assert config.mic_volume == 100


def test_audio_volumes_persisted_values_used() -> None:
    args = build_arg_parser(persisted={"desktop_volume": 150, "mic_volume": 50}).parse_args([])
    config = config_from_args(args)

    assert config.desktop_volume == 150
    assert config.mic_volume == 50


def test_audio_volumes_cli_flags_override_persisted() -> None:
    args = build_arg_parser(persisted={"desktop_volume": 150, "mic_volume": 50}).parse_args(
        ["--desktop-volume", "80", "--mic-volume", "120"]
    )
    config = config_from_args(args)

    assert config.desktop_volume == 80
    assert config.mic_volume == 120


def test_default_buffer_dir_is_a_fresh_temp_dir_marked_for_cleanup() -> None:
    args = build_arg_parser(persisted={}).parse_args([])
    config = config_from_args(args)

    assert config.buffer_dir_is_temp is True
    assert config.buffer_dir.exists()
    # The dir is empty at construction -- remove it so the test itself doesn't
    # leak the very temp dirs the shutdown cleanup was built for.
    config.buffer_dir.rmdir()


def test_user_supplied_buffer_dir_is_not_marked_for_cleanup(tmp_path: Path) -> None:
    args = build_arg_parser(persisted={}).parse_args(["--buffer-dir", str(tmp_path / "buf")])
    config = config_from_args(args)

    assert config.buffer_dir == tmp_path / "buf"
    assert config.buffer_dir_is_temp is False


def test_wrong_typed_persisted_config_does_not_crash_startup_parsing(tmp_path: Path) -> None:
    # The reported bug: wrong-typed values in a hand-edited config used to
    # kill startup -- argparse SystemExit on {"buffer_seconds": "abc"} (a str
    # default converted through type=int), TypeError on {"clips_dir": 123} in
    # Path(). load_overrides now drops those values, so parsing falls back to
    # the hardcoded defaults, same as a corrupt-JSON config.
    path = tmp_path / "config.json"
    path.write_text('{"buffer_seconds": "abc", "clips_dir": 123}', encoding="utf-8")

    args = build_arg_parser(persisted=load_overrides(path)).parse_args([])

    assert args.buffer_seconds == 60
    assert args.clips_dir == Path.home() / "Videos" / "Clipersal"


def test_framerate_defaults_to_30_and_persisted_value_seeds_it() -> None:
    args = build_arg_parser(persisted={}).parse_args([])
    assert config_from_args(args).framerate == 30

    args = build_arg_parser(persisted={"framerate": 60}).parse_args([])
    assert config_from_args(args).framerate == 60


def test_framerate_cli_flag_overrides_persisted() -> None:
    args = build_arg_parser(persisted={"framerate": 60}).parse_args(["--framerate", "24"])
    assert config_from_args(args).framerate == 24


def test_resolution_scale_defaults_to_native() -> None:
    args = build_arg_parser(persisted={}).parse_args([])
    assert config_from_args(args).resolution_scale == "native"


def test_resolution_scale_persisted_value_used() -> None:
    args = build_arg_parser(persisted={"resolution_scale": "1080p"}).parse_args([])
    assert config_from_args(args).resolution_scale == "1080p"


def test_resolution_scale_cli_flag_overrides_persisted() -> None:
    args = build_arg_parser(persisted={"resolution_scale": "720p"}).parse_args(["--resolution-scale", "1080p"])
    assert config_from_args(args).resolution_scale == "1080p"


def test_resolution_scale_rejects_unknown_value() -> None:
    import pytest

    with pytest.raises(SystemExit):
        build_arg_parser(persisted={}).parse_args(["--resolution-scale", "4k"])


def test_quick_save_and_screenshot_hotkeys_default_to_disabled() -> None:
    args = build_arg_parser(persisted={}).parse_args([])
    config = config_from_args(args)

    assert config.quick_save_hotkey_1 == ""
    assert config.quick_save_seconds_1 == 30
    assert config.quick_save_hotkey_2 == ""
    assert config.quick_save_seconds_2 == 60
    assert config.screenshot_hotkey == ""


def test_quick_save_and_screenshot_hotkeys_persisted_values_used() -> None:
    persisted = {
        "quick_save_hotkey_1": "<ctrl>+1",
        "quick_save_seconds_1": 15,
        "quick_save_hotkey_2": "<ctrl>+2",
        "quick_save_seconds_2": 45,
        "screenshot_hotkey": "<ctrl>+<f12>",
    }

    args = build_arg_parser(persisted=persisted).parse_args([])
    config = config_from_args(args)

    assert config.quick_save_hotkey_1 == "<ctrl>+1"
    assert config.quick_save_seconds_1 == 15
    assert config.quick_save_hotkey_2 == "<ctrl>+2"
    assert config.quick_save_seconds_2 == 45
    assert config.screenshot_hotkey == "<ctrl>+<f12>"


def test_quick_save_and_screenshot_hotkeys_cli_flags_override_persisted() -> None:
    persisted = {"quick_save_hotkey_1": "<ctrl>+1", "quick_save_seconds_1": 15}

    args = build_arg_parser(persisted=persisted).parse_args(
        ["--quick-save-hotkey-1", "<ctrl>+9", "--quick-save-seconds-1", "25", "--screenshot-hotkey", "<ctrl>+0"]
    )
    config = config_from_args(args)

    assert config.quick_save_hotkey_1 == "<ctrl>+9"
    assert config.quick_save_seconds_1 == 25
    assert config.screenshot_hotkey == "<ctrl>+0"


# ---- 0.1.4 fields: clips size cap / save sound -----------------------------------


def test_wave5_fields_default_to_preexisting_behavior() -> None:
    args = build_arg_parser(persisted={}).parse_args([])
    config = config_from_args(args)

    # 0 = unlimited, the toggle off -- an old config file (keys absent)
    # produces exactly the pre-0.1.4 behavior.
    assert config.clips_max_gb == 0
    assert config.save_sound_enabled is False


def test_wave5_fields_persisted_values_used() -> None:
    persisted = {
        "clips_max_gb": 10,
        "save_sound_enabled": True,
    }

    args = build_arg_parser(persisted=persisted).parse_args([])
    config = config_from_args(args)

    assert config.clips_max_gb == 10
    assert config.save_sound_enabled is True


def test_wave5_fields_cli_flags_override_persisted() -> None:
    persisted = {
        "clips_max_gb": 10,
        "save_sound_enabled": True,
    }

    args = build_arg_parser(persisted=persisted).parse_args(
        [
            "--clips-max-gb", "25",
            "--no-save-sound-enabled",
        ]
    )
    config = config_from_args(args)

    assert config.clips_max_gb == 25
    assert config.save_sound_enabled is False
