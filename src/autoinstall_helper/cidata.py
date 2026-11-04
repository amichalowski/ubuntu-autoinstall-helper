"""Embedding and reading autoinstall.yaml in the cidata (FAT32) partition."""

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
    """Run a command, optionally capturing the output."""
    return subprocess.run(
        list(cmd),
        check=check,
        capture_output=capture,
        text=capture,
        **kwargs,
    )


def _losetup_attach(image: Path) -> str:
    """Attach an image file to a loop device and return its path, e.g. /dev/loop46."""
    result = _run("losetup", "-fP", "--show", str(image), capture=True)
    loop_dev = result.stdout.strip()
    subprocess.run(["udevadm", "settle"], check=False)
    return loop_dev


def _losetup_detach(loop_dev: str) -> None:
    """Detach the loop device."""
    _run("losetup", "-d", loop_dev, check=False)


@contextlib.contextmanager
def _loop_context(image: Path):
    """Context manager: attach the image to a loop device and detach it on exit."""
    loop_dev = _losetup_attach(image)
    print(f"[autoinstall-helper] Image attached at: {loop_dev}")
    try:
        yield loop_dev
    finally:
        _losetup_detach(loop_dev)
        print(f"[autoinstall-helper] Detached: {loop_dev}")


def _parse_pairs(line: str) -> dict[str, str]:
    """Parse the KEY=\"VALUE\" format returned by e.g. lsblk -P."""
    return dict(re.findall(r'(\w+)="([^"]*)"', line))


def _get_partitions(disk: str) -> list[str]:
    """Return a list of paths to all partitions on the given disk/image."""
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
         all space up to the end of the disk (no room for another one).
    """
    subprocess.run(["udevadm", "settle"], capture_output=True, check=False)

    # 1. Check via lsblk (LABEL and PARTLABEL)
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
        if disk_size is not None and last_end is not None and (disk_size - last_end) < 10.0:
            last_part = partitions[-1]
            print(
                f"[autoinstall-helper] Found an existing trailing partition {last_part} "
                f"occupying all free space ({last_end:.0f}MB / {disk_size:.0f}MB) — assuming it is cidata."
            )
            return last_part

    return None


def _get_last_partition_end(disk: str) -> str:
    """Return the end of the last partition in MB (as a string to pass to parted)."""
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
            # Format: "Number  Start  End  ..."
            tokens = stripped.split()
            if len(tokens) >= 3:
                last_end = tokens[2]
    if last_end is None:
        raise SystemExit("ERROR: Could not determine the end of the last partition (parted print).")
    return last_end


def _ensure_cidata_partition(disk: str) -> str:
    """Ensure that a FAT32 partition labeled 'cidata' exists.

    If the partition already exists (identified by blkid/lsblk label or as the trailing
    partition), it reformats it (mkfs.vfat) and returns its path.
    Otherwise it creates a new one at the end of the disk and formats it.
    It always returns the partition path.
    """
    # Make sure GPT sees the whole available disk/file size
    _fix_gpt_backup_header(disk)
    subprocess.run(["udevadm", "settle"], capture_output=True, check=False)

    existing = _find_cidata_partition(disk)
    if existing:
        print(f"[autoinstall-helper] Using cidata partition: {existing} — formatting FAT32...")
        _run("mkfs.vfat", "-F", "32", "-n", CIDATA_LABEL, existing)
        return existing

    # Check whether there is enough free space for a new partition
    disk_size, last_end_mb = _get_disk_size_and_last_end(disk)
    if disk_size is not None and last_end_mb is not None:
        free_mb = disk_size - last_end_mb
        if free_mb < 10.0:
            raise SystemExit(
                f"ERROR: Not enough free space on disk {disk} to create the 'cidata' partition.\n"
                f"Disk: {disk_size:.0f} MB, end of last partition: {last_end_mb:.0f} MB (free: {free_mb:.1f} MB).\n"
                f"If the medium is an image file, grow it (fallocate/truncate), or point to an existing partition."
            )

    # Create a new partition at the end
    print("[autoinstall-helper] No 'cidata' partition found — creating a new one at the end of the disk...")
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
    # Force the kernel to re-read the partition table
    subprocess.run(["partprobe", disk], check=False)
    subprocess.run(["udevadm", "settle"], check=False)

    # Determine the number of the new (last) partition
    part_path = _find_last_partition(disk)
    if not part_path:
        raise SystemExit("ERROR: Could not find the newly created partition.")

    print(f"[autoinstall-helper] Formatting {part_path} as FAT32 (label: {CIDATA_LABEL})...")
    _run("mkfs.vfat", "-F", "32", "-n", CIDATA_LABEL, part_path)
    return part_path


def _find_last_partition(disk: str) -> str | None:
    """Return the path of the last partition on the disk."""
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
    """Context manager: mount the partition in a temporary directory and unmount it on exit."""
    mount_point = Path(tempfile.mkdtemp(prefix="cidata_mount_"))
    print(f"[autoinstall-helper] Mounting {partition} → {mount_point}")
    _run("mount", partition, str(mount_point))
    try:
        yield mount_point
    finally:
        _run("sync")
        _run("umount", str(mount_point), check=False)
        shutil.rmtree(mount_point, ignore_errors=True)
        print(f"[autoinstall-helper] Unmounted and removed directory {mount_point}.")


def inject(target: Path, yaml_path: Path) -> int:
    """Embed autoinstall.yaml in the cidata (FAT32) partition on the target.

    It handles both image files (via losetup) and block devices.
    """
    if not yaml_path.exists():
        raise SystemExit(f"ERROR: YAML file does not exist: {yaml_path}")
    if not yaml_path.is_file():
        raise SystemExit(f"ERROR: The given path is not a file: {yaml_path}")
    if not target.exists():
        raise SystemExit(f"ERROR: Target does not exist: {target}")

    is_image_file = target.is_file() and not _is_block_device(target)

    if is_image_file:
        # Image file → attach via losetup
        with _loop_context(target) as loop_dev:
            part = _ensure_cidata_partition(loop_dev)
            with _mount_context(part) as mnt:
                _write_cidata(yaml_path, mnt)
    else:
        # Block device → directly
        disk = str(target)
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
    if not target.exists():
        raise SystemExit(f"ERROR: Target does not exist: {target}")
    if yaml_path.exists() and yaml_path.is_dir():
        raise SystemExit(f"ERROR: The given output path is a directory: {yaml_path}")

    is_image_file = target.is_file() and not _is_block_device(target)

    if is_image_file:
        with _loop_context(target) as loop_dev:
            part = _find_cidata_partition(loop_dev)
            if not part:
                raise SystemExit(f"ERROR: 'cidata' partition not found in image {target}.")
            with _mount_context(part) as mnt:
                _read_cidata(mnt, yaml_path)
    else:
        disk = str(target)
        part = _find_cidata_partition(disk)
        if not part:
            raise SystemExit(f"ERROR: 'cidata' partition not found on device {target}.")
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
