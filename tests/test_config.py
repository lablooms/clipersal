from pathlib import Path

from clipersal.config import build_arg_parser, config_from_args


def test_hardcoded_defaults_used_when_nothing_persisted_and_no_cli_flags() -> None:
    args = build_arg_parser(persisted={}).parse_args([])

    assert args.buffer_seconds == 60
    assert args.bitrate == "8M"
    assert args.encoder is None
    assert args.filename_template == "clip-{date}-{time}"
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
