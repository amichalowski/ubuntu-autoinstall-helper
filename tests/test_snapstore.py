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
        monkeypatch.setattr(snapstore.time, "sleep", lambda seconds: None)

        assert snapstore._get_snap_version_from_api("ubuntu-desktop-bootstrap", "24.04/stable") is None

    def test_bare_risk_resolves_through_the_default_track(self, monkeypatch):
        data = _snap_info([{"channel": {"name": "6/stable", "architecture": "amd64"}, "version": "6.1"}], "6")
        _install_store(monkeypatch, {"ubuntu-desktop-bootstrap": data})
        monkeypatch.setattr(snapstore.platform, "machine", lambda: "x86_64")

        assert snapstore._get_snap_version_from_api("ubuntu-desktop-bootstrap", "stable") == "6.1"


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


class TestSupportsEmbeddedValidator:
    """Tests for supports_embedded_validator(version)."""

    @pytest.mark.parametrize("version", ["26.04", "26.04.1", "26.10", "27.04.2"])
    def test_supported_versions(self, version):
        assert snapstore.supports_embedded_validator(version) is True

    @pytest.mark.parametrize("version", ["24.04.4.1", "25.10", "26.03"])
    def test_older_versions_are_rejected(self, version):
        assert snapstore.supports_embedded_validator(version) is False

    @pytest.mark.parametrize("version", ["", "garbage", "26", "v26.04", "26.x"])
    def test_unparsable_versions_are_rejected(self, version):
        assert snapstore.supports_embedded_validator(version) is False


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


def _snap_info(channel_map: list[dict], default_track: str | None = None) -> dict:
    return {"default-track": default_track, "channel-map": channel_map}


def _channel(name: str, confinement: str = "strict", architecture: str = "amd64") -> dict:
    return {"channel": {"name": name, "architecture": architecture}, "confinement": confinement}


