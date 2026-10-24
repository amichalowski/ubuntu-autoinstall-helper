#!/usr/bin/env python3
"""Narzędzie do zarządzania plikiem autoinstall.yaml.

Aktualnie zaimplementowana funkcjonalność:
  - walidacja: odczytuje kanał z autoinstall.yaml, ustala wersję przez
    `snap info`, buduje obraz Dockera (jeśli brak) i uruchamia walidator
    Subiquity w kontenerze.
  - flash: zapisuje obraz ISO na target (urządzenie blokowe lub plik),
    tworząc plik docelowy przez fallocate, jeśli jeszcze nie istnieje.
  - import: osadza autoinstall.yaml w partycji 'cidata' (FAT32) na targecie
    (urządzeniu blokowym lub pliku-obrazie). Jeśli partycja nie istnieje,
    tworzy ją na końcu dostępnego miejsca.
"""

import argparse
import contextlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# Zapas miejsca dodawany przy tworzeniu pliku-targetu (na partycję z autoinstall.yaml)
FLASH_EXTRA_BYTES = 128 * 1024 * 1024  # 128 MB


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
# Flash (zapis ISO na target)
# ---------------------------------------------------------------------------

def _is_block_device(path: Path) -> bool:
    """Sprawdza czy ścieżka wskazuje na urządzenie blokowe."""
    try:
        return path.stat().st_mode & 0o170000 == 0o060000
    except OSError:
        return False


