"""Zapis obrazu ISO na target (urządzenie blokowe lub plik-obraz)."""

import subprocess
import sys
from pathlib import Path

from autoinstall_helper.disk import _fix_gpt_backup_header, _is_block_device

# Zapas miejsca dodawany przy tworzeniu pliku-targetu (na partycję z autoinstall.yaml)
FLASH_EXTRA_BYTES = 128 * 1024 * 1024  # 128 MB


def _is_btrfs(path: Path) -> bool:
    """Sprawdza czy ścieżka leży na systemie plików BTRFS."""
    result = subprocess.run(
        ["stat", "-f", "--format=%T", str(path)],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "btrfs"


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