def _install_store(monkeypatch, responses: dict[str, object]):
    """Serve urlopen responses keyed by the snap name encoded in the request URL."""
    calls: list[str] = []

    def fake_urlopen(request, timeout=15):
        calls.append(request.full_url)
        name = request.full_url.split("/snaps/info/", 1)[1].split("?", 1)[0]
        value = responses[name]
        if isinstance(value, BaseException):
            raise value
        return _json_response(value)

    monkeypatch.setattr(snapstore.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(snapstore.time, "sleep", lambda seconds: None)
    return calls


class TestParseChannel:
    """Tests for parse_channel(channel)."""

    @pytest.mark.parametrize(
        ("channel", "expected"),
        [
            ("stable", snapstore.SnapChannel(None, "stable", None)),
            ("edge", snapstore.SnapChannel(None, "edge", None)),
            ("24.04/stable", snapstore.SnapChannel("24.04", "stable", None)),
            ("latest/beta", snapstore.SnapChannel("latest", "beta", None)),
            ("latest/stable/hotfix-1", snapstore.SnapChannel("latest", "stable", "hotfix-1")),
        ],
    )
    def test_valid_channels(self, channel, expected):
        assert snapstore.parse_channel(channel) == expected

    @pytest.mark.parametrize(
        "channel",
        [
            "",
            "stable/",
            "/stable",
            "latest/prerelease",
            "latest/stable/hotfix/extra",
            "latest/stable/bad_branch",
            "bad track/stable",
        ],
    )
    def test_invalid_channels(self, channel):
        assert snapstore.parse_channel(channel) is None


class TestChannelNames:
    """Tests for channel_names(channel, default_track)."""

    def test_risk_only_uses_latest_when_no_default_track(self):
        channel = snapstore.SnapChannel(None, "stable", None)
        assert snapstore.channel_names(channel, None) == {"stable", "latest/stable"}

    def test_risk_only_uses_the_snap_default_track(self):
        channel = snapstore.SnapChannel(None, "stable", None)
        assert snapstore.channel_names(channel, "6") == {"stable", "6/stable"}

    def test_default_track_also_has_a_bare_risk_alias(self):
        channel = snapstore.SnapChannel("6", "stable", None)
        assert snapstore.channel_names(channel, "6") == {"6/stable", "stable"}

    def test_non_default_track_is_explicit_only(self):
        channel = snapstore.SnapChannel("latest", "stable", None)
        assert snapstore.channel_names(channel, "6") == {"latest/stable"}


class TestCollectSnaps:
    """Tests for collect_snaps(entries)."""

    def test_no_snaps_section(self):
        assert snapstore.collect_snaps(None) == ([], [])

    def test_snaps_not_a_list(self):
        assert snapstore.collect_snaps("firefox") == ([], ["autoinstall.snaps must be a list"])

    def test_entry_not_a_mapping(self):
        assert snapstore.collect_snaps(["firefox"]) == ([], ["snaps[0] must be a mapping"])

    def test_missing_name(self):
        assert snapstore.collect_snaps([{"channel": "stable"}]) == (
            [],
            ["snaps[0].name must be a non-empty string"],
        )

    def test_invalid_name(self):
        assert snapstore.collect_snaps([{"name": "Firefox"}]) == (
            [],
            ["snaps[0].name 'Firefox' is not a valid snap name"],
        )

    def test_name_with_path_separator_is_rejected(self):
        errors = snapstore.collect_snaps([{"name": "firefox/evil"}])[1]
        assert errors == ["snaps[0].name 'firefox/evil' is not a valid snap name"]

    def test_channel_not_a_string(self):
        assert snapstore.collect_snaps([{"name": "firefox", "channel": 24}]) == (
            [],
            ["snaps[0].channel must be a string"],
        )

    def test_invalid_channel(self):
        errors = snapstore.collect_snaps([{"name": "firefox", "channel": "latest/prerelease"}])[1]
        assert errors and "is not a valid snap channel" in errors[0]

    def test_classic_not_a_boolean(self):
        assert snapstore.collect_snaps([{"name": "code", "classic": "yes"}]) == (
            [],
            ["snaps[0].classic must be a boolean"],
        )

    def test_defaults_and_multiple_entries(self):
        snaps, errors = snapstore.collect_snaps(
            [
                {"name": "firefox"},
                {"name": "code", "channel": "stable", "classic": True},
            ]
        )
        assert errors == []
        assert [(snap.label, snap.name, snap.channel, snap.classic) for snap in snaps] == [
            ("snaps[0]", "firefox", "stable", False),
            ("snaps[1]", "code", "stable", True),
        ]


class TestFetchSnapInfo:
    """Tests for fetch_snap_info(name)."""

    def test_not_found_returns_none(self, monkeypatch):
        _install_store(monkeypatch, {"missing": urllib.error.HTTPError("url", 404, "Not Found", None, None)})
        assert snapstore.fetch_snap_info("missing") is None

    def test_transient_rate_limit_is_retried(self, monkeypatch):
        calls = []

        def fake_urlopen(request, timeout=15):
            calls.append(request.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError("url", 429, "Too Many Requests", None, None)
            return _json_response(_snap_info([_channel("stable")]))

        monkeypatch.setattr(snapstore.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(snapstore.time, "sleep", lambda seconds: None)

        info = snapstore.fetch_snap_info("firefox")
        assert info["channel-map"][0]["channel"]["name"] == "stable"
        assert len(calls) == 2

    def test_persistent_network_error_raises(self, monkeypatch):
        calls = _install_store(monkeypatch, {"firefox": urllib.error.URLError("DNS failure")})
        with pytest.raises(snapstore.SnapStoreError, match="could not query the Snap Store"):
            snapstore.fetch_snap_info("firefox")
        assert len(calls) == snapstore.SNAP_STORE_MAX_ATTEMPTS

    def test_request_asks_for_all_fields_used_downstream(self, monkeypatch):
        # Without `version` the API drops it from the channel map, which would
        # silently break the installer-channel version resolution.
        calls = _install_store(monkeypatch, {"firefox": _snap_info([_channel("stable")])})
        snapstore.fetch_snap_info("firefox")
        assert "fields=channel-map,confinement,version" in calls[0]


class TestValidateSnaps:
    """End-to-end assembly of the snap layer (urllib mocked)."""

    def test_no_snaps_is_clean_without_network(self, monkeypatch, capsys):
        calls = _install_store(monkeypatch, {})
        assert snapstore.validate_snaps([]) is True
        assert calls == []
        assert "No snaps declared" in capsys.readouterr().out

    def test_strict_snap_without_classic_passes(self, monkeypatch, capsys):
        _install_store(monkeypatch, {"firefox": _snap_info([_channel("stable", "strict")])})
        assert snapstore.validate_snaps([{"name": "firefox"}]) is True
        assert "Snap declarations are valid." in capsys.readouterr().out

    def test_classic_snap_with_classic_flag_passes(self, monkeypatch):
        _install_store(monkeypatch, {"code": _snap_info([_channel("stable", "classic")])})
        assert snapstore.validate_snaps([{"name": "code", "classic": True}]) is True

    def test_classic_snap_without_classic_flag_fails(self, monkeypatch, capsys):
        _install_store(monkeypatch, {"code": _snap_info([_channel("stable", "classic")])})
        assert snapstore.validate_snaps([{"name": "code"}]) is False
        out = capsys.readouterr().out
        assert "is a classic snap on 'stable'" in out
        assert "set 'classic: true'" in out

    def test_strict_snap_with_classic_flag_fails(self, monkeypatch, capsys):
        _install_store(monkeypatch, {"firefox": _snap_info([_channel("stable", "strict")])})
        assert snapstore.validate_snaps([{"name": "firefox", "classic": True}]) is False
        out = capsys.readouterr().out
        assert "is not a classic snap on 'stable'" in out
        assert "remove 'classic: true'" in out

    def test_missing_snap_fails(self, monkeypatch, capsys):
        _install_store(monkeypatch, {"nope": urllib.error.HTTPError("url", 404, "Not Found", None, None)})
        assert snapstore.validate_snaps([{"name": "nope"}]) is False
        assert "does not exist in the Snap Store" in capsys.readouterr().out

    def test_missing_channel_lists_published_channels(self, monkeypatch, capsys):
        _install_store(
            monkeypatch,
            {"firefox": _snap_info([_channel("stable", "strict"), _channel("beta", "strict")])},
        )
        assert snapstore.validate_snaps([{"name": "firefox", "channel": "24.04/stable"}]) is False
        out = capsys.readouterr().out
        assert "channel '24.04/stable' is not published" in out
        assert "published channels: beta, stable" in out

    def test_bare_risk_matches_the_default_track(self, monkeypatch):
        info = _snap_info([_channel("6/stable", "strict")], default_track="6")
        _install_store(monkeypatch, {"lxd": info})
        assert snapstore.validate_snaps([{"name": "lxd", "channel": "stable"}]) is True

    def test_explicit_track_outside_the_default_is_rejected(self, monkeypatch, capsys):
        info = _snap_info([_channel("6/stable", "strict")], default_track="6")
        _install_store(monkeypatch, {"lxd": info})
        assert snapstore.validate_snaps([{"name": "lxd", "channel": "latest/stable"}]) is False
        assert "is not published" in capsys.readouterr().out

    def test_branch_channel_checks_the_base_channel(self, monkeypatch, capsys):
        _install_store(monkeypatch, {"firefox": _snap_info([_channel("stable", "strict")])})
        assert snapstore.validate_snaps([{"name": "firefox", "channel": "latest/stable/hotfix-1"}]) is True
        assert "cannot be verified" in capsys.readouterr().out

    def test_missing_confinement_warns_and_skips_the_classic_check(self, monkeypatch, capsys):
        _install_store(monkeypatch, {"firefox": _snap_info([{"channel": {"name": "stable"}}])})
        assert snapstore.validate_snaps([{"name": "firefox", "classic": True}]) is True
        assert "confinement of 'firefox' on 'stable' is unknown" in capsys.readouterr().out

    def test_one_request_per_snap_name(self, monkeypatch):
        calls = _install_store(monkeypatch, {"firefox": _snap_info([_channel("stable", "strict")])})
        data = [{"name": "firefox"}, {"name": "firefox", "channel": "candidate"}]
        assert snapstore.validate_snaps(data) is False  # candidate is not published
        assert len(calls) == 1

    def test_structural_error_fails_without_requests(self, monkeypatch, capsys):
        calls = _install_store(monkeypatch, {})
        assert snapstore.validate_snaps([{"channel": "stable"}]) is False
        assert calls == []
        assert "Structure error" in capsys.readouterr().out

    def test_store_failure_fails_loudly(self, monkeypatch, capsys):
        _install_store(monkeypatch, {"firefox": urllib.error.URLError("offline")})
        assert snapstore.validate_snaps([{"name": "firefox"}]) is False
        assert "could not query the Snap Store" in capsys.readouterr().out
