"""Managing libvirt/QEMU virtual machines that test installation media.

The tool creates throwaway VMs which boot an installation image (for example
``pendrive.img`` produced by ``flash`` + ``inject``) attached as a removable USB
disk. That exercises the whole autoinstall flow without physical hardware.

No administrator privileges are required: access to libvirt is granted by the
``libvirt`` (and ``kvm``) group. The system connection (``qemu:///system``) is
preferred and the per-user session (``qemu:///session``) is used as a fallback.

The libvirt domain is the single source of truth — the tool keeps no registry of
its own. Domains created here carry a marker inside their ``<description>``:

    autoinstall-helper: managed=auto media=/path/to/pendrive.img

The marker lets ``vm reinstall`` re-attach the media and lets ``vm delete``
tell the target VM disk apart from the installation medium, which is never
deleted.
"""

import dataclasses
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from autoinstall_helper import fsprobe

SYSTEM_URI = "qemu:///system"
SESSION_URI = "qemu:///session"

VM_MARKER_PREFIX = "autoinstall-helper:"

# Tool identifier stored inside the domain marker; used to recognize VMs created
# by this tool and keep them apart from arbitrary libvirt domains.
TOOL_NAME = "autoinstall-helper"

DEFAULT_DISK_SIZE_GIB = 40
DEFAULT_MEMORY_MB = 4096
DEFAULT_VCPUS = 4
DEFAULT_OS_VARIANT = "ubuntu24.04"

USB_BUS = "usb"
BOOTABLE_PREFIX = ("sd", "vd")

# Packages required for the full experience (see `vm doctor`).
APT_PACKAGES = "virtinst qemu-system-x86 libvirt-daemon-system ovmf swtpm virt-viewer"

# UEFI firmware locations used by libvirt for automatic firmware selection.
UEFI_FIRMWARE_DIRS = (Path("/usr/share/OVMF"), Path("/usr/share/edk2/ovmf"), Path("/usr/share/edk2/x64"))

RUNNING_STATES = ("running", "idle", "paused", "in shutdown", "blocked")


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], *, check: bool, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a command with an explicit check=True/False, optionally capturing output."""
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def virsh_command(uri: str, *args: str) -> list[str]:
    """Build the virsh command line for the given connection URI."""
    return ["virsh", "--connect", uri, *args]


def _virsh(uri: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run virsh with captured output and raise SystemExit on failure."""
    res = _run(virsh_command(uri, *args), check=False, capture=True)
    if check and res.returncode != 0:
        detail = (res.stderr or res.stdout).strip()
        raise SystemExit(f"ERROR: 'virsh {' '.join(args)}' failed:\n{detail}")
    return res


def _virsh_interactive(uri: str, *args: str) -> int:
    """Run virsh attached to the current terminal (no output capture)."""
    return _run(virsh_command(uri, *args), check=False).returncode


# ---------------------------------------------------------------------------
# Preflight (tools, connection, hardware)
# ---------------------------------------------------------------------------


def require_tools(*commands: str) -> None:
    """Exit with an installation hint when a required command is missing."""
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        raise SystemExit(
            f"ERROR: missing required command(s): {', '.join(missing)}.\n"
            f"Install them with:\n"
            f"  sudo apt install {APT_PACKAGES}"
        )


def uri_works(uri: str) -> bool:
    """Return True when virsh can connect to the given libvirt URI."""
    if shutil.which("virsh") is None:
        return False
    return _run(["virsh", "--connect", uri, "version"], check=False, capture=True).returncode == 0


def uri_help(uri: str | None) -> str:
    """Build the error message shown when libvirt is unreachable."""
    target = f"'{uri}'" if uri else "libvirt"
    return (
        f"ERROR: cannot connect to {target}.\n"
        f"Make sure libvirt is running and your user is allowed to use it:\n"
        f"  sudo apt install libvirt-daemon-system\n"
        f"  sudo usermod -aG libvirt,kvm $USER\n"
        f"Then log out and back in (or run 'newgrp libvirt') and retry."
    )


def resolve_uri(requested: str | None = None) -> str:
    """Pick the libvirt URI: the explicit one, otherwise system, otherwise session."""
    if requested:
        if not uri_works(requested):
            raise SystemExit(uri_help(requested))
        return requested
    for candidate in (SYSTEM_URI, SESSION_URI):
        if uri_works(candidate):
            return candidate
    raise SystemExit(uri_help(None))


