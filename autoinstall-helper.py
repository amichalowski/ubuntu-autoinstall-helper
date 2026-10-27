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
"""

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
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
        with config_file.open() as f:
            return f.readline().strip() == "#cloud-config"
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Uprawnienia i walidacja środowiska
# ---------------------------------------------------------------------------

def _require_root(action: str) -> None:
    """Sprawdza czy proces posiada uprawnienia roota; jeśli nie, kończy z czytelnym komunikatem."""
    if os.geteuid() != 0:
        cmd_args = " ".join(sys.argv)
        raise SystemExit(
            f"BŁĄD: Operacja '{action}' wymaga uprawnień administratora (root).\n"
            f"Uruchom ponownie z sudo:\n"
            f"  sudo {sys.executable} {cmd_args}"
        )


# ---------------------------------------------------------------------------
# Snap + snapcraft.yaml
# ---------------------------------------------------------------------------

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
            channel_entries = [
                e for e in data.get("channel-map", [])
                if e.get("channel", {}).get("name") == channel
            ]
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
                capture_output=True, text=True, check=True,
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


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

def docker_image_exists(tag: str) -> bool:
    res = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
    )
    return res.returncode == 0


def docker_image_tag(version: str, dockerfile: Path) -> str:
    """Include local validation assets in the image tag to avoid stale images."""
    digest = hashlib.sha256()
    for path in (
        dockerfile,
        dockerfile.parent / "timedatectl.sh",
        dockerfile.parent / "validate-autoinstall.py",
    ):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return f"{DOCKER_IMAGE_REPO}:{version}-{digest.hexdigest()[:12]}"


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


def docker_validate(tag: str, config_file: Path, validation: str) -> int:
    container_path = "/data/autoinstall.yaml"
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{config_file.resolve()}:{container_path}:ro",
        tag,
        "python3", "scripts/validate-autoinstall.py",
        container_path,
        "--validation", validation,
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


def _fix_gpt_backup_header(target: Path | str) -> None:
    """Przenosi kopię zapasową nagłówka GPT (Backup GPT) na fizyczny koniec dysku/obrazu.

    Po powiększeniu pliku przez fallocate lub nagraniu ISO na większy pendrive
    nagłówek Backup GPT znajduje się w starym miejscu, co powoduje błędy i ostrzeżenia
    w parted/kernelu. sgdisk -e naprawia tablicę i przesuwa nagłówek na koniec.
    """
    target_str = str(target)
    if shutil.which("sgdisk"):
        res = subprocess.run(["sgdisk", "-e", target_str], capture_output=True, text=True, check=False)
        if res.returncode == 0:
            print(f"[autoinstall-helper] Dopasowano zapasowy nagłówek GPT do końca nośnika ({target_str}).")
    subprocess.run(["partprobe", target_str], capture_output=True, check=False)


def flash(iso_path: Path, target: Path, yes: bool = False) -> int:
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
        if not yes:
            if sys.stdin.isatty():
                confirm = input(
                    f"[autoinstall-helper] OSTRZEŻENIE: Cel '{target}' jest urządzeniem blokowym!\n"
                    f"Wszystkie dane na {target} zostaną bezpowrotnie utracone. Kontynuować? [y/N]: "
                ).strip().lower()
                if confirm not in ("y", "yes", "t", "tak"):
                    print("[autoinstall-helper] Anulowano przez użytkownika.")
                    return 1
            else:
                raise SystemExit(
                    f"BŁĄD: Zapis na urządzenie blokowe {target} w trybie nieinteraktywnym wymaga flagi -y / --yes."
                )
    elif target.exists():
        total_size = iso_size + FLASH_EXTRA_BYTES
        current_size = target.stat().st_size
        if current_size < total_size:
            print(
                f"[autoinstall-helper] Target to istniejący plik {target} ({current_size / 1024**2:.1f} MB). "
                f"Powiększam do {total_size / 1024**2:.1f} MB (ISO + 128 MB zapasu)..."
            )
            subprocess.run(["fallocate", "-l", str(total_size), str(target)], check=True)
        else:
            print(f"[autoinstall-helper] Target: istniejący plik {target} ({current_size / 1024**2:.1f} MB)")
    else:
        # Utwórz plik-target
        total_size = iso_size + FLASH_EXTRA_BYTES
        print(
            f"[autoinstall-helper] Target nie istnieje. Tworzę plik "
            f"{target} ({total_size / 1024**2:.1f} MB = ISO + 128 MB zapasu)..."
        )

        # Na BTRFS ustaw atrybut No-COW przed zapisem danych
        if _is_btrfs(target.parent):
            print(f"[autoinstall-helper] Wykryto BTRFS — tworzę plik i ustawiam atrybut No-COW (chattr +C).")
            # Plik musi istnieć przed chattr
            target.touch()
            subprocess.run(["chattr", "+C", str(target)], check=True)

        subprocess.run(
            ["fallocate", "-l", str(total_size), str(target)],
            check=True,
        )
        print(f"[autoinstall-helper] Plik {target} utworzony.")

    # Zapis ISO przez dd (conv=notrunc,fsync zapobiega obcięciu pliku do rozmiaru ISO)
    print(f"[autoinstall-helper] Zapisywanie ISO na {target} (dd)... To może chwilę potrwać.")
    dd_cmd = [
        "dd",
        f"if={iso_path}",
        f"of={target}",
        "bs=4M",
        "status=progress",
        "conv=notrunc,fsync",
    ]
    result = subprocess.run(dd_cmd)
    if result.returncode != 0:
        raise SystemExit(f"BŁĄD: dd zakończył się kodem {result.returncode}.")

    print("[autoinstall-helper] Synchronizacja buforów (sync)...")
    subprocess.run(["sync"], check=True)

    # Napraw tablicę GPT po dd na powiększony target
    _fix_gpt_backup_header(target)

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
    loop_dev = result.stdout.strip()
    subprocess.run(["udevadm", "settle"], check=False)
    return loop_dev


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


def _parse_pairs(line: str) -> dict[str, str]:
    """Parsuje format KEY=\"VALUE\" zwracany m.in. przez lsblk -P."""
    return dict(re.findall(r'(\w+)="([^"]*)"', line))


