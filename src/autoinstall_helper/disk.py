"""Shared helpers for working with block devices and GPT tables."""

import shutil
import subprocess
from pathlib import Path


def is_block_device(path: Path) -> bool:
    """Check whether the path points to a block device."""
    try:
        return path.stat().st_mode & 0o170000 == 0o060000
    except OSError:
        return False


def fix_gpt_backup_header(target: Path | str) -> None:
    """Move the backup GPT header to the physical end of the disk/image.

    After growing a file with fallocate or writing an ISO to a larger USB stick,
    the backup GPT header is left in its old location, causing errors and warnings
    in parted/kernel. sgdisk -e repairs the table and moves the header to the end.
    """
    target_str = str(target)
    if shutil.which("sgdisk"):
        res = subprocess.run(["sgdisk", "-e", target_str], capture_output=True, text=True, check=False)
        if res.returncode == 0:
            print(f"[autoinstall-helper] Adjusted the backup GPT header to the end of the medium ({target_str}).")
    subprocess.run(["partprobe", target_str], capture_output=True, text=True, check=False)
