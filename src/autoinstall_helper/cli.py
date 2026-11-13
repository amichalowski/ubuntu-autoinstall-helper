#!/usr/bin/env python3
"""Tool for managing the autoinstall.yaml file.

Available commands:
  - validate: reads the channel from autoinstall.yaml (falling back to the
    latest Ubuntu LTS channel when `refresh-installer.channel` is absent),
    resolves the version via the Snap Store, builds the Docker image (if
    missing) and runs the
    Subiquity/Curtin validator plus the shell-syntax check of the
    early/late/error commands inside the container. The `snaps:` section is
    checked against the Snap Store on the host (no Docker needed for
    `--validation snaps`). `validate doctor` checks the
    Docker host instead (binary present, daemon reachable, no sudo needed) and
    `validate images` lists or prunes the locally built validator images.
  - flash: writes an ISO image to the target (block device or file),
    creating the target file with fallocate if it does not exist yet.
  - inject: embeds autoinstall.yaml in the 'cidata' (FAT32) partition on the
    target (block device or image file). If the partition does not exist,
    it is created at the end of the available space.
  - extract: reads user-data from the 'cidata' partition on the target and saves it to a file.
  - vm: manages libvirt/QEMU virtual machines that boot an installation image
    (e.g. pendrive.img) as a removable USB disk, to test the autoinstall flow
    without physical hardware (create / eject / reinstall / delete / console / ...).

The logic of each command lives in dedicated modules:
  - config.py       — autoinstall.yaml parsing
  - snapstore.py    — snap version and base image resolution
  - docker_utils.py — validator Docker image and docker/ directory assets
  - flash.py        — writing the ISO to the medium
  - cidata.py       — cidata partition (inject / extract)
  - disk.py         — shared block device and GPT helpers
  - vm.py           — libvirt virtual machines for testing installation media
"""

import argparse
import os
import sys
import zipimport
from pathlib import Path

from autoinstall_helper import fsprobe, vm
from autoinstall_helper.cidata import extract, inject
from autoinstall_helper.config import extract_installer_channel, extract_snaps, require_cloud_config_header
from autoinstall_helper.disk import is_block_device
from autoinstall_helper.docker_utils import (
    SHELLCHECK_SEVERITIES,
    docker_build,
    docker_doctor,
    docker_image_exists,
    docker_image_tag,
    docker_images,
    docker_validate,
    get_docker_dir,
)
from autoinstall_helper.flash import flash
from autoinstall_helper.snapstore import (
    SnapStoreError,
    get_base_image,
    get_snap_version,
    latest_lts_channel,
    supports_embedded_validator,
    validate_snaps,
)

# The positional CONFIG slot of `validate` doubles as an action name, so these
# words are reserved (a configuration file literally named like one of them is
# passed as './doctor' or './images').
VALIDATE_ACTIONS = ("doctor", "images")


def _require_root(action: str) -> None:
    """Check whether the process has root privileges; exit with a clear message otherwise."""
    if os.geteuid() != 0:
        cmd_args = " ".join(sys.argv)
        raise SystemExit(
            f"ERROR: Operation '{action}' requires administrator (root) privileges.\n"
            f"Re-run with sudo:\n"
            f"  sudo {sys.executable} {cmd_args}"
        )


