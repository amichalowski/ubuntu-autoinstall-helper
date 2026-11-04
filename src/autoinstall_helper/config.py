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
        with config_file.open() as f:
            return f.readline().strip() == "#cloud-config"
    except OSError:
        return False
