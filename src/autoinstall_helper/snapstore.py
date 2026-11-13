"""Snap Store queries, snap validation and 'base' to Ubuntu image mapping."""

import json
import platform
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

SNAP_NAME = "ubuntu-desktop-bootstrap"

SNAP_STORE_INFO_URL = "https://api.snapcraft.io/v2/snaps/info/{name}"
# The store returns a channel entry only with the fields named here, so
# `confinement` (snap validation) and `version` (channel resolution) must both
# be requested explicitly.
SNAP_STORE_FIELDS = "channel-map,confinement,version"
SNAP_STORE_SERIES = "16"
SNAP_STORE_TIMEOUT_S = 15

# The store rate-limits bursty clients, so transient failures are retried.
SNAP_STORE_MAX_ATTEMPTS = 3
SNAP_STORE_RETRY_STATUSES = (429, 500, 502, 503, 504)
SNAP_STORE_RETRY_BASE_S = 2

# Snap channels are `<track>/<risk>[/<branch>]`; a missing track means the
# snap's default track and a missing risk defaults to `stable`.
SNAP_RISKS = ("stable", "candidate", "beta", "edge")
DEFAULT_SNAP_CHANNEL = "stable"
DEFAULT_TRACK = "latest"

# Subiquity ships the validator API used by docker/validate-autoinstall.py
# (parse_autoinstall/make_app/verify_autoinstall) starting with the 26.04
# release; older installers only expose the legacy main() entry point.
MIN_VALIDATOR_VERSION = (26, 4)

# Snap names and channel components are restricted to a small character set, so
# an entry can never smuggle a path or query into the store URL.
_SNAP_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
_SNAP_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")

# Ubuntu LTS releases ship in April of even years, so their snap tracks look
# like '24.04' or '26.04' (a two-digit even year followed by '.04').
_LTS_TRACK_PATTERN = re.compile(r"^(\d{2})\.04$")

ARCH_MAP = {
    "x86_64": "amd64",
    "aarch64": "arm64",
    "armv7l": "armhf",
    "ppc64le": "ppc64el",
    "s390x": "s390x",
}


class SnapStoreError(RuntimeError):
    """The Snap Store could not be queried, so a snap cannot be verified."""


@dataclass(frozen=True)
class SnapChannel:
    """A parsed snap channel: an optional track, a risk and an optional branch."""

    track: str | None
    risk: str
    branch: str | None


@dataclass(frozen=True)
class SnapRequest:
    """One `snaps:` entry to check, plus the path it came from in the config."""

    label: str
    name: str
    channel: str
    classic: bool


def parse_channel(channel: str) -> SnapChannel | None:
    """Parse ``[track/]risk[/branch]``; return None when it is not a valid channel."""
    parts = channel.split("/")
    if len(parts) > 3:
        return None
    branch = None
    if len(parts) == 3:
        track, risk, branch = parts
    elif len(parts) == 2:
        track, risk = parts
    else:
        track, risk = None, parts[0]
    if risk not in SNAP_RISKS:
        return None
    if track is not None and not _SNAP_COMPONENT_PATTERN.match(track):
        return None
    if branch is not None and not _SNAP_COMPONENT_PATTERN.match(branch):
        return None
    return SnapChannel(track, risk, branch)


def channel_names(channel: SnapChannel, default_track: str | None) -> set[str]:
    """Return the store channel names that satisfy a requested channel.

    The store lists the default track both as ``<track>/<risk>`` and as a bare
    ``<risk>`` alias, and a bare risk always means the default track (which is
    not necessarily ``latest``).
    """
    default = default_track or DEFAULT_TRACK
    if channel.track is None:
        return {channel.risk, f"{default}/{channel.risk}"}
    names = {f"{channel.track}/{channel.risk}"}
    if channel.track == default:
        names.add(channel.risk)
    return names


def _channel_map(info: dict[str, Any]) -> list[dict[str, Any]]:
    channel_map = info.get("channel-map")
    if not isinstance(channel_map, list):
        raise SnapStoreError("the Snap Store response has no channel map")
    return [entry for entry in channel_map if isinstance(entry, dict)]