def validate(
    config_file: Path,
    validation: str = "full",
    *,
    shellcheck: bool = True,
    shellcheck_severity: str = "error",
) -> int:
    """Validate autoinstall.yaml against Subiquity, Curtin, the Snap Store and shell syntax."""
    if not fsprobe.probe(config_file).exists:
        raise SystemExit(f"ERROR: File does not exist: {config_file}")

    print(f"[autoinstall-helper] Reading: {config_file}")
    require_cloud_config_header(config_file)

    # The snap layer needs only the YAML and the Snap Store, so it runs on the
    # host and `--validation snaps` never builds or starts the Docker image.
    snaps_clean = True
    if validation in ("snaps", "full"):
        snaps_clean = validate_snaps(extract_snaps(config_file))
    if validation == "snaps":
        return 0 if snaps_clean else 1

    channel = extract_installer_channel(config_file)
    if channel is None:
        try:
            channel = latest_lts_channel()
        except SnapStoreError as exc:
            raise SystemExit(
                f"ERROR: No 'autoinstall.refresh-installer.channel' in {config_file} and the latest "
                f"Ubuntu LTS channel could not be resolved from the Snap Store ({exc}).\n"
                f"Set 'autoinstall.refresh-installer.channel' explicitly (e.g. '26.04/stable')."
            ) from exc
        print(
            f"[autoinstall-helper] No 'refresh-installer.channel' found; assuming the latest LTS installer: {channel}"
        )
    else:
        print(f"[autoinstall-helper] Installer channel: {channel}")

    version = get_snap_version(channel)
    print(f"[autoinstall-helper] Installer version: {version}")
    if not supports_embedded_validator(version):
        raise SystemExit(
            f"ERROR: Installer version '{version}' does not ship Subiquity's built-in validator.\n"
            f"Only installers from 26.04 onwards expose the validation API used by this tool; "
            f"older releases cannot be validated in the container.\n"
            f"Point 'autoinstall.refresh-installer.channel' at '26.04/stable' or newer.\n"
            f"Hint: 'validate <config> --validation snaps' works without the container."
        )

    tag = docker_image_tag(version)
    if docker_image_exists(tag):
        print(f"[autoinstall-helper] Image '{tag}' already exists.")
    else:
        dockerfile = get_docker_dir() / "Dockerfile"
        if not fsprobe.probe(dockerfile).exists:
            raise SystemExit(f"ERROR: Dockerfile not found in {dockerfile.parent}")
        base_image = get_base_image(version)
        print(f"[autoinstall-helper] Base image: {base_image}")
        docker_build(tag, version, base_image, dockerfile)

    rc = docker_validate(
        tag,
        config_file,
        validation,
        shellcheck=shellcheck,
        shellcheck_severity=shellcheck_severity,
    )
    if rc == 0 and not snaps_clean:
        print("[autoinstall-helper] Validation failed: the snaps layer reported errors.", file=sys.stderr)
        return 1
    if rc == 0:
        print("[autoinstall-helper] Validation completed successfully!")
    else:
        print(f"[autoinstall-helper] Validation failed (exit code: {rc}).", file=sys.stderr)
    return rc


def is_packaged_zipapp() -> bool:
    """Return True when running from a packaged .pyz archive.

    Dev-only features (such as ``--record``) are hidden from the release artifact
    so a packaged CLI cannot capture session recordings.
    """
    return isinstance(globals().get("__loader__"), zipimport.zipimporter)