def _is_btrfs(path: Path) -> bool:
    """Sprawdza czy ścieżka leży na systemie plików BTRFS."""
    result = subprocess.run(
        ["stat", "-f", "--format=%T", str(path)],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "btrfs"


def flash(iso_path: Path, target: Path) -> int:
    """Zapisuje obraz ISO na target używając dd.

    Jeśli target nie istnieje i nie jest urządzeniem blokowym, tworzy
    plik o rozmiarze ISO + FLASH_EXTRA_BYTES przez fallocate.
    Na systemach plików BTRFS plik otrzymuje atrybut No-COW (chattr +C).
    Po zakończeniu dd wywołuje sync.
    """
    # Walidacja ISO
    if not iso_path.exists():
        raise SystemExit(f"BŁĄD: Plik ISO nie istnieje: {iso_path}")
    if not iso_path.is_file():
        raise SystemExit(f"BŁĄD: Podana ścieżka ISO nie jest plikiem: {iso_path}")

    iso_size = iso_path.stat().st_size
    print(f"[autoinstall-helper] ISO: {iso_path} ({iso_size / 1024**2:.1f} MB)")

    target_is_block = _is_block_device(target)

    if target_is_block:
        print(f"[autoinstall-helper] Target: urządzenie blokowe {target}")
    elif target.exists():
        print(f"[autoinstall-helper] Target: istniejący plik {target}")
    else:
        # Utwórz plik-target
        total_size = iso_size + FLASH_EXTRA_BYTES
        print(
            f"[autoinstall-helper] Target nie istnieje. Tworzę plik "
            f"{target} ({total_size / 1024**2:.1f} MB = ISO + 128 MB zapasu)..."
        )

        # Na BTRFS ustaw atrybut No-COW przed zapisem danych
        parent = target.parent if not target.exists() else target
        if _is_btrfs(parent):
            print(f"[autoinstall-helper] Wykryto BTRFS — tworzę plik i ustawiam atrybut No-COW (chattr +C).")
            # Plik musi istnieć przed chattr
            target.touch()
            subprocess.run(["chattr", "+C", str(target)], check=True)

        subprocess.run(
            ["fallocate", "-l", str(total_size), str(target)],
            check=True,
        )
        print(f"[autoinstall-helper] Plik {target} utworzony.")

    # Zapis ISO przez dd
    print(f"[autoinstall-helper] Zapisywanie ISO na {target} (dd)... To może chwilę potrwać.")
    dd_cmd = [
        "dd",
        f"if={iso_path}",
        f"of={target}",
        "bs=4M",
        "status=progress",
        "conv=fsync",
    ]
    result = subprocess.run(dd_cmd)
    if result.returncode != 0:
        raise SystemExit(f"BŁĄD: dd zakończył się kodem {result.returncode}.")

    print("[autoinstall-helper] Synchronizacja buforów (sync)...")
    subprocess.run(["sync"], check=True)

    print(f"[autoinstall-helper] Flash zakończony pomyślnie: {iso_path} → {target}")
    return 0


# ---------------------------------------------------------------------------
# Import autoinstall.yaml → partycja cidata (FAT32)
# ---------------------------------------------------------------------------

CIDATA_LABEL = "cidata"


def _run(*cmd: str, check: bool = True, capture: bool = False, **kwargs) -> subprocess.CompletedProcess:
    """Uruchamia polecenie, opcjonalnie przechwytując wyjście."""
    return subprocess.run(
        list(cmd),
        check=check,
        capture_output=capture,
        text=capture,
        **kwargs,
    )


def _losetup_attach(image: Path) -> str:
    """Podpina plik-obraz pod loop device, zwraca ścieżkę np. /dev/loop46."""
    result = _run("losetup", "-fP", "--show", str(image), capture=True)
    return result.stdout.strip()


def _losetup_detach(loop_dev: str) -> None:
    """Odpina loop device."""
    _run("losetup", "-d", loop_dev, check=False)


@contextlib.contextmanager
def _loop_context(image: Path):
    """Context manager: podpina obraz pod loop device i odpina po wyjściu."""
    loop_dev = _losetup_attach(image)
    print(f"[autoinstall-helper] Obraz podpięty pod: {loop_dev}")
    try:
        yield loop_dev
    finally:
        _losetup_detach(loop_dev)
        print(f"[autoinstall-helper] Odpięto: {loop_dev}")


def _find_cidata_partition(disk: str) -> str | None:
    """Szuka partycji z etykietą 'cidata' na dysku/loop dev.

    Zwraca ścieżkę partycji (np. /dev/loop46p4) lub None.
    Używa 'blkid' do sprawdzenia etykiet wszystkich partycji.
    """
    result = subprocess.run(
        ["blkid", "-o", "device", disk],
        capture_output=True, text=True, check=False,
    )
    # blkid bez partycji — szukamy po wzorcu disk + 'p' lub cyfra
    # Pobieramy listę partycji przez lsblk
    lsblk = subprocess.run(
        ["lsblk", "-ln", "-o", "NAME,LABEL", disk],
        capture_output=True, text=True, check=False,
    )
    for line in lsblk.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].lower() == CIDATA_LABEL:
            name = parts[0]
            # lsblk zwraca np. "loop46p4" lub "sdb4" — trzeba dodać /dev/
            if not name.startswith("/"):
                name = f"/dev/{name}"
            return name
    return None


def _get_last_partition_end(disk: str) -> str:
    """Zwraca koniec ostatniej partycji w MB (jako string do przekazania do parted)."""
    result = _run(
        "parted", disk, "--script", "unit", "MB", "print",
        capture=True, check=False,
    )
    last_end = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped and stripped[0].isdigit():
            # Format: "Numer  Początek  Koniec  ..."
            tokens = stripped.split()
            if len(tokens) >= 3:
                last_end = tokens[2].rstrip("MB").rstrip("B").strip()
                # usuń jednostkę: wartość może być "6482MB" lub "6482"
                last_end = tokens[2]
    if last_end is None:
        raise SystemExit("BŁĄD: Nie można ustalić końca ostatniej partycji (parted print).")
    return last_end


