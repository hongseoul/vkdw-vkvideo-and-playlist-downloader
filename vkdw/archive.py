"""Where each downloaded video was saved, by its VK id.

The check «already saved» looks for the file under the name a download would give it
now, in the folder chosen now. The archive also finds a video that was renamed in VK or
saved to another folder. It never forbids a download: once the file is deleted or moved
away, the video counts as not saved and downloads again.

Kept in %LOCALAPPDATA%\\VKDW\\archive.json as {"owner_video": "full path"}.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from vkdw.settings import write_atomic


def archive_path() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(root) / "VKDW" / "archive.json"


def short_id(raw_id: str) -> str:
    """owner_video without the access key."""
    return "_".join(raw_id.split("_")[:2])


class Archive:
    """Written by the download thread, read by the window; path None keeps it in memory only."""

    def __init__(self, path: Path | None = None):
        self._path = path
        self._lock = threading.Lock()
        self._files: dict[str, str] = {}
        if path is not None:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if isinstance(data, dict):
                self._files = {str(key): str(value) for key, value in data.items() if isinstance(value, str)}

    def add(self, raw_id: str, path: Path) -> None:
        key, value = short_id(raw_id), str(Path(path).resolve())
        with self._lock:
            if self._files.get(key) == value:
                return
            self._files[key] = value
            text = json.dumps(self._files, ensure_ascii=False, separators=(",", ":"))
        if self._path is not None:
            write_atomic(self._path, text)

    def where(self, raw_id: str) -> Path | None:
        """The saved file of the video, if it is still there and not empty."""
        with self._lock:
            value = self._files.get(short_id(raw_id))
        if not value:
            return None
        path = Path(value)
        try:
            return path if path.is_file() and path.stat().st_size > 0 else None
        except OSError:
            return None

    def found(self, items, skip=()) -> dict[str, Path]:
        """raw_id -> file, for the given videos the archive still finds; ids in skip are not checked."""
        skip = set(skip)
        found = {}
        for item in items:
            if item.raw_id in skip:
                continue
            path = self.where(item.raw_id)
            if path is not None:
                found[item.raw_id] = path
        return found
