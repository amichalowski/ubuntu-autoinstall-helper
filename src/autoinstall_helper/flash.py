"""Write an ISO image to the target (block device or image file)."""

import subprocess
import sys
from pathlib import Path

from autoinstall_helper.disk import fix_gpt_backup_header, is_block_device

# Extra space added when creating the target file (for the partition holding autoinstall.yaml)
FLASH_EXTRA_BYTES = 128 * 1024 * 1024  # 128 MB
FLASH_EXTRA_MB = FLASH_EXTRA_BYTES // (1024 * 1024)


def _is_btrfs(path: Path) -> bool:
    """Check whether the path resides on a BTRFS filesystem."""
    result = subprocess.run(
        ["stat", "-f", "--format=%T", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "btrfs"


def _apply_nocow(target: Path) -> None:
    """Set the No-COW attribute on a new or empty image file on BTRFS.

    ``chattr +C`` only affects extents written afterwards (see chattr(1)), so it
    is effective solely while the file is still empty. Files that already contain
    data are left untouched because converting their existing extents is not
    possible.
    """
    if not _is_btrfs(target.parent):
        return
    if target.exists():
        if target.stat().st_size != 0:
            return
    else:
        target.touch()
    print("[autoinstall-helper] BTRFS detected — setting the No-COW attribute (chattr +C).")
    subprocess.run(["chattr", "+C", str(target)], check=True)


def flash(iso_path: Path, target: Path, yes: bool = False) -> int:
    """Write the ISO image to the target using dd.

    If the target does not exist and is not a block device, it creates
    a file of size ISO + FLASH_EXTRA_BYTES via fallocate.
    On BTRFS filesystems the file gets the No-COW attribute (chattr +C).
    It calls sync after dd finishes.

    Note: Writing to block devices requires administrator (root) privileges,
    enforced at the CLI entry point. Flashing to a regular file does not
    require root by design.
    """
    # ISO validation
    if not iso_path.exists():
        raise SystemExit(f"ERROR: ISO file does not exist: {iso_path}")
    if not iso_path.is_file():
        raise SystemExit(f"ERROR: The given ISO path is not a file: {iso_path}")

    iso_size = iso_path.stat().st_size
    print(f"[autoinstall-helper] ISO: {iso_path} ({iso_size / 1024**2:.1f} MB)")

    target_is_block = is_block_device(target)

    if target_is_block:
        print(f"[autoinstall-helper] Target: block device {target}")
        if not yes:
            if sys.stdin.isatty():
                confirm = (
                    input(
                        f"[autoinstall-helper] WARNING: Target '{target}' is a block device!\n"
                        f"All data on {target} will be permanently lost. Continue? [y/N]: "
                    )
                    .strip()
                    .lower()
                )
                if confirm not in ("y", "yes"):
                    print("[autoinstall-helper] Cancelled by user.")
                    return 1
            else:
                raise SystemExit(
                    f"ERROR: Writing to block device {target} in non-interactive mode requires the -y / --yes flag."
                )
    elif target.exists():
        total_size = iso_size + FLASH_EXTRA_BYTES
        current_size = target.stat().st_size
        if current_size < total_size:
            print(
                f"[autoinstall-helper] Target is an existing file {target} ({current_size / 1024**2:.1f} MB). "
                f"Growing to {total_size / 1024**2:.1f} MB (ISO + {FLASH_EXTRA_MB} MB spare)..."
            )
            _apply_nocow(target)
            subprocess.run(["fallocate", "-l", str(total_size), str(target)], check=True)
        else:
            print(f"[autoinstall-helper] Target: existing file {target} ({current_size / 1024**2:.1f} MB)")
    else:
        # Create the target file
        total_size = iso_size + FLASH_EXTRA_BYTES
        print(
            f"[autoinstall-helper] Target does not exist. Creating file "
            f"{target} ({total_size / 1024**2:.1f} MB = ISO + {FLASH_EXTRA_MB} MB spare)..."
        )

        # On BTRFS set the No-COW attribute before writing any data
        _apply_nocow(target)

        subprocess.run(
            ["fallocate", "-l", str(total_size), str(target)],
            check=True,
        )
        print(f"[autoinstall-helper] File {target} created.")

    # Write the ISO via dd (conv=notrunc,fsync prevents truncating the file to the ISO size)
    print(f"[autoinstall-helper] Writing ISO to {target} (dd)... This may take a while.")
    dd_cmd = [
        "dd",
        f"if={iso_path}",
        f"of={target}",
        "bs=4M",
        "status=progress",
        "conv=notrunc,fsync",
    ]
    result = subprocess.run(dd_cmd, check=False)
    if result.returncode != 0:
        raise SystemExit(f"ERROR: dd exited with code {result.returncode}.")

    print("[autoinstall-helper] Synchronizing buffers (sync)...")
    subprocess.run(["sync"], check=True)

    # Repair the GPT after dd to the enlarged target
    fix_gpt_backup_header(target)

    print(f"[autoinstall-helper] Flash completed successfully: {iso_path} → {target}")
    return 0
