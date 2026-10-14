import re
import string
import unittest
from pathlib import Path

from vkdw import i18n
from vkdw.i18n import en, ru

ROOT = Path(__file__).resolve().parent.parent / "vkdw"


def placeholders(text: str) -> set[str]:
    return {name for _literal, name, _spec, _conv in string.Formatter().parse(text) if name}


class CatalogTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("ru")

    def test_both_languages_have_the_same_keys_and_values(self):
        self.assertEqual(set(ru.TEXTS), set(en.TEXTS))
        self.assertEqual(set(ru.NOUNS), set(en.NOUNS))
        for key, text in ru.TEXTS.items():
            with self.subTest(key=key):
                self.assertEqual(placeholders(text), placeholders(en.TEXTS[key]))

    def test_every_key_in_the_code_exists(self):
        used: set[str] = set()
        nouns: set[str] = set()
        for path in ROOT.rglob("*.py"):
            if path.parent.name == "i18n":
                continue
            source = path.read_text(encoding="utf-8")
            used |= set(re.findall(r"""\btr\(\s*["']([^"'{]+)["']""", source))
            nouns |= set(re.findall(r"""\bcount\([^()]*?,\s*["'](\w+)["']\)""", source))
        self.assertTrue(used)
        self.assertEqual(sorted(used - set(ru.TEXTS)), [])
        self.assertEqual(sorted(nouns - set(ru.NOUNS)), [])

    def test_keys_built_from_states(self):
        for prefix, states in {
            "card": ("waiting", "running", "interrupted", "done", "error"),
            "job": ("waiting", "running", "interrupted", "done", "error"),
            "kind": ("playlist", "video", "owner", "collection"),
            "offer": ("playlist", "video", "owner"),
            "theme": ("auto", "dark", "light"),
            "summary": ("done", "stopped"),
        }.items():
            for state in states:
                with self.subTest(key=f"{prefix}.{state}"):
                    self.assertIn(f"{prefix}.{state}", ru.TEXTS)

    def test_russian_and_english_counts(self):
        i18n.set_language("ru")
        self.assertEqual(i18n.count(1, "video"), "1 ролик")
        self.assertEqual(i18n.count(2, "video"), "2 ролика")
        self.assertEqual(i18n.count(5, "video"), "5 роликов")
        self.assertEqual(i18n.count(11, "video"), "11 роликов")
        self.assertEqual(i18n.count(21, "video"), "21 ролик")
        self.assertEqual(i18n.count(41414, "video"), "41 414 роликов")
        i18n.set_language("en")
        self.assertEqual(i18n.count(1, "video"), "1 video")
        self.assertEqual(i18n.count(21, "video"), "21 videos")
        self.assertEqual(i18n.count(41414, "video"), "41,414 videos")

    def test_switch_and_fallback(self):
        i18n.set_language("en")
        self.assertEqual(i18n.tr("app.title"), "VK Video Downloader")
        self.assertEqual(i18n.tr("list.counter", picked=1, total=4), "1 of 4 selected")
        i18n.set_language("de")
        self.assertEqual(i18n.language(), "ru")
        self.assertEqual(i18n.tr("app.title"), "Загрузчик VK Видео")
        self.assertEqual(i18n.pick_language({"language": "en"}), "en")
        self.assertIn(i18n.pick_language({}), i18n.LANGUAGES)

    def test_sizes_follow_the_language(self):
        from vkdw.settings import fmt_size

        i18n.set_language("ru")
        self.assertEqual(fmt_size(1536 * 1024), "1,5 МБ")
        i18n.set_language("en")
        self.assertEqual(fmt_size(1536 * 1024), "1.5 MB")
        self.assertEqual(fmt_size(12), "12 B")


if __name__ == "__main__":
    unittest.main()
