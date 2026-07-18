from pathlib import Path

import pytest

from clipersal import update_check


# ---- version parsing / comparison ------------------------------------------


def test_is_newer_simple_patch_bump() -> None:
    assert update_check.is_newer("0.2.0", "0.1.0") is True


def test_is_newer_false_for_equal_versions() -> None:
    assert update_check.is_newer("0.1.0", "0.1.0") is False


def test_is_newer_false_for_older_candidate() -> None:
    assert update_check.is_newer("0.1.0", "0.2.0") is False


def test_is_newer_strips_leading_v() -> None:
    assert update_check.is_newer("v0.2.0", "0.1.0") is True


def test_is_newer_strips_prerelease_suffix() -> None:
    assert update_check.is_newer("0.2.0-beta", "0.1.0") is True


def test_is_newer_treats_suffix_stripped_version_as_equal_not_newer() -> None:
    # Documented simplification: "0.1.0" vs running "0.1.0-beta" compares
    # equal (not newer) -- a beta-to-stable promotion of the same numeric
    # version intentionally doesn't trigger a banner.
    assert update_check.is_newer("0.1.0", "0.1.0-beta") is False


def test_is_newer_pads_shorter_tuple() -> None:
    assert update_check.is_newer("2", "1.9.9") is True
    assert update_check.is_newer("1.9", "1.9.0") is False


def test_is_newer_false_for_unparseable_candidate() -> None:
    assert update_check.is_newer("not-a-version", "0.1.0") is False


def test_is_newer_false_for_unparseable_current() -> None:
    assert update_check.is_newer("0.2.0", "not-a-version") is False


# ---- fetch_latest_release ---------------------------------------------------


def test_fetch_latest_release_returns_none_for_empty_repo() -> None:
    calls = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return b"{}"

    assert update_check.fetch_latest_release("", fetch=fetch) is None
    assert calls == []


def test_fetch_latest_release_parses_valid_payload() -> None:
    def fetch(url: str) -> bytes:
        assert url == "https://api.github.com/repos/lablooms/clipersal/releases/latest"
        return b'{"tag_name": "v0.2.0", "html_url": "https://example.invalid/releases/tag/v0.2.0"}'

    result = update_check.fetch_latest_release("lablooms/clipersal", fetch=fetch)
    assert result == update_check.ReleaseInfo(version="v0.2.0", url="https://example.invalid/releases/tag/v0.2.0")


def test_fetch_latest_release_returns_none_when_fetch_raises() -> None:
    def fetch(url: str) -> bytes:
        raise OSError("network down")

    assert update_check.fetch_latest_release("lablooms/clipersal", fetch=fetch) is None


def test_fetch_latest_release_returns_none_for_malformed_json() -> None:
    def fetch(url: str) -> bytes:
        return b"not json"

    assert update_check.fetch_latest_release("lablooms/clipersal", fetch=fetch) is None


def test_fetch_latest_release_returns_none_for_missing_keys() -> None:
    def fetch(url: str) -> bytes:
        return b"{}"

    assert update_check.fetch_latest_release("lablooms/clipersal", fetch=fetch) is None


# ---- cache read/write --------------------------------------------------------


def test_load_cache_returns_empty_dict_when_file_missing(tmp_path: Path) -> None:
    assert update_check.load_cache(tmp_path / "does-not-exist.json") == {}


def test_save_then_load_cache_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "update_check_cache.json"
    values = {"last_checked": 12345.0, "available_version": "0.2.0", "available_url": "https://example.invalid"}

    update_check.save_cache(values, path)

    assert update_check.load_cache(path) == values


def test_load_cache_recovers_from_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "update_check_cache.json"
    path.write_text("{not valid json", encoding="utf-8")

    assert update_check.load_cache(path) == {}


def test_load_cache_recovers_from_non_object_json(tmp_path: Path) -> None:
    path = tmp_path / "update_check_cache.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    assert update_check.load_cache(path) == {}


def test_save_cache_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "update_check_cache.json"

    update_check.save_cache({"last_checked": 1.0}, path)

    assert path.exists()
    assert update_check.load_cache(path) == {"last_checked": 1.0}


# ---- check_for_update_once ---------------------------------------------------


def test_check_for_update_once_returns_none_for_unset_repo(tmp_path: Path) -> None:
    calls = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return b'{"tag_name": "v9.9.9", "html_url": "https://example.invalid"}'

    result = update_check.check_for_update_once(
        repo="", current_version="0.1.0", cache_path=tmp_path / "cache.json", fetch=fetch
    )
    assert result is None
    assert calls == []
    assert not (tmp_path / "cache.json").exists()


def test_check_for_update_once_returns_newer_release(tmp_path: Path) -> None:
    def fetch(url: str) -> bytes:
        return b'{"tag_name": "v0.2.0", "html_url": "https://example.invalid/v0.2.0"}'

    result = update_check.check_for_update_once(
        repo="lablooms/clipersal", current_version="0.1.0", cache_path=tmp_path / "cache.json", fetch=fetch
    )
    assert result == ("v0.2.0", "https://example.invalid/v0.2.0")


def test_check_for_update_once_returns_none_when_not_newer(tmp_path: Path) -> None:
    def fetch(url: str) -> bytes:
        return b'{"tag_name": "v0.1.0", "html_url": "https://example.invalid/v0.1.0"}'

    result = update_check.check_for_update_once(
        repo="lablooms/clipersal", current_version="0.1.0", cache_path=tmp_path / "cache.json", fetch=fetch
    )
    assert result is None


