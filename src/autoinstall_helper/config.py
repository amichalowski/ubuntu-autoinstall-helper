"""Parsing and validation of the autoinstall.yaml file."""

from pathlib import Path


def extract_installer_channel(config_file: Path) -> str | None:
    """Extract the autoinstall.refresh-installer.channel value from the YAML file.

    Returns None when the entry is absent (no ``refresh-installer`` block, no
    ``channel`` key, or no ``autoinstall`` mapping at all); the caller decides
    on a fallback.
    """
    # Lazy import: pyyaml is available in Ubuntu environments (python3-yaml)
    # and dev environments, but production code keeps zero hard dependencies in [project.dependencies].
    import yaml  # type: ignore[import-untyped]

    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    autoinstall = data.get("autoinstall") if isinstance(data, dict) else None
    if not isinstance(autoinstall, dict):
        return None
    refresh = autoinstall.get("refresh-installer")
    if not isinstance(refresh, dict):
        return None
    channel = refresh.get("channel")
    if not channel:
        return None
    return str(channel).strip() or None


def extract_snaps(config_file: Path) -> object:
    """Extract the raw autoinstall.snaps value from the YAML file ([] when absent).

    The value is returned unvalidated so the snap layer can report structural
    problems itself instead of failing with a bare parse error.
    """
    # Lazy import: see extract_installer_channel.
    import yaml  # type: ignore[import-untyped]

    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    autoinstall = data.get("autoinstall") if isinstance(data, dict) else None
    if not isinstance(autoinstall, dict):
        return []
    return autoinstall.get("snaps", [])


def has_cloud_config_header(config_file: Path) -> bool:
    """Check whether the file starts with '#cloud-config'."""
    try:
        with config_file.open(encoding="utf-8") as f:
            return f.readline().strip() == "#cloud-config"
    except OSError:
        return False


def require_cloud_config_header(config_file: Path) -> None:
    """Exit with a clear message when the file lacks the '#cloud-config' header.

    An autoinstall file is delivered as cloud-config, so the header is mandatory:
    without it the top-level 'autoinstall' key would be ignored by the installer.
    """
    if not has_cloud_config_header(config_file):
        raise SystemExit(
            f"ERROR: File '{config_file}' must start with the '#cloud-config' header.\n"
            f"An autoinstall file is delivered as cloud-config, e.g.:\n"
            f"  #cloud-config\n"
            f"  autoinstall:\n"
            f"    version: 1"
        )
