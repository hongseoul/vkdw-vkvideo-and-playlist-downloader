"""The console window behind VKDW when it is started from run.bat.

The app runs under python.exe, not pythonw.exe: the VPN rule on this machine
matches python.exe. run.bat sets VKDW_HIDE_CONSOLE=1 and starts the app in a
console of its own, which is hidden here before the window libraries load.
Only the standard library is used, so this loads at once.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import time
import traceback
from pathlib import Path

from vkdw.i18n import tr

SW_HIDE = 0
SW_SHOW = 5
HIDE_VARIABLE = "VKDW_HIDE_CONSOLE"

# Kept open for the life of the process: faulthandler writes into it on a hard crash.
_fault_file = None


def log_path() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(root) / "VKDW" / "errors.log"


def _console_window() -> int:
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        return ctypes.windll.kernel32.GetConsoleWindow() or 0
    except (AttributeError, OSError):
        return 0


def _show_window(hwnd: int, command: int) -> None:
    try:
        import ctypes

        ctypes.windll.user32.ShowWindow(hwnd, command)
    except (AttributeError, OSError):
        pass


def hide_if_asked() -> bool:
    """Hide the console if run.bat asked for it. A terminal you started yourself stays."""
    if os.environ.get(HIDE_VARIABLE) != "1":
        return False
    hwnd = _console_window()
    if not hwnd:
        return False
    _show_window(hwnd, SW_HIDE)
    return True


def _write(text: str) -> None:
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(time.strftime("\n%Y-%m-%d %H:%M:%S\n"))
            handle.write(text)
    except OSError:
        pass


def log_crashes() -> None:
    """Keep errors in errors.log, since the console that would show them is hidden."""
    global _fault_file
    previous = sys.excepthook

    def hook(kind, value, tb):
        _write("".join(traceback.format_exception(kind, value, tb)))
        if sys.stderr is not None:
            previous(kind, value, tb)

    sys.excepthook = hook
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _fault_file = path.open("a", encoding="utf-8")
        faulthandler.enable(file=_fault_file)
    except OSError:
        pass


def report_crash(hidden: bool) -> None:
    """The app failed to start or stopped on an error: log it and let the user read it."""
    text = traceback.format_exc()
    _write(text)
    if sys.stderr is not None:
        sys.stderr.write(text)
    if not hidden:
        return
    hwnd = _console_window()
    if hwnd:
        _show_window(hwnd, SW_SHOW)
    print("\n" + tr("crash.stopped", path=log_path()))
    try:
        input(tr("crash.press_enter"))
    except (EOFError, RuntimeError):
        pass
