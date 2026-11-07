"""Parsing and validation of the autoinstall.yaml file."""

from pathlib import Path


def extract_installer_channel(config_file: Path) -> str:
    """Extract the autoinstall.refresh-installer.channel value from the YAML file."""
    import yaml  # type: ignore[import-untyped]

    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    channel = data.get("autoinstall", {}).get("refresh-installer", {}).get("channel")
    if not channel:
        raise SystemExit(f"ERROR: Field 'autoinstall.refresh-installer.channel' not found in {config_file}")
    return str(channel).strip()


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