def build_parser() -> argparse.ArgumentParser:
    """Construct the command-line argument parser for autoinstall-helper."""
    parser = argparse.ArgumentParser(
        prog="autoinstall-helper",
        description="Tool for managing autoinstall configuration and Ubuntu installation media.",
    )
    if not is_packaged_zipapp():
        parser.add_argument(
            "--record",
            metavar="FILE",
            default=None,
            help="Dev mode: record all subprocess/network interactions to FILE (pass before COMMAND)",
        )
    subparsers = parser.add_subparsers(dest="subcommand", metavar="COMMAND")

    # validate
    p_val = subparsers.add_parser(
        "validate",
        help="Validate the autoinstall.yaml file in a Docker container",
        description=(
            "Validate the autoinstall.yaml file in a Docker container. "
            "The first argument also accepts the actions 'doctor' (Docker preflight check) "
            "and 'images' (list and prune the validator images)."
        ),
    )
    p_val.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path("autoinstall.yaml"),
        metavar="CONFIG|ACTION",
        help="Path to the YAML file (default: autoinstall.yaml), or the action 'doctor' / 'images'",
    )
    p_val.add_argument(
        "--validation",
        choices=("autoinstall", "storage", "commands", "snaps", "full"),
        default="full",
        help=(
            "Validation scope: autoinstall (Subiquity), storage (storage actions + "
            "Curtin schema), commands (shell syntax of early/late/error commands), "
            "snaps (host-side Snap Store existence, channel and classic confinement), "
            "full (all checks; default)"
        ),
    )
    p_val.add_argument(
        "--no-shellcheck",
        dest="shellcheck",
        action="store_false",
        default=True,
        help="Skip the shellcheck pass (the sh -n / bash -n syntax check always runs)",
    )
    p_val.add_argument(
        "--shellcheck-severity",
        choices=SHELLCHECK_SEVERITIES,
        default="error",
        help="Minimum shellcheck severity that fails validation (default: error)",
    )
    p_val.add_argument(
        "--prune",
        action="store_true",
        help="'images' action: remove validator images built from outdated docker/ assets",
    )
    p_val.add_argument(
        "--all",
        dest="remove_all",
        action="store_true",
        help="'images' action: with --prune, remove every validator image",
    )
    p_val.add_argument(
        "--dry-run",
        action="store_true",
        help="'images' action: print what --prune would remove without removing anything",
    )
    p_val.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="'images' action: skip the confirmation prompt before pruning",
    )

    # flash
    p_flash = subparsers.add_parser(
        "flash",
        help="Write an ISO image to the target (block device or image file)",
    )
    p_flash.add_argument("iso", type=Path, help="Path to the ISO file")
    p_flash.add_argument(
        "target",
        type=Path,
        help="Block device (e.g. /dev/sdb) or image file",
    )
    p_flash.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation before writing to a block device",
    )

    # inject (alias: import)
    p_inject = subparsers.add_parser(
        "inject",
        aliases=["import"],
        help="Embed the YAML in the cidata (FAT32) partition on the target",
    )
    p_inject.add_argument("target", type=Path, help="Target (block device or image file)")
    p_inject.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path("autoinstall.yaml"),
        help="Path to the YAML file (default: autoinstall.yaml)",
    )

    # extract (alias: export)
    p_extract = subparsers.add_parser(
        "extract",
        aliases=["export"],
        help="Read user-data from the cidata (FAT32) partition on the target",
    )
    p_extract.add_argument("target", type=Path, help="Target (block device or image file)")
    p_extract.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=Path("autoinstall.yaml"),
        help="Path to the output file (default: autoinstall.yaml)",
    )

    # cidata group (cidata inject / cidata extract)
    p_cidata = subparsers.add_parser(
        "cidata",
        help="Manage the cidata partition (subcommands: inject, extract)",
    )
    cidata_sub = p_cidata.add_subparsers(dest="cidata_subcommand", metavar="ACTION")

    c_inject = cidata_sub.add_parser("inject", help="Embed the YAML in the cidata partition")
    c_inject.add_argument("target", type=Path, help="Target (block device or image file)")
    c_inject.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path("autoinstall.yaml"),
        help="Path to the YAML file (default: autoinstall.yaml)",
    )

    c_extract = cidata_sub.add_parser("extract", help="Read user-data from the cidata partition")
    c_extract.add_argument("target", type=Path, help="Target (block device or image file)")
    c_extract.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=Path("autoinstall.yaml"),
        help="Path to the output file (default: autoinstall.yaml)",
    )

    # vm group (libvirt virtual machines for testing installation media)
    p_vm = subparsers.add_parser(
        "vm",
        help="Manage libvirt VMs that boot installation media (subcommands: create, eject, ...)",
    )
    vm_sub = p_vm.add_subparsers(dest="vm_subcommand", metavar="ACTION")

    v_create = vm_sub.add_parser("create", help="Define and start a VM booting the installation media")
    v_create.add_argument("name", help="VM (libvirt domain) name")
    v_create.add_argument("--media", type=Path, required=True, help="Installation media image (e.g. pendrive.img)")
    v_create.add_argument("--disk", type=Path, default=None, help="Existing target disk image to reuse")
    v_create.add_argument(
        "--disk-size",
        type=int,
        default=vm.DEFAULT_DISK_SIZE_GIB,
        metavar="GiB",
        help=f"Target disk size in GiB (default: {vm.DEFAULT_DISK_SIZE_GIB})",
    )
    v_create.add_argument(
        "--memory",
        type=int,
        default=vm.DEFAULT_MEMORY_MB,
        metavar="MB",
        help=f"RAM in MB (default: {vm.DEFAULT_MEMORY_MB})",
    )
    v_create.add_argument("--vcpus", type=int, default=vm.DEFAULT_VCPUS, help="Number of virtual CPUs")
    v_create.add_argument("--os-variant", default=vm.DEFAULT_OS_VARIANT, help="libvirt OS variant")
    v_create.add_argument("--firmware", choices=("uefi", "bios"), default="uefi", help="Firmware type")
    v_create.add_argument("--no-tpm", action="store_true", help="Do not add an emulated TPM 2.0 device")
    v_create.add_argument(
        "--network",
        default="",
        help="Network: 'default', 'user', or any libvirt 'key=value' specification",
    )
    v_create.add_argument("--graphics", choices=("spice", "vnc", "none"), default="spice", help="Console type")
    v_create.add_argument("--serial", action="store_true", help="Add a serial console (needed for --graphics none)")
    v_create.add_argument("--no-kvm", action="store_true", help="Force software emulation (TCG)")
    v_create.add_argument(
        "--wait",
        action="store_true",
        help="Attach to the console and wait instead of detaching the process (default: detach)",
    )
    v_create.add_argument(
        "--reinstall",
        action="store_true",
        help="Reinstall on the existing disk when the domain already exists",
    )
    v_create.add_argument("--uri", default=None, help="libvirt connection URI (default: auto-detect)")
    v_create.add_argument("--dry-run", action="store_true", help="Print the virt-install command without running it")

    v_eject = vm_sub.add_parser("eject", help="Eject the installation media from the VM")
    v_eject.add_argument("name", help="VM (libvirt domain) name")
    v_eject.add_argument("--live-only", action="store_true", help="Detach at runtime only (re-attached on restart)")
    v_eject.add_argument("--reboot", action="store_true", help="Reboot the VM so it boots the installed system")
    v_eject.add_argument("--uri", default=None, help="libvirt connection URI (default: auto-detect)")

    v_reinstall = vm_sub.add_parser("reinstall", help="Reinstall the OS on an existing VM disk")
    v_reinstall.add_argument("name", help="VM (libvirt domain) name")
    v_reinstall.add_argument("--media", type=Path, default=None, help="Media image to attach (default: the marker)")
    v_reinstall.add_argument("--reset-disk", action="store_true", help="Recreate the target disk image before booting")
    v_reinstall.add_argument("--wait", action="store_true", help="Attach to the console and wait")
    v_reinstall.add_argument("--uri", default=None, help="libvirt connection URI (default: auto-detect)")
    v_reinstall.add_argument("--dry-run", action="store_true", help="Print the plan without changing anything")

    v_delete = vm_sub.add_parser("delete", help="Remove the VM definition (keeps the installation media)")
    v_delete.add_argument("name", help="VM (libvirt domain) name")
    v_delete.add_argument("--purge-disk", action="store_true", help="Also delete the VM target disk image")
    v_delete.add_argument("-y", "--yes", action="store_true", help="Skip confirmation before deleting the disk")
    v_delete.add_argument("--uri", default=None, help="libvirt connection URI (default: auto-detect)")

    v_start = vm_sub.add_parser("start", help="Start a defined VM")
    v_start.add_argument("name", help="VM (libvirt domain) name")
    v_start.add_argument("--uri", default=None, help="libvirt connection URI (default: auto-detect)")

    v_stop = vm_sub.add_parser("stop", help="Shut down a VM")
    v_stop.add_argument("name", help="VM (libvirt domain) name")
    v_stop.add_argument("--force", action="store_true", help="Destroy the VM instead of a graceful shutdown")
    v_stop.add_argument("--uri", default=None, help="libvirt connection URI (default: auto-detect)")

    v_restart = vm_sub.add_parser("restart", help="Reboot a running VM (or start a stopped one)")
    v_restart.add_argument("name", help="VM (libvirt domain) name")
    v_restart.add_argument("--uri", default=None, help="libvirt connection URI (default: auto-detect)")

    v_status = vm_sub.add_parser("status", help="Show the state, disks and media of a VM")
    v_status.add_argument("name", help="VM (libvirt domain) name")
    v_status.add_argument("--uri", default=None, help="libvirt connection URI (default: auto-detect)")

    v_console = vm_sub.add_parser("console", help="Open the graphical or serial console")
    v_console.add_argument("name", help="VM (libvirt domain) name")
    v_console.add_argument("--serial", action="store_true", help="Use the serial console (virsh console)")
    v_console.add_argument("--uri", default=None, help="libvirt connection URI (default: auto-detect)")

    v_list = vm_sub.add_parser("list", help="List the VMs managed by autoinstall-helper")
    v_list.add_argument("--all", action="store_true", help="Also list domains created outside autoinstall-helper")
    v_list.add_argument("--uri", default=None, help="libvirt connection URI (default: auto-detect)")

    v_doctor = vm_sub.add_parser("doctor", help="Check whether the host is ready to run VM tests")
    v_doctor.add_argument("--uri", default=None, help="libvirt connection URI to check")

    parser._cidata_parser = p_cidata  # type: ignore[attr-defined]
    parser._vm_parser = p_vm  # type: ignore[attr-defined]
    return parser


