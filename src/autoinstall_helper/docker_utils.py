"""Management of the validator Docker image and docker/ directory assets."""

import atexit
import functools
import hashlib
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

DOCKER_IMAGE_REPO = "ubuntu-desktop-bootstrap"

# Validator assets hashed into the image tag; order is significant and must stay stable.
DOCKER_ASSET_FILES = ("Dockerfile", "timedatectl.sh", "validate-autoinstall.py")


def compute_assets_digest(docker_dir: Path) -> str:
    """Compute the 12-character SHA-256 digest of the validator assets in docker_dir.

    The file names and contents are hashed in a fixed order, so the digest is
    deterministic and changes whenever any asset changes.
    """
    digest = hashlib.sha256()
    for name in DOCKER_ASSET_FILES:
        asset = docker_dir / name
        if not asset.exists():
            raise FileNotFoundError(f"Missing required file in the docker directory: {asset}")
        digest.update(name.encode("utf-8"))
        digest.update(asset.read_bytes())
    return digest.hexdigest()[:12]


@functools.cache
def get_docker_dir() -> Path:
    """Return the path to the docker/ directory.

    In development mode (unpacked) it returns the existing directory next to the module.
    In zipapp (.pyz) mode it extracts the docker/ directory contents into a temporary
    directory that is automatically removed when the program exits.
    """
    current_file = Path(__file__).resolve()
    candidate = current_file.parent / "docker"
    if candidate.is_dir() and (candidate / "Dockerfile").exists():
        return candidate

    archive_path: Path | None = None
    for parent in [current_file] + list(current_file.parents):
        try:
            if parent.is_file() and zipfile.is_zipfile(parent):
                archive_path = parent
                break
        except (OSError, ValueError):
            continue

    if archive_path is not None:
        tmp_dir = Path(tempfile.mkdtemp(prefix="autoinstall_helper_docker_"))
        atexit.register(shutil.rmtree, tmp_dir, True)
        prefix = "autoinstall_helper/docker/"
        with zipfile.ZipFile(archive_path) as zf:
            for member in zf.namelist():
                if member.startswith(prefix) and not member.endswith("/"):
                    rel_name = member[len(prefix) :]
                    target_file = tmp_dir / rel_name
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    target_file.write_bytes(zf.read(member))
                    info = zf.getinfo(member)
                    perms = (info.external_attr >> 16) & 0o777
                    if perms:
                        target_file.chmod(perms)
        return tmp_dir

    raise SystemExit(f"ERROR: 'docker' directory not found (looked in {candidate}).")


def docker_image_exists(tag: str) -> bool:
    """Check whether a Docker image with the given tag exists locally."""
    res = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
        check=False,
    )
    return res.returncode == 0


@functools.cache
def get_docker_digest() -> str:
    """Return the 12-character SHA-256 digest of the container assets without writing to disk.

    1. In zipapp mode it reads the DOCKER_DIGEST file directly from the archive into RAM.
    2. In development mode it checks the DOCKER_DIGEST file next to the module or computes the digest on the fly.
    """
    current_file = Path(__file__).resolve()

    # 1. Check whether the DOCKER_DIGEST file exists directly on disk (next to the module)
    digest_file = current_file.parent / "DOCKER_DIGEST"
    if digest_file.is_file():
        return digest_file.read_text(encoding="utf-8").strip()

    # 2. Development mode (docker/ directory on disk)
    docker_dir = current_file.parent / "docker"
    if docker_dir.is_dir() and (docker_dir / "Dockerfile").exists():
        return compute_assets_digest(docker_dir)

    # 3. Zipapp mode: read DOCKER_DIGEST directly from the zip archive without extracting
    for parent in [current_file] + list(current_file.parents):
        try:
            if parent.is_file() and zipfile.is_zipfile(parent):
                with zipfile.ZipFile(parent) as zf:
                    namelist = zf.namelist()
                    if "autoinstall_helper/DOCKER_DIGEST" in namelist:
                        return zf.read("autoinstall_helper/DOCKER_DIGEST").decode("utf-8").strip()
                    # Emergency fallback: compute from files in the archive in RAM without creating tmp files
                    digest = hashlib.sha256()
                    for name in DOCKER_ASSET_FILES:
                        entry = f"autoinstall_helper/docker/{name}"
                        if entry in namelist:
                            digest.update(name.encode("utf-8"))
                            digest.update(zf.read(entry))
                    return digest.hexdigest()[:12]
        except (OSError, ValueError):
            continue

    raise SystemExit("ERROR: Could not determine the digest of the docker/ assets.")


def docker_image_tag(version: str, digest_or_dockerfile: str | Path | None = None) -> str:
    """Include local validation assets in the image tag to avoid stale images."""
    if isinstance(digest_or_dockerfile, Path):
        tag_hash = compute_assets_digest(digest_or_dockerfile.parent)
    elif isinstance(digest_or_dockerfile, str):
        tag_hash = digest_or_dockerfile
    else:
        tag_hash = get_docker_digest()
    return f"{DOCKER_IMAGE_REPO}:{version}-{tag_hash}"


def docker_build(tag: str, version: str, base_image: str, dockerfile: Path) -> None:
    """Build the validator Docker image with the specified tag and base image."""
    print(f"[autoinstall-helper] Building image '{tag}' (base: {base_image})...")
    subprocess.run(
        [
            "docker",
            "build",
            "--build-arg",
            f"SUBIQUITY_VERSION={version}",
            "--build-arg",
            f"BASE_IMAGE={base_image}",
            "-t",
            tag,
            "-f",
            str(dockerfile),
            str(dockerfile.parent),
        ],
        check=True,
    )
    print(f"[autoinstall-helper] Image '{tag}' built.")


def docker_validate(tag: str, config_file: Path, validation: str) -> int:
    """Run validation inside the Docker container and return the exit code."""
    container_path = "/data/autoinstall.yaml"
    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{config_file.resolve()}:{container_path}:ro",
        tag,
        "python3",
        "scripts/validate-autoinstall.py",
        container_path,
        "--validation",
        validation,
    ]
    print(f"[autoinstall-helper] Validating '{config_file.name}' in container '{tag}'...")
    result = subprocess.run(cmd, check=False)
    return result.returncode
