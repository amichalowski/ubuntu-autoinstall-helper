"""Wspólne narzędzia do pracy z urządzeniami blokowymi i tablicami GPT."""

import shutil
import subprocess
from pathlib import Path


def _is_block_device(path: Path) -> bool:
    """Sprawdza czy ścieżka wskazuje na urządzenie blokowe."""
    try:
        return path.stat().st_mode & 0o170000 == 0o060000
    except OSError:
        return False


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