def _ensure_cidata_partition(disk: str) -> str:
    """Upewnia się że istnieje partycja FAT32 z etykietą 'cidata'.

    Jeśli partycja już istnieje (po etykiecie blkid/lsblk), formatuje ją
    ponownie (mkfs.vfat) i zwraca jej ścieżkę.
    Jeśli nie — tworzy nową na końcu dysku i formatuje.
    Zawsze zwraca ścieżkę partycji.
    """
    existing = _find_cidata_partition(disk)
    if existing:
        print(f"[autoinstall-helper] Znaleziono istniejącą partycję cidata: {existing} — formatowanie FAT32...")
        _run("mkfs.vfat", "-F", "32", "-n", CIDATA_LABEL, existing)
        return existing

    # Utwórz nową partycję na końcu
    print("[autoinstall-helper] Brak partycji 'cidata' — tworzę nową na końcu dysku...")
    last_end = _get_last_partition_end(disk)
    _run(
        "parted", disk, "--script",
        "mkpart", "primary", "fat32", last_end, "100%",
    )
    # Wymuś ponowne odczytanie tablicy partycji przez kernel
    subprocess.run(["partprobe", disk], check=False)
    subprocess.run(["udevadm", "settle"], check=False)

    # Ustal numer nowej partycji (ostatniej)
    part_path = _find_last_partition(disk)
    if not part_path:
        raise SystemExit("BŁĄD: Nie można znaleźć nowo utworzonej partycji.")

    print(f"[autoinstall-helper] Formatowanie {part_path} jako FAT32 (etykieta: {CIDATA_LABEL})...")
    _run("mkfs.vfat", "-F", "32", "-n", CIDATA_LABEL, part_path)
    return part_path


def _find_last_partition(disk: str) -> str | None:
    """Zwraca ścieżkę ostatniej partycji na dysku."""
    lsblk = subprocess.run(
        ["lsblk", "-ln", "-o", "NAME", disk],
        capture_output=True, text=True, check=False,
    )
    parts = []
    for line in lsblk.stdout.splitlines():
        name = line.strip()
        if name and name != disk.lstrip("/dev/"):
            parts.append(f"/dev/{name}")
    return parts[-1] if parts else None


@contextlib.contextmanager
def _mount_context(partition: str):
    """Context manager: montuje partycję w katalogu tymczasowym i odmontowuje po wyjściu."""
    mount_point = Path(tempfile.mkdtemp(prefix="cidata_mount_"))
    print(f"[autoinstall-helper] Montowanie {partition} → {mount_point}")
    _run("mount", partition, str(mount_point))
    try:
        yield mount_point
    finally:
        _run("sync")
        _run("umount", str(mount_point), check=False)
        shutil.rmtree(mount_point, ignore_errors=True)
        print(f"[autoinstall-helper] Odmontowano i usunięto katalog {mount_point}.")


def import_autoinstall(yaml_path: Path, target: Path) -> int:
    """Osadza autoinstall.yaml w partycji cidata (FAT32) na targecie.

    Obsługuje zarówno plik-obraz (przez losetup) jak i urządzenie blokowe.
    """
    if not yaml_path.exists():
        raise SystemExit(f"BŁĄD: Plik YAML nie istnieje: {yaml_path}")
    if not yaml_path.is_file():
        raise SystemExit(f"BŁĄD: Podana ścieżka nie jest plikiem: {yaml_path}")
    if not target.exists() and not _is_block_device(target):
        raise SystemExit(f"BŁĄD: Target nie istnieje: {target}")

    is_image_file = target.is_file() and not _is_block_device(target)

    if is_image_file:
        # Plik-obraz → podepnij przez losetup
        with _loop_context(target) as loop_dev:
            part = _ensure_cidata_partition(loop_dev)
            with _mount_context(part) as mnt:
                _write_cidata(yaml_path, mnt)
    else:
        # Urządzenie blokowe → bezpośrednio
        disk = str(target)
        part = _ensure_cidata_partition(disk)
        with _mount_context(part) as mnt:
            _write_cidata(yaml_path, mnt)

    print(f"[autoinstall-helper] Import zakończony pomyślnie: {yaml_path} → {target} (cidata)")
    return 0


def _write_cidata(yaml_path: Path, mount_point: Path) -> None:
    """Kopiuje autoinstall.yaml jako user-data i tworzy pusty meta-data."""
    user_data = mount_point / "user-data"
    meta_data = mount_point / "meta-data"

    shutil.copy2(yaml_path, user_data)
    print(f"[autoinstall-helper] Skopiowano {yaml_path.name} → {user_data}")

    meta_data.touch()
    print(f"[autoinstall-helper] Utworzono pusty plik: {meta_data}")


