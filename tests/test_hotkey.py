from clipersal.hotkey import is_supported, is_valid_combo
from clipersal.platform_detect import OS, LinuxSessionType


def test_windows_always_supported() -> None:
    assert is_supported(OS.WINDOWS, None) is True


def test_linux_x11_supported() -> None:
    assert is_supported(OS.LINUX, LinuxSessionType.X11) is True


def test_linux_wayland_not_supported() -> None:
    assert is_supported(OS.LINUX, LinuxSessionType.WAYLAND) is False


def test_linux_unknown_session_not_supported() -> None:
    assert is_supported(OS.LINUX, LinuxSessionType.UNKNOWN) is False


def test_macos_not_supported_yet() -> None:
    assert is_supported(OS.MACOS, None) is False


# ---- is_valid_combo: the gate that keeps free-typed garbage out of the
# persisted hotkey_combo (which is rebound at every launch) -------------------


def test_is_valid_combo_accepts_pynput_format_combos() -> None:
    assert is_valid_combo("<ctrl>+<alt>+r") is True
    assert is_valid_combo("<shift>+k") is True
    assert is_valid_combo("a") is True


def test_is_valid_combo_rejects_unparseable_text() -> None:
    assert is_valid_combo("Press keys...") is False  # the recorder's placeholder
    assert is_valid_combo("garbage combo") is False
    assert is_valid_combo("<bogus>") is False


def test_is_valid_combo_rejects_empty_and_whitespace() -> None:
    assert is_valid_combo("") is False
    assert is_valid_combo("   ") is False


# ---- HotkeyListener: combo->callback mapping (quick-save / screenshot
# bindings ride the same GlobalHotKeys instance as the main save combo) ----


def _fake_global_hotkeys(monkeypatch, captured):
    import pynput.keyboard as pynput_keyboard

    class FakeGlobalHotKeys:
        def __init__(self, mapping):
            captured["mapping"] = dict(mapping)

        def start(self):
            captured["started"] = True

        def stop(self):
            captured["stopped"] = True

    monkeypatch.setattr(pynput_keyboard, "GlobalHotKeys", FakeGlobalHotKeys)


def test_single_combo_constructor_binds_exactly_that_combo(monkeypatch) -> None:
    from clipersal.hotkey import HotkeyListener

    captured = {}
    _fake_global_hotkeys(monkeypatch, captured)
    callback = object()

    listener = HotkeyListener("<ctrl>+<alt>+r", callback)
    listener.start()
    listener.stop()

    assert captured["mapping"] == {"<ctrl>+<alt>+r": callback}
    assert captured["started"] is True
    assert captured["stopped"] is True


def test_from_mapping_binds_every_combo_to_its_own_callback(monkeypatch) -> None:
    from clipersal.hotkey import HotkeyListener

    captured = {}
    _fake_global_hotkeys(monkeypatch, captured)
    save_cb = object()
    quick_cb = object()
    shot_cb = object()

    listener = HotkeyListener.from_mapping(
        {"<ctrl>+<alt>+r": save_cb, "<ctrl>+1": quick_cb, "<ctrl>+<f12>": shot_cb}
    )
    listener.start()

    assert captured["mapping"] == {
        "<ctrl>+<alt>+r": save_cb,
        "<ctrl>+1": quick_cb,
        "<ctrl>+<f12>": shot_cb,
    }


def test_from_mapping_filters_out_empty_combos(monkeypatch) -> None:
    # "" is the persisted "disabled" value for the quick-save/screenshot
    # bindings -- it must never reach pynput's parser.
    from clipersal.hotkey import HotkeyListener

    captured = {}
    _fake_global_hotkeys(monkeypatch, captured)

    listener = HotkeyListener.from_mapping(
        {"<ctrl>+<alt>+r": object(), "": object(), "   ": object()}
    )
    listener.start()

    assert list(captured["mapping"]) == ["<ctrl>+<alt>+r"]
