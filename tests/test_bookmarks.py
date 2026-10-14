import os
import tempfile
import unittest
from unittest.mock import patch

from vkdw import bookmarks
from vkdw.i18n import set_language
from vkdw.vk_client import PlaylistInfo, Target, VideoItem


def make_playlist(title: str, owner: int, album: int | None, amount: int, kind: str = "playlist", total: int = 0) -> PlaylistInfo:
    videos = tuple(VideoItem(owner, index, f"видео {index}", 10, "author", "") for index in range(1, amount + 1))
    return PlaylistInfo(owner, album, title, videos, cover_url="https://c/1.jpg", kind=kind, total=total)


class BookmarkDataTests(unittest.TestCase):
    def test_playlists_and_pages_can_be_kept_other_lists_cannot(self):
        mark = bookmarks.for_playlist(make_playlist("Сериал", -5, 7, 3), Target("playlist", -5, 7, "ключ"))
        self.assertEqual(mark.key, (-5, 7))
        self.assertEqual(mark.target, Target("playlist", -5, 7, "ключ"))
        self.assertEqual(mark.count, 3)
        page = bookmarks.for_playlist(make_playlist("Канал", -9, None, 2, "owner", total=5000), Target("owner", -9, 0))
        self.assertEqual(page.key, (-9, "all"))
        self.assertEqual(page.target, Target("owner", -9, 0))
        self.assertEqual(page.count, 5000)
        self.assertIsNone(bookmarks.for_playlist(make_playlist("Видео", 1, None, 1, "video")))
        self.assertIsNone(bookmarks.for_playlist(PlaylistInfo(0, None, "Сборник", (), kind="collection", collection_id=1)))

    def test_settings_round_trip_skips_what_cannot_be_read(self):
        marks = [bookmarks.for_playlist(make_playlist("Сериал", -5, 7, 3))]
        data = bookmarks.to_data(marks) + [{"kind": "video", "owner_id": 1, "item_id": 2}, {"owner_id": "x"}, "мусор"]
        data += bookmarks.to_data(marks)  # the same list twice
        self.assertEqual(bookmarks.from_data(data), marks)
        self.assertEqual(bookmarks.from_data(None), [])

    def test_an_owner_page_counts_only_videos_added_after_its_boundary(self):
        page = PlaylistInfo(-9, None, "Канал", (VideoItem(-9, 2, "б", 1, "", "", added=200), VideoItem(-9, 1, "а", 1, "", "", added=100)), kind="owner")
        mark = bookmarks.started(bookmarks.for_playlist(page), page)
        self.assertEqual(mark.since, 200)
        self.assertFalse(bookmarks.is_new(mark, page.videos[0]))
        self.assertTrue(bookmarks.is_new(mark, VideoItem(-9, 3, "в", 1, "", "", added=300)))
        self.assertEqual(bookmarks.from_data(bookmarks.to_data([mark])), [mark])
        # Read again, the boundary stays; a page kept before boundaries existed gets one.
        later = PlaylistInfo(-9, None, "Канал", (VideoItem(-9, 3, "в", 1, "", "", added=300),) + page.videos, kind="owner")
        self.assertEqual(bookmarks.refreshed(mark, later).since, 200)
        self.assertEqual(bookmarks.refreshed(bookmarks.for_playlist(page), later).since, 300)
        # A playlist has no boundary: everything of it not saved is new.
        series = make_playlist("Сериал", -5, 7, 2)
        series_mark = bookmarks.started(bookmarks.for_playlist(series), series)
        self.assertEqual(series_mark.since, 0)
        self.assertTrue(bookmarks.is_new(series_mark, series.videos[0]))

    def test_recent_lists_put_the_latest_first_without_repeats(self):
        recent = []
        for album in (1, 2, 3):
            recent = bookmarks.remember(recent, make_playlist(f"Плейлист {album}", -5, album, 1))
        recent = bookmarks.remember(recent, make_playlist("Плейлист 1", -5, 1, 4))
        self.assertEqual([mark.key for mark in recent], [(-5, 1), (-5, 3), (-5, 2)])
        self.assertEqual(recent[0].count, 4)
        self.assertIs(bookmarks.remember(recent, make_playlist("Видео", 1, None, 1, "video")), recent)
        for album in range(10, 30):
            recent = bookmarks.remember(recent, make_playlist("Ещё", -5, album, 1))
        self.assertEqual(len(recent), bookmarks.RECENT_LIMIT)
        self.assertEqual(recent[0].key, (-5, 29))


try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    import vkdw.ui  # noqa: F401
except ImportError:  # pragma: no cover - only without the GUI libraries
    QApplication = None


