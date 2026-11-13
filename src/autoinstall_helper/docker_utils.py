"""Management of the validator Docker image, their local cache, and docker/ directory assets."""

import atexit
import functools
import grp
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

DOCKER_IMAGE_REPO = "ubuntu-desktop-bootstrap"

# Fields of `docker image ls --format` used to describe a validator image.
DOCKER_IMAGE_FIELDS = ("Repository", "Tag", "ID", "CreatedSince", "Size")

# Must stay in sync with SHELLCHECK_SEVERITIES in docker/validate-autoinstall.py.
SHELLCHECK_SEVERITIES = ("error", "warning", "info", "style")

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
        text=True,
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


def docker_validate(
    tag: str,
    config_file: Path,
    validation: str,
    *,
    shellcheck: bool = True,
    shellcheck_severity: str = "error",
    release: str | None = None,
) -> int:
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
        "--shellcheck-severity",
        shellcheck_severity,
    ]
    if not shellcheck:
        cmd.append("--no-shellcheck")
    if release:
        cmd.extend(["--release", release])
    print(f"[autoinstall-helper] Validating '{config_file.name}' in container '{tag}'...")
    result = subprocess.run(cmd, check=False)
    return result.returncode


def _docker_server_version() -> tuple[str | None, str]:
    """Query the Docker daemon through the CLI.

    Returns the server version, or (None, error) when the daemon cannot be
    reached (not running, no permission to access the socket, ...).
    """
    res = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip(), ""
    return None, _stderr_summary(res, "the Docker daemon is not reachable")


def _stderr_summary(res: subprocess.CompletedProcess, fallback: str) -> str:
    """Return the last non-empty stderr line (Docker repeats the cause in earlier lines)."""
    lines = [line.strip() for line in (res.stderr or "").splitlines() if line.strip()]
    return lines[-1] if lines else fallback


def _docker_group_membership() -> bool | None:
    """Report whether the current process belongs to the 'docker' group.

    Returns None when the group does not exist on this host.
    """
    try:
        gid = grp.getgrnam("docker").gr_gid
    except KeyError:
        return None
    return gid in os.getgroups()


def docker_doctor() -> int:
    """Report whether the host can run the validator container without sudo.

    Mirrors `vm doctor`: prints a one-line-per-check report and returns 0 when
    the host is ready, 1 otherwise.
    """
    print("[autoinstall-helper] Docker preflight check")
    ready = True

    docker_path = shutil.which("docker")
    if docker_path is None:
        print(f"[autoinstall-helper]   {'docker':<13} MISSING (required)")
        print("[autoinstall-helper] Install Docker with: sudo apt install docker.io")
        print("[autoinstall-helper] Docker support is NOT ready.")
        return 1
    print(f"[autoinstall-helper]   {'docker':<13} {docker_path}")

    version, error = _docker_server_version()
    if version is None:
        print(f"[autoinstall-helper]   {'daemon':<13} unreachable")
        print(f"[autoinstall-helper]   {error}")
        print("[autoinstall-helper]   Start the daemon: sudo systemctl enable --now docker")
        ready = False
    else:
        print(f"[autoinstall-helper]   {'daemon':<13} reachable (server {version})")

    if os.geteuid() == 0:
        # Root bypasses the 'docker' group, so this check cannot prove anything here.
        print(f"[autoinstall-helper]   {'without sudo':<13} unknown (running as root)")
        print("[autoinstall-helper]   Run this check as a normal user to verify docker group access.")
    elif ready:
        print(f"[autoinstall-helper]   {'without sudo':<13} ok")
    else:
        print(f"[autoinstall-helper]   {'without sudo':<13} no")
        if _docker_group_membership() is False:
            print("[autoinstall-helper]   Join the 'docker' group: sudo usermod -aG docker $USER")
            print("[autoinstall-helper]   Then log out and back in (or run: newgrp docker)")

    print(
        "[autoinstall-helper] Docker support is ready."
        if ready
        else "[autoinstall-helper] Docker support is NOT ready."
    )
    return 0 if ready else 1


