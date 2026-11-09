"""Unit tests for autoinstall_helper.snapstore.

External boundaries (urllib, shutil.which, subprocess.run) are mocked at the
point of use, per AGENTS.md.
"""

import json
import urllib.error
from types import SimpleNamespace

import pytest

from autoinstall_helper import snapstore


class _FakeResponse:
    """Minimal context-manager response for urllib.request.urlopen."""

    def __init__(self, payload: str):
        self._data = payload.encode("utf-8")

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _json_response(data: dict) -> _FakeResponse:
    return _FakeResponse(json.dumps(data))


class TestGetSnapVersionFromApi:
    """Tests for _get_snap_version_from_api(snap_name, channel)."""

    def test_matching_architecture_wins(self, monkeypatch):
        data = {
            "channel-map": [
                {"channel": {"name": "24.04/stable", "architecture": "arm64"}, "version": "1.0"},
                {"channel": {"name": "24.04/stable", "architecture": "amd64"}, "version": "2.0"},
            ]
        }
        monkeypatch.setattr(snapstore.platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(snapstore.urllib.request, "urlopen", lambda req, timeout=15: _json_response(data))

        assert snapstore._get_snap_version_from_api("ubuntu-desktop-bootstrap", "24.04/stable") == "2.0"

    def test_missing_architecture_falls_back_to_first(self, monkeypatch):
        data = {
            "channel-map": [
                {"channel": {"name": "24.04/stable", "architecture": "arm64"}, "version": "1.0"},
            ]
        }
        monkeypatch.setattr(snapstore.platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(snapstore.urllib.request, "urlopen", lambda req, timeout=15: _json_response(data))

        assert snapstore._get_snap_version_from_api("ubuntu-desktop-bootstrap", "24.04/stable") == "1.0"

    def test_channel_without_entries_returns_none(self, monkeypatch):
        data = {"channel-map": [{"channel": {"name": "latest/edge", "architecture": "amd64"}, "version": "9.9"}]}
        monkeypatch.setattr(snapstore.platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(snapstore.urllib.request, "urlopen", lambda req, timeout=15: _json_response(data))

        assert snapstore._get_snap_version_from_api("ubuntu-desktop-bootstrap", "24.04/stable") is None

    def test_network_exception_returns_none(self, monkeypatch):
        def _boom(req, timeout=15):
            raise OSError("network down")

        monkeypatch.setattr(snapstore.urllib.request, "urlopen", _boom)

        assert snapstore._get_snap_version_from_api("ubuntu-desktop-bootstrap", "24.04/stable") is None


class TestGetSnapVersion:
    """Tests for get_snap_version(channel)."""

    def test_api_hit_short_circuits_snap_cli(self, monkeypatch):
        monkeypatch.setattr(snapstore, "_get_snap_version_from_api", lambda name, channel: "1.2.3")

        def _fail(*args, **kwargs):
            raise AssertionError("snap CLI / subprocess must not be used when the API succeeds")

        monkeypatch.setattr(snapstore.shutil, "which", _fail)
        monkeypatch.setattr(snapstore.subprocess, "run", _fail)

        assert snapstore.get_snap_version("24.04/stable") == "1.2.3"

    def test_falls_back_to_snap_cli(self, monkeypatch):
        monkeypatch.setattr(snapstore, "_get_snap_version_from_api", lambda name, channel: None)
        monkeypatch.setattr(snapstore.shutil, "which", lambda name: "/usr/bin/snap")
        monkeypatch.setattr(
            snapstore.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(stdout="  24.04/stable: 1.2.3 2024-01-01\n"),
        )

        assert snapstore.get_snap_version("24.04/stable") == "1.2.3"

    def test_no_snap_available_exits(self, monkeypatch):
        monkeypatch.setattr(snapstore, "_get_snap_version_from_api", lambda name, channel: None)
        monkeypatch.setattr(snapstore.shutil, "which", lambda name: None)

        with pytest.raises(SystemExit, match="Could not determine the snap version"):
            snapstore.get_snap_version("24.04/stable")

    def test_unpublished_channel_exits(self, monkeypatch):
        monkeypatch.setattr(snapstore, "_get_snap_version_from_api", lambda name, channel: None)
        monkeypatch.setattr(snapstore.shutil, "which", lambda name: "/usr/bin/snap")
        monkeypatch.setattr(
            snapstore.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(stdout="  24.04/stable: -- --\n"),
        )

        with pytest.raises(SystemExit, match="has no published version"):
            snapstore.get_snap_version("24.04/stable")

    @pytest.mark.parametrize(
        "invalid_channel",
        [
            "",
            "bad channel",
            "channel with spaces/stable",
            "invalid;chars",
        ],
    )
    def test_invalid_channel_name_exits(self, invalid_channel):
        with pytest.raises(SystemExit, match="Invalid channel name"):
            snapstore.get_snap_version(invalid_channel)


class TestGetBaseImage:
    """Tests for get_base_image(version)."""

    @pytest.mark.parametrize(
        ("base", "expected"),
        [
            ("core24", "ubuntu:24.04"),
            ("core22", "ubuntu:22.04"),
        ],
    )
    def test_base_mapping(self, monkeypatch, base, expected):
        monkeypatch.setattr(
            snapstore.urllib.request,
            "urlopen",
            lambda url, timeout=30: _FakeResponse(f"base: {base}\n"),
        )

        assert snapstore.get_base_image("2.0") == expected

    def test_second_url_used_after_404(self, monkeypatch):
        calls = {"count": 0}

        def _urlopen(url, timeout=30):
            calls["count"] += 1
            if calls["count"] == 1:
                raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
            return _FakeResponse("base: core24\n")

        monkeypatch.setattr(snapstore.urllib.request, "urlopen", _urlopen)

        assert snapstore.get_base_image("2.0") == "ubuntu:24.04"
        assert calls["count"] == 2

    def test_both_urls_404_exits(self, monkeypatch):
        def _urlopen(url, timeout=30):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

        monkeypatch.setattr(snapstore.urllib.request, "urlopen", _urlopen)

        with pytest.raises(SystemExit, match="snapcraft.yaml not found"):
            snapstore.get_base_image("2.0")

    @pytest.mark.parametrize(
        "invalid_base",
        [
            "not-a-core",
            "core",
            "core-dev",
        ],
    )
    def test_unexpected_base_exits(self, monkeypatch, invalid_base):
        monkeypatch.setattr(
            snapstore.urllib.request,
            "urlopen",
            lambda url, timeout=30: _FakeResponse(f"base: {invalid_base}\n"),
        )

        with pytest.raises(SystemExit, match="Unexpected 'base' value"):
            snapstore.get_base_image("2.0")