def find_uefi_firmware() -> str | None:
    """Return the first UEFI firmware blob found, or None when OVMF is missing."""
    for directory in UEFI_FIRMWARE_DIRS:
        for pattern in ("OVMF_CODE*.fd", "*_CODE.fd"):
            matches = sorted(directory.glob(pattern))
            if matches:
                return str(matches[0])
    return None


def detect_virt_type(no_kvm: bool) -> str | None:
    """Return 'qemu' for software emulation, or None to let virt-install autodetect."""
    if no_kvm:
        return "qemu"
    if not Path("/dev/kvm").exists():
        print("[autoinstall-helper] WARNING: /dev/kvm is unavailable — using slow software emulation (TCG).")
        return "qemu"
    return None


# ---------------------------------------------------------------------------
# Domain inspection helpers
# ---------------------------------------------------------------------------


@dataclass
class DiskInfo:
    """A single <disk> device read back from a domain definition."""

    target: str
    bus: str
    device: str
    source: str
    source_type: str


def parse_disks(xml: str) -> list[DiskInfo]:
    """Parse every <disk> element of a domain XML."""
    root = ET.fromstring(xml)
    disks: list[DiskInfo] = []
    for disk in root.findall("./devices/disk"):
        target = disk.find("target")
        source = disk.find("source")
        path = ""
        source_type = "none"
        if source is not None:
            if source.get("file"):
                path, source_type = source.get("file"), "file"
            elif source.get("dev"):
                path, source_type = source.get("dev"), "block"
        disks.append(
            DiskInfo(
                target=target.get("dev") if target is not None else "",
                bus=(target.get("bus") if target is not None else None) or "",
                device=disk.get("device") or "",
                source=path,
                source_type=source_type,
            )
        )
    return disks


def parse_description(xml: str) -> str | None:
    """Return the <description> text of a domain, or None."""
    return ET.fromstring(xml).findtext("description")


def parse_graphics(xml: str) -> str | None:
    """Return the type of the first <graphics> device (e.g. 'spice'), or None."""
    graphics = ET.fromstring(xml).find("./devices/graphics")
    return graphics.get("type") if graphics is not None else None


def has_nvram(xml: str) -> bool:
    """Return True when the domain definition uses an NVRAM file (UEFI)."""
    os_element = ET.fromstring(xml).find("os")
    return os_element is not None and os_element.find("nvram") is not None


def parse_domain_list(output: str) -> list[tuple[str, str]]:
    """Parse the table printed by 'virsh list --all' into (name, state) pairs."""
    domains: list[tuple[str, str]] = []
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        ident, name, state = parts
        if not (ident.isdigit() or line.strip().startswith("-")):
            continue
        domains.append((name, state.strip()))
    return domains


def domain_names(uri: str) -> list[str]:
    """Return the names of all defined domains (running and shut off)."""
    res = _virsh(uri, "list", "--all", "--name")
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def domain_exists(uri: str, name: str) -> bool:
    """Check whether a domain with the given name is defined."""
    return name in domain_names(uri)


def dumpxml(uri: str, name: str) -> str:
    """Return the XML definition of a domain."""
    return _virsh(uri, "dumpxml", name).stdout


def domain_state(uri: str, name: str) -> str:
    """Return the state of a domain, e.g. 'running' or 'shut off'."""
    return _virsh(uri, "domstate", name).stdout.strip()


def is_running(uri: str, name: str) -> bool:
    """Check whether a domain is currently active."""
    return domain_state(uri, name) in RUNNING_STATES


# ---------------------------------------------------------------------------
# Domain markers
# ---------------------------------------------------------------------------


def build_marker(media: Path, managed: str) -> str:
    """Build the <description> marker embedded into the domain definition."""
    return f"{VM_MARKER_PREFIX} tool={TOOL_NAME} managed={managed} media={media}"


def parse_marker(description: str | None) -> dict[str, str]:
    """Parse key=value pairs out of a domain <description>, if it carries the marker."""
    if not description or VM_MARKER_PREFIX not in description:
        return {}
    parsed: dict[str, str] = {}
    for token in description.split(VM_MARKER_PREFIX, 1)[1].split():
        if "=" in token:
            key, value = token.split("=", 1)
            parsed[key] = value
    return parsed


