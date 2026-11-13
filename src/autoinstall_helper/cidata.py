"""Embedding and reading autoinstall.yaml in the cidata (FAT32) partition."""

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

from autoinstall_helper import fsprobe
from autoinstall_helper.disk import fix_gpt_backup_header

CIDATA_LABEL = "cidata"
MIN_CIDATA_FREE_SPACE_MB = 10.0


def _run(*cmd: str, check: bool, capture: bool = False, **kwargs) -> subprocess.CompletedProcess:
    """Run a command with explicit check=True/False, optionally capturing the output."""
    return subprocess.run(
        list(cmd),
        check=check,
        capture_output=capture,
        text=True,
        **kwargs,
    )


def _losetup_attach(image: Path) -> str:
    """Attach an image file to a loop device and return its path, e.g. /dev/loop46."""
    result = _run("losetup", "-fP", "--show", str(image), check=True, capture=True)
    loop_dev = result.stdout.strip()
    subprocess.run(["udevadm", "settle"], check=False)
    return loop_dev


def _losetup_detach(loop_dev: str) -> None:
    """Detach the loop device."""
    _run("losetup", "-d", loop_dev, check=False)


@contextlib.contextmanager
def _loop_context(image: Path) -> Iterator[str]:
    """Context manager: attach the image to a loop device and detach it on exit."""
    loop_dev = _losetup_attach(image)
    print(f"[autoinstall-helper] Image attached at: {loop_dev}")
    try:
        yield loop_dev
    finally:
        _losetup_detach(loop_dev)
        print(f"[autoinstall-helper] Detached: {loop_dev}")


@contextlib.contextmanager
def _target_disk_context(target: Path) -> Iterator[str]:
    """Yield the disk or attached loop device path for the target."""
    info = fsprobe.probe(target)
    if info.is_file and not info.is_block:
        with _loop_context(target) as loop_dev:
            yield loop_dev
    else:
        yield str(target)


