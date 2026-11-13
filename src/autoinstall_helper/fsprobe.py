"""Thin filesystem seam so dev-mode recording can capture path metadata.

Production code resolves path metadata through :func:`probe` so the recorder can
journal it. Only metadata is captured (existence, type, size) — never contents.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import NamedTuple


class PathInfo(NamedTuple):
    """Metadata about a path, as captured by :func:`probe`."""

    exists: bool
    is_file: bool
    is_dir: bool
    is_block: bool
    size: int


def probe(path: Path) -> PathInfo:
    """Return the metadata a recording needs to reproduce a path probe."""
    try:
        st = path.stat()
    except OSError:
        return PathInfo(exists=False, is_file=False, is_dir=False, is_block=False, size=0)
    return PathInfo(
        exists=True,
        is_file=stat.S_ISREG(st.st_mode),
        is_dir=stat.S_ISDIR(st.st_mode),
        is_block=stat.S_ISBLK(st.st_mode),
        size=st.st_size,
    )
