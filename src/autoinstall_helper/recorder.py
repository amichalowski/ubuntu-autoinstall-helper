"""Record subprocess/network/filesystem interactions for dev-mode testing."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autoinstall_helper import fsprobe
from autoinstall_helper.fsprobe import PathInfo
from autoinstall_helper.interactions import (
    FakeResponse,
    decode,
    extract_headers,
    extract_method,
    extract_url,
)

SCHEMA_VERSION = 1

# Environment variable names whose *values* are never written to a recording.
SENSITIVE_ENV_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "API_KEY", "APIKEY")


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _os_release() -> dict[str, str]:
    """Return the /etc/os-release fields (distribution, version, codename).

    Uses the stdlib equivalent of ``lsb_release -a`` (Python >= 3.10), so a
    recording captures the distro release (e.g. Ubuntu 25.10 "questing") and not
    just the kernel reported by ``platform.platform()``.
    """
    try:
        return dict(platform.freedesktop_os_release())
    except OSError:
        return {}


def _env_diff(env: dict[str, str] | None) -> dict[str, str] | None:
    """Return the environment keys that differ from the current process, with secrets redacted."""
    if env is None or env is os.environ:
        return None
    diff = {key: value for key, value in env.items() if os.environ.get(key) != value}
    if not diff:
        return None
    return {
        key: ("<redacted>" if any(marker in key.upper() for marker in SENSITIVE_ENV_MARKERS) else value)
        for key, value in diff.items()
    }


def _describe_error(exc: BaseException) -> dict[str, Any]:
    """Capture just enough of an exception to reconstruct it during replay."""
    info: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)}
    if isinstance(exc, urllib.error.HTTPError):
        info["code"] = exc.code
        info["headers"] = dict(exc.headers or {})
    if isinstance(exc, subprocess.CalledProcessError):
        info["returncode"] = exc.returncode
    return info


class Recorder:
    """Accumulates interactions and writes them to a JSON file."""

    def __init__(self, output_path: Path) -> None:
        self._path = output_path
        self._interactions: list[dict[str, Any]] = []
        self._seq = 0
        self._original_subprocess_run: Any = None
        self._original_urlopen: Any = None
        self._original_probe: Any = None
        self.exit_code: int | None = None

    # -- recording --------------------------------------------------------

    def _record(self, entry: dict[str, Any]) -> None:
        entry["seq"] = self._seq
        self._seq += 1
        self._interactions.append(entry)

    def _wrap_subprocess_run(self, cmd, /, **kwargs):
        """Call the real ``subprocess.run``, record the interaction, return it unchanged."""
        captured = bool(
            kwargs.get("capture_output") or kwargs.get("stdout") is not None or kwargs.get("stderr") is not None
        )
        entry: dict[str, Any] = {
            "type": "subprocess",
            "timestamp": _now(),
            "command": list(map(str, cmd)),
            "cwd": str(kwargs["cwd"]) if kwargs.get("cwd") is not None else None,
            "env_diff": _env_diff(kwargs.get("env")),
            "text": bool(kwargs.get("text") or kwargs.get("universal_newlines")),
        }
        start = time.monotonic()
        try:
            result = self._original_subprocess_run(cmd, **kwargs)
        except BaseException as exc:
            entry["duration_ms"] = round((time.monotonic() - start) * 1000)
            entry["result"] = None
            entry["error"] = _describe_error(exc)
            self._record(entry)
            raise
        entry["duration_ms"] = round((time.monotonic() - start) * 1000)
        entry["result"] = {
            "returncode": result.returncode,
            "stdout": decode(result.stdout) if captured else None,
            "stderr": decode(result.stderr) if captured else None,
        }
        entry["error"] = None
        self._record(entry)
        return result

    def _wrap_urlopen(self, url_or_req, /, **kwargs):
        """Call the real ``urllib.request.urlopen``, record the interaction, return a replayable response."""
        entry: dict[str, Any] = {
            "type": "urlopen",
            "timestamp": _now(),
            "url": extract_url(url_or_req),
            "method": extract_method(url_or_req),
            "request_headers": extract_headers(url_or_req),
        }
        start = time.monotonic()
        try:
            response = self._original_urlopen(url_or_req, **kwargs)
            body = response.read()
            status = response.status
            headers = dict(response.headers)
        except BaseException as exc:
            entry["duration_ms"] = round((time.monotonic() - start) * 1000)
            entry["result"] = None
            entry["error"] = _describe_error(exc)
            self._record(entry)
            raise
        entry["duration_ms"] = round((time.monotonic() - start) * 1000)
        entry["result"] = {
            "status": status,
            "body": decode(body) or "",
            "response_headers": headers,
        }
        entry["error"] = None
        self._record(entry)
        response.close()
        return FakeResponse(body, status, headers)

    def _wrap_probe(self, path: Path) -> PathInfo:
        """Probe real filesystem metadata, record it, and return it unchanged."""
        info = self._original_probe(path)
        self._record(
            {
                "type": "fs",
                "timestamp": _now(),
                "op": "probe",
                "path": str(path),
                "result": dict(info._asdict()),
            }
        )
        return info

    # -- patch lifecycle --------------------------------------------------

    def patch(self) -> None:
        """Monkey-patch subprocess.run, urllib.request.urlopen and fsprobe.probe (idempotent)."""
        if self._original_subprocess_run is None:
            self._original_subprocess_run = subprocess.run
            self._original_urlopen = urllib.request.urlopen
            self._original_probe = fsprobe.probe
        subprocess.run = self._wrap_subprocess_run
        urllib.request.urlopen = self._wrap_urlopen
        fsprobe.probe = self._wrap_probe

    def unpatch(self) -> None:
        """Restore the originals captured by the first :meth:`patch`."""
        if self._original_subprocess_run is not None:
            subprocess.run = self._original_subprocess_run
            urllib.request.urlopen = self._original_urlopen
            fsprobe.probe = self._original_probe
            self._original_subprocess_run = None
            self._original_urlopen = None
            self._original_probe = None

    # -- persistence ------------------------------------------------------

    def save(self) -> Path:
        """Write accumulated interactions to the JSON file."""
        data = {
            "metadata": {
                "schema_version": SCHEMA_VERSION,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "argv": sys.argv[:],
                "python": platform.python_version(),
                "kernel": platform.platform(),
                "os_release": _os_release(),
                "exit_code": self.exit_code,
            },
            "interactions": self._interactions,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return self._path


@contextmanager
def recording_session(output_path: Path | str) -> Iterator[Recorder]:
    """Context manager: patch, yield the recorder, and save on exit."""
    rec = Recorder(Path(output_path))
    rec.patch()
    try:
        yield rec
    finally:
        rec.unpatch()
        saved = rec.save()
        print(f"[autoinstall-helper] Session recorded to {saved}", file=sys.stderr)
