#!/usr/bin/env python3
"""Narzędzie do zarządzania plikiem autoinstall.yaml.

Aktualnie zaimplementowana funkcjonalność:
  - walidacja: odczytuje kanał z autoinstall.yaml, ustala wersję przez
    `snap info`, buduje obraz Dockera (jeśli brak) i uruchamia walidator
    Subiquity w kontenerze.
"""

import argparse
import subprocess
import sys
import urllib.request
from pathlib import Path


SNAP_NAME = "ubuntu-desktop-bootstrap"
DOCKER_IMAGE_REPO = "ubuntu-desktop-bootstrap"


# ---------------------------------------------------------------------------
# Parsowanie YAML
# ---------------------------------------------------------------------------

def extract_installer_channel(config_file: Path) -> str:
    """Wyciąga wartość autoinstall.refresh-installer.channel z pliku YAML."""
    import yaml  # type: ignore[import-untyped]

    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    channel = (
        data.get("autoinstall", {})
            .get("refresh-installer", {})
            .get("channel")
    )
    if not channel:
        raise SystemExit(
            f"BŁĄD: Nie znaleziono pola 'autoinstall.refresh-installer.channel' w {config_file}"
        )
    return str(channel).strip()


def has_cloud_config_header(config_file: Path) -> bool:
    """Sprawdza czy plik zaczyna się od '#cloud-config'."""
    try:
        return config_file.open().readline().strip() == "#cloud-config"
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Snap + snapcraft.yaml
# ---------------------------------------------------------------------------

def get_snap_version(channel: str) -> str:
    """Zwraca wersję snapa ubuntu-desktop-bootstrap dla podanego kanału."""
    result = subprocess.run(
        ["snap", "info", SNAP_NAME],
        capture_output=True, text=True, check=True,
    )
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith(f"{channel}:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1] != "--":
                return parts[1]
            raise SystemExit(f"BŁĄD: Kanał '{channel}' nie ma opublikowanej wersji.")
    raise SystemExit(f"BŁĄD: Nie znaleziono kanału '{channel}' w `snap info {SNAP_NAME}`.")


def get_base_image(version: str) -> str:
    """Pobiera snapcraft.yaml z repo Subiquity i mapuje 'base' na obraz Ubuntu.

    Mapowanie: coreXX -> ubuntu:XX.04
    """
    import yaml  # type: ignore[import-untyped]

    url = f"https://raw.githubusercontent.com/canonical/subiquity/{version}/snap/snapcraft.yaml"
    print(f"[autoinstall-helper] Pobieranie snapcraft.yaml z {url}...")
    with urllib.request.urlopen(url) as resp:
        content = resp.read().decode("utf-8")

    base = yaml.safe_load(content).get("base", "")
    if not base.startswith("core"):
        raise SystemExit(f"BŁĄD: Nieoczekiwana wartość 'base' w snapcraft.yaml: '{base}'")

    number = base.removeprefix("core")
    return f"ubuntu:{number}.04"


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

def docker_image_exists(tag: str) -> bool:
    res = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
    )
    return res.returncode == 0


def docker_build(tag: str, version: str, base_image: str, dockerfile: Path) -> None:
    print(f"[autoinstall-helper] Budowanie obrazu '{tag}' (base: {base_image})...")
    subprocess.run(
        [
            "docker", "build",
            "--build-arg", f"SUBIQUITY_VERSION={version}",
            "--build-arg", f"BASE_IMAGE={base_image}",
            "-t", tag,
            "-f", str(dockerfile),
            str(dockerfile.parent),
        ],
        check=True,
    )
    print(f"[autoinstall-helper] Obraz '{tag}' zbudowany.")


def docker_validate(tag: str, config_file: Path) -> int:
    container_path = "/data/autoinstall.yaml"
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{config_file.resolve()}:{container_path}:ro",
        tag,
        "python3", "scripts/validate-autoinstall-user-data.py",
        container_path,
    ]
    if not has_cloud_config_header(config_file):
        cmd.append("--no-expect-cloudconfig")

    print(f"[autoinstall-helper] Walidacja '{config_file.name}' w kontenerze '{tag}'...")
    result = subprocess.run(cmd)
    return result.returncode


# ---------------------------------------------------------------------------
# Główna logika
# ---------------------------------------------------------------------------

def validate(config_file: Path) -> int:
    if not config_file.exists():
        raise SystemExit(f"BŁĄD: Plik nie istnieje: {config_file}")

    dockerfile = Path(__file__).resolve().parent / "Dockerfile"
    if not dockerfile.exists():
        raise SystemExit(f"BŁĄD: Nie znaleziono Dockerfile w {dockerfile.parent}")

    print(f"[autoinstall-helper] Odczytywanie: {config_file}")
    channel = extract_installer_channel(config_file)
    print(f"[autoinstall-helper] Kanał instalatora: {channel}")

    version = get_snap_version(channel)
    print(f"[autoinstall-helper] Wersja instalatora: {version}")

    tag = f"{DOCKER_IMAGE_REPO}:{version}"
    if docker_image_exists(tag):
        print(f"[autoinstall-helper] Obraz '{tag}' już istnieje.")
    else:
        base_image = get_base_image(version)
        print(f"[autoinstall-helper] Obraz bazowy: {base_image}")
        docker_build(tag, version, base_image, dockerfile)

    rc = docker_validate(tag, config_file)
    if rc == 0:
        print("[autoinstall-helper] Walidacja zakończona sukcesem!")
    else:
        print(f"[autoinstall-helper] Walidacja nieudana (kod: {rc}).", file=sys.stderr)
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="autoinstall-helper.py",
        description="Narzędzie do zarządzania plikiem autoinstall.yaml.",
    )
    parser.add_argument(
        "-v", "--validate",
        metavar="PLIK_YAML",
        type=Path,
        help="Waliduje podany plik autoinstall.yaml",
    )
    args = parser.parse_args()

    if args.validate:
        return validate(args.validate)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