def _lsblk_devices(*args: str) -> list[dict]:
    """Run lsblk with JSON output and return the top-level ``blockdevices`` list."""
    res = subprocess.run(
        ["lsblk", "-J", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return json.loads(res.stdout).get("blockdevices", [])
    except json.JSONDecodeError:
        return []


def _iter_devices(devices: list[dict]) -> Iterator[dict]:
    """Yield each device and all of its descendants (depth-first)."""
    for dev in devices:
        yield dev
        yield from _iter_devices(dev.get("children") or [])


def _get_partitions(disk: str) -> list[str]:
    """Return a list of paths to all partitions on the given disk/image."""
    subprocess.run(["udevadm", "settle"], capture_output=True, text=True, check=False)
    devices = _lsblk_devices("-p", "-o", "PATH,TYPE", disk)
    return [dev["path"] for dev in _iter_devices(devices) if dev.get("type") == "part" and dev.get("path")]


def _get_disk_size_and_last_end(disk: str) -> tuple[float | None, float | None]:
    """Return (disk_size_MB, last_partition_end_MB) from the parted output."""
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
    """Look for a cidata partition on the disk/loop dev.

    It verifies:
      1. The filesystem label (LABEL) and partition label (PARTLABEL) via lsblk.
      2. Direct superblock probing via blkid -p (independent of the udev cache).
      3. The case where the medium already has an extra partition (no. > 3) taking up
         all space up to the end of the disk, ensuring it does not have a conflicting
         non-FAT filesystem or other label.
    """
    subprocess.run(["udevadm", "settle"], capture_output=True, text=True, check=False)

    # 1. Check via lsblk (LABEL and PARTLABEL)
    for dev in _iter_devices(_lsblk_devices("-p", "-o", "PATH,LABEL,PARTLABEL", disk)):
        path = dev.get("path", "")
        label = (dev.get("label") or "").lower()
        partlabel = (dev.get("partlabel") or "").lower()
        if path != disk and (label == CIDATA_LABEL or partlabel == CIDATA_LABEL):
            return path

    # 2. Direct superblock query via blkid -p for each partition
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

    # 3. If an extra partition (> 3) already exists and reaches the end of the disk (no room for a new one)
    if len(partitions) > 3:
        disk_size, last_end = _get_disk_size_and_last_end(disk)
        if disk_size is not None and last_end is not None and (disk_size - last_end) < MIN_CIDATA_FREE_SPACE_MB:
            last_part = partitions[-1]
            b_res = subprocess.run(
                ["blkid", "-p", "-o", "export", last_part],
                capture_output=True,
                text=True,
                check=False,
            )
            b_info = {}
            for b_line in b_res.stdout.splitlines():
                if "=" in b_line:
                    k, v = b_line.split("=", 1)
                    b_info[k.strip()] = v.strip()
            fs_type = b_info.get("TYPE", "").lower()
            label = b_info.get("LABEL", "").lower()
            partlabel = b_info.get("PARTLABEL", "").lower()
            # Only consider it if it does not have an explicit conflicting label or non-FAT filesystem
            if fs_type in ("", "vfat", "msdos") and label in ("", CIDATA_LABEL) and partlabel in ("", CIDATA_LABEL):
                print(
                    f"[autoinstall-helper] Found an existing trailing partition {last_part} "
                    f"occupying all free space ({last_end:.0f}MB / {disk_size:.0f}MB) — assuming it is cidata."
                )
                return last_part

    return None


def _get_last_partition_end(disk: str) -> str:
    """Return the end of the last partition in MB (as a string to pass to parted)."""
    _, last_end = _get_disk_size_and_last_end(disk)
    if last_end is None:
        raise SystemExit("ERROR: Could not determine the end of the last partition (parted print).")
    if last_end.is_integer():
        return f"{int(last_end)}MB"
    return f"{last_end}MB"


def _ensure_cidata_partition(disk: str) -> str:
    """Ensure that a FAT32 partition labeled 'cidata' exists.

    If the partition already exists (identified by blkid/lsblk label or as the trailing
    partition), it reformats it (mkfs.vfat) and returns its path.
    Otherwise it creates a new one at the end of the disk and formats it.
    It always returns the partition path.
    """
    # Make sure GPT sees the whole available disk/file size
    fix_gpt_backup_header(disk)
    subprocess.run(["udevadm", "settle"], capture_output=True, text=True, check=False)

    existing = _find_cidata_partition(disk)
    if existing:
        print(f"[autoinstall-helper] Using cidata partition: {existing} — formatting FAT32...")
        _run("mkfs.vfat", "-F", "32", "-n", CIDATA_LABEL, existing, check=True)
        return existing

    # Check whether there is enough free space for a new partition
    disk_size, last_end_mb = _get_disk_size_and_last_end(disk)
    if disk_size is not None and last_end_mb is not None:
        free_mb = disk_size - last_end_mb
        if free_mb < MIN_CIDATA_FREE_SPACE_MB:
            raise SystemExit(
                f"ERROR: Not enough free space on disk {disk} to create the 'cidata' partition.\n"
                f"Disk: {disk_size:.0f} MB, end of last partition: {last_end_mb:.0f} MB (free: {free_mb:.1f} MB).\n"
                f"If the medium is an image file, grow it (fallocate/truncate), or point to an existing partition."
            )

    if last_end_mb is None:
        raise SystemExit("ERROR: Could not determine the end of the last partition (parted print).")

    # Create a new partition at the end
    print("[autoinstall-helper] No 'cidata' partition found — creating a new one at the end of the disk...")
    last_end = f"{int(last_end_mb)}MB" if last_end_mb.is_integer() else f"{last_end_mb}MB"
    partitions_before = _get_partitions(disk)
    _run(
        "parted",
        disk,
        "--script",
        "unit",
        "MB",
        "mkpart",
        CIDATA_LABEL,
        "fat32",
        last_end,
        "100%",
        check=True,
    )
    # Force the kernel to re-read the partition table
    subprocess.run(["partprobe", disk], check=False)
    subprocess.run(["udevadm", "settle"], check=False)

    # Determine the number of the new (last) partition and verify count increased
    partitions_after = _get_partitions(disk)
    if len(partitions_after) <= len(partitions_before):
        raise SystemExit(f"ERROR: Failed to create 'cidata' partition on {disk} (partition count did not increase).")

    part_path = partitions_after[-1]
    print(f"[autoinstall-helper] Formatting {part_path} as FAT32 (label: {CIDATA_LABEL})...")
    _run("mkfs.vfat", "-F", "32", "-n", CIDATA_LABEL, part_path, check=True)
    return part_path


def _find_last_partition(disk: str) -> str | None:
    """Return the path of the last partition on the disk."""
    partitions = _get_partitions(disk)
    return partitions[-1] if partitions else None


@contextlib.contextmanager
def _mount_context(partition: str) -> Iterator[Path]:
    """Context manager: mount the partition in a temporary directory and unmount it on exit."""
    mount_point = Path(tempfile.mkdtemp(prefix="cidata_mount_"))
    print(f"[autoinstall-helper] Mounting {partition} → {mount_point}")
    _run("mount", partition, str(mount_point), check=True)
    try:
        yield mount_point
    finally:
        _run("sync", check=True)
        _run("umount", str(mount_point), check=False)
        shutil.rmtree(mount_point, ignore_errors=True)
        print(f"[autoinstall-helper] Unmounted and removed directory {mount_point}.")


def inject(target: Path, yaml_path: Path) -> int:
    """Embed autoinstall.yaml in the cidata (FAT32) partition on the target.

    It handles both image files (via losetup) and block devices.
    """
    yaml_info = fsprobe.probe(yaml_path)
    if not yaml_info.exists:
        raise SystemExit(f"ERROR: YAML file does not exist: {yaml_path}")
    if not yaml_info.is_file:
        raise SystemExit(f"ERROR: The given path is not a file: {yaml_path}")
    if not fsprobe.probe(target).exists:
        raise SystemExit(f"ERROR: Target does not exist: {target}")

    with _target_disk_context(target) as disk:
        part = _ensure_cidata_partition(disk)
        with _mount_context(part) as mnt:
            _write_cidata(yaml_path, mnt)

    print(f"[autoinstall-helper] Injection completed successfully: {yaml_path} → {target} (cidata)")
    return 0


# Backward-compatibility alias
import_autoinstall = inject


def _write_cidata(yaml_path: Path, mount_point: Path) -> None:
    """Copy autoinstall.yaml as user-data and create an empty meta-data."""
    user_data = mount_point / "user-data"
    meta_data = mount_point / "meta-data"

    shutil.copy2(yaml_path, user_data)
    print(f"[autoinstall-helper] Copied {yaml_path.name} → {user_data}")

    meta_data.touch()
    print(f"[autoinstall-helper] Created empty file: {meta_data}")


def extract(target: Path, yaml_path: Path) -> int:
    """Read user-data from the cidata (FAT32) partition on the target and save it to a file.

    It handles both image files (via losetup) and block devices.
    """
    if not fsprobe.probe(target).exists:
        raise SystemExit(f"ERROR: Target does not exist: {target}")
    yaml_info = fsprobe.probe(yaml_path)
    if yaml_info.exists and yaml_info.is_dir:
        raise SystemExit(f"ERROR: The given output path is a directory: {yaml_path}")

    with _target_disk_context(target) as disk:
        part = _find_cidata_partition(disk)
        if not part:
            target_info = fsprobe.probe(target)
            target_desc = "image" if target_info.is_file and not target_info.is_block else "device"
            raise SystemExit(f"ERROR: 'cidata' partition not found on {target_desc} {target}.")
        with _mount_context(part) as mnt:
            _read_cidata(mnt, yaml_path)

    print(f"[autoinstall-helper] Export completed successfully: {target} (cidata) → {yaml_path}")
    return 0


# Backward-compatibility alias
export_autoinstall = extract


def _read_cidata(mount_point: Path, yaml_path: Path) -> None:
    """Copy user-data from the mounted cidata partition to the output file."""
    user_data = mount_point / "user-data"
    if not user_data.exists():
        raise SystemExit(f"ERROR: 'user-data' file missing on the cidata partition (looked in {user_data}).")
    shutil.copy2(user_data, yaml_path)
    print(f"[autoinstall-helper] Copied user-data → {yaml_path}")
