"""Osadzanie i odczyt autoinstall.yaml w partycji cidata (FAT32)."""

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from autoinstall_helper.disk import _fix_gpt_backup_header, _is_block_device

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
        capture_output=True,
        text=True,
        check=False,
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
        capture_output=True,
        text=True,
        check=False,
        env=env,
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
        capture_output=True,
        text=True,
        check=False,
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
            capture_output=True,
            text=True,
            check=False,
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
        "parted",
        disk,
        "--script",
        "unit",
        "MB",
        "print",
        capture=True,
        check=False,
        env=env,
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
        "parted",
        disk,
        "--script",
        "mkpart",
        CIDATA_LABEL,
        "fat32",
        last_end,
        "100%",
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
        capture_output=True,
        text=True,
        check=False,
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
                raise SystemExit(f"BŁĄD: Nie znaleziono partycji 'cidata' w obrazie {target}.")
            with _mount_context(part) as mnt:
                _read_cidata(mnt, yaml_path)
    else:
        disk = str(target)
        part = _find_cidata_partition(disk)
        if not part:
            raise SystemExit(f"BŁĄD: Nie znaleziono partycji 'cidata' na urządzeniu {target}.")
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
        raise SystemExit(f"BŁĄD: Brak pliku 'user-data' na partycji cidata (szukano w {user_data}).")
    shutil.copy2(user_data, yaml_path)
    print(f"[autoinstall-helper] Skopiowano user-data → {yaml_path}")
