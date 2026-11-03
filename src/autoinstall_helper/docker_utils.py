"""Zarządzanie obrazem Dockera walidatora i zasobami katalogu docker/."""

import atexit
import hashlib
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from autoinstall_helper.config import has_cloud_config_header

DOCKER_IMAGE_REPO = "ubuntu-desktop-bootstrap"

_DOCKER_DIR_CACHE: Path | None = None


def _get_docker_dir() -> Path:
    """Zwraca ścieżkę do katalogu docker/.

    W trybie deweloperskim (niespakowanym) zwraca istniejący katalog obok modułu.
    W trybie zipapp (.pyz) wypakowuje zawartość katalogu docker/ do katalogu tymczasowego,
    który jest automatycznie usuwany przy zakończeniu działania programu.
    """
    global _DOCKER_DIR_CACHE
    if _DOCKER_DIR_CACHE is not None and _DOCKER_DIR_CACHE.is_dir():
        return _DOCKER_DIR_CACHE

    current_file = Path(__file__).resolve()
    candidate = current_file.parent / "docker"
    if candidate.is_dir() and (candidate / "Dockerfile").exists():
        _DOCKER_DIR_CACHE = candidate
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
        _DOCKER_DIR_CACHE = tmp_dir
        return tmp_dir

    raise SystemExit(f"BŁĄD: Nie znaleziono katalogu 'docker' (szukano w {candidate}).")


def docker_image_exists(tag: str) -> bool:
    res = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
        check=False,
    )
    return res.returncode == 0


_DOCKER_DIGEST_CACHE: str | None = None


def get_docker_digest() -> str:
    """Zwraca 12-znakowy skrót SHA-256 zasobów kontenera bez rozpakowywania na dysk.

    1. W trybie zipapp odczytuje plik DOCKER_DIGEST bezpośrednio z archiwum do pamięci RAM.
    2. W trybie deweloperskim sprawdza plik DOCKER_DIGEST obok modułu lub oblicza skrót w locie.
    """
    global _DOCKER_DIGEST_CACHE
    if _DOCKER_DIGEST_CACHE is not None:
        return _DOCKER_DIGEST_CACHE

    current_file = Path(__file__).resolve()

    # 1. Sprawdź czy plik DOCKER_DIGEST istnieje bezpośrednio na dysku (obok modułu)
    digest_file = current_file.parent / "DOCKER_DIGEST"
    if digest_file.is_file():
        _DOCKER_DIGEST_CACHE = digest_file.read_text(encoding="utf-8").strip()
        return _DOCKER_DIGEST_CACHE

    # 2. Tryb deweloperski (katalog docker/ na dysku)
    docker_dir = current_file.parent / "docker"
    if docker_dir.is_dir() and (docker_dir / "Dockerfile").exists():
        digest = hashlib.sha256()
        for name in ("Dockerfile", "timedatectl.sh", "validate-autoinstall.py"):
            asset = docker_dir / name
            digest.update(name.encode("utf-8"))
            digest.update(asset.read_bytes())
        _DOCKER_DIGEST_CACHE = digest.hexdigest()[:12]
        return _DOCKER_DIGEST_CACHE

    # 3. Tryb zipapp: odczytaj DOCKER_DIGEST bezpośrednio z archiwum zip bez rozpakowywania
    for parent in [current_file] + list(current_file.parents):
        try:
            if parent.is_file() and zipfile.is_zipfile(parent):
                with zipfile.ZipFile(parent) as zf:
                    namelist = zf.namelist()
                    if "autoinstall_helper/DOCKER_DIGEST" in namelist:
                        _DOCKER_DIGEST_CACHE = zf.read("autoinstall_helper/DOCKER_DIGEST").decode("utf-8").strip()
                        return _DOCKER_DIGEST_CACHE
                    # Awaryjny fallback: policz z plików w archiwum w RAM bez tworzenia plików w tmp
                    digest = hashlib.sha256()
                    for name in ("Dockerfile", "timedatectl.sh", "validate-autoinstall.py"):
                        entry = f"autoinstall_helper/docker/{name}"
                        if entry in namelist:
                            digest.update(name.encode("utf-8"))
                            digest.update(zf.read(entry))
                    _DOCKER_DIGEST_CACHE = digest.hexdigest()[:12]
                    return _DOCKER_DIGEST_CACHE
        except (OSError, ValueError):
            continue

    raise SystemExit("BŁĄD: Nie można ustalić skrótu zasobów katalogu docker.")


def docker_image_tag(version: str, digest_or_dockerfile: str | Path | None = None) -> str:
    """Include local validation assets in the image tag to avoid stale images."""
    if isinstance(digest_or_dockerfile, Path):
        digest = hashlib.sha256()
        for path in (
            digest_or_dockerfile,
            digest_or_dockerfile.parent / "timedatectl.sh",
            digest_or_dockerfile.parent / "validate-autoinstall.py",
        ):
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
        tag_hash = digest.hexdigest()[:12]
    elif isinstance(digest_or_dockerfile, str):
        tag_hash = digest_or_dockerfile
    else:
        tag_hash = get_docker_digest()
    return f"{DOCKER_IMAGE_REPO}:{version}-{tag_hash}"


def docker_build(tag: str, version: str, base_image: str, dockerfile: Path) -> None:
    print(f"[autoinstall-helper] Budowanie obrazu '{tag}' (base: {base_image})...")
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
    print(f"[autoinstall-helper] Obraz '{tag}' zbudowany.")


def docker_validate(tag: str, config_file: Path, validation: str) -> int:
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
    if not has_cloud_config_header(config_file):
        cmd.append("--no-expect-cloudconfig")

    print(f"[autoinstall-helper] Walidacja '{config_file.name}' w kontenerze '{tag}'...")
    result = subprocess.run(cmd, check=False)
    return result.returncode
