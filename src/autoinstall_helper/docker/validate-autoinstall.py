#!/usr/bin/env python3
"""Run Subiquity autoinstall and Curtin storage validation."""

import argparse
import asyncio
import copy
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CURTIN_ROOT = ROOT / "curtin"
SUBIQUITY_VALIDATOR_PATH = Path(__file__).with_name("validate-autoinstall-user-data.py")


def load_subiquity_validator():
    spec = importlib.util.spec_from_file_location("subiquity_user_data_validator", SUBIQUITY_VALIDATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load Subiquity's autoinstall validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_storage_structure(ai_data: dict[str, Any]) -> list[str]:
    """Check structural invariants that tools cannot catch gracefully.

    Validates only things that are:
    - tool-agnostic (no hardcoded Subiquity/Curtin version-specific lists), and
    - not checked cleanly by either subiquity dry-run or curtin schema-validate.

    Specifically, duplicate or missing action ids cause both tools to either
    crash with an opaque KeyError or silently produce wrong results, so we
    catch those early with a clear message.
    """
    storage = ai_data.get("storage")
    if storage is None:
        return []
    if not isinstance(storage, dict):
        return ["autoinstall.storage must be a mapping"]

    actions = storage.get("config")
    if actions is None:
        # layout-based or empty storage — subiquity handles it
        return []
    if not isinstance(actions, list):
        return ["autoinstall.storage.config must be a list"]

    errors = []
    seen: dict[str, int] = {}
    for index, action in enumerate(actions):
        prefix = f"autoinstall.storage.config[{index}]"
        if not isinstance(action, dict):
            errors.append(f"{prefix} must be a mapping")
            continue
        action_id = action.get("id")
        if not isinstance(action_id, str) or not action_id:
            errors.append(f"{prefix}.id must be a non-empty string")
        elif action_id in seen:
            errors.append(
                f"Duplicate storage action id {action_id!r} at {prefix} (first declared at index {seen[action_id]})"
            )
        else:
            seen[action_id] = index

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
    """Project explicit autoinstall actions into Curtin's static schema shape.

    Subiquity extends the Curtin storage config format with a few constructs
    that Curtin does not understand.  This function strips or normalises those
    extensions so the result can be passed directly to `curtin schema-validate`.

    Transformations applied:
    - disk.match is removed; if no concrete identity remains, a placeholder
      path is inserted so Curtin's required-field check still passes.
    - partition/lvm_partition size=-1 and size=N% are replaced with a valid
      literal size ("1B") because Curtin's schema requires size >= 1.
    - dm_crypt.dm_name is filled in from the action id when absent, matching
      Curtin's own defaulting behaviour.
    """
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


async def validate(ai_data: dict[str, Any], mode: str, verbosity: int, validator) -> int:
    check_storage = mode in ("storage", "full")
    structural_errors = validate_storage_structure(ai_data) if check_storage else []

    for error in structural_errors:
        print(f"Storage structure error: {error}", file=sys.stderr)

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
        if structural_errors:
            print("[storage] Curtin validation skipped due to structural errors.")
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
        curtin_config_path.write_text(yaml.safe_dump(rendered), encoding="utf-8")
        command = [
            str(CURTIN_ROOT / "bin" / "curtin"),
            "schema-validate",
            "--storage",
            "--config",
            str(curtin_config_path),
        ]
        print("[storage] Running Curtin schema validation...")
        result = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if result.stdout:
            print(result.stdout, end="")
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
        ai_data = validator.parse_autoinstall(args.input.read(), args.expect_cloudconfig)
        if not isinstance(ai_data, dict):
            raise TypeError("autoinstall data must be a mapping")
    except Exception as exc:
        print(f"FAILURE: {exc}", file=sys.stderr)
        return 1

    try:
        return asyncio.run(validate(ai_data, args.validation, args.verbosity, validator))
    except Exception as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
