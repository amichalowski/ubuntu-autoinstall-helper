#!/usr/bin/env python3
"""Tool for managing the autoinstall.yaml file.

Available commands:
  - validate: reads the channel from autoinstall.yaml, resolves the version
    via `snap info`, builds the Docker image (if missing) and runs the
    Subiquity validator inside the container.
  - flash: writes an ISO image to the target (block device or file),
    creating the target file with fallocate if it does not exist yet.
  - inject: embeds autoinstall.yaml in the 'cidata' (FAT32) partition on the
    target (block device or image file). If the partition does not exist,
    it is created at the end of the available space.
  - extract: reads user-data from the 'cidata' partition on the target and saves it to a file.

The logic of each command lives in dedicated modules:
  - config.py       — autoinstall.yaml parsing
  - snapstore.py    — snap version and base image resolution
  - docker_utils.py — validator Docker image and docker/ directory assets
  - flash.py        — writing the ISO to the medium
  - cidata.py       — cidata partition (inject / extract)
  - disk.py         — shared block device and GPT helpers
"""

import argparse
import os
import sys
from pathlib import Path

from autoinstall_helper.cidata import extract, inject
from autoinstall_helper.config import extract_installer_channel, require_cloud_config_header
from autoinstall_helper.disk import is_block_device
from autoinstall_helper.docker_utils import (
    docker_build,
    docker_image_exists,
    docker_image_tag,
    docker_validate,
    get_docker_dir,
)
from autoinstall_helper.flash import flash
from autoinstall_helper.snapstore import get_base_image, get_snap_version


def _require_root(action: str) -> None:
    """Check whether the process has root privileges; exit with a clear message otherwise."""
    if os.geteuid() != 0:
        cmd_args = " ".join(sys.argv)
        raise SystemExit(
            f"ERROR: Operation '{action}' requires administrator (root) privileges.\n"
            f"Re-run with sudo:\n"
            f"  sudo {sys.executable} {cmd_args}"
        )


def validate(config_file: Path, validation: str = "full") -> int:
    """Validate autoinstall.yaml against Subiquity and Curtin in a Docker container."""
    if not config_file.exists():
        raise SystemExit(f"ERROR: File does not exist: {config_file}")

    print(f"[autoinstall-helper] Reading: {config_file}")
    require_cloud_config_header(config_file)
    channel = extract_installer_channel(config_file)
    print(f"[autoinstall-helper] Installer channel: {channel}")

    version = get_snap_version(channel)
    print(f"[autoinstall-helper] Installer version: {version}")

    tag = docker_image_tag(version)
    if docker_image_exists(tag):
        print(f"[autoinstall-helper] Image '{tag}' already exists.")
    else:
        dockerfile = get_docker_dir() / "Dockerfile"
        if not dockerfile.exists():
            raise SystemExit(f"ERROR: Dockerfile not found in {dockerfile.parent}")
        base_image = get_base_image(version)
        print(f"[autoinstall-helper] Base image: {base_image}")
        docker_build(tag, version, base_image, dockerfile)

    rc = docker_validate(tag, config_file, validation)
    if rc == 0:
        print("[autoinstall-helper] Validation completed successfully!")
    else:
        print(f"[autoinstall-helper] Validation failed (exit code: {rc}).", file=sys.stderr)
    return rc


def build_parser() -> argparse.ArgumentParser:
    """Construct the command-line argument parser for autoinstall-helper."""
    parser = argparse.ArgumentParser(
        prog="autoinstall-helper",
        description="Tool for managing autoinstall configuration and Ubuntu installation media.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", metavar="COMMAND")

    # validate
    p_val = subparsers.add_parser(
        "validate",
        help="Validate the autoinstall.yaml file in a Docker container",
    )
    p_val.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path("autoinstall.yaml"),
        help="Path to the YAML file (default: autoinstall.yaml)",
    )
    p_val.add_argument(
        "--validation",
        choices=("autoinstall", "storage", "full"),
        default="full",
        help=(
            "Validation scope: autoinstall (Subiquity), storage (storage actions + "
            "Curtin schema), full (both checks; default)"
        ),
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

    parser._cidata_parser = p_cidata  # type: ignore[attr-defined]
    return parser


def dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Dispatch the parsed command-line arguments to the appropriate handler."""
    if args.subcommand == "validate":
        return validate(args.config, validation=args.validation)

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

    parser.print_help()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Main CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return dispatch(args, parser)


def zipapp_main() -> None:
    """Entry point for the standalone zipapp (.pyz).

    zipapp's generated __main__.py calls the entry point without sys.exit(),
    so the status code returned by main() must be raised explicitly here.
    """
    sys.exit(main())


if __name__ == "__main__":
    sys.exit(main())