def _get_partitions(disk: str) -> list[str]:
    """Zwraca listę ścieżek do wszystkich partycji na danym dysku/obrazie."""
    subprocess.run(["udevadm", "settle"], capture_output=True, check=False)
    res = subprocess.run(
        ["lsblk", "-np", "-o", "PATH,TYPE", disk],
        capture_output=True, text=True, check=False,
    )
    partitions = []
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "part":
            partitions.append(parts[0])
    return partitions


def _get_disk_size_and_last_end(disk: str) -> tuple[float | None, float | None]:
    """Zwraca (rozmiar_dysku_MB, koniec_ostatniej_partycji_MB) z wyjścia parted."""
    env = dict(os.environ, LC_ALL="C")
    result = subprocess.run(
        ["parted", disk, "--script", "unit", "MB", "print"],
        capture_output=True, text=True, check=False, env=env,
    )
    disk_size = None
    last_end = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        m = re.match(r"^Disk\s+.+?:\s*([0-9.]+)\s*MB", stripped)
        if m:
            try:
                disk_size = float(m.group(1))
            except ValueError:
                pass
        elif stripped and stripped[0].isdigit():
            tokens = stripped.split()
            if len(tokens) >= 3:
                num_str = tokens[2].rstrip("MB").rstrip("B")
                try:
                    last_end = float(num_str)
                except ValueError:
                    pass
    return disk_size, last_end


