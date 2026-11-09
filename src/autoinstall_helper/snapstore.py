"""Snap Store queries and 'base' to Ubuntu image mapping."""

import json
import platform
import re
import shutil
import subprocess
import urllib.error
import urllib.request

SNAP_NAME = "ubuntu-desktop-bootstrap"
CHANNEL_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+(/[a-zA-Z0-9_.-]+)*$")


def _get_snap_version_from_api(snap_name: str, channel: str) -> str | None:
    """Query the Snap Store API for the snap version without requiring an installed snapd."""
    arch_map = {
        "x86_64": "amd64",
        "aarch64": "arm64",
        "armv7l": "armhf",
        "ppc64le": "ppc64el",
        "s390x": "s390x",
    }
    target_arch = arch_map.get(platform.machine().lower(), "amd64")

    url = f"https://api.snapcraft.io/v2/snaps/info/{snap_name}"
    req = urllib.request.Request(url, headers={"Snap-Device-Series": "16"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            channel_entries = [e for e in data.get("channel-map", []) if e.get("channel", {}).get("name") == channel]
            for entry in channel_entries:
                if entry.get("channel", {}).get("architecture") == target_arch:
                    return str(entry.get("version"))
            if channel_entries:
                return str(channel_entries[0].get("version"))
    except Exception:
        # Network errors, timeouts, or API schema changes fall back to local `snap info` (BLE001 accepted pattern).
        return None
    return None


def get_snap_version(channel: str) -> str:
    """Return the ubuntu-desktop-bootstrap snap version for the given channel.

    It first queries the public Snap Store API (works on any distribution
    without snapd) and falls back to the local `snap info` command if offline.
    """
    if not channel or not CHANNEL_PATTERN.match(channel):
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