def is_managed(marker: dict[str, str]) -> bool:
    """Check whether a parsed domain marker was written by this tool."""
    return marker.get("tool") == TOOL_NAME


def set_description(xml: str, text: str) -> str:
    """Replace (or insert) the <description> element of a domain XML."""
    root = ET.fromstring(xml)
    description = root.find("description")
    if description is None:
        description = ET.Element("description")
        index = next((i + 1 for i, child in enumerate(root) if child.tag == "name"), 0)
        root.insert(index, description)
    description.text = text
    return ET.tostring(root, encoding="unicode")


def add_media_disk(xml: str, media: Path) -> str:
    """Append a removable USB disk pointing at the installation media image."""
    root = ET.fromstring(xml)
    devices = root.find("devices")
    if devices is None:
        raise SystemExit("ERROR: the domain XML has no <devices> section.")
    used = {disk.target for disk in parse_disks(xml)}
    dev = next((f"sd{letter}" for letter in "abcdefgh" if f"sd{letter}" not in used), "sda")
    disk = ET.SubElement(devices, "disk", {"type": "file", "device": "disk"})
    ET.SubElement(disk, "driver", {"name": "qemu", "type": "raw"})
    ET.SubElement(disk, "source", {"file": str(media)})
    ET.SubElement(disk, "target", {"dev": dev, "bus": USB_BUS, "removable": "on"})
    return ET.tostring(root, encoding="unicode")


def set_media_path(xml: str, media: Path) -> str:
    """Point every USB disk in the domain definition at a new media image."""
    root = ET.fromstring(xml)
    for disk in root.findall("./devices/disk"):
        target = disk.find("target")
        if target is not None and target.get("bus") == USB_BUS:
            source = disk.find("source")
            if source is not None:
                source.set("file", str(media))
    return ET.tostring(root, encoding="unicode")


# ---------------------------------------------------------------------------
# Storage helpers (target disks only — never the installation media)
# ---------------------------------------------------------------------------


def build_undefine_command(uri: str, name: str, nvram: bool) -> list[str]:
    """Build the 'virsh undefine' command.

    The storage removal flag is intentionally never used: the installation
    medium is attached as a disk and would be destroyed along with it.
    """
    cmd = virsh_command(uri, "undefine", name)
    if nvram:
        cmd.append("--nvram")
    return cmd


def undefine(uri: str, name: str, nvram: bool) -> None:
    """Remove a domain definition, passing --nvram for UEFI definitions."""
    res = _run(build_undefine_command(uri, name, nvram), check=False, capture=True)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip()
        raise SystemExit(f"ERROR: 'virsh undefine {name}' failed:\n{detail}")


def pool_for_path(uri: str, path: Path) -> str | None:
    """Return the name of the libvirt storage pool that owns the given path."""
    for pool in _virsh(uri, "pool-list", "--all", "--name").stdout.split():
        target = ET.fromstring(_virsh(uri, "pool-dumpxml", pool).stdout).findtext("./target/path")
        if target and Path(target) == path.parent:
            return pool
    return None


def remove_storage(uri: str, path: Path) -> str:
    """Delete a disk image, preferring the libvirt storage API (works without root)."""
    pool = pool_for_path(uri, path)
    if pool:
        _virsh(uri, "vol-delete", path.name, "--pool", pool)
        return f"libvirt volume {pool}/{path.name}"
    path.unlink(missing_ok=True)
    return f"file {path}"


def recreate_storage(uri: str, path: Path) -> None:
    """Recreate an empty disk image with the same capacity and format."""
    pool = pool_for_path(uri, path)
    if pool:
        volume = ET.fromstring(_virsh(uri, "vol-dumpxml", path.name, "--pool", pool).stdout)
        capacity = volume.findtext("capacity")
        if not capacity:
            raise SystemExit(f"ERROR: cannot determine the capacity of volume {pool}/{path.name}.")
        format_element = volume.find("./target/format")
        volume_format = format_element.get("type") if format_element is not None else "qcow2"
        _virsh(uri, "vol-delete", path.name, "--pool", pool)
        _virsh(uri, "vol-create-as", pool, path.name, capacity, "--format", volume_format)
        return

    res = _run(["qemu-img", "info", "--output=json", str(path)], check=False, capture=True)
    if res.returncode != 0:
        raise SystemExit(f"ERROR: cannot inspect the disk image {path} (qemu-img info failed).")
    info = json.loads(res.stdout)
    path.unlink(missing_ok=True)
    _run(
        ["qemu-img", "create", "-f", info.get("format", "qcow2"), str(path), str(info["virtual-size"])],
        check=True,
    )


