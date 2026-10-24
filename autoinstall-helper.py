#!/usr/bin/env python3
"""Autoinstall Helper - Ubuntu Desktop Bootstrap Autoinstall Validator.

This script parses an Ubuntu autoinstall configuration file (autoinstall.yaml),
extracts the installer refresh channel, queries the snap store for the installer version,
ensures a Docker validation image exists (building it if necessary), and runs the
Subiquity validator inside the container.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple


class AutoinstallError(Exception):
    """Base exception for autoinstall-helper errors."""


class ConfigError(AutoinstallError):
    """Raised when configuration parsing or validation fails."""


class SnapError(AutoinstallError):
    """Raised when interacting with snap fails."""


class DockerError(AutoinstallError):
    """Raised when interacting with docker fails."""


class AutoinstallConfigParser:
    """Parses autoinstall YAML configuration files using PyYAML or pure standard library."""

    @staticmethod
    def extract_installer_channel(file_path: Path) -> str:
        """Extract the `autoinstall.refresh-installer.channel` value from the YAML file.

        Args:
            file_path: Path to the autoinstall.yaml file.

        Returns:
            The channel string (e.g., '26.04/stable').

        Raises:
            ConfigError: If the file cannot be read or the channel field is missing.
        """
        if not file_path.exists():
            raise ConfigError(f"Plik konfiguracyjny nie istnieje: {file_path}")

        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Nie można odczytać pliku {file_path}: {exc}") from exc

        # Attempt parsing with PyYAML if available
        try:
            import yaml  # type: ignore[import-untyped]

            try:
                data = yaml.safe_load(content)
                if isinstance(data, dict):
                    channel = (
                        data.get("autoinstall", {})
                        .get("refresh-installer", {})
                        .get("channel")
                    )
                    if channel:
                        return str(channel).strip()
            except Exception:
                # Fall back to standard library parser if PyYAML fails
                pass
        except ImportError:
            pass

        # Standard library indentation-aware line parser fallback
        channel = AutoinstallConfigParser._extract_channel_stdlib(content)
        if not channel:
            raise ConfigError(
                f"Nie znaleziono pola 'autoinstall.refresh-installer.channel' w pliku {file_path}"
            )
        return channel

    @staticmethod
    def _extract_channel_stdlib(content: str) -> Optional[str]:
        """Extract refresh-installer channel using pure standard library line parsing."""
        in_autoinstall = False
        in_refresh_installer = False
        refresh_indent = 0

        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            current_indent = len(line) - len(line.lstrip(" "))

            if stripped == "autoinstall:":
                in_autoinstall = True
                continue

            if in_autoinstall:
                if current_indent == 0:
                    in_autoinstall = False
                    in_refresh_installer = False
                elif stripped == "refresh-installer:":
                    in_refresh_installer = True
                    refresh_indent = current_indent
                elif in_refresh_installer:
                    if current_indent <= refresh_indent:
                        in_refresh_installer = False
                    elif stripped.startswith("channel:"):
                        raw_value = stripped.split(":", 1)[1].strip()
                        return raw_value.strip("\"'")

        # Secondary regex fallback
        pattern = re.compile(
            r"refresh-installer\s*:\s*\n(?:[ \t]+[^\n]+\n)*?[ \t]+channel\s*:\s*[\"']?([^\"'\s\n]+)[\"']?"
        )
        match = pattern.search(content)
        if match:
            return match.group(1).strip("\"'")

        return None

    @staticmethod
    def has_cloud_config_header(file_path: Path) -> bool:
        """Check whether the file begins with the `#cloud-config` header."""
        try:
            with file_path.open("r", encoding="utf-8") as f:
                first_line = f.readline().strip()
                return first_line == "#cloud-config"
        except OSError:
            return False


class SnapInspector:
    """Queries snap information from the host system."""

    DEFAULT_SNAP = "ubuntu-desktop-bootstrap"

    @classmethod
    def get_version_for_channel(
        cls, channel: str, snap_name: str = DEFAULT_SNAP
    ) -> str:
        """Query snap info to determine the version assigned to a given channel.

        Args:
            channel: Snap channel name (e.g., '26.04/stable').
            snap_name: Name of the snap package.

        Returns:
            The version string (e.g., '26.04.1').

        Raises:
            SnapError: If snap tool is not found, command fails, or channel is not found.
        """
        if not shutil.which("snap"):
            raise SnapError(
                "Narzędzie 'snap' nie jest zainstalowane lub nie znajduje się w zmiennej PATH."
            )

        cmd = ["snap", "info", snap_name]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, check=True
            )
        except subprocess.CalledProcessError as exc:
            stderr_msg = exc.stderr.strip() if exc.stderr else str(exc)
            raise SnapError(
                f"Błąd podczas wywołania polecenia '{' '.join(cmd)}': {stderr_msg}"
            ) from exc

        target_prefix = f"{channel}:"
        for line in proc.stdout.splitlines():
            trimmed = line.strip()
            if trimmed.startswith(target_prefix):
                parts = trimmed.split()
                if len(parts) >= 2:
                    version = parts[1]
                    if version != "--":
                        return version
                raise SnapError(
                    f"Kanał '{channel}' dla snap '{snap_name}' nie posiada opublikowanej wersji."
                )

        raise SnapError(
            f"Nie znaleziono kanału '{channel}' w informacjach o snapie '{snap_name}'."
        )


class DockerManager:
    """Manages Docker container validation images and execution."""

    IMAGE_REPOSITORY = "ubuntu-desktop-bootstrap"

    def __init__(self, dockerfile_path: Path) -> None:
        self.dockerfile_path = dockerfile_path
        self._docker_prefix: List[str] = self._resolve_docker_prefix()

    @staticmethod
    def _resolve_docker_prefix() -> List[str]:
        """Determine if 'docker' can be invoked directly or requires 'sg docker -c'."""
        if not shutil.which("docker"):
            raise DockerError(
                "Narzędzie 'docker' nie jest zainstalowane lub nie znajduje się w zmiennej PATH."
            )

        # Test direct execution
        try:
            res = subprocess.run(
                ["docker", "info"], capture_output=True, text=True, timeout=5
            )
            if res.returncode == 0:
                return ["docker"]
        except (subprocess.SubprocessError, OSError):
            pass

        # Test with group wrapper (sg docker)
        if shutil.which("sg"):
            try:
                res = subprocess.run(
                    ["sg", "docker", "-c", "docker info"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if res.returncode == 0:
                    return ["sg", "docker", "-c"]
            except (subprocess.SubprocessError, OSError):
                pass

        # Fallback to direct docker
        return ["docker"]

    def _run_docker_cmd(
        self,
        args: List[str],
        capture_output: bool = False,
        check: bool = True,
        stream_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a docker command with the resolved docker prefix."""
        if self._docker_prefix == ["sg", "docker", "-c"]:
            full_command_str = "docker " + " ".join(shlex.quote(a) for a in args)
            cmd = ["sg", "docker", "-c", full_command_str]
        else:
            cmd = self._docker_prefix + args

        if stream_output:
            proc = subprocess.run(cmd, check=check, text=True)
            return proc

        return subprocess.run(
            cmd, capture_output=capture_output, text=True, check=check
        )

    def image_tag(self, version: str) -> str:
        """Return the full image tag for a given installer version."""
        return f"{self.IMAGE_REPOSITORY}:{version}"

    def image_exists(self, version: str) -> bool:
        """Check whether the docker image for the given version exists locally."""
        tag = self.image_tag(version)
        try:
            res = self._run_docker_cmd(["image", "inspect", tag], capture_output=True, check=False)
            return res.returncode == 0
        except Exception:
            return False

    def build_image(self, version: str) -> None:
        """Build the docker validation image for the specified version.

        Args:
            version: Installer version to build (passed as SUBIQUITY_VERSION build arg).
        """
        tag = self.image_tag(version)
        context_dir = self.dockerfile_path.parent.resolve()

        print(f"[autoinstall-helper] Budowanie obrazu Dockera '{tag}'...")
        build_args = [
            "build",
            "--build-arg",
            f"SUBIQUITY_VERSION={version}",
            "-t",
            tag,
            "-f",
            str(self.dockerfile_path.resolve()),
            str(context_dir),
        ]

        try:
            self._run_docker_cmd(build_args, stream_output=True, check=True)
            print(f"[autoinstall-helper] Obraz '{tag}' został pomyślnie zbudowany.")
        except subprocess.CalledProcessError as exc:
            raise DockerError(f"Błąd podczas budowania obrazu Dockera '{tag}': kod {exc.returncode}") from exc

    def validate_file(self, config_file: Path, version: str) -> int:
        """Validate the autoinstall configuration file using the docker image.

        Args:
            config_file: Path to the autoinstall.yaml file.
            version: Installer version / tag of the docker image.

        Returns:
            Return code of the validation process (0 for success).
        """
        tag = self.image_tag(version)
        resolved_file = config_file.resolve()

        # Mount target inside container
        container_mount_path = "/data/autoinstall.yaml"

        validation_args: List[str] = [container_mount_path]
        if not AutoinstallConfigParser.has_cloud_config_header(resolved_file):
            validation_args.append("--no-expect-cloudconfig")

        run_args = [
            "run",
            "--rm",
            "-v",
            f"{resolved_file}:{container_mount_path}:ro",
            tag,
            "python3",
            "scripts/validate-autoinstall-user-data.py",
            *validation_args,
        ]

        print(f"[autoinstall-helper] Uruchamianie walidacji pliku '{config_file.name}' w kontenerze '{tag}'...")
        proc = self._run_docker_cmd(run_args, stream_output=True, check=False)
        return proc.returncode