def export_autoinstall(target: Path, yaml_path: Path) -> int:
    """Odczytuje user-data z partycji cidata (FAT32) na targecie i zapisuje do pliku.

    Obsługuje zarówno plik-obraz (przez losetup) jak i urządzenie blokowe.
    """
    if not target.exists() and not _is_block_device(target):
        raise SystemExit(f"BŁĄD: Target nie istnieje: {target}")
    if yaml_path.exists() and yaml_path.is_dir():
        raise SystemExit(f"BŁĄD: Podana ścieżka wyjściowa jest katalogiem: {yaml_path}")

    is_image_file = target.is_file() and not _is_block_device(target)

    if is_image_file:
        with _loop_context(target) as loop_dev:
            part = _find_cidata_partition(loop_dev)
            if not part:
                raise SystemExit(
                    f"BŁĄD: Nie znaleziono partycji 'cidata' w obrazie {target}."
                )
            with _mount_context(part) as mnt:
                _read_cidata(mnt, yaml_path)
    else:
        disk = str(target)
        part = _find_cidata_partition(disk)
        if not part:
            raise SystemExit(
                f"BŁĄD: Nie znaleziono partycji 'cidata' na urządzeniu {target}."
            )
        with _mount_context(part) as mnt:
            _read_cidata(mnt, yaml_path)

    print(f"[autoinstall-helper] Export zakończony pomyślnie: {target} (cidata) → {yaml_path}")
    return 0


def _read_cidata(mount_point: Path, yaml_path: Path) -> None:
    """Kopiuje user-data z zamontowanej partycji cidata do pliku wyjściowego."""
    user_data = mount_point / "user-data"
    if not user_data.exists():
        raise SystemExit(
            f"BŁĄD: Brak pliku 'user-data' na partycji cidata (szukano w {user_data})."
        )
    shutil.copy2(user_data, yaml_path)
    print(f"[autoinstall-helper] Skopiowano user-data → {yaml_path}")


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
    parser.add_argument(
        "--flash",
        nargs=2,
        metavar=("ISO", "TARGET"),
        help=(
            "Zapisuje obraz ISO na TARGET (urządzenie blokowe np. /dev/sdb "
            "lub plik udający pendrive). Jeśli TARGET nie istnieje, zostanie "
            "utworzony przez fallocate (rozmiar ISO + 128 MB). "
            "Na systemach BTRFS plik otrzyma atrybut No-COW."
        ),
    )
    parser.add_argument(
        "--import",
        nargs=2,
        metavar=("YAML", "TARGET"),
        dest="import_autoinstall",
        help=(
            "Osadza YAML jako autoinstall (user-data) w partycji FAT32 "
            "z etykietą 'cidata' na TARGET (urządzenie blokowe lub plik-obraz). "
            "Jeśli partycja 'cidata' nie istnieje, zostanie utworzona na końcu "
            "wolnego miejsca. Istniejące user-data jest nadpisywane."
        ),
    )
    parser.add_argument(
        "--export",
        nargs=2,
        metavar=("TARGET", "YAML"),
        help=(
            "Odczytuje user-data z partycji 'cidata' na TARGET (urządzenie "
            "blokowe lub plik-obraz) i zapisuje do pliku YAML. "
            "Błąd jeśli partycja 'cidata' lub plik user-data nie istnieje."
        ),
    )
    args = parser.parse_args()

    if args.validate:
        return validate(args.validate)

    if args.flash:
        iso_path, target = Path(args.flash[0]), Path(args.flash[1])
        return flash(iso_path, target)

    if args.import_autoinstall:
        yaml_path, target = Path(args.import_autoinstall[0]), Path(args.import_autoinstall[1])
        return import_autoinstall(yaml_path, target)

    if args.export:
        target, yaml_path = Path(args.export[0]), Path(args.export[1])
        return export_autoinstall(target, yaml_path)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
