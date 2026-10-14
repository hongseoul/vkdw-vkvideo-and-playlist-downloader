"""Colours, sizes, the light/dark switch and the language of the built-in widgets."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QLocale
from PySide6.QtGui import QColor, QFont
from qfluentwidgets import FluentTranslator, Theme, getFont, isDarkTheme, setTheme, setThemeColor, themeColor

from vkdw import i18n

ACCENT = "#0077FF"

THEME_MODES = {"auto": Theme.AUTO, "dark": Theme.DARK, "light": Theme.LIGHT}

# Window behind everything, and the raised layer with the playlist on it.
# The layer is translucent, so on Windows 11 the Mica backdrop shows through.
WINDOW_BG = ("#f3f3f3", "#202020")

# (light, dark) pairs, read at paint time so a theme switch needs only a repaint.
_PALETTE = {
    "text": (QColor(0, 0, 0, 228), QColor(255, 255, 255)),
    "text2": (QColor(0, 0, 0, 158), QColor(255, 255, 255, 200)),
    "text3": (QColor(0, 0, 0, 114), QColor(255, 255, 255, 139)),
    "layer": (QColor(255, 255, 255, 128), QColor(58, 58, 58, 77)),
    "stroke": (QColor(0, 0, 0, 15), QColor(255, 255, 255, 18)),
    "hover": (QColor(0, 0, 0, 10), QColor(255, 255, 255, 14)),
    "pressed": (QColor(0, 0, 0, 16), QColor(255, 255, 255, 9)),
    "selected": (QColor(0, 0, 0, 13), QColor(255, 255, 255, 20)),
    "card": (QColor(255, 255, 255, 179), QColor(255, 255, 255, 13)),
    "placeholder": (QColor(0, 0, 0, 20), QColor(255, 255, 255, 22)),
    "track": (QColor(0, 0, 0, 30), QColor(255, 255, 255, 40)),
    "ok": (QColor("#0f7b0f"), QColor("#6ccb5f")),
    "error": (QColor("#c42b1c"), QColor("#ff99a4")),
    "warn": (QColor("#9d5d00"), QColor("#f0c05a")),
}

# Video row states (from the controller) and queue job statuses, by colour name.
ROW_COLORS = {"active": "accent", "done": "ok", "failed": "error", "stopped": "text3", "restricted": "warn", "warning": "text2"}
JOB_COLORS = {"waiting": "text2", "running": "accent", "interrupted": "warn", "done": "ok", "error": "error"}


def color(name: str) -> QColor:
    if name == "accent":
        return QColor(themeColor())
    light, dark = _PALETTE[name]
    return QColor(dark if isDarkTheme() else light)


def tinted(name: str, alpha: int) -> QColor:
    value = color(name)
    value.setAlpha(alpha)
    return value


def font(size: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    return getFont(size, weight)


def apply_theme(mode: str) -> None:
    setTheme(THEME_MODES.get(mode, Theme.AUTO))
    setThemeColor(ACCENT)


_translator: FluentTranslator | None = None


def apply_language(code: str) -> None:
    """Switch the app's texts, and those QFluentWidgets draws itself (menus of text fields, dialogs)."""
    global _translator
    i18n.set_language(code)
    app = QCoreApplication.instance()
    if app is None:
        return
    if _translator is not None:
        app.removeTranslator(_translator)
    if i18n.language() == "ru":
        locale = QLocale(QLocale.Language.Russian, QLocale.Country.Russia)
    else:
        locale = QLocale(QLocale.Language.English, QLocale.Country.UnitedStates)
    _translator = FluentTranslator(locale)
    app.installTranslator(_translator)