# ---------------------------------------------------------------------------
# virt-install command construction
# ---------------------------------------------------------------------------


@dataclass
class VmSpec:
    """Everything needed to define a new VM booting an installation image."""

    name: str
    media: Path
    uri: str = SYSTEM_URI
    disk: Path | None = None
    disk_size_gib: int = DEFAULT_DISK_SIZE_GIB
    memory_mb: int = DEFAULT_MEMORY_MB
    vcpus: int = DEFAULT_VCPUS
    os_variant: str = DEFAULT_OS_VARIANT
    firmware: str = "uefi"
    tpm: bool = True
    network: str = ""
    graphics: str = "spice"
    serial: bool = False
    wait: bool = False
    virt_type: str | None = None


def network_argument(network: str) -> str:
    """Turn a user-supplied network value into a virt-install --network argument."""
    if not network:
        return "network=default"
    if network in ("user", "none") or "=" in network:
        return network
    return f"network={network}"


def build_virt_install_command(spec: VmSpec) -> list[str]:
    """Build the virt-install command line for the given VM specification."""
    cmd = [
        "virt-install",
        "--connect",
        spec.uri,
        "--name",
        spec.name,
        "--memory",
        str(spec.memory_mb),
        "--vcpus",
        str(spec.vcpus),
        "--cpu",
        "host-passthrough",
        "--os-variant",
        spec.os_variant,
    ]
    if spec.disk is not None:
        cmd += ["--disk", f"path={spec.disk},bus=virtio"]
    else:
        cmd += ["--disk", f"size={spec.disk_size_gib},bus=virtio"]
    cmd += ["--disk", f"path={spec.media},bus={USB_BUS},removable=on"]
    cmd += ["--boot", spec.firmware]
    cmd += ["--network", network_argument(spec.network)]
    cmd += ["--graphics", spec.graphics]
    if spec.tpm:
        cmd += ["--tpm", "model=tpm-tis,version=2.0,backend.type=emulator"]
    if spec.virt_type:
        cmd += ["--virt-type", spec.virt_type]
    if spec.serial:
        cmd += ["--console", "pty,target_type=serial"]
    cmd += ["--description", build_marker(spec.media, "auto" if spec.disk is None else "manual")]
    if not spec.wait:
        cmd.append("--noautoconsole")
    return cmd


def _finalize_spec(spec: VmSpec, uri: str, no_kvm: bool) -> VmSpec:
    """Fill in the connection URI, the network and the virtualization type."""
    default_network = "default" if uri == SYSTEM_URI else "user"
    return dataclasses.replace(
        spec,
        uri=uri,
        network=spec.network or default_network,
        virt_type=spec.virt_type or detect_virt_type(no_kvm),
    )


