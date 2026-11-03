"""Zapytania do Snap Store i mapowanie 'base' na obraz Ubuntu."""

import json
import platform
import shutil
import subprocess
import urllib.error
import urllib.request

SNAP_NAME = "ubuntu-desktop-bootstrap"


def _get_snap_version_from_api(snap_name: str, channel: str) -> str | None:
    """Odpytuje Snap Store API o wersję snapa bez konieczności posiadania zainstalowanego snapd."""
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
        return None
    return None


def get_snap_version(channel: str) -> str:
    """Zwraca wersję snapa ubuntu-desktop-bootstrap dla podanego kanału.

    Najpierw próbuje odpytać publiczne Snap Store API (działa na dowolnej dystrybucji
    bez snapd), a w razie braku połączenia wykonuje fallback do lokalnego polecenia `snap info`.
    """
    version = _get_snap_version_from_api(SNAP_NAME, channel)
    if version:
        return version

    # Fallback do lokalnego polecenia `snap info` (jeśli snapd jest zainstalowany)
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
                    raise SystemExit(f"BŁĄD: Kanał '{channel}' nie ma opublikowanej wersji.")
        except subprocess.CalledProcessError:
            pass

    raise SystemExit(
        f"BŁĄD: Nie można ustalić wersji snapa dla kanału '{channel}'. "
        f"Sprawdź połączenie z siecią lub poprawność nazwy kanału."
    )


def get_base_image(version: str) -> str:
    """Pobiera snapcraft.yaml z repo Subiquity i mapuje 'base' na obraz Ubuntu.

    Mapowanie: coreXX -> ubuntu:XX.04
    """
    import yaml  # type: ignore[import-untyped]

    urls = [
        f"https://raw.githubusercontent.com/canonical/subiquity/{version}/snap/snapcraft.yaml",
        f"https://raw.githubusercontent.com/canonical/subiquity/{version}/snapcraft.yaml",
    ]
    content = None
    for url in urls:
        try:
            print(f"[autoinstall-helper] Pobieranie snapcraft.yaml z {url}...")
            with urllib.request.urlopen(url, timeout=30) as resp:
                content = resp.read().decode("utf-8")
                break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise

    if content is None:
        raise SystemExit(f"BŁĄD: Nie znaleziono snapcraft.yaml dla wersji '{version}' w repo Subiquity.")

    base = yaml.safe_load(content).get("base", "")
    if not base.startswith("core"):
        raise SystemExit(f"BŁĄD: Nieoczekiwana wartość 'base' w snapcraft.yaml: '{base}'")

    number = base.removeprefix("core")
    return f"ubuntu:{number}.04"