def test_check_for_update_once_returns_none_when_dismissed(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    update_check.save_cache({"dismissed_version": "v0.2.0"}, cache_path)

    def fetch(url: str) -> bytes:
        return b'{"tag_name": "v0.2.0", "html_url": "https://example.invalid/v0.2.0"}'

    result = update_check.check_for_update_once(
        repo="lablooms/clipersal", current_version="0.1.0", cache_path=cache_path, fetch=fetch
    )
    assert result is None


def test_check_for_update_once_records_last_checked(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"

    def fetch(url: str) -> bytes:
        return b'{"tag_name": "v0.1.0", "html_url": "https://example.invalid"}'

    update_check.check_for_update_once(
        repo="lablooms/clipersal", current_version="0.1.0", now=1000.0, cache_path=cache_path, fetch=fetch
    )

    assert update_check.load_cache(cache_path)["last_checked"] == 1000.0


def test_check_for_update_once_throttled_skips_network_call(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    update_check.save_cache({"last_checked": 1000.0}, cache_path)
    calls = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return b'{"tag_name": "v9.9.9", "html_url": "https://example.invalid"}'

    result = update_check.check_for_update_once(
        repo="lablooms/clipersal", current_version="0.1.0", now=1000.0 + 60, cache_path=cache_path, fetch=fetch
    )

    assert calls == []
    assert result is None  # nothing was ever cached as available, so nothing to show


def test_check_for_update_once_throttled_still_returns_cached_undismissed_update(tmp_path: Path) -> None:
    # Throttling must only suppress the network call, not a real, undismissed
    # update -- otherwise the banner would silently vanish on a same-day
    # relaunch before the user ever got to dismiss it.
    cache_path = tmp_path / "cache.json"
    update_check.save_cache(
        {"last_checked": 1000.0, "available_version": "v0.2.0", "available_url": "https://example.invalid/v0.2.0"},
        cache_path,
    )
    calls = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return b'{"tag_name": "v9.9.9", "html_url": "https://example.invalid"}'

    result = update_check.check_for_update_once(
        repo="lablooms/clipersal", current_version="0.1.0", now=1000.0 + 60, cache_path=cache_path, fetch=fetch
    )

    assert calls == []
    assert result == ("v0.2.0", "https://example.invalid/v0.2.0")


def test_check_for_update_once_throttled_cache_hit_still_respects_dismissal(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    update_check.save_cache(
        {
            "last_checked": 1000.0,
            "available_version": "v0.2.0",
            "available_url": "https://example.invalid/v0.2.0",
            "dismissed_version": "v0.2.0",
        },
        cache_path,
    )

    result = update_check.check_for_update_once(
        repo="lablooms/clipersal",
        current_version="0.1.0",
        now=1000.0 + 60,
        cache_path=cache_path,
        fetch=lambda url: b"{}",
    )

    assert result is None


def test_check_for_update_once_banner_clears_once_user_has_updated(tmp_path: Path) -> None:
    # Re-running is_newer against the *current* current_version on every call
    # (even the throttled path) means the banner naturally stops appearing
    # once the user actually installs the newer version.
    cache_path = tmp_path / "cache.json"
    update_check.save_cache(
        {"last_checked": 1000.0, "available_version": "v0.2.0", "available_url": "https://example.invalid/v0.2.0"},
        cache_path,
    )

    result = update_check.check_for_update_once(
        repo="lablooms/clipersal",
        current_version="0.2.0",  # user has since updated
        now=1000.0 + 60,
        cache_path=cache_path,
        fetch=lambda url: b"{}",
    )

    assert result is None


def test_check_for_update_once_past_throttle_calls_fetch_again(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    update_check.save_cache({"last_checked": 1000.0}, cache_path)
    calls = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return b'{"tag_name": "v0.2.0", "html_url": "https://example.invalid/v0.2.0"}'

    result = update_check.check_for_update_once(
        repo="lablooms/clipersal",
        current_version="0.1.0",
        now=1000.0 + update_check._CHECK_INTERVAL_SECONDS + 1,
        cache_path=cache_path,
        fetch=fetch,
    )

    assert len(calls) == 1
    assert result == ("v0.2.0", "https://example.invalid/v0.2.0")


def test_check_for_update_once_never_raises_when_fetch_raises(tmp_path: Path) -> None:
    def fetch(url: str) -> bytes:
        raise RuntimeError("boom")

    result = update_check.check_for_update_once(
        repo="lablooms/clipersal", current_version="0.1.0", cache_path=tmp_path / "cache.json", fetch=fetch
    )
    assert result is None


def test_check_for_update_once_never_raises_when_cache_path_unwritable(tmp_path: Path) -> None:
    # A directory where a file is expected -- save_cache's open() call raises,
    # which check_for_update_once's top-level try/except must swallow.
    bad_path = tmp_path / "cache_dir"
    bad_path.mkdir()

    def fetch(url: str) -> bytes:
        return b'{"tag_name": "v0.2.0", "html_url": "https://example.invalid"}'

    result = update_check.check_for_update_once(
        repo="lablooms/clipersal", current_version="0.1.0", cache_path=bad_path, fetch=fetch
    )
    assert result is None