class AutoinstallHelper:
    """Main orchestrator for autoinstall validation."""

    def __init__(self, workspace_dir: Optional[Path] = None) -> None:
        self.workspace_dir = (workspace_dir or Path.cwd()).resolve()
        self.dockerfile_path = self.workspace_dir / "Dockerfile"

    def run_validation(self, config_file_path: Path) -> int:
        """Run the full validation workflow for a given autoinstall configuration file."""
        print(f"[autoinstall-helper] Odczytywanie pliku: {config_file_path}")
        channel = AutoinstallConfigParser.extract_installer_channel(config_file_path)
        print(f"[autoinstall-helper] Wykryty kanał instalatora: {channel}")

        print("[autoinstall-helper] Sprawdzanie wersji w snap info...")
        version = SnapInspector.get_version_for_channel(channel)
        print(f"[autoinstall-helper] Ustalona wersja instalatora: {version}")

        if not self.dockerfile_path.exists():
            raise DockerError(f"Nie znaleziono pliku Dockerfile w {self.dockerfile_path}")

        docker_mgr = DockerManager(self.dockerfile_path)
        if not docker_mgr.image_exists(version):
            print(f"[autoinstall-helper] Obraz {docker_mgr.image_tag(version)} nie istnieje lokalnie.")
            docker_mgr.build_image(version)
        else:
            print(f"[autoinstall-helper] Obraz {docker_mgr.image_tag(version)} już istnieje lokalnie.")

        return docker_mgr.validate_file(config_file_path, version)


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="autoinstall-helper.py",
        description="Narzędzie do walidacji plików autoinstall.yaml Ubuntu przy użyciu obrazu Docker i Subiquity.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-v",
        "--validate",
        metavar="PLIK_YAML",
        type=Path,
        help="Ścieżka do pliku konfiguracyjnego autoinstall.yaml do walidacji",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for the autoinstall-helper script."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if not args.validate:
        parser.print_help()
        return 0

    target_file: Path = args.validate
    if not target_file.exists():
        print(f"BŁĄD: Podany plik nie istnieje: {target_file}", file=sys.stderr)
        return 1

    script_dir = Path(__file__).resolve().parent
    helper = AutoinstallHelper(workspace_dir=script_dir)

    try:
        exit_code = helper.run_validation(target_file)
        if exit_code == 0:
            print("[autoinstall-helper] Walidacja zakończona sukcesem!")
        else:
            print(f"[autoinstall-helper] Walidacja zakończona niepowodzeniem (kod wyjścia: {exit_code}).", file=sys.stderr)
        return exit_code
    except AutoinstallError as exc:
        print(f"BŁĄD: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nPrzerwano przez użytkownika.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
