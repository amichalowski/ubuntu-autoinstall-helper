#!/usr/bin/env python3
"""Run Subiquity autoinstall and Curtin storage validation."""

import argparse
import asyncio
import copy
import importlib
import importlib.util
import subprocess
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any

import jsonschema
import yaml


ROOT = Path(__file__).resolve().parents[1]
CURTIN_ROOT = ROOT / "curtin"
SUBIQUITY_VALIDATOR_PATH = Path(__file__).with_name(
    "validate-autoinstall-user-data.py"
)


def load_subiquity_validator():
    spec = importlib.util.spec_from_file_location(
        "subiquity_user_data_validator", SUBIQUITY_VALIDATOR_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load Subiquity's autoinstall validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_storage_input(ai_data: dict[str, Any]) -> list[str]:
    """Reject storage actions or fields that Subiquity/Curtin would drop."""
    from curtin import storage_config as curtin_storage

    for module_name in (
        "subiquity.models.storage",
        "subiquity.models.filesystem",
    ):
        try:
            subiquity_storage = importlib.import_module(module_name)
            break
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                raise
    else:
        raise RuntimeError("Could not find Subiquity's storage model module")

    storage = ai_data.get("storage")
    if storage is None:
        return []
    if not isinstance(storage, dict):
        return ["autoinstall.storage must be a mapping"]

    errors = []
    supported_storage_keys = {"config", "layout", "swap", "grub"}
    for key in sorted(storage.keys() - supported_storage_keys, key=str):
        if key == "accepted-errors":
            errors.append(
                "autoinstall.storage.accepted-errors is only supported inside "
                "storage.layout for the hybrid layout"
            )
        else:
            errors.append(f"Unsupported autoinstall.storage key: {key}")

    if "config" in storage and "layout" in storage:
        errors.append(
            "autoinstall.storage cannot contain both 'config' and 'layout'; "
            "Subiquity ignores 'config' when 'layout' is present"
        )
    if "layout" in storage:
        layout = storage["layout"]
        if not isinstance(layout, dict):
            errors.append("autoinstall.storage.layout must be a mapping")
        elif not isinstance(layout.get("name"), str) or layout["name"] not in {
            "direct", "dd", "hybrid", "lvm", "zfs"
        }:
            errors.append(
                "autoinstall.storage.layout.name must be one of: "
                "direct, dd, hybrid, lvm, zfs"
            )

    actions = storage.get("config", [])
    if not isinstance(actions, list):
        errors.append("autoinstall.storage.config must be a list")
        return errors

    action_ids: dict[str, tuple[int, dict[str, Any]]] = {}
    action_schemas = curtin_storage.STORAGE_CONFIG_TYPES
    subiquity_action_types = subiquity_storage._type_to_cls

    for index, action in enumerate(actions):
        prefix = f"autoinstall.storage.config[{index}]"
        if not isinstance(action, dict):
            errors.append(f"{prefix} must be a mapping")
            continue

        action_type = action.get("type")
        if not isinstance(action_type, str):
            errors.append(f"{prefix}.type must be a string")
            continue
        if action_type not in subiquity_action_types:
            errors.append(
                f"{prefix}.type={action_type!r} is not supported by Subiquity's "
                "storage model; the action would be ignored"
            )
            continue
        if action_type not in action_schemas:
            errors.append(
                f"{prefix}.type={action_type!r} has no Curtin storage schema"
            )
            continue

        schema = action_schemas[action_type].schema
        curtin_keys = set(schema.get("properties", {}))
        model_fields = getattr(
            subiquity_action_types[action_type], "__attrs_attrs__", ()
        )
        allowed_keys = {
            field.name.lstrip("_")
            for field in model_fields
            if field.name != "_m" and not field.metadata.get("for_api", False)
        }
        if action_type == "disk":
            # Autoinstall extends Curtin disk selection with a match spec.
            allowed_keys.add("match")
        for key in sorted(action.keys() - allowed_keys, key=str):
            errors.append(
                f"Unsupported field {prefix}.{key} for action type "
                f"{action_type!r}; Subiquity may discard it"
            )

        schema_input = {
            key: value for key, value in action.items() if key in curtin_keys
        }
        schema_to_validate = copy.deepcopy(schema)
        if action_type == "disk":
            schema_input.pop("match", None)
            if not any(key in schema_input for key in ("path", "serial", "wwn")):
                # Subiquity resolves match specs to a concrete disk path.
                schema_input["path"] = "/dev/autoinstall-match"
        if action_type in ("partition", "lvm_partition"):
            size = schema_input.get("size")
            if size == -1 or (isinstance(size, str) and size.endswith("%")):
                # Subiquity resolves the remaining-space and percentage extensions.
                schema_input["size"] = 1
        if action_type == "dm_crypt" and "dm_name" not in schema_input:
            # Curtin defaults dm_name to the action id when omitted.
            schema_input["dm_name"] = action.get("id")
        for validation_error in jsonschema.Draft7Validator(
            schema_to_validate
        ).iter_errors(schema_input):
            errors.append(
                f"Invalid Curtin-compatible action at {prefix}: "
                f"{validation_error.message}"
            )

        action_id = action.get("id")
        if not isinstance(action_id, str) or not action_id:
            errors.append(f"{prefix}.id must be a non-empty string")
        elif action_id in action_ids:
            errors.append(
                f"Duplicate storage action id {action_id!r} at {prefix} "
                f"(first declared at index {action_ids[action_id][0]})"
            )
        else:
            action_ids[action_id] = (index, action)

    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            continue
        action_type = action.get("type")
        if action_type not in action_schemas:
            continue
        action_id = action.get("id", f"<index {index}>")
        for dep_key in curtin_storage._stype_to_deps(action_type):
            dep_value = action.get(dep_key)
            if dep_value is None:
                continue
            dep_ids = dep_value if isinstance(dep_value, list) else [dep_value]
            for dep_id in dep_ids:
                if not isinstance(dep_id, str) or dep_id not in action_ids:
                    errors.append(
                        f"Storage action {action_id!r} references missing "
                        f"{dep_key} id {dep_id!r}"
                    )
                    continue
                dep_index, _ = action_ids[dep_id]
                if dep_index >= index:
                    errors.append(
                        f"Storage action {dep_id!r} must appear before "
                        f"{action_id!r} (dependency {dep_key})"
                    )
                try:
                    curtin_storage._validate_dep_type(
                        action_id, dep_key, dep_id,
                        OrderedDict(
                            (known_id, value[1])
                            for known_id, value in action_ids.items()
                        ),
                    )
                except (KeyError, ValueError) as exc:
                    errors.append(f"Invalid storage dependency: {exc}")

    for index, action in enumerate(actions):
        if not isinstance(action, dict) or action.get("size") not in (-1, "-1"):
            continue
        action_type = action.get("type")
        parent_key = {
            "partition": "device",
            "lvm_partition": "volgroup",
        }.get(action_type)
        if parent_key is None:
            continue
        parent_id = action.get(parent_key)
        if any(
            isinstance(later, dict)
            and later.get("type") == action_type
            and later.get(parent_key) == parent_id
            for later in actions[index + 1 :]
        ):
            errors.append(
                f"Storage action {action.get('id')!r} uses size -1 but is not "
                f"the final {action_type} on {parent_key} {parent_id!r}"
            )

    has_root_mount = any(
        isinstance(action, dict)
        and (
            (action.get("type") == "mount" and action.get("path") == "/")
            or (
                action.get("type") == "zpool"
                and action.get("mountpoint") == "/"
            )
            or (
                action.get("type") == "zfs"
                and isinstance(action.get("properties"), dict)
                and action["properties"].get("mountpoint") == "/"
            )
        )
        for action in actions
    )
    if actions and not has_root_mount:
        errors.append(
            "autoinstall.storage.config does not define a root mount at '/'"
        )

    return errors


def storage_only_data(ai_data: dict[str, Any]) -> dict[str, Any]:
    """Keep the context Subiquity needs while avoiding unrelated sections."""
    data = {
        "version": ai_data.get("version", 1),
        "interactive-sections": ai_data.get("interactive-sections", ["identity"]),
    }
    for key in ("source", "storage"):
        if key in ai_data:
            data[key] = ai_data[key]
    return data


def curtin_storage_input(ai_data: dict[str, Any]) -> dict[str, Any] | None:
    """Project explicit autoinstall actions into Curtin's static schema shape."""
    storage = ai_data.get("storage")
    if not isinstance(storage, dict) or "config" not in storage:
        return None

    actions = copy.deepcopy(storage["config"])
    if not isinstance(actions, list):
        return None

    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = action.get("type")
        if action_type == "disk":
            action.pop("match", None)
            if not any(key in action for key in ("path", "serial", "wwn")):
                # Disk identity is resolved by Subiquity against installer probes.
                action["path"] = "/dev/autoinstall-match"
        if action_type in ("partition", "lvm_partition"):
            size = action.get("size")
            if size == -1 or (isinstance(size, str) and size.endswith("%")):
                # These Subiquity sizes need disk geometry; use a schema-only value.
                action["size"] = "1B"
        if action_type == "dm_crypt" and "dm_name" not in action:
            action["dm_name"] = action.get("id")

    return {"storage": {"version": 1, "config": actions}}


async def validate(
    ai_data: dict[str, Any], mode: str, verbosity: int, validator
) -> int:
    check_storage = mode in ("storage", "full")
    input_errors = validate_storage_input(ai_data) if check_storage else []

    for error in input_errors:
        print(f"Storage input error: {error}", file=sys.stderr)

    app = await validator.make_app()
    validation_data = storage_only_data(ai_data) if mode == "storage" else ai_data
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "autoinstall.yaml"
        config_path.write_text(yaml.safe_dump(validation_data), encoding="utf-8")
        print(f"[{mode}] Running Subiquity dry-run validation...")
        subiquity_result = await validator.verify_autoinstall(
            app=app,
            cfg_path=str(config_path),
            verbosity=verbosity,
        )

        if subiquity_result != 0:
            return 1
        if not check_storage:
            return 0
        if input_errors:
            print("[storage] Curtin validation skipped due to input errors.")
            return 1

        rendered = curtin_storage_input(ai_data)
        if rendered is None:
            if isinstance(ai_data.get("storage"), dict) and "layout" in ai_data["storage"]:
                reason = "layout-based storage is expanded by Subiquity at install time"
            else:
                reason = "there is no explicit storage.config action list"
            print(f"[storage] Curtin schema validation skipped: {reason}.")
            return 0

        curtin_config_path = Path(td) / "curtin-storage.yaml"
        curtin_config_path.write_text(
            yaml.safe_dump(rendered), encoding="utf-8"
        )
        command = [
            str(CURTIN_ROOT / "bin" / "curtin"),
            "schema-validate",
            "--storage",
            "--config",
            str(curtin_config_path),
        ]
        print("[storage] Running Curtin schema validation...")
        result = subprocess.run(command, cwd=ROOT, check=False, text=True)
        if result.returncode != 0:
            print(f"[storage] Curtin schema validation failed ({result.returncode}).")
            return 1

    return 0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=argparse.FileType("r"), default="-")
    parser.add_argument(
        "--validation",
        choices=("autoinstall", "storage", "full"),
        default="full",
        help="Validation scope (default: full)",
    )
    parser.add_argument(
        "--no-expect-cloudconfig",
        dest="expect_cloudconfig",
        action="store_false",
        default=True,
        help="Assume the data is not wrapped in cloud-config.",
    )
    parser.add_argument("-v", "--verbosity", action="count", default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validator = load_subiquity_validator()
    try:
        ai_data = validator.parse_autoinstall(
            args.input.read(), args.expect_cloudconfig
        )
        if not isinstance(ai_data, dict):
            raise ValueError("autoinstall data must be a mapping")
    except Exception as exc:
        print(f"FAILURE: {exc}", file=sys.stderr)
        return 1

    try:
        return asyncio.run(
            validate(ai_data, args.validation, args.verbosity, validator)
        )
    except Exception as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
