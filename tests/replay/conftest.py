"""Fixtures for recorded-replay component tests (tests/replay/).

The tier is scaffolded: there are no committed recordings yet. Follow the
scenario plan in ``tests/replay/README.md`` to record real sessions and add the
matching tests here.
"""

import subprocess
import urllib.request
from pathlib import Path

import pytest

from autoinstall_helper import fsprobe
from autoinstall_helper.replayer import Replayer

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def replayed_session(request, monkeypatch):
    """Replay tests/replay/fixtures/<name>.recording.json for one test.

    The recording is selected with indirect parametrization:

        @pytest.mark.parametrize("replayed_session", ["flash-existing-target"], indirect=True)
        def test_flash_existing_target(replayed_session):
            ...  # subprocess/urllib/fsprobe calls return recorded data
    """
    name = request.param
    path = FIXTURES_DIR / f"{name}.recording.json"
    if not path.exists():
        pytest.fail(f"Recording fixture not found: {path}")

    replayer = Replayer(path)

    # All modules share the same objects, so one patch per boundary is enough
    # (subprocess: cidata, disk, docker_utils, flash, snapstore, vm;
    # fsprobe: flash, cidata, disk, cli, vm).
    monkeypatch.setattr(subprocess, "run", replayer.next_subprocess)
    monkeypatch.setattr(urllib.request, "urlopen", replayer.next_urlopen)
    monkeypatch.setattr(fsprobe, "probe", replayer.next_probe)

    yield replayer

    replayer.assert_exhausted()