def _entry_name(entry: dict[str, Any]) -> str | None:
    channel = entry.get("channel")
    if not isinstance(channel, dict):
        return None
    name = channel.get("name")
    return name if isinstance(name, str) else None


def channel_entries(info: dict[str, Any], names: set[str]) -> list[dict[str, Any]]:
    """Return the channel-map entries published under any of the given names."""
    return [entry for entry in _channel_map(info) if _entry_name(entry) in names]


def _default_track(info: dict[str, Any]) -> str | None:
    default_track = info.get("default-track")
    return default_track if isinstance(default_track, str) else None


def fetch_snap_info(name: str) -> dict[str, Any] | None:
    """Return the Snap Store metadata for a snap, or None when it does not exist.

    This is the single entry point for Snap Store lookups, shared by the
    installer-channel resolution and the ``snaps:`` validation.  Transient
    failures (rate limiting, server errors, connection problems) are retried a
    few times; anything still failing raises :class:`SnapStoreError` so callers
    can decide whether to degrade or fail.
    """
    url = f"{SNAP_STORE_INFO_URL.format(name=name)}?fields={SNAP_STORE_FIELDS}"
    request = urllib.request.Request(url, headers={"Snap-Device-Series": SNAP_STORE_SERIES})
    last_error = "unknown error"
    for attempt in range(1, SNAP_STORE_MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=SNAP_STORE_TIMEOUT_S) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            last_error = f"HTTP {exc.code}"
            if exc.code in SNAP_STORE_RETRY_STATUSES and attempt < SNAP_STORE_MAX_ATTEMPTS:
                time.sleep(SNAP_STORE_RETRY_BASE_S * attempt)
                continue
            break
        except OSError as exc:
            last_error = str(exc) or exc.__class__.__name__
            if attempt < SNAP_STORE_MAX_ATTEMPTS:
                time.sleep(SNAP_STORE_RETRY_BASE_S * attempt)
                continue
            break
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SnapStoreError(f"the Snap Store returned an invalid response for {name!r}: {exc}") from exc

        if not isinstance(data, dict):
            raise SnapStoreError(f"the Snap Store returned an invalid response for {name!r}")
        return data

    raise SnapStoreError(f"could not query the Snap Store for {name!r} ({last_error})")


def _get_snap_version_from_api(snap_name: str, channel: str) -> str | None:
    """Query the Snap Store API for the snap version without requiring an installed snapd."""
    parsed = parse_channel(channel)
    if parsed is None:
        return None
    target_arch = ARCH_MAP.get(platform.machine().lower(), "amd64")

    try:
        info = fetch_snap_info(snap_name)
        if info is None:
            return None
        entries = channel_entries(info, channel_names(parsed, _default_track(info)))
    except SnapStoreError:
        # Network errors, timeouts, or API schema changes fall back to local `snap info`.
        return None

    for entry in entries:
        if entry.get("channel", {}).get("architecture") == target_arch:
            return str(entry.get("version"))
    if entries:
        return str(entries[0].get("version"))
    return None


