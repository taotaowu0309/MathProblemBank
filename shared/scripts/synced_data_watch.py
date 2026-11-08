"""Detect subject data files that Syncthing replaces while the control center runs.

Subject databases and PDFs sync both ways between the Mac and Windows through
Syncthing.  The control center opens SQLite per query, so
replaced files are readable at once; this module only decides *when* a file
has settled on a new version so the UI can reload it.
"""

from __future__ import annotations

import os
from pathlib import Path

FileSignature = tuple[int, int] | None


def file_signature(path: Path) -> FileSignature:
    """Return ``(mtime_ns, size)``, or ``None`` when the file is missing."""

    try:
        stat = os.stat(path)
    except OSError:
        return None
    return int(stat.st_mtime_ns), int(stat.st_size)


class SettledChangeTracker:
    """Report a file as changed once its new signature holds for two polls.

    The first poll of a path only records a baseline.  A changed signature is
    held as pending and confirmed on the next poll if it is still the same, so
    a file caught in the middle of a write is not reloaded half-finished.
    """

    def __init__(self) -> None:
        self._baseline: dict[str, FileSignature] = {}
        self._pending: dict[str, FileSignature] = {}

    @staticmethod
    def _key(path: Path) -> str:
        return str(Path(path).resolve())

    def poll(self, paths: list[Path]) -> list[Path]:
        changed: list[Path] = []
        for path in paths:
            key = self._key(path)
            signature = file_signature(Path(key))
            if key not in self._baseline:
                self._baseline[key] = signature
                continue
            if signature == self._baseline[key]:
                self._pending.pop(key, None)
                continue
            if signature is not None and self._pending.get(key) == signature:
                self._baseline[key] = signature
                self._pending.pop(key, None)
                changed.append(Path(key))
                continue
            self._pending[key] = signature
        return changed
