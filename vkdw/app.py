"""Window for downloading VK Video playlists one after another."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

import vkdw.ui  # noqa: F401  (loads QFluentWidgets quietly)
from vkdw import i18n
from vkdw.archive import archive_path
from vkdw.session import session_path
from vkdw.settings import load_settings
from vkdw.ui.theme import apply_language, apply_theme
from vkdw.ui.window import MainWindow


def _set_taskbar_id() -> None:
    """Group the window under its own taskbar icon instead of python.exe's."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VKDW.Downloader")
    except (AttributeError, OSError):
        pass


def main() -> None:
    _set_taskbar_id()
    app = QApplication(sys.argv)
    app.setApplicationName("VKDW")
    settings = load_settings()
    apply_language(i18n.pick_language(settings))
    apply_theme(settings.get("theme", "auto"))
    window = MainWindow(settings, session_file=session_path(), archive_file=archive_path())
    if window.start_maximized():
        window.showMaximized()
    else:
        window.show()
    sys.exit(app.exec())
