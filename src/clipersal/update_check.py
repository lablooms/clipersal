"""GitHub Releases update checker.

Notify-only: never downloads or installs anything, just tells the user a
newer release exists and lets them open it in a browser. Best-effort by
design, same as ffmpeg encoder detection / audio-loopback probing / tray
construction elsewhere in this app -- any failure (no internet, GitHub down,
malformed JSON, DNS failure, timeout) is swallowed and logged, never raised
up into startup.

"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from clipersal import config_store

log = logging.getLogger(__name__)

GITHUB_REPO = "lablooms/clipersal"

# The list endpoint, not /releases/latest: GitHub's /latest excludes
# pre-releases, and every release this project has published so far is
# marked pre-release, so /latest 404s and the check would stay a no-op
# until the first stable release. The list is newest-first; draft entries
# are skipped in fetch_latest_release (unauthenticated callers never see
# them anyway).
_API_URL_TEMPLATE = "https://api.github.com/repos/{repo}/releases"
_CACHE_FILENAME = "update_check_cache.json"
_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
_REQUEST_TIMEOUT_SECONDS = 5.0

FetchFn = Callable[[str], bytes]


@dataclass
class ReleaseInfo:
    version: str  # tag_name, as published (may have a leading "v")
    url: str  # html_url -- opened by the Home tab banner's Download button


def _default_fetch(url: str) -> bytes:
    # GitHub's API 403s any request with no User-Agent header -- a real,
    # documented API requirement, not defensive padding.
    request = urllib.request.Request(
        url, headers={"User-Agent": "Clipersal-update-check", "Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        return response.read()


def fetch_latest_release(repo: str, fetch: FetchFn = _default_fetch) -> ReleaseInfo | None:
    """The newest non-draft release (pre-releases included -- see the
    _API_URL_TEMPLATE comment), or None on literally any failure:
    unreachable network, non-200, bad JSON, a non-list payload, no
    releases at all. `fetch` is injectable so tests never touch a real
    socket, the same "inject the boundary function" convention toast_qt.py's
    _ThumbnailFetcher already uses for its ffmpeg call.
    """
    if not repo:
        return None
    try:
        raw = fetch(_API_URL_TEMPLATE.format(repo=repo))
        data = json.loads(raw)
        if not isinstance(data, list):
            return None
        for item in data:
            if not isinstance(item, dict) or item.get("draft"):
                continue
            return ReleaseInfo(version=str(item["tag_name"]), url=str(item["html_url"]))
        return None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _parse_version(text: str) -> tuple[int, ...] | None:
    """"v1.2.3-beta" -> (1, 2, 3). None for anything that doesn't reduce to
    a dotted run of integers -- callers must treat None as "cannot compare",
    never as "0", so a malformed tag can never falsely appear newer.
    """
    text = text.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    core = re.split(r"[-+]", text, maxsplit=1)[0]
    parts = core.split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def is_newer(candidate: str, current: str) -> bool:
    """False (never True, never raises) if either string is unparseable --
    "cannot determine" must never be treated as "yes, show the banner".
    Note: stripping the "-suffix" means "0.1.0" compares equal to (not newer
    than) "0.1.0-beta" -- a beta-to-stable promotion of the same numeric
    version intentionally won't trigger a banner.
    """
    candidate_parts = _parse_version(candidate)
    current_parts = _parse_version(current)
    if candidate_parts is None or current_parts is None:
        return False
    width = max(len(candidate_parts), len(current_parts))
    candidate_padded = candidate_parts + (0,) * (width - len(candidate_parts))
    current_padded = current_parts + (0,) * (width - len(current_parts))
    return candidate_padded > current_padded


def default_cache_path() -> Path:
    """A sibling of config.json, not inside it -- config_store.py's own
    docstring says only Settings-window-exposed fields belong in the main
    config file, enforced by its PERSISTED_KEYS allowlist. last_checked/
    available_version/available_url/dismissed_version are none of those, so
    they get their own small file, the same "separate cache" shape
    thumbnails.py already uses for its .thumbnails directory.
    """
    return config_store.default_config_path().parent / _CACHE_FILENAME


def load_cache(path: Path | None = None) -> dict[str, Any]:
    path = path or default_cache_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(values: dict[str, Any], path: Path | None = None) -> None:
    path = path or default_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same write-to-.tmp-then-replace() pattern as config_store.save_overrides
    # -- a crash mid-write must never leave a half-written, unparseable cache.
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(values, f, indent=2)
    tmp_path.replace(path)


def _cached_result(cache: dict[str, Any], current_version: str) -> tuple[str, str] | None:
    available_version = cache.get("available_version")
    available_url = cache.get("available_url")
    if not available_version or not available_url:
        return None
    if not is_newer(available_version, current_version):
        return None
    if available_version == cache.get("dismissed_version"):
        return None
    return available_version, available_url


def check_for_update_once(
    repo: str = GITHUB_REPO,
    current_version: str = "",
    now: float | None = None,
    cache_path: Path | None = None,
    fetch: FetchFn = _default_fetch,
) -> tuple[str, str] | None:
    """The one function cli.py calls, on a background thread, once per
    launch. Returns (version, url) to show a banner for, or None for
    "nothing to show" -- covers: repo unset, fetch failed, not actually
    newer, or already dismissed.

    Throttled to at most one network call per _CHECK_INTERVAL_SECONDS, but
    throttling only suppresses the *network call* -- a found-but-undismissed
    update is re-derived from the cache and still returned on a same-day
    relaunch, since otherwise it would silently vanish before the user
    dismissed it, reading as a bug rather than a deliberate rate-limit. This
    also means the banner naturally clears itself once the user actually
    updates: current_version no longer compares as older than the cached
    available_version.

    Never raises -- wraps its own body, the same "one best-effort function,
    one try/except, boring failure path" shape as cli.py's
    _another_instance_running.
    """
    try:
        if not repo:
            return None
        cache = load_cache(cache_path)
        now_ts = now if now is not None else time.time()
        last_checked = cache.get("last_checked")
        throttled = isinstance(last_checked, (int, float)) and (now_ts - last_checked) < _CHECK_INTERVAL_SECONDS

        if throttled:
            return _cached_result(cache, current_version)

        release = fetch_latest_release(repo, fetch=fetch)
        cache["last_checked"] = now_ts
        if release is not None:
            cache["available_version"] = release.version
            cache["available_url"] = release.url
        save_cache(cache, cache_path)

        return _cached_result(cache, current_version) if release is not None else None
    except Exception:  # noqa: BLE001 -- a background update check must never crash or hang startup
        log.exception("Update check failed")
        return None
