"""Tool for building a single zipapp executable file (.pyz)."""

import argparse
import hashlib
import stat
import sys
import zipapp
from pathlib import Path


def exclude_pycache(path: Path) -> bool:
    """Exclude __pycache__ directories and compiled .pyc files from the archive."""
    return "__pycache__" not in path.parts and not path.name.endswith(".pyc")


def compute_docker_digest(docker_dir: Path) -> str:
    """Compute a 12-character SHA-256 digest from the key files in the docker/ directory."""
    digest = hashlib.sha256()
    for name in ("Dockerfile", "timedatectl.sh", "validate-autoinstall.py"):
        asset = docker_dir / name
        if not asset.exists():
            raise FileNotFoundError(f"Missing required file in the docker directory: {asset}")
        digest.update(name.encode("utf-8"))
        digest.update(asset.read_bytes())
    return digest.hexdigest()[:12]


def build_zipapp(
    target: Path | str = "autoinstall-helper.pyz",
    src_dir: Path | str | None = None,
) -> Path:
    """Create a zipapp (.pyz) archive from the source code."""
    if src_dir is None:
        # By default the src/ directory that is the parent of the autoinstall_helper package
        src_path = Path(__file__).resolve().parents[1]
    else:
        src_path = Path(src_dir).resolve()

    target_path = Path(target).resolve()

    # Make sure the package directories contain __init__.py
    pkg_docker = src_path / "autoinstall_helper" / "docker"
    digest_file: Path | None = None
    if pkg_docker.is_dir():
        (pkg_docker / "__init__.py").touch()
        # Compute the asset hash and place the DOCKER_DIGEST file in the package, but OUTSIDE the docker/ directory
        digest = compute_docker_digest(pkg_docker)
        digest_file = src_path / "autoinstall_helper" / "DOCKER_DIGEST"
        digest_file.write_text(digest + "\n", encoding="utf-8")

    target_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        print(f"[build] Packaging {src_path} -> {target_path}...")
        zipapp.create_archive(
            source=src_path,
            target=target_path,
            interpreter="/usr/bin/env python3",
            main="autoinstall_helper.cli:main",
            filter=exclude_pycache,
        )
    finally:
        # Remove the temporary file from the source directory after packaging
        if digest_file is not None and digest_file.exists():
            digest_file.unlink()

    # Grant executable permissions (+x)
    current_mode = target_path.stat().st_mode
    target_path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    size_kb = target_path.stat().st_size / 1024
    print(f"[build] Success: created {target_path.name} ({size_kb:.1f} KB)")
    return target_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build-pyz",
        description="Build a standalone autoinstall-helper.pyz executable (zipapp).",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("autoinstall-helper.pyz"),
        help="Path to the output .pyz file (default: autoinstall-helper.pyz)",
    )
    parser.add_argument(
        "--src",
        type=Path,
        default=None,
        help="Source directory (default: the src directory)",
    )

    args = parser.parse_args(argv)
    try:
        build_zipapp(target=args.output, src_dir=args.src)
        return 0
    except Exception as exc:
        print(f"BUILD ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