def get_snap_version(channel: str) -> str:
    """Return the ubuntu-desktop-bootstrap snap version for the given channel.

    It first queries the public Snap Store API (works on any distribution
    without snapd) and falls back to the local `snap info` command if offline.
    """
    if parse_channel(channel) is None:
        raise SystemExit(
            f"ERROR: Invalid channel name '{channel}'. Expected format like 'latest/stable' or '24.04/edge'."
        )

    version = _get_snap_version_from_api(SNAP_NAME, channel)
    if version:
        return version

    # Fallback to the local `snap info` command (if snapd is installed)
    if shutil.which("snap"):
        try:
            result = subprocess.run(
                ["snap", "info", SNAP_NAME],
                capture_output=True,
                text=True,
                check=True,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith(f"{channel}:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] != "--":
                        return parts[1]
                    raise SystemExit(f"ERROR: Channel '{channel}' has no published version.")
        except subprocess.CalledProcessError:
            pass

    raise SystemExit(
        f"ERROR: Could not determine the snap version for channel '{channel}'. "
        f"Check the network connection or the channel name."
    )


def latest_lts_channel(snap_name: str = SNAP_NAME) -> str:
    """Return the ``<track>/stable`` channel of the newest Ubuntu LTS in the store.

    Used as the fallback installer channel when the autoinstall configuration
    does not pin ``refresh-installer.channel``.  The store publishes one track
    per Ubuntu release, so the newest LTS is simply the highest even-year
    ``XX.04`` track that has a published ``stable`` risk.  Nothing is hardcoded,
    so the fallback follows 28.04 (and later) on its own.

    Raises :class:`SnapStoreError` when the store cannot be queried or exposes
    no LTS track, so the caller can degrade with a clear message.
    """
    info = fetch_snap_info(snap_name)
    if info is None:
        raise SnapStoreError(f"snap {snap_name!r} does not exist in the Snap Store")

    lts_tracks: set[str] = set()
    for entry in _channel_map(info):
        channel = entry.get("channel")
        if not isinstance(channel, dict) or channel.get("risk") != "stable":
            continue
        track = channel.get("track")
        if not isinstance(track, str):
            continue
        match = _LTS_TRACK_PATTERN.match(track)
        if match and int(match.group(1)) % 2 == 0:
            lts_tracks.add(track)

    if not lts_tracks:
        raise SnapStoreError(f"no Ubuntu LTS channel is published for {snap_name!r}")

    latest = max(lts_tracks, key=lambda track: tuple(int(part) for part in track.split(".")))
    return f"{latest}/stable"


def supports_embedded_validator(version: str) -> bool:
    """Return whether the installer version ships Subiquity's built-in validator API.

    Only installers from 26.04 onwards expose parse_autoinstall/make_app/
    verify_autoinstall, which the in-container validator imports.
    """
    parts = version.split(".")
    try:
        major, minor = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return False
    return (major, minor) >= MIN_VALIDATOR_VERSION


def _channel_confinement(entries: list[dict[str, Any]]) -> str | None:
    """Return the confinement shared by every entry, or None when it is mixed/missing."""
    consumed = {entry.get("confinement") for entry in entries if entry.get("confinement")}
    return consumed.pop() if len(consumed) == 1 else None


def is_valid_snap_name(name: str) -> bool:
    """Return whether ``name`` is a syntactically valid snap name."""
    return bool(_SNAP_NAME_PATTERN.match(name))


def collect_snaps(entries: object) -> tuple[list[SnapRequest], list[str]]:
    """Collect every `snaps:` entry to check and any structural errors.

    ``entries`` is the raw value of ``autoinstall.snaps`` from the YAML file.
    """
    if entries is None:
        return [], []
    if not isinstance(entries, list):
        return [], ["autoinstall.snaps must be a list"]

    snaps: list[SnapRequest] = []
    errors: list[str] = []
    for index, entry in enumerate(entries):
        prefix = f"snaps[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix} must be a mapping")
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            errors.append(f"{prefix}.name must be a non-empty string")
            continue
        if not is_valid_snap_name(name):
            errors.append(f"{prefix}.name {name!r} is not a valid snap name")
            continue
        channel = entry.get("channel", DEFAULT_SNAP_CHANNEL)
        if channel is None:
            channel = DEFAULT_SNAP_CHANNEL
        if not isinstance(channel, str):
            errors.append(f"{prefix}.channel must be a string")
            continue
        if parse_channel(channel) is None:
            errors.append(
                f"{prefix}.channel {channel!r} is not a valid snap channel (expected e.g. 'stable' or '24.04/stable')"
            )
            continue
        classic = entry.get("classic", False)
        if classic is None:
            classic = False
        if not isinstance(classic, bool):
            errors.append(f"{prefix}.classic must be a boolean")
            continue
        snaps.append(SnapRequest(prefix, name, channel, classic))
    return snaps, errors


def validate_snaps(entries: object) -> bool:
    """Verify every `snaps:` entry against the Snap Store; return True when clean.

    Checked per entry:
    - the snap exists in the store,
    - the requested channel is published,
    - `classic` matches the confinement of that channel (classic snaps need
      ``classic: true`` and strict snaps must not set it).
    """
    snaps, errors = collect_snaps(entries)
    for error in errors:
        print(f"[snaps] Structure error: {error}")

    if not snaps:
        if not errors:
            print("[snaps] No snaps declared; nothing to check.")
        return not errors

    print(f"[snaps] Checking {len(snaps)} snap(s) against the Snap Store...")
    cache: dict[str, dict[str, Any] | None] = {}
    was_clean = not errors
    for snap in snaps:
        try:
            if snap.name not in cache:
                cache[snap.name] = fetch_snap_info(snap.name)
            info = cache[snap.name]
            if info is None:
                print(f"[snaps] {snap.label}: snap {snap.name!r} does not exist in the Snap Store")
                was_clean = False
                continue

            channel = parse_channel(snap.channel)
            if channel is None:  # Unreachable: collect_snaps already rejected it.
                was_clean = False
                continue
            entries = channel_entries(info, channel_names(channel, _default_track(info)))
        except SnapStoreError as exc:
            print(f"[snaps] {snap.label}: {exc}")
            was_clean = False
            continue

        if not entries:
            published = sorted({name for entry in _channel_map(info) if (name := _entry_name(entry))})
            hint = f" (published channels: {', '.join(published)})" if published else ""
            print(f"[snaps] {snap.label}: channel {snap.channel!r} is not published for snap {snap.name!r}{hint}")
            was_clean = False
            continue

        if channel.branch is not None:
            print(
                f"[snaps] {snap.label}: branch {channel.branch!r} cannot be verified "
                f"from the Snap Store channel map; checked the base channel {snap.channel!r}."
            )

        confinement = _channel_confinement(entries)
        if confinement is None:
            print(
                f"[snaps] {snap.label}: confinement of {snap.name!r} on {snap.channel!r} "
                f"is unknown; skipped the classic check."
            )
            continue
        if confinement == "classic" and not snap.classic:
            print(f"[snaps] {snap.label}: {snap.name!r} is a classic snap on {snap.channel!r}; set 'classic: true'")
            was_clean = False
        elif confinement != "classic" and snap.classic:
            print(
                f"[snaps] {snap.label}: {snap.name!r} is not a classic snap on {snap.channel!r} "
                f"(confinement: {confinement}); remove 'classic: true'"
            )
            was_clean = False

    print("[snaps] Snap declarations are valid." if was_clean else "[snaps] Snap validation failed.")
    return was_clean


def get_base_image(version: str) -> str:
    """Fetch snapcraft.yaml from the Subiquity repo and map 'base' to an Ubuntu image.

    Mapping: coreXX -> ubuntu:XX.04
    """
    # Lazy import: pyyaml is available in Ubuntu environments (python3-yaml)
    # and dev environments, but production code keeps zero hard dependencies in [project.dependencies].
    import yaml  # type: ignore[import-untyped]

    urls = [
        f"https://raw.githubusercontent.com/canonical/subiquity/{version}/snap/snapcraft.yaml",
        f"https://raw.githubusercontent.com/canonical/subiquity/{version}/snapcraft.yaml",
    ]
    content = None
    for url in urls:
        try:
            print(f"[autoinstall-helper] Downloading snapcraft.yaml from {url}...")
            with urllib.request.urlopen(url, timeout=30) as resp:
                content = resp.read().decode("utf-8")
                break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise

    if content is None:
        raise SystemExit(f"ERROR: snapcraft.yaml not found for version '{version}' in the Subiquity repo.")

    base = yaml.safe_load(content).get("base", "")
    if not isinstance(base, str) or not base.startswith("core"):
        raise SystemExit(f"ERROR: Unexpected 'base' value in snapcraft.yaml: '{base}'")

    number = base.removeprefix("core")
    if not number.isdigit():
        raise SystemExit(f"ERROR: Unexpected 'base' value in snapcraft.yaml: '{base}'")

    return f"ubuntu:{number}.04"