def _dispatch_vm(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Dispatch the `vm` command group (libvirt virtual machines)."""
    if args.vm_subcommand == "create":
        spec = vm.VmSpec(
            name=args.name,
            media=args.media,
            uri=args.uri or "",
            disk=args.disk,
            disk_size_gib=args.disk_size,
            memory_mb=args.memory,
            vcpus=args.vcpus,
            os_variant=args.os_variant,
            firmware=args.firmware,
            tpm=not args.no_tpm,
            network=args.network,
            graphics=args.graphics,
            serial=args.serial,
            wait=args.wait,
        )
        return vm.create(spec, reinstall=args.reinstall, no_kvm=args.no_kvm, dry_run=args.dry_run)

    if args.vm_subcommand == "eject":
        return vm.eject(args.name, uri=args.uri, live_only=args.live_only, reboot=args.reboot)

    if args.vm_subcommand == "reinstall":
        return vm.reinstall_domain(
            args.name,
            media=args.media,
            uri=args.uri,
            reset_disk=args.reset_disk,
            wait=args.wait,
            dry_run=args.dry_run,
        )

    if args.vm_subcommand == "delete":
        return vm.delete(args.name, uri=args.uri, purge_disk=args.purge_disk, yes=args.yes)

    if args.vm_subcommand == "start":
        return vm.start(args.name, uri=args.uri)

    if args.vm_subcommand == "stop":
        return vm.stop(args.name, uri=args.uri, force=args.force)

    if args.vm_subcommand == "restart":
        return vm.restart(args.name, uri=args.uri)

    if args.vm_subcommand == "status":
        return vm.status(args.name, uri=args.uri)

    if args.vm_subcommand == "console":
        return vm.console(args.name, uri=args.uri, serial=args.serial)

    if args.vm_subcommand == "list":
        return vm.list_domains(uri=args.uri, show_all=args.all)

    if args.vm_subcommand == "doctor":
        return vm.doctor(args.uri)

    p_vm = getattr(parser, "_vm_parser", parser)
    p_vm.print_help()
    return 0


def dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Dispatch the parsed command-line arguments to the appropriate handler."""
    if args.subcommand == "validate":
        action = str(args.config)
        image_flags = args.prune or args.remove_all or args.dry_run or args.yes
        # Image management options must not be silently ignored by a plain validation run.
        if image_flags and action != "images":
            parser.error("--prune/--all/--dry-run/--yes are only valid with 'validate images'")
        if action == "images":
            return docker_images(
                prune=args.prune,
                remove_all=args.remove_all,
                dry_run=args.dry_run,
                yes=args.yes,
            )
        if action == "doctor":
            return docker_doctor()
        return validate(
            args.config,
            validation=args.validation,
            shellcheck=args.shellcheck,
            shellcheck_severity=args.shellcheck_severity,
        )

    if args.subcommand == "flash":
        # Flashing to a physical block device requires root to prevent accidental overwrites,
        # but creating/writing to a regular image file does not require root by design.
        if is_block_device(args.target):
            _require_root("flash to a block device")
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
        p_cidata = getattr(parser, "_cidata_parser", parser)
        p_cidata.print_help()
        return 0

    if args.subcommand == "vm":
        return _dispatch_vm(args, parser)

    parser.print_help()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Main CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    record_path = getattr(args, "record", None)
    if record_path:
        from autoinstall_helper.recorder import recording_session

        with recording_session(record_path) as rec:
            rc = dispatch(args, parser)
            rec.exit_code = rc
        return rc
    return dispatch(args, parser)


def zipapp_main() -> None:
    """Entry point for the standalone zipapp (.pyz).

    zipapp's generated __main__.py calls the entry point without sys.exit(),
    so the status code returned by main() must be raised explicitly here.
    """
    sys.exit(main())


if __name__ == "__main__":
    sys.exit(main())
