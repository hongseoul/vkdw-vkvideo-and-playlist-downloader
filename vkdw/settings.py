"""Saved choices: folder, quality, theme, language, window position."""

from __future__ import annotations

import json
import os
from pathlib import Path

from vkdw.i18n import tr


def settings_path() -> Path:
    root = os.environ.get("APPDATA") or str(Path.home())
    return Path(root) / "VKDW" / "settings.json"


def default_folder() -> Path:
    videos = Path.home() / "Videos"
    if videos.is_dir():
        return videos / "VK"
    return Path.home() / "Downloads" / "VK"


def load_settings() -> dict:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("quality") == "Лучшее":
        data["quality"] = "best"  # saved by versions that kept the Russian label
    return data


def write_atomic(path: Path, text: str) -> bool:
    """Replace the file in one step: a crash mid-write leaves the old file, never half of the new one."""
    temp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except OSError:
        return False
    return True


def save_settings(data: dict) -> None:
    write_atomic(settings_path(), json.dumps(data, ensure_ascii=False, indent=2))


def fmt_size(num: float) -> str:
    """1,5 МБ in Russian, 1.5 MB in English."""
    value = float(num)
    units = tr("size.units").split()
    point = tr("size.point")
    for position, unit in enumerate(units):
        if value < 1024 or position == len(units) - 1:
            if position == 0:
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}".replace(".", point)
        value /= 1024
    return f"{value:.1f} {units[-1]}".replace(".", point)
