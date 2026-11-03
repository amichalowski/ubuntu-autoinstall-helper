"""Parsowanie i walidacja pliku autoinstall.yaml."""

from pathlib import Path


def extract_installer_channel(config_file: Path) -> str:
    """Wyciąga wartość autoinstall.refresh-installer.channel z pliku YAML."""
    import yaml  # type: ignore[import-untyped]

    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    channel = (
        data.get("autoinstall", {})
            .get("refresh-installer", {})
            .get("channel")
    )
    if not channel:
        raise SystemExit(
            f"BŁĄD: Nie znaleziono pola 'autoinstall.refresh-installer.channel' w {config_file}"
        )
    return str(channel).strip()


def has_cloud_config_header(config_file: Path) -> bool:
    """Sprawdza czy plik zaczyna się od '#cloud-config'."""
    try:
        with config_file.open() as f:
            return f.readline().strip() == "#cloud-config"
    except OSError:
        return False
