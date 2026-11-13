"""Replay recorded interactions for deterministic testing."""

from __future__ import annotations

import json
import subprocess
import urllib.error
import warnings
from pathlib import Path
from typing import Any

from autoinstall_helper.fsprobe import PathInfo
from autoinstall_helper.interactions import FakeResponse, extract_url


class ReplayError(Exception):
    """Raised when replay encounters an unexpected or unmapped interaction."""


class Replayer:
    """Serves recorded interactions, matching by command/URL with ordered fallback."""

    def __init__(self, recording_path: Path) -> None:
        data = json.loads(recording_path.read_text(encoding="utf-8"))
        self.metadata = data["metadata"]
        self._interactions: list[dict[str, Any]] = data["interactions"]
        self._consumed: set[int] = set()

    # -- lookup -----------------------------------------------------------

    @staticmethod
    def _key_of(entry: dict[str, Any]) -> str | None:
        if entry["type"] == "subprocess":
            return "\x00".join(entry.get("command", []))
        if entry["type"] == "urlopen":
            return entry.get("url")
        if entry["type"] == "fs":
            return entry.get("path")
        return None

    def _take(self, kind: str, key: str | None) -> dict[str, Any]:
        for index, entry in enumerate(self._interactions):
            if index not in self._consumed and entry["type"] == kind and self._key_of(entry) == key:
                self._consumed.add(index)
                return entry
        # Desync recovery: fall back to the next unconsumed entry of the same kind.
        for index, entry in enumerate(self._interactions):
            if index not in self._consumed and entry["type"] == kind:
                warnings.warn(
                    f"Replay mismatch: no record for {kind} {key!r}; using seq={index} ({self._key_of(entry)!r})",
                    stacklevel=3,
                )
                self._consumed.add(index)
                return entry
        raise ReplayError(f"No recorded {kind} interaction left (requested {key!r})")

    def _raise_recorded_error(self, entry: dict[str, Any], *, kind: str) -> None:
        error = entry.get("error")
        if not error:
            return
        name = error.get("type", "")
        message = error.get("message", "")
        if name == "CalledProcessError":
            raise subprocess.CalledProcessError(error.get("returncode", 1), entry.get("command"), output=None)
        if name == "HTTPError":
            raise urllib.error.HTTPError(
                entry.get("url", ""), error.get("code", 0), message, error.get("headers", {}), None
            )
        if name == "URLError":
            raise urllib.error.URLError(message)
        if name == "FileNotFoundError":
            raise FileNotFoundError(message)
        if name == "TimeoutExpired":
            raise subprocess.TimeoutExpired(entry.get("command"), error.get("timeout"))
        raise ReplayError(f"Recorded {kind} error {name!r} cannot be reconstructed faithfully: {message}")

    # -- serving ----------------------------------------------------------

    def next_subprocess(self, cmd, /, **kwargs):
        """Return the recorded subprocess result (or re-raise the recorded error)."""
        entry = self._take("subprocess", "\x00".join(map(str, cmd)))
        self._raise_recorded_error(entry, kind="subprocess")

        result = entry.get("result") or {}
        returncode = result.get("returncode", 0)
        text_mode = bool(kwargs.get("text") or kwargs.get("universal_newlines"))
        stdout = result.get("stdout")
        stderr = result.get("stderr")
        if stdout is not None and not text_mode:
            stdout = stdout.encode("utf-8")
        if stderr is not None and not text_mode:
            stderr = stderr.encode("utf-8")

        if kwargs.get("check") and returncode != 0:
            raise subprocess.CalledProcessError(returncode, list(cmd), output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(list(cmd), returncode, stdout, stderr)

    def next_urlopen(self, url_or_req, /, **kwargs):
        """Return the recorded urllib response (or re-raise the recorded error)."""
        entry = self._take("urlopen", extract_url(url_or_req))
        self._raise_recorded_error(entry, kind="urlopen")

        result = entry.get("result") or {}
        return FakeResponse(
            body=result.get("body", "").encode("utf-8"),
            status=result.get("status", 200),
            headers=result.get("response_headers", {}),
        )

    def next_probe(self, path: Path) -> PathInfo:
        """Return the recorded filesystem metadata for ``path``."""
        entry = self._take("fs", str(path))
        self._raise_recorded_error(entry, kind="fs")
        return PathInfo(**entry["result"])

    def assert_exhausted(self) -> None:
        """Assert every recorded interaction was replayed."""
        remaining = [i for i in range(len(self._interactions)) if i not in self._consumed]
        if remaining:
            details = ", ".join(f"seq={i}({self._interactions[i]['type']})" for i in remaining)
            raise ReplayError(f"{len(remaining)} recorded interactions were not replayed: {details}")