@dataclass(frozen=True)
class ValidatorImage:
    """A validator image present in the local Docker store.

    ``version`` and ``digest`` are parsed from the ``<version>-<digest>`` tag
    produced by :func:`docker_image_tag`; both are None for images built by hand.
    """

    tag: str
    image_id: str
    created: str
    size: str
    version: str | None = None
    digest: str | None = None

    def status(self, current_digest: str) -> str:
        """Return 'current', 'stale' or 'unrecognized' relative to the bundled assets."""
        if self.digest is None:
            return "unrecognized"
        return "current" if self.digest == current_digest else "stale"

    def is_prunable(self, current_digest: str) -> bool:
        """An image can be pruned once it no longer matches the bundled assets."""
        return self.status(current_digest) != "current"


def list_validator_images() -> list[ValidatorImage]:
    """Return the validator images in the local Docker store (newest first)."""
    image_format = "\t".join(f"{{{{.{field}}}}}" for field in DOCKER_IMAGE_FIELDS)
    res = subprocess.run(
        ["docker", "image", "ls", DOCKER_IMAGE_REPO, "--format", image_format],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        error = _stderr_summary(res, "docker image ls failed")
        raise SystemExit(
            f"ERROR: Could not list validator images: {error}\n"
            "Hint: run 'autoinstall-helper validate doctor' to check the Docker setup."
        )

    images = []
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        repository, tag, image_id, created, size = (fields + [""] * len(DOCKER_IMAGE_FIELDS))[
            : len(DOCKER_IMAGE_FIELDS)
        ]
        version, separator, digest = tag.rpartition("-")
        images.append(
            ValidatorImage(
                tag=f"{repository}:{tag}",
                image_id=image_id,
                created=created,
                size=size,
                version=version if separator else None,
                digest=digest if separator else None,
            )
        )
    return images


def docker_images(*, prune: bool = False, remove_all: bool = False, dry_run: bool = False, yes: bool = False) -> int:
    """List the validator images and optionally prune the ones built from outdated assets.

    Staleness is network-free: the digest embedded in the tag is compared with
    the digest of the bundled docker/ assets.
    """
    current_digest = get_docker_digest()
    images = list_validator_images()

    print(f"[autoinstall-helper] Validator images (bundled assets digest: {current_digest})")
    if not images:
        print("[autoinstall-helper]   none — 'validate <config>' builds one on demand")
    for image in images:
        print(
            f"[autoinstall-helper]   {image.tag:<48} {image.size:>8}  {image.created:<16} {image.status(current_digest)}"
        )

    if not prune:
        return 0

    targets = images if remove_all else [image for image in images if image.is_prunable(current_digest)]
    if not targets:
        scope = "validator images" if remove_all else "stale validator images"
        print(f"[autoinstall-helper] Nothing to prune: no {scope}.")
        return 0

    action = "Would remove" if dry_run else "Removing"
    print(f"[autoinstall-helper] {action} {len(targets)} validator image(s):")
    for image in targets:
        print(f"[autoinstall-helper]   {image.tag}")
    if dry_run:
        return 0

    if not yes:
        if not sys.stdin.isatty():
            raise SystemExit("ERROR: --prune in non-interactive mode requires -y / --yes.")
        answer = input("[autoinstall-helper] Remove these images? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("[autoinstall-helper] Cancelled by user.")
            return 1

    removed = 0
    for image in targets:
        res = subprocess.run(["docker", "image", "rm", image.tag], capture_output=True, text=True, check=False)
        if res.returncode == 0:
            removed += 1
            print(f"[autoinstall-helper]   removed {image.tag}")
        else:
            error = _stderr_summary(res, "docker image rm failed")
            print(f"[autoinstall-helper]   FAILED {image.tag}: {error}", file=sys.stderr)

    failed = len(targets) - removed
    summary = f"[autoinstall-helper] Removed {removed} of {len(targets)} image(s)"
    print(summary + (f" ({failed} failed)." if failed else "."))
    return 0 if failed == 0 else 1
