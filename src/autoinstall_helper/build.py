"""Narzędzie do budowania pojedynczego pliku wykonywalnego zipapp (.pyz)."""

import argparse
import hashlib
import stat
import sys
import zipapp
from pathlib import Path


def exclude_pycache(path: Path) -> bool:
    """Wyklucza katalogi __pycache__ i skompilowane pliki .pyc z archiwum."""
    return "__pycache__" not in path.parts and not path.name.endswith(".pyc")


def compute_docker_digest(docker_dir: Path) -> str:
    """Oblicza 12-znakowy skrót SHA-256 z kluczowych plików katalogu docker/."""
    digest = hashlib.sha256()
    for name in ("Dockerfile", "timedatectl.sh", "validate-autoinstall.py"):
        asset = docker_dir / name
        if not asset.exists():
            raise FileNotFoundError(f"Brak wymaganego pliku w katalogu docker: {asset}")
        digest.update(name.encode("utf-8"))
        digest.update(asset.read_bytes())
    return digest.hexdigest()[:12]


def build_zipapp(
    target: Path | str = "autoinstall-helper.pyz",
    src_dir: Path | str | None = None,
) -> Path:
    """Tworzy archiwum zipapp (.pyz) z kodu źródłowego."""
    if src_dir is None:
        # Domyślnie katalog src/ nadrzędny wobec pakietu autoinstall_helper
        src_path = Path(__file__).resolve().parents[1]
    else:
        src_path = Path(src_dir).resolve()

    target_path = Path(target).resolve()

    # Upewnij się, że katalogi pakietów zawierają __init__.py
    pkg_docker = src_path / "autoinstall_helper" / "docker"
    digest_file: Path | None = None
    if pkg_docker.is_dir():
        (pkg_docker / "__init__.py").touch()
        # Wylicz hash zasobów i umieść plik DOCKER_DIGEST w pakiecie, ale POZA katalogiem docker/
        digest = compute_docker_digest(pkg_docker)
        digest_file = src_path / "autoinstall_helper" / "DOCKER_DIGEST"
        digest_file.write_text(digest + "\n", encoding="utf-8")

    target_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        print(f"[build] Pakowanie {src_path} -> {target_path}...")
        zipapp.create_archive(
            source=src_path,
            target=target_path,
            interpreter="/usr/bin/env python3",
            main="autoinstall_helper.cli:main",
            filter=exclude_pycache,
        )
    finally:
        # Usuń tymczasowy plik z katalogu źródłowego po spakowaniu
        if digest_file is not None and digest_file.exists():
            digest_file.unlink()

    # Nadanie uprawnień wykonywalności (+x)
    current_mode = target_path.stat().st_mode
    target_path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    size_kb = target_path.stat().st_size / 1024
    print(f"[build] Sukces: utworzono {target_path.name} ({size_kb:.1f} KB)")
    return target_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build-pyz",
        description="Buduje samodzielny plik wykonywalny autoinstall-helper.pyz (zipapp).",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=Path("autoinstall-helper.pyz"),
        help="Ścieżka do wyjściowego pliku .pyz (domyślnie: autoinstall-helper.pyz)",
    )
    parser.add_argument(
        "--src",
        type=Path,
        default=None,
        help="Katalog źródłowy (domyślnie katalog src)",
    )

    args = parser.parse_args(argv)
    try:
        build_zipapp(target=args.output, src_dir=args.src)
        return 0
    except Exception as exc:
        print(f"BŁĄD budowania: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