def _validate_media(media: Path) -> None:
    """Validate that the installation media is an image file, not a block device."""
    info = fsprobe.probe(media)
    if not info.exists:
        raise SystemExit(f"ERROR: installation media image does not exist: {media}")
    if info.is_block:
        raise SystemExit(
            "ERROR: block devices are not supported as VM media.\n"
            "Provide an image file (for example pendrive.img created by 'flash')."
        )
    if not info.is_file:
        raise SystemExit(f"ERROR: installation media is not a regular file: {media}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def create(spec: VmSpec, *, reinstall: bool = False, no_kvm: bool = False, dry_run: bool = False) -> int:
    """Define and start a new VM booting the installation media image."""
    _validate_media(spec.media)
    uri = spec.uri or SYSTEM_URI
    if not dry_run:
        require_tools("virt-install", "virsh")
        uri = resolve_uri(spec.uri)
        if domain_exists(uri, spec.name):
            if reinstall:
                print(f"[autoinstall-helper] Domain '{spec.name}' already exists — reinstalling on the existing disk.")
                return reinstall_domain(spec.name, media=spec.media, uri=uri, wait=spec.wait)
            raise SystemExit(
                f"ERROR: domain '{spec.name}' already exists.\n"
                f"Reinstall it with 'vm reinstall {spec.name}', or remove it with 'vm delete {spec.name}'."
            )

    spec = _finalize_spec(spec, uri, no_kvm)
    cmd = build_virt_install_command(spec)
    if dry_run:
        print("[autoinstall-helper] " + shlex.join(cmd))
        return 0

    print(f"[autoinstall-helper] Creating VM '{spec.name}' ({uri})...")
    if _run(cmd, check=False).returncode != 0:
        raise SystemExit("ERROR: virt-install failed. See the output above for details.")
    if spec.wait:
        print(f"[autoinstall-helper] Installation console closed — VM '{spec.name}' is defined.")
    else:
        print(f"[autoinstall-helper] VM '{spec.name}' is installing in the background.")
        print(f"[autoinstall-helper] Follow the console with: vm console {spec.name}")
    return 0


def reinstall_domain(
    name: str,
    *,
    media: Path | None = None,
    uri: str | None = None,
    reset_disk: bool = False,
    wait: bool = False,
    dry_run: bool = False,
) -> int:
    """Reinstall the operating system on an existing VM disk.

    The installation media is re-attached (from the domain marker or the
    --media argument) and the existing definition is re-created, so the
    installer runs again and reformats the target disk.
    """
    require_tools("virsh")
    resolved = resolve_uri(uri)
    if not domain_exists(resolved, name):
        raise SystemExit(f"ERROR: domain '{name}' does not exist — create it first with 'vm create'.")

    xml = dumpxml(resolved, name)
    marker = parse_marker(parse_description(xml))
    media_path = media if media is not None else (Path(marker["media"]) if marker.get("media") else None)
    if media_path is not None:
        _validate_media(media_path)

    disks = parse_disks(xml)
    usb_disks = [disk for disk in disks if disk.bus == USB_BUS]
    if usb_disks:
        if media_path is not None:
            xml = set_media_path(xml, media_path)
    elif media_path is not None:
        xml = add_media_disk(xml, media_path)
        print(f"[autoinstall-helper] Re-attached installation media: {media_path}")
    else:
        print("[autoinstall-helper] WARNING: no installation media attached — the VM will boot the installed system.")
    if media_path is not None:
        xml = set_description(xml, build_marker(media_path, marker.get("managed", "auto")))

    targets = [Path(disk.source) for disk in disks if disk.bus != USB_BUS and disk.source_type == "file"]
    running = is_running(resolved, name)
    if running and not dry_run:
        _virsh(resolved, "destroy", name)

    if reset_disk:
        for path in targets:
            print(f"[autoinstall-helper] Resetting disk image: {path}")
            if not dry_run:
                recreate_storage(resolved, path)

    if dry_run:
        print(f"[autoinstall-helper] Reinstall plan for '{name}':")
        print(f"[autoinstall-helper]   target disks: {', '.join(str(p) for p in targets) or 'none'}")
        print(f"[autoinstall-helper]   media:        {media_path or 'none'}")
        print(f"[autoinstall-helper]   reset-disk:   {reset_disk}")
        return 0

    undefine(resolved, name, has_nvram(xml))

    with tempfile.NamedTemporaryFile("w", prefix="autoinstall-helper-", suffix=".xml", delete=False) as handle:
        handle.write(xml)
        xml_path = Path(handle.name)
    try:
        _virsh(resolved, "define", str(xml_path))
    finally:
        xml_path.unlink(missing_ok=True)

    if wait:
        print(f"[autoinstall-helper] Waiting for the installation of '{name}' (Ctrl+] to detach)...")
        return _virsh_interactive(resolved, "start", "--console", name)
    _virsh(resolved, "start", name)
    print(f"[autoinstall-helper] VM '{name}' is reinstalling in the background.")
    print(f"[autoinstall-helper] Follow the console with: vm console {name}")
    return 0


def eject(name: str, *, uri: str | None = None, live_only: bool = False, reboot: bool = False) -> int:
    """Detach the installation media (USB disk) from the VM."""
    require_tools("virsh")
    resolved = resolve_uri(uri)
    if not domain_exists(resolved, name):
        raise SystemExit(f"ERROR: domain '{name}' does not exist.")

    xml = dumpxml(resolved, name)
    media_disks = [disk for disk in parse_disks(xml) if disk.bus == USB_BUS]
    if not media_disks:
        raise SystemExit(f"ERROR: no installation media (USB disk) is attached to domain '{name}'.")

    running = is_running(resolved, name)
    if live_only:
        flags = ["--live"]
    else:
        flags = ["--live", "--config"] if running else ["--config"]

    for disk in media_disks:
        _virsh(resolved, "detach-disk", name, disk.target, *flags)
        print(f"[autoinstall-helper] Ejected media disk {disk.target} ({disk.source}).")

    if reboot:
        if not running:
            raise SystemExit(f"ERROR: --reboot requires a running domain ('{name}' is not running).")
        _virsh(resolved, "reboot", name)
        print(f"[autoinstall-helper] Domain '{name}' is rebooting without the installation media.")
    return 0


def delete(name: str, *, uri: str | None = None, purge_disk: bool = False, yes: bool = False) -> int:
    """Remove the VM definition, optionally deleting its target disk.

    The installation medium attached as a USB disk is never deleted.
    """
    require_tools("virsh")
    resolved = resolve_uri(uri)
    if not domain_exists(resolved, name):
        raise SystemExit(f"ERROR: domain '{name}' does not exist.")

    xml = dumpxml(resolved, name)
    disks = parse_disks(xml)
    marker = parse_marker(parse_description(xml))

    if is_running(resolved, name):
        _virsh(resolved, "destroy", name)
        print(f"[autoinstall-helper] Domain '{name}' stopped.")

    targets = [Path(disk.source) for disk in disks if disk.bus != USB_BUS and disk.source_type == "file"]
    if purge_disk and targets:
        if not yes:
            listing = "\n".join(f"  - {path}" for path in targets)
            if not sys.stdin.isatty():
                raise SystemExit("ERROR: --purge-disk in non-interactive mode requires -y / --yes.")
            answer = input(f"[autoinstall-helper] Delete the target disk image(s)?\n{listing}\nContinue? [y/N]: ")
            if answer.strip().lower() not in ("y", "yes"):
                print("[autoinstall-helper] Cancelled by user.")
                return 1
        for path in targets:
            print(f"[autoinstall-helper] Removed {remove_storage(resolved, path)}.")

    undefine(resolved, name, has_nvram(xml))
    media = marker.get("media") or next((disk.source for disk in disks if disk.bus == USB_BUS), None)
    note = f"installation media '{media}' was left untouched" if media else "no installation media was attached"
    print(f"[autoinstall-helper] Domain '{name}' removed ({note}).")
    return 0


def start(name: str, *, uri: str | None = None) -> int:
    """Start a defined domain."""
    require_tools("virsh")
    resolved = resolve_uri(uri)
    _virsh(resolved, "start", name)
    print(f"[autoinstall-helper] Domain '{name}' started.")
    return 0


def stop(name: str, *, uri: str | None = None, force: bool = False) -> int:
    """Request a graceful shutdown, or destroy the domain with force=True."""
    require_tools("virsh")
    resolved = resolve_uri(uri)
    _virsh(resolved, "destroy" if force else "shutdown", name)
    print(f"[autoinstall-helper] Domain '{name}' {'destroyed' if force else 'shutdown requested'}.")
    return 0


def restart(name: str, *, uri: str | None = None) -> int:
    """Reboot a running domain, or start a stopped one."""
    require_tools("virsh")
    resolved = resolve_uri(uri)
    if is_running(resolved, name):
        _virsh(resolved, "reboot", name)
        print(f"[autoinstall-helper] Domain '{name}' rebooting.")
    else:
        _virsh(resolved, "start", name)
        print(f"[autoinstall-helper] Domain '{name}' started.")
    return 0


def console(name: str, *, uri: str | None = None, serial: bool = False) -> int:
    """Open the graphical (virt-viewer) or serial (virsh console) console."""
    require_tools("virsh")
    resolved = resolve_uri(uri)
    if not domain_exists(resolved, name):
        raise SystemExit(f"ERROR: domain '{name}' does not exist.")

    graphics_type = parse_graphics(dumpxml(resolved, name))
    if serial or graphics_type in (None, "none"):
        print(f"[autoinstall-helper] Attaching to the serial console of '{name}' (Ctrl+] to detach)...")
        return _virsh_interactive(resolved, "console", name)
    if shutil.which("virt-viewer") is None:
        raise SystemExit(
            "ERROR: virt-viewer is required for a graphical console.\n"
            "Install it with: sudo apt install virt-viewer\n"
            f"Or use 'vm console {name} --serial' for the serial console."
        )
    print(f"[autoinstall-helper] Opening the {graphics_type} console of '{name}'...")
    return _run(["virt-viewer", "--connect", resolved, name], check=False).returncode


def status(name: str, *, uri: str | None = None) -> int:
    """Print the state, disks and media of a single domain."""
    require_tools("virsh")
    resolved = resolve_uri(uri)
    if not domain_exists(resolved, name):
        raise SystemExit(f"ERROR: domain '{name}' does not exist.")

    xml = dumpxml(resolved, name)
    marker = parse_marker(parse_description(xml))
    print(f"[autoinstall-helper] Domain:   {name}")
    print(f"[autoinstall-helper] URI:      {resolved}")
    print(f"[autoinstall-helper] State:    {domain_state(resolved, name)}")
    print(f"[autoinstall-helper] Graphics: {parse_graphics(xml) or 'none'}")
    print(f"[autoinstall-helper] Media:    {marker.get('media', 'none')}")
    for disk in parse_disks(xml):
        kind = "media" if disk.bus == USB_BUS else "disk"
        print(f"[autoinstall-helper]   {kind:<5} {disk.target:<4} ({disk.bus:<6}) {disk.source}")
    return 0


def _domain_marker(uri: str, name: str) -> dict[str, str]:
    """Read and parse the autoinstall-helper marker of a domain (empty when absent)."""
    try:
        return parse_marker(parse_description(dumpxml(uri, name)))
    except SystemExit:
        return {}


def list_domains(*, uri: str | None = None, show_all: bool = False) -> int:
    """List VMs created by autoinstall-helper (or every domain when show_all=True)."""
    require_tools("virsh")
    resolved = resolve_uri(uri)
    domains = parse_domain_list(_virsh(resolved, "list", "--all").stdout)
    entries = [(name, state, _domain_marker(resolved, name)) for name, state in domains]

    if show_all:
        selected = entries
        title = f"Domains on {resolved}:"
    else:
        selected = [entry for entry in entries if is_managed(entry[2])]
        title = f"VMs managed by {TOOL_NAME} on {resolved}:"

    if not selected:
        if show_all:
            print(f"[autoinstall-helper] No domains found on {resolved}.")
        else:
            print(f"[autoinstall-helper] No VMs managed by {TOOL_NAME} on {resolved} (use --all to list every domain).")
        return 0

    print(f"[autoinstall-helper] {title}")
    for name, state, marker in selected:
        row = f"  {name:<24} {state:<10}".rstrip()
        if marker.get("media"):
            row += f"  media={marker['media']}"
        if show_all and is_managed(marker):
            row += f"  [{TOOL_NAME}]"
        print(row)
    return 0


def doctor(uri: str | None = None) -> int:
    """Report whether the host is ready to run VM tests."""
    print("[autoinstall-helper] VM preflight check")
    ready = True

    for command in ("virt-install", "virsh", "virt-viewer", "qemu-img", "swtpm"):
        path = shutil.which(command)
        required = command in ("virt-install", "virsh")
        if path:
            state = path
        elif required:
            state = "MISSING (required)"
            ready = False
        else:
            state = "missing (optional)"
        print(f"[autoinstall-helper]   {command:<13} {state}")

    print(
        f"[autoinstall-helper]   {'/dev/kvm':<13} "
        f"{'available' if Path('/dev/kvm').exists() else 'missing — software emulation (TCG) will be used'}"
    )
    firmware = find_uefi_firmware()
    print(f"[autoinstall-helper]   {'UEFI firmware':<13} {firmware or 'missing — install ovmf for --firmware uefi'}")

    candidates = list(dict.fromkeys([uri] if uri else [SYSTEM_URI, SESSION_URI]))
    reachable = {candidate: uri_works(candidate) for candidate in candidates}
    if shutil.which("virsh"):
        for candidate in candidates:
            print(f"[autoinstall-helper]   {candidate:<13} {'reachable' if reachable[candidate] else 'unreachable'}")
        if not any(reachable.values()):
            print(uri_help(uri))
            ready = False
    else:
        print(uri_help(None))
        ready = False

    print("[autoinstall-helper] VM support is ready." if ready else "[autoinstall-helper] VM support is NOT ready.")
    return 0 if ready else 1
