#!/usr/bin/env python3
"""Narzędzie do zarządzania plikiem autoinstall.yaml.

Dostępne komendy:
  - validate: odczytuje kanał z autoinstall.yaml, ustala wersję przez
    `snap info`, buduje obraz Dockera (jeśli brak) i uruchamia walidator
    Subiquity w kontenerze.
  - flash: zapisuje obraz ISO na target (urządzenie blokowe lub plik),
    tworząc plik docelowy przez fallocate, jeśli jeszcze nie istnieje.
  - inject: osadza autoinstall.yaml w partycji 'cidata' (FAT32) na targecie
    (urządzeniu blokowym lub pliku-obrazie). Jeśli partycja nie istnieje,
    tworzy ją na końcu dostępnego miejsca.
  - extract: odczytuje user-data z partycji 'cidata' na targecie i zapisuje do pliku.

Logika poszczególnych komend znajduje się w dedykowanych modułach:
  - config.py       — parsowanie autoinstall.yaml
  - snapstore.py    — ustalanie wersji snapa i obrazu bazowego
  - docker_utils.py — obraz Dockera walidatora i zasoby katalogu docker/
  - flash.py        — zapis ISO na nośnik
  - cidata.py       — partycja cidata (inject / extract)
  - disk.py         — wspólne narzędzia urządzeń blokowych i GPT
"""

import argparse
import os
import sys
from pathlib import Path

from autoinstall_helper.cidata import extract, inject
from autoinstall_helper.config import extract_installer_channel
from autoinstall_helper.disk import _is_block_device
from autoinstall_helper.docker_utils import (
    _get_docker_dir,
    docker_build,
    docker_image_exists,
    docker_image_tag,
    docker_validate,
)
from autoinstall_helper.flash import flash
from autoinstall_helper.snapstore import get_base_image, get_snap_version


def _require_root(action: str) -> None:
    """Sprawdza czy proces posiada uprawnienia roota; jeśli nie, kończy z czytelnym komunikatem."""
    if os.geteuid() != 0:
        cmd_args = " ".join(sys.argv)
        raise SystemExit(
            f"BŁĄD: Operacja '{action}' wymaga uprawnień administratora (root).\n"
            f"Uruchom ponownie z sudo:\n"
            f"  sudo {sys.executable} {cmd_args}"
        )


def validate(config_file: Path, validation: str = "full") -> int:
    if not config_file.exists():
        raise SystemExit(f"BŁĄD: Plik nie istnieje: {config_file}")

    print(f"[autoinstall-helper] Odczytywanie: {config_file}")
    channel = extract_installer_channel(config_file)
    print(f"[autoinstall-helper] Kanał instalatora: {channel}")

    version = get_snap_version(channel)
    print(f"[autoinstall-helper] Wersja instalatora: {version}")

    tag = docker_image_tag(version)
    if docker_image_exists(tag):
        print(f"[autoinstall-helper] Obraz '{tag}' już istnieje.")
    else:
        dockerfile = _get_docker_dir() / "Dockerfile"
        if not dockerfile.exists():
            raise SystemExit(f"BŁĄD: Nie znaleziono Dockerfile w {dockerfile.parent}")
        base_image = get_base_image(version)
        print(f"[autoinstall-helper] Obraz bazowy: {base_image}")
        docker_build(tag, version, base_image, dockerfile)

    rc = docker_validate(tag, config_file, validation)
    if rc == 0:
        print("[autoinstall-helper] Walidacja zakończona sukcesem!")
    else:
        print(f"[autoinstall-helper] Walidacja nieudana (kod: {rc}).", file=sys.stderr)
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="autoinstall-helper",
        description="Narzędzie do zarządzania konfiguracją autoinstall i nośnikami instalacyjnymi Ubuntu.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", metavar="KOMENDA")

    # validate
    p_val = subparsers.add_parser(
        "validate",
        help="Waliduje plik autoinstall.yaml w kontenerze Docker",
    )
    p_val.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path("autoinstall.yaml"),
        help="Ścieżka do pliku YAML (domyślnie: autoinstall.yaml)",
    )
    p_val.add_argument(
        "--validation",
        choices=("autoinstall", "storage", "full"),
        default="full",
        help=(
            "Zakres walidacji: autoinstall (Subiquity), storage (akcje storage + "
            "Curtin schema), full (oba sprawdzenia; domyślnie)"
        ),
    )

    # flash
    p_flash = subparsers.add_parser(
        "flash",
        help="Zapisuje obraz ISO na target (urządzenie blokowe lub plik-obraz)",
    )
    p_flash.add_argument("iso", type=Path, help="Ścieżka do pliku ISO")
    p_flash.add_argument(
        "target",
        type=Path,
        help="Urządzenie blokowe (np. /dev/sdb) lub plik-obraz",
    )
    p_flash.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Pomiń potwierdzenie zapisu na urządzenie blokowe",
    )

    # inject (alias: import)
    p_inject = subparsers.add_parser(
        "inject",
        aliases=["import"],
        help="Osadza YAML w partycji cidata (FAT32) na targecie",
    )
    p_inject.add_argument("target", type=Path, help="Target (urządzenie blokowe lub plik-obraz)")
    p_inject.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path("autoinstall.yaml"),
        help="Ścieżka do pliku YAML (domyślnie: autoinstall.yaml)",
    )

    # extract (alias: export)
    p_extract = subparsers.add_parser(
        "extract",
        aliases=["export"],
        help="Odczytuje user-data z partycji cidata (FAT32) na targecie",
    )
    p_extract.add_argument("target", type=Path, help="Target (urządzenie blokowe lub plik-obraz)")
    p_extract.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=Path("autoinstall.yaml"),
        help="Ścieżka do pliku wyjściowego (domyślnie: autoinstall.yaml)",
    )

    # cidata group (cidata inject / cidata extract)
    p_cidata = subparsers.add_parser(
        "cidata",
        help="Zarządzanie partycją cidata (podkomendy: inject, extract)",
    )
    cidata_sub = p_cidata.add_subparsers(dest="cidata_subcommand", metavar="AKCJA")

    c_inject = cidata_sub.add_parser("inject", help="Osadza YAML w partycji cidata")
    c_inject.add_argument("target", type=Path, help="Target (urządzenie blokowe lub plik-obraz)")
    c_inject.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path("autoinstall.yaml"),
        help="Ścieżka do pliku YAML (domyślnie: autoinstall.yaml)",
    )

    c_extract = cidata_sub.add_parser("extract", help="Odczytuje user-data z partycji cidata")
    c_extract.add_argument("target", type=Path, help="Target (urządzenie blokowe lub plik-obraz)")
    c_extract.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=Path("autoinstall.yaml"),
        help="Ścieżka do pliku wyjściowego (domyślnie: autoinstall.yaml)",
    )

    args = parser.parse_args()

    if args.subcommand == "validate":
        return validate(args.config, validation=args.validation)

    if args.subcommand == "flash":
        if _is_block_device(args.target):
            _require_root("flash na urządzenie blokowe")
        return flash(args.iso, args.target, yes=args.yes)

    if args.subcommand in ("inject", "import"):
        _require_root("inject")
        return inject(args.target, args.config)

    if args.subcommand in ("extract", "export"):
        _require_root("extract")
        return extract(args.target, args.output)

    if args.subcommand == "cidata":
        if args.cidata_subcommand == "inject":
            _require_root("cidata inject")
            return inject(args.target, args.config)
        if args.cidata_subcommand == "extract":
            _require_root("cidata extract")
            return extract(args.target, args.output)
        p_cidata.print_help()
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
