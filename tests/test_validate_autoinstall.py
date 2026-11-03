"""Unit tests for pure validation and transformation functions in docker/validate-autoinstall.py."""

import copy
import importlib.util
from pathlib import Path

import pytest

# Dynamic load because the filename has a hyphen: validate-autoinstall.py
_VALIDATOR_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "autoinstall_helper" / "docker" / "validate-autoinstall.py"
)
_spec = importlib.util.spec_from_file_location("validate_autoinstall", _VALIDATOR_PATH)
assert _spec and _spec.loader
_validator_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_validator_mod)

validate_storage_structure = _validator_mod.validate_storage_structure
storage_only_data = _validator_mod.storage_only_data
curtin_storage_input = _validator_mod.curtin_storage_input


class TestValidateStorageStructure:
    """Tests for validate_storage_structure(ai_data)."""

    def test_no_storage_section(self):
        assert validate_storage_structure({}) == []

    def test_storage_none(self):
        assert validate_storage_structure({"storage": None}) == []

    def test_storage_not_mapping(self):
        errors = validate_storage_structure({"storage": "invalid"})
        assert errors == ["autoinstall.storage must be a mapping"]

    def test_storage_layout_based_without_config(self):
        data = {"storage": {"layout": {"name": "lvm"}}}
        assert validate_storage_structure(data) == []

    def test_storage_config_not_list(self):
        data = {"storage": {"config": "not-a-list"}}
        errors = validate_storage_structure(data)
        assert errors == ["autoinstall.storage.config must be a list"]

    def test_storage_config_entry_not_dict(self):
        data = {"storage": {"config": ["not-a-dict"]}}
        errors = validate_storage_structure(data)
        assert errors == ["autoinstall.storage.config[0] must be a mapping"]

    def test_storage_action_missing_id(self):
        data = {
            "storage": {
                "config": [
                    {"type": "disk", "path": "/dev/sda"},
                ]
            }
        }
        errors = validate_storage_structure(data)
        assert errors == ["autoinstall.storage.config[0].id must be a non-empty string"]

    def test_storage_action_empty_id(self):
        data = {
            "storage": {
                "config": [
                    {"id": "", "type": "disk"},
                ]
            }
        }
        errors = validate_storage_structure(data)
        assert errors == ["autoinstall.storage.config[0].id must be a non-empty string"]

    def test_storage_duplicate_id(self):
        data = {
            "storage": {
                "config": [
                    {"id": "disk-0", "type": "disk"},
                    {"id": "part-0", "type": "partition"},
                    {"id": "disk-0", "type": "disk"},
                ]
            }
        }
        errors = validate_storage_structure(data)
        assert len(errors) == 1
        assert "Duplicate storage action id 'disk-0' at autoinstall.storage.config[2]" in errors[0]
        assert "first declared at index 0" in errors[0]

    def test_valid_storage_config(self):
        data = {
            "storage": {
                "config": [
                    {"id": "disk-0", "type": "disk", "path": "/dev/sda"},
                    {"id": "part-0", "type": "partition", "device": "disk-0", "size": 1024},
                ]
            }
        }
        assert validate_storage_structure(data) == []


class TestStorageOnlyData:
    """Tests for storage_only_data(ai_data)."""

    def test_defaults_when_empty(self):
        result = storage_only_data({})
        assert result == {
            "version": 1,
            "interactive-sections": ["identity"],
        }

    def test_preserves_version_and_interactive_sections(self):
        data = {
            "version": 2,
            "interactive-sections": ["network", "storage"],
        }
        result = storage_only_data(data)
        assert result["version"] == 2
        assert result["interactive-sections"] == ["network", "storage"]

    def test_includes_source_and_storage(self):
        data = {
            "version": 1,
            "source": {"id": "ubuntu-desktop"},
            "storage": {"layout": {"name": "direct"}},
            "identity": {"username": "admin"},
            "packages": ["vim", "curl"],
        }
        result = storage_only_data(data)
        assert "source" in result
        assert "storage" in result
        assert "identity" not in result
        assert "packages" not in result


class TestCurtinStorageInput:
    """Tests for curtin_storage_input(ai_data)."""

    def test_no_storage_returns_none(self):
        assert curtin_storage_input({}) is None

    def test_layout_only_storage_returns_none(self):
        assert curtin_storage_input({"storage": {"layout": {"name": "lvm"}}}) is None

    def test_non_list_config_returns_none(self):
        assert curtin_storage_input({"storage": {"config": "not-list"}}) is None

    def test_disk_match_removed_and_placeholder_inserted(self):
        data = {
            "storage": {
                "config": [
                    {"id": "disk-0", "type": "disk", "match": {"size": "largest"}},
                ]
            }
        }
        result = curtin_storage_input(data)
        assert result is not None
        cfg = result["storage"]["config"][0]
        assert "match" not in cfg
        assert cfg["path"] == "/dev/autoinstall-match"

    def test_disk_with_path_and_match_keeps_existing_path(self):
        data = {
            "storage": {
                "config": [
                    {"id": "disk-0", "type": "disk", "path": "/dev/nvme0n1", "match": {"ssd": True}},
                ]
            }
        }
        result = curtin_storage_input(data)
        assert result is not None
        cfg = result["storage"]["config"][0]
        assert "match" not in cfg
        assert cfg["path"] == "/dev/nvme0n1"

    @pytest.mark.parametrize("size_value", [-1, "100%", "50%"])
    def test_partition_size_placeholder(self, size_value):
        data = {
            "storage": {
                "config": [
                    {"id": "part-1", "type": "partition", "size": size_value},
                    {"id": "lvm-1", "type": "lvm_partition", "size": size_value},
                ]
            }
        }
        result = curtin_storage_input(data)
        assert result is not None
        assert result["storage"]["config"][0]["size"] == "1B"
        assert result["storage"]["config"][1]["size"] == "1B"

    def test_partition_explicit_size_untouched(self):
        data = {
            "storage": {
                "config": [
                    {"id": "part-1", "type": "partition", "size": 1073741824},
                    {"id": "part-2", "type": "partition", "size": "10GB"},
                ]
            }
        }
        result = curtin_storage_input(data)
        assert result is not None
        assert result["storage"]["config"][0]["size"] == 1073741824
        assert result["storage"]["config"][1]["size"] == "10GB"

    def test_dm_crypt_defaults_dm_name_to_id(self):
        data = {
            "storage": {
                "config": [
                    {"id": "crypt-root", "type": "dm_crypt", "volume": "part-root"},
                ]
            }
        }
        result = curtin_storage_input(data)
        assert result is not None
        cfg = result["storage"]["config"][0]
        assert cfg["dm_name"] == "crypt-root"

    def test_dm_crypt_preserves_explicit_dm_name(self):
        data = {
            "storage": {
                "config": [
                    {"id": "crypt-root", "type": "dm_crypt", "dm_name": "custom-name"},
                ]
            }
        }
        result = curtin_storage_input(data)
        assert result is not None
        cfg = result["storage"]["config"][0]
        assert cfg["dm_name"] == "custom-name"

    def test_does_not_mutate_original_input(self):
        original = {
            "storage": {
                "config": [
                    {"id": "disk-0", "type": "disk", "match": {"size": "largest"}},
                    {"id": "part-0", "type": "partition", "size": -1},
                ]
            }
        }
        copied = copy.deepcopy(original)
        _ = curtin_storage_input(original)
        assert original == copied