def _find_cidata_partition(disk: str) -> str | None:
    """Szuka partycji cidata na dysku/loop dev.

    Weryfikuje:
      1. Etykietę systemu plików (LABEL) oraz etykietę partycji (PARTLABEL) przez lsblk.
      2. Bezpośrednie próbkowanie superbloków przez blkid -p (niezależne od pamięci podręcznej udev).
      3. Sytuację, gdy na nośniku istnieje już dodatkowa partycja (nr > 3) zajmująca
         całą przestrzeń do końca dysku (brak miejsca na kolejną).
    """
    subprocess.run(["udevadm", "settle"], capture_output=True, check=False)

    # 1. Sprawdzenie przez lsblk (LABEL oraz PARTLABEL)
    lsblk = subprocess.run(
        ["lsblk", "-npP", "-o", "PATH,LABEL,PARTLABEL", disk],
        capture_output=True, text=True, check=False,
    )
    for line in lsblk.stdout.splitlines():
        info = _parse_pairs(line)
        path = info.get("PATH", "")
        label = info.get("LABEL", "").lower()
        partlabel = info.get("PARTLABEL", "").lower()
        if path != disk and (label == CIDATA_LABEL or partlabel == CIDATA_LABEL):
            return path

    # 2. Bezpośrednie odpytanie superbloków przez blkid -p dla każdej partycji
    partitions = _get_partitions(disk)
    for part in partitions:
        b_res = subprocess.run(
            ["blkid", "-p", "-o", "export", part],
            capture_output=True, text=True, check=False,
        )
        b_info = {}
        for b_line in b_res.stdout.splitlines():
            if "=" in b_line:
                k, v = b_line.split("=", 1)
                b_info[k.strip()] = v.strip()
        label = b_info.get("LABEL", "").lower()
        partlabel = b_info.get("PARTLABEL", "").lower()
        if label == CIDATA_LABEL or partlabel == CIDATA_LABEL:
            return part

    # 3. Jeśli istnieje już partycja dodatkowa (> 3) i sięga końca dysku (brak miejsca na nową)
    if len(partitions) > 3:
        disk_size, last_end = _get_disk_size_and_last_end(disk)
        if disk_size is not None and last_end is not None:
            if (disk_size - last_end) < 10.0:
                last_part = partitions[-1]
                print(
                    f"[autoinstall-helper] Znaleziono istniejącą partycję końcową {last_part} "
                    f"zajmującą całą wolną przestrzeń ({last_end:.0f}MB / {disk_size:.0f}MB) — przyjęto jako cidata."
                )
                return last_part

    return None


def _get_last_partition_end(disk: str) -> str:
    """Zwraca koniec ostatniej partycji w MB (jako string do przekazania do parted)."""
    env = dict(os.environ, LC_ALL="C")
    result = _run(
        "parted", disk, "--script", "unit", "MB", "print",
        capture=True, check=False, env=env,
    )
    last_end = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped and stripped[0].isdigit():
            # Format: "Numer  Początek  Koniec  ..."
            tokens = stripped.split()
            if len(tokens) >= 3:
                last_end = tokens[2]
    if last_end is None:
        raise SystemExit("BŁĄD: Nie można ustalić końca ostatniej partycji (parted print).")
    return last_end


