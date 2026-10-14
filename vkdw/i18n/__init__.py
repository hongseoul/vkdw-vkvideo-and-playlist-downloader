"""Texts of the app in Russian and English, and the switch between them.

tr("key", name=value) returns the text in the current language. Background
threads call it too: the language is one global setting, so after a switch
their next message comes out in the new language.

Only the standard library is used, so console.py can load this before Qt.
"""

from __future__ import annotations

import locale
import os
import sys

from vkdw.i18n import en, ru

LANGUAGES = {"ru": "Русский", "en": "English"}
_CATALOGS = {"ru": ru, "en": en}
_language = "ru"


def set_language(code: str) -> None:
    global _language
    _language = code if code in _CATALOGS else "ru"


def language() -> str:
    return _language


def tr(key: str, **params) -> str:
    """The text for key in the current language, or the Russian one if it is missing."""
    text = _CATALOGS[_language].TEXTS.get(key)
    if text is None:
        text = ru.TEXTS.get(key, key)
    return text.format(**params) if params else text


def count(amount: int, noun: str) -> str:
    """A number with its noun: 1 ролик, 2 ролика, 5 роликов / 1 video, 5 videos."""
    forms = _CATALOGS[_language].NOUNS.get(noun) or ru.NOUNS[noun]
    return f"{number(amount)} {plural(amount, forms)}"


def plural(number: int, forms: tuple[str, ...]) -> str:
    """Pick the noun form: three forms follow Russian rules, two follow English ones."""
    n = abs(int(number))
    if len(forms) == 2:
        return forms[0] if n == 1 else forms[1]
    tail = n % 100
    digit = n % 10
    if digit == 1 and tail != 11:
        return forms[0]
    if digit in (2, 3, 4) and tail not in (12, 13, 14):
        return forms[1]
    return forms[2]


def number(value: int) -> str:
    """41 414 in Russian, 41,414 in English."""
    text = f"{int(value):,}"
    return text.replace(",", " ") if _language == "ru" else text


def system_language() -> str:
    """Russian when Windows (or the locale) is Russian, English otherwise."""
    try:
        if sys.platform == "win32":
            import ctypes

            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "ru" if lang_id & 0x3FF == 0x19 else "en"
        name = locale.getlocale()[0] or os.environ.get("LANG", "")
        return "ru" if name.lower().startswith("ru") else "en"
    except (AttributeError, OSError, ValueError):
        return "en"


def pick_language(settings: dict) -> str:
    saved = settings.get("language")
    return saved if saved in LANGUAGES else system_language()
