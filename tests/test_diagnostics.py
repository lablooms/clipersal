import zipfile
from pathlib import Path

from clipersal import diagnostics
from clipersal.monitors import MonitorInfo


def _names(zip_path: Path) -> set[str]:
    with zipfile.ZipFile(zip_path) as bundle:
        return set(bundle.namelist())


def test_export_includes_logs_config_and_system_txt(tmp_path: Path) -> None:
    log_path = tmp_path / "clipersal.log"
    log_path.write_text("live log\n", encoding="utf-8")
    for suffix in (".1", ".2", ".3"):
        (tmp_path / f"clipersal.log{suffix}").write_text(f"rotated {suffix}\n", encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text('{"buffer_seconds": 60}', encoding="utf-8")
    buffer_dir = tmp_path / "buffer"
    buffer_dir.mkdir()
    (buffer_dir / "ffmpeg.log").write_text("ffmpeg output\n", encoding="utf-8")

    target = tmp_path / "out" / "clipersal-diagnostics.zip"
    result = diagnostics.export_diagnostics_zip(target, log_path, config_path, buffer_dir, {"os": "test-os"})

    assert result == target
    assert _names(target) == {
        "clipersal.log",
        "clipersal.log.1",
        "clipersal.log.2",
        "clipersal.log.3",
        "ffmpeg.log",
        "config.json",
        "system.txt",
    }
    with zipfile.ZipFile(target) as bundle:
        assert bundle.read("system.txt").decode() == "os: test-os\n"
        assert bundle.read("config.json").decode() == '{"buffer_seconds": 60}'


def test_export_tolerates_missing_sources(tmp_path: Path) -> None:
    # Nothing exists: no log, no rotations, no ffmpeg.log, no config, no
    # buffer dir at all -- the zip must still be written, with system.txt.
    target = tmp_path / "diag.zip"
    result = diagnostics.export_diagnostics_zip(
        target,
        tmp_path / "missing.log",
        tmp_path / "missing-config.json",
        tmp_path / "missing-buffer",
        {"app_version": "0.0.0-test"},
    )

    assert result == target
    assert _names(target) == {"system.txt"}


def test_export_tolerates_a_none_buffer_dir(tmp_path: Path) -> None:
    log_path = tmp_path / "clipersal.log"
    log_path.write_text("live log\n", encoding="utf-8")

    target = tmp_path / "diag.zip"
    result = diagnostics.export_diagnostics_zip(target, log_path, tmp_path / "missing.json", None, {})

    assert result == target
    assert _names(target) == {"clipersal.log", "system.txt"}


def test_export_returns_none_and_never_raises_on_total_failure(tmp_path: Path) -> None:
    # A directory where the zip file itself should go -- ZipFile() can't
    # create it, which is the one failure that has no partial answer.
    assert (
        diagnostics.export_diagnostics_zip(tmp_path, tmp_path / "x.log", tmp_path / "x.json", None, {}) is None
    )


def test_export_skips_an_unreadable_source_but_keeps_the_rest(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "clipersal.log"
    log_path.write_text("live log\n", encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    real_write = zipfile.ZipFile.write

    def flaky_write(self, filename, arcname=None, *args, **kwargs):
        if Path(filename).name == "config.json":
            raise OSError("locked by another process (fake)")
        return real_write(self, filename, arcname, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "write", flaky_write)

    target = tmp_path / "diag.zip"
    result = diagnostics.export_diagnostics_zip(target, log_path, config_path, None, {"k": "v"})

    assert result == target
    assert _names(target) == {"clipersal.log", "system.txt"}


# ---- collect_facts ------------------------------------------------------------


def test_collect_facts_contains_the_static_baseline() -> None:
    facts = diagnostics.collect_facts(ffmpeg_path=None, encoder=None)
    assert facts["app_version"]
    assert facts["python"]
    assert facts["os"]
    assert facts["session_type"]
    # No ffmpeg/encoder given -> those lines are omitted, never "None".
    assert "ffmpeg_path" not in facts
    assert "encoder" not in facts
    assert all(value and value != "None" for value in facts.values())


def test_collect_facts_includes_ffmpeg_and_encoder_when_given(monkeypatch) -> None:
    class _Result:
        stdout = "ffmpeg version 7.1-test\nsecond line\n"

    monkeypatch.setattr(diagnostics.subprocess, "run", lambda *a, **k: _Result())
    facts = diagnostics.collect_facts(ffmpeg_path="ffmpeg", encoder="libx264")
    assert facts["ffmpeg_path"] == "ffmpeg"
    assert facts["ffmpeg_version"] == "ffmpeg version 7.1-test"
    assert facts["encoder"] == "libx264"


def test_collect_facts_omits_ffmpeg_version_when_the_probe_fails(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise OSError("ffmpeg not found (fake)")

    monkeypatch.setattr(diagnostics.subprocess, "run", boom)
    facts = diagnostics.collect_facts(ffmpeg_path="ffmpeg", encoder="libx264")
    assert "ffmpeg_version" not in facts
    assert facts["ffmpeg_path"] == "ffmpeg"  # the path itself is still recorded


def test_collect_facts_omits_monitors_when_enumeration_fails(monkeypatch) -> None:
    def boom(os_):
        raise OSError("display unavailable (fake)")

    monkeypatch.setattr(diagnostics.monitors, "list_monitors", boom)
    facts = diagnostics.collect_facts()
    assert "monitors" not in facts


def test_collect_facts_summarizes_monitors(monkeypatch) -> None:
    monkeypatch.setattr(
        diagnostics.monitors,
        "list_monitors",
        lambda os_: [
            MonitorInfo(index=0, name="A", x=0, y=0, width=1920, height=1080, is_primary=True),
            MonitorInfo(index=1, name="B", x=1920, y=0, width=2560, height=1440, is_primary=False),
        ],
    )
    facts = diagnostics.collect_facts()
    assert facts["monitors"] == "monitor 1: 1920x1080 (primary), monitor 2: 2560x1440"