@unittest.skipIf(QApplication is None, "no Qt")
class WindowBookmarkTests(unittest.TestCase):
    def setUp(self):
        set_language("ru")
        self.app = QApplication.instance() or QApplication([])
        self.folder = tempfile.TemporaryDirectory()
        self.save = patch("vkdw.ui.window.save_settings")
        self.saved = self.save.start()
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.controller.close()
            window._confirm_close = lambda: True
            window.close()
            window.deleteLater()
        self.app.processEvents()
        self.save.stop()
        self.folder.cleanup()

    def _window(self, settings=None):
        from vkdw.ui.window import MainWindow

        window = MainWindow(settings or {"folder": self.folder.name}, watch_system_theme=False)
        self.windows.append(window)
        return window

    def _open(self, window, playlist, source=None):
        window.controller._events.put(("loaded", "open", None, playlist, source))
        window._pump()

    def test_the_star_keeps_a_playlist_and_settings_bring_it_back(self):
        window = self._window()
        playlist = make_playlist("Сериал", -5, 7, 3)
        self._open(window, playlist, Target("playlist", -5, 7))
        self.assertTrue(window.hero.bookmark_button.isVisibleTo(window.hero))
        window._toggle_bookmark()
        self.assertEqual([mark.key for mark in window._bookmarks], [(-5, 7)])
        stored = self.saved.call_args[0][0]["bookmarks"]
        self.assertEqual(stored[0]["title"], "Сериал")

        again = self._window({"folder": self.folder.name, "bookmarks": stored})
        rows = again._bookmark_rows()
        self.assertEqual([row.title for row in rows], ["Сериал"])
        self.assertEqual(rows[0].subtitle, "Плейлист · 3 видео")

        window._toggle_bookmark()
        self.assertEqual(window._bookmarks, [])
        self.assertEqual(self.saved.call_args[0][0]["bookmarks"], [])

    def test_no_star_for_a_single_video(self):
        window = self._window()
        self._open(window, make_playlist("Видео", 1, None, 1, "video"))
        self.assertFalse(window.hero.bookmark_button.isVisibleTo(window.hero))
        window._toggle_bookmark()
        self.assertEqual(window._bookmarks, [])

    def test_a_bookmark_opens_fresh_from_vk_and_tells_what_is_new(self):
        mark = bookmarks.for_playlist(make_playlist("Сериал", -5, 7, 3))
        window = self._window({"folder": self.folder.name, "bookmarks": bookmarks.to_data([mark])})
        with patch.object(window.controller, "open_target") as open_target:
            window._open_bookmark(window._bookmark_card_id(mark.key))
        open_target.assert_called_once_with(Target("playlist", -5, 7))
        self.assertIs(window.pages.currentWidget(), window.skeleton)
        with patch.object(window, "_toast") as toast:
            self._open(window, make_playlist("Сериал (новое имя)", -5, 7, 5), Target("playlist", -5, 7))
        self.assertIn("добавилось 2 ролика", toast.call_args[0][2])
        self.assertEqual((window._bookmarks[0].title, window._bookmarks[0].count), ("Сериал (новое имя)", 5))
        self.assertTrue(window.hero.bookmark_button.isVisibleTo(window.hero))

    def test_a_bookmark_is_removed_from_the_list_without_opening_it(self):
        marks = [bookmarks.for_playlist(make_playlist("Первый", 1, 10, 1)), bookmarks.for_playlist(make_playlist("Второй", 2, 20, 1))]
        window = self._window({"folder": self.folder.name, "bookmarks": bookmarks.to_data(marks)})
        window._show_bookmarks()
        view = window._bookmarks_view
        self.assertEqual(len(view.card_ids()), 2)
        view._remove(window._bookmark_card_id((1, 10)))
        self.assertEqual([mark.title for mark in window._bookmarks], ["Второй"])
        self.assertEqual(len(view.card_ids()), 1)
        window._bookmarks_flyout.close()

    def test_a_list_taken_out_of_the_queue_is_one_click_away(self):
        window = self._window()
        self._open(window, make_playlist("Сериал", -5, 7, 3), Target("playlist", -5, 7, "ключ"))
        self._open(window, make_playlist("Видео", 1, None, 1, "video"))
        self.assertEqual([mark.key for mark in window._recent], [(-5, 7)])
        stored = self.saved.call_args[0][0]["recent"]

        again = self._window({"folder": self.folder.name, "recent": stored})
        self.assertIs(again.pages.currentWidget(), again.empty)
        card_id = again._recent_card_id((-5, 7))
        self.assertEqual(again.empty.recent_ids(), [card_id])
        self.assertTrue(again.empty.recent.isVisibleTo(again.empty))
        self.assertFalse(again.empty.examples.isVisibleTo(again.empty))
        with patch.object(again.controller, "open_target") as open_target:
            again._open_recent(card_id)
        open_target.assert_called_once_with(Target("playlist", -5, 7, "ключ"))
        self.assertIs(again.pages.currentWidget(), again.skeleton)

        again._forget_recent(card_id)
        self.assertEqual(again._recent, [])
        self.assertEqual(again.empty.recent_ids(), [])
        self.assertTrue(again.empty.examples.isVisibleTo(again.empty))
        self.assertEqual(self.saved.call_args[0][0]["recent"], [])


if __name__ == "__main__":
    unittest.main()