def _ensure_cidata_partition(disk: str) -> str:
    """Upewnia się że istnieje partycja FAT32 z etykietą 'cidata'.

    Jeśli partycja już istnieje (po etykiecie blkid/lsblk lub jako partycja końcowa),
    formatuje ją ponownie (mkfs.vfat) i zwraca jej ścieżkę.
    Jeśli nie — tworzy nową na końcu dysku i formatuje.
    Zawsze zwraca ścieżkę partycji.
    """
    # Upewnij się, że GPT widzi cały dostępny rozmiar dysku/pliku
    _fix_gpt_backup_header(disk)
    subprocess.run(["udevadm", "settle"], capture_output=True, check=False)

    existing = _find_cidata_partition(disk)
    if existing:
        print(f"[autoinstall-helper] Użycie partycji cidata: {existing} — formatowanie FAT32...")
        _run("mkfs.vfat", "-F", "32", "-n", CIDATA_LABEL, existing)
        return existing

    # Sprawdź czy jest wystarczająco dużo wolnego miejsca na nową partycję
    disk_size, last_end_mb = _get_disk_size_and_last_end(disk)
    if disk_size is not None and last_end_mb is not None:
        free_mb = disk_size - last_end_mb
        if free_mb < 10.0:
            raise SystemExit(
                f"BŁĄD: Brak wolnego miejsca na dysku {disk} na utworzenie partycji 'cidata'.\n"
                f"Dysk: {disk_size:.0f} MB, Koniec ostatniej partycji: {last_end_mb:.0f} MB (wolne: {free_mb:.1f} MB).\n"
                f"Jeśli nośnik to plik-obraz, powiększ go (fallocate/truncate), lub wskaż istniejącą partycję."
            )

    # Utwórz nową partycję na końcu
    print("[autoinstall-helper] Brak partycji 'cidata' — tworzę nową na końcu dysku...")
    last_end = _get_last_partition_end(disk)
    _run(
        "parted", disk, "--script",
        "mkpart", CIDATA_LABEL, "fat32", last_end, "100%",
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
        if name and name != disk.removeprefix("/dev/"):
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


def inject(target: Path, yaml_path: Path) -> int:
    """Osadza autoinstall.yaml w partycji cidata (FAT32) na targecie.

    Obsługuje zarówno plik-obraz (przez losetup) jak i urządzenie blokowe.
    """
    if not yaml_path.exists():
        raise SystemExit(f"BŁĄD: Plik YAML nie istnieje: {yaml_path}")
    if not yaml_path.is_file():
        raise SystemExit(f"BŁĄD: Podana ścieżka nie jest plikiem: {yaml_path}")
    if not target.exists():
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

    print(f"[autoinstall-helper] Wstrzykiwanie zakończone pomyślnie: {yaml_path} → {target} (cidata)")
    return 0


# Alias wstecznej kompatybilności
import_autoinstall = inject


def _write_cidata(yaml_path: Path, mount_point: Path) -> None:
    """Kopiuje autoinstall.yaml jako user-data i tworzy pusty meta-data."""
    user_data = mount_point / "user-data"
    meta_data = mount_point / "meta-data"

    shutil.copy2(yaml_path, user_data)
    print(f"[autoinstall-helper] Skopiowano {yaml_path.name} → {user_data}")

    meta_data.touch()
    print(f"[autoinstall-helper] Utworzono pusty plik: {meta_data}")


def extract(target: Path, yaml_path: Path) -> int:
    """Odczytuje user-data z partycji cidata (FAT32) na targecie i zapisuje do pliku.

    Obsługuje zarówno plik-obraz (przez losetup) jak i urządzenie blokowe.
    """
    if not target.exists():
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

    print(f"[autoinstall-helper] Eksport zakończony pomyślnie: {target} (cidata) → {yaml_path}")
    return 0


# Alias wstecznej kompatybilności
export_autoinstall = extract


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

def validate(config_file: Path, validation: str = "full") -> int:
    if not config_file.exists():
        raise SystemExit(f"BŁĄD: Plik nie istnieje: {config_file}")

    dockerfile = Path(__file__).resolve().parent / "docker" / "Dockerfile"
    if not dockerfile.exists():
        raise SystemExit(f"BŁĄD: Nie znaleziono Dockerfile w {dockerfile.parent}")

    print(f"[autoinstall-helper] Odczytywanie: {config_file}")
    channel = extract_installer_channel(config_file)
    print(f"[autoinstall-helper] Kanał instalatora: {channel}")

    version = get_snap_version(channel)
    print(f"[autoinstall-helper] Wersja instalatora: {version}")

    tag = docker_image_tag(version, dockerfile)
    if docker_image_exists(tag):
        print(f"[autoinstall-helper] Obraz '{tag}' już istnieje.")
    else:
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
        prog="autoinstall-helper.py",
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
        "-y", "--yes",
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
