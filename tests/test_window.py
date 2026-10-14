import os
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    import vkdw.ui  # noqa: F401
except ImportError as exc:  # pragma: no cover - only without the GUI libraries
    raise unittest.SkipTest(f"no Qt: {exc}")

from pathlib import Path

from qfluentwidgets import InfoBar, PushButton

from vkdw import __version__
from vkdw.downloader import EXISTS
from vkdw.i18n import set_language
from vkdw.vk_client import Cancelled, PlaylistInfo, Target, VideoItem, VkError
from vkdw.updates import REPO_URL

app = None
DRAFT_ID = "draft"


def setUpModule():
    global app
    app = QApplication.instance() or QApplication([])


def make_playlist(title: str, owner: int, album: int, names: list[str], restricted: int = 0) -> PlaylistInfo:
    videos = tuple(
        VideoItem(owner, index, name, 10, "author", "закрыто" if index == restricted else "")
        for index, name in enumerate(names, start=1)
    )
    return PlaylistInfo(owner, album, title, videos)


class WindowTests(unittest.TestCase):
    def setUp(self):
        set_language("ru")
        self.folder = tempfile.TemporaryDirectory()
        self.save = patch("vkdw.ui.window.save_settings")
        self.save.start()
        self.download = patch("vkdw.controller.download_video", side_effect=self._fake_download)
        self.download.start()
        self.release = threading.Event()
        self.block = False
        from vkdw.ui.window import MainWindow

        self.window = MainWindow({"folder": self.folder.name}, watch_system_theme=False)

    def tearDown(self):
        self.release.set()
        controller = self.window.controller
        controller.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with controller._lock:
                if not controller._worker_active:
                    break
            time.sleep(0.02)
        self.window._confirm_close = lambda: True
        self.window.close()
        self.window.deleteLater()
        app.processEvents()
        self.download.stop()
        self.save.stop()
        self.folder.cleanup()

    def _fake_download(self, client, item, dest, max_height, cancel, on_progress):
        if self.block:
            while not self.release.is_set():
                if cancel.is_set():
                    raise Cancelled()
                time.sleep(0.02)
        if item.title == "плохой":
            raise VkError(0, "нет файла", "access")
        on_progress(5, 10, 100)
        on_progress(10, 10, 100)
        return "360p"

    def _pump(self):
        app.processEvents()
        self.window._pump()

    def _wait(self, predicate, timeout: float = 5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._pump()
            if predicate():
                return
            time.sleep(0.02)
        self.fail("timeout")

    def _open(self, playlist: PlaylistInfo, source: Target | None = None) -> None:
        self.window.controller._events.put(("loaded", "open", None, playlist, source))
        self._pump()

    def _pause_queue(self):
        with self.window.controller._lock:
            self.window.controller._queue.paused = True

    def _titles(self):
        model = self.window.model
        return [model.index(row).data() for row in range(model.rowCount())]

    def test_open_link_shows_placeholder_then_the_playlist(self):
        playlist = make_playlist("Плейлист", 1, 10, ["а", "б"])
        with patch("vkdw.controller.VkClient") as client:
            client.return_value.open_target.return_value = playlist
            self.window.link.setText("https://vkvideo.ru/playlist/1_10")
            self.window._open_link()
            self.assertIs(self.window.pages.currentWidget(), self.window.skeleton)
            self.assertEqual(self.window.open_button.text(), "Отмена")
            self._wait(lambda: self.window.pages.currentWidget() is self.window.playlist_page)
        self.assertEqual(self.window.hero.title.text(), "Плейлист")
        self.assertEqual(self._titles(), ["а", "б"])
        self.assertEqual(self.window.open_button.text(), "Открыть")
        self.assertEqual(self.window.sidebar.card_ids(), ["draft"])

    def test_failed_link_goes_back_and_explains(self):
        with patch("vkdw.controller.VkClient") as client:
            client.return_value.open_target.side_effect = VkError(15, "Плейлист недоступен.")
            self.window.link.setText("https://vkvideo.ru/playlist/1_10")
            self.window._open_link()
            self._wait(lambda: not self.window.controller.loading)
        self.assertIs(self.window.pages.currentWidget(), self.window.empty)
        self.assertEqual(self.window.link_error.text(), "Плейлист недоступен.")
        self.assertTrue(self.window.link.isError())

    def test_not_a_vk_link_is_refused_under_the_field(self):
        self.window.link.setText("https://example.com/video")
        self.window._open_link()
        self.assertFalse(self.window.controller.loading)
        self.assertTrue(self.window.link_error.text())
        self.window.link.setText("https://vkvideo.ru/playlist/-1_2")
        self.assertEqual(self.window.link_error.text(), "")

    def test_restricted_videos_start_unticked(self):
        self._open(make_playlist("Плейлист", 1, 10, ["а", "б", "в"], restricted=2))
        model = self.window.model
        self.assertEqual(model.checked, {"1_1", "1_3"})
        self.assertEqual(model.note("1_2"), ("закрыто", "restricted"))
        self.assertEqual(self.window.actions.download_button.text(), "Скачать 2")
        self.assertEqual(self.window.toolbar.all_box.checkState(), Qt.CheckState.PartiallyChecked)

    def test_videos_with_an_age_warning_are_ticked_and_not_called_unavailable(self):
        playlist = make_playlist("Плейлист", 1, 10, ["а", "б"])
        adult = replace(playlist.videos[1], warning="Видео с возрастным ограничением")
        self._open(replace(playlist, videos=(playlist.videos[0], adult)))
        model = self.window.model
        self.assertEqual(model.checked, {"1_1", "1_2"})
        self.assertEqual(model.note("1_2"), ("Видео с возрастным ограничением", "warning"))
        self.assertNotIn("restricted", self.window.notices.names())
        self.assertNotIn("недоступны", self.window.hero.meta.text())

    def test_click_toggles_a_row_and_shift_click_a_range(self):
        self.window.resize(1180, 780)
        self.window.show()
        self._open(make_playlist("Плейлист", 1, 10, ["а", "б", "в", "г"]))
        self._pump()
        view = self.window.list
        model = self.window.model
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, view.visualRect(model.index(0)).center())
        self.assertEqual(model.checked, {"1_2", "1_3", "1_4"})
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, view.visualRect(model.index(2)).center())
        self.assertEqual(model.checked, {"1_4"})
        self.assertEqual(self.window.actions.download_button.text(), "Скачать 1")
        self.assertIn("Выбрано 1 из 4", self.window.toolbar.counter.text())

    def test_search_hides_rows_but_keeps_ticks(self):
        self._open(make_playlist("Плейлист", 1, 10, ["кошки", "собаки", "кошки 2"]))
        self.window.toolbar.search.setText("кош")
        self.assertEqual(self._titles(), ["кошки", "кошки 2"])
        self.window._toggle_all()
        self.assertEqual(self.window.model.checked, {"1_2"})
        self.window.toolbar.search.clear()
        self.assertEqual(len(self._titles()), 3)
        self.assertEqual([index for index, _item in self.window.model.checked_items()], [2])

    def test_queue_card_switches_the_list_and_keeps_video_statuses(self):
        self._open(make_playlist("Первый", 1, 10, ["один", "плохой"]))
        self.window._add_to_queue()
        self._wait(lambda: "Готово «Первый»" in self.window.log_drawer.text())
        self._wait(lambda: self.window.dock.mode() == "finished")
        self.assertIn("Готово «Первый»", self.window.dock.status_text())
        self._open(make_playlist("Второй", 2, 20, ["два"]))
        self.assertEqual(self.window.hero.title.text(), "Второй")
        self.assertEqual(self.window.sidebar.card_ids(), ["job-1", "draft"])
        self.assertEqual(self.window.sidebar.selected_id(), "draft")

        self.window._on_card("job-1")
        self.assertEqual(self.window.hero.title.text(), "Первый")
        self.assertEqual(self._titles(), ["один", "плохой"])
        model = self.window.model
        self.assertTrue(model.locked)
        self.assertEqual(model.note("1_1"), ("360p", "done"))
        self.assertEqual(model.note("1_2"), ("нет файла", "failed"))
        self.assertIn("готово", self.window.hero.status.text())
        self.assertFalse(self.window.hero.options.isVisibleTo(self.window.hero))
        self.assertEqual(self.window.sidebar.selected_id(), "job-1")

        self.window._on_card("draft")
        self.assertEqual(self.window.hero.title.text(), "Второй")
        self.assertEqual(self.window.hero.status.text(), "Открыт · не в очереди")

        self._pause_queue()
        self.window._add_to_queue()
        self.assertEqual(self.window.sidebar.card_ids(), ["job-1", "job-2"])
        self.assertEqual(self.window.sidebar.selected_id(), "job-2")
        self.assertIn("В очереди: ждёт", self.window.hero.status.text())

    def test_two_single_videos_of_one_owner_stay_apart(self):
        self._pause_queue()
        first = PlaylistInfo(-7, None, "Серия 1", (VideoItem(-7, 1, "Серия 1", 10, "канал", ""),), kind="video")
        second = PlaylistInfo(-7, None, "Серия 2", (VideoItem(-7, 2, "Серия 2", 10, "канал", ""),), kind="video")
        self._open(first)
        self.window._add_to_queue()
        self._open(second)
        self.assertEqual(self.window.sidebar.card_ids(), ["job-1", "draft"])
        self.assertEqual(self.window.hero.status.text(), "Открыт · не в очереди")
        self.window._on_card("job-1")
        self.assertEqual(self._titles(), ["Серия 1"])
        self.window._on_card("draft")
        self.assertEqual(self._titles(), ["Серия 2"])

    def test_unqueued_ticks_survive_a_switch(self):
        self._pause_queue()
        self._open(make_playlist("Первый", 1, 10, ["один"]))
        self.window._add_to_queue()
        self._open(make_playlist("Второй", 2, 20, ["а", "б", "в"]))
        self.window.model.set_all_visible(False)
        self.window.model.set_rows([1], True)
        self.window._on_card("job-1")
        self.window._on_card("draft")
        self.assertEqual(self.window.model.checked, {"2_2"})

    def test_new_pick_reopens_a_queued_playlist_for_editing(self):
        self._pause_queue()
        self._open(make_playlist("Первый", 1, 10, ["а", "б"]))
        self.window.model.set_rows([0], False)
        self.window._add_to_queue()
        self.assertTrue(self.window.model.locked)
        self.window._new_pick()
        self.assertFalse(self.window.model.locked)
        self.assertEqual(self.window.model.checked, {"1_2"})
        self.assertTrue(self.window.actions.queue_button.isEnabled())

    def test_cover_arrives_in_header_and_card(self):
        from PIL import Image

        self._pause_queue()
        playlist = PlaylistInfo(1, 10, "Первый", (VideoItem(1, 1, "один", 10, "", ""),), cover_url="cover")
        self._open(playlist)
        self.window._add_to_queue()
        card = self.window.sidebar.card("job-1")
        self.assertIsNone(card.cover.pixmap())
        self.window._on_thumb(("cover", (1, 10)), Image.new("RGBA", (320, 180), "red"))
        self.assertIsNotNone(self.window.sidebar.card("job-1").cover.pixmap())
        self.assertIsNotNone(self.window.hero.cover.pixmap())

    def test_progress_reaches_row_card_and_dock(self):
        self.block = True
        self._open(make_playlist("Первый", 1, 10, ["один", "два"]))
        self.window._download()
        self._wait(lambda: self.window.dock.mode() == "running" and self.window.model.note("1_1")[1] == "active")
        self.assertEqual(self.window.sidebar.card("job-1").bar.isVisibleTo(self.window.sidebar.card("job-1")), True)
        self.window._stop()
        self._wait(lambda: self.window.dock.mode() == "paused")
        self.assertTrue(self.window.controller.can_resume())
        self.release.set()
        self.window._resume()
        self._wait(lambda: self.window.dock.mode() == "finished")
        self.assertEqual(self.window.model.note("1_2"), ("360p", "done"))

    def _save_file(self, name: str, folder: str = "Первый") -> Path:
        path = Path(self.folder.name) / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        return path

    def test_videos_already_in_the_folder_are_marked_and_left_unticked(self):
        self._save_file("001 - один.mp4")
        self._open(make_playlist("Первый", 1, 10, ["один", "два"]))
        model = self.window.model
        self.assertEqual(model.saved, {"1_1"})
        self.assertEqual(model.checked, {"1_2"})
        self.assertEqual(model.note("1_1"), (EXISTS, "done"))
        self.assertIn("saved", self.window.notices.names())
        self.assertEqual(self.window.notices._shown["saved"][1], "Уже скачаны: 1 из 2")

    def test_ticked_saved_videos_can_be_unticked_from_the_notice(self):
        self._save_file("001 - один.mp4")
        self._open(make_playlist("Первый", 1, 10, ["один", "два"]))
        self.window.model.toggle(0)
        self.assertIn("Из них отмечено 1", self.window.notices._shown["saved"][2])
        bar = self.window.notices._bars["saved"]
        button = next(child for child in bar.findChildren(PushButton) if child.text() == "Снять с них галочки")
        button.click()
        self.assertEqual(self.window.model.checked, {"1_2"})
        self.assertNotIn("Из них отмечено", self.window.notices._shown["saved"][2])

    def test_a_fully_downloaded_playlist_has_nothing_ticked(self):
        self._save_file("001 - один.mp4")
        self._save_file("002 - два.mp4")
        self._open(make_playlist("Первый", 1, 10, ["один", "два"]))
        self.assertEqual(self.window.model.checked, set())
        self.assertFalse(self.window.actions.download_button.isEnabled())
        self.assertEqual(self.window.notices._shown["saved"][1], "Все ролики уже скачаны")

    def test_the_check_follows_the_folder_settings(self):
        self._save_file("001 - один.mp4")
        self._open(make_playlist("Первый", 1, 10, ["один", "два"]))
        self.window._on_subfolder(False)
        self.assertEqual(self.window.model.saved, set())
        self.assertNotIn("saved", self.window.notices.names())
        self.window._on_subfolder(True)
        self.assertEqual(self.window.model.saved, {"1_1"})

    def test_a_queued_playlist_shows_what_is_already_in_its_folder(self):
        self._pause_queue()
        self._open(make_playlist("Первый", 1, 10, ["один", "два"]))
        self.window.model.set_all_visible(True)
        self.window._add_to_queue()
        self.assertEqual(self.window.model.saved, set())
        self._save_file("001 - один.mp4")
        self.window._recheck_saved()
        self.assertEqual(self.window.model.note("1_1"), (EXISTS, "done"))
        self.assertEqual(self.window.notices._shown["saved"][2], "Эти файлы уже есть в папке, при скачивании они будут пропущены.")

    def test_removing_the_paused_playlist_hides_the_download_panel(self):
        self.block = True
        self._open(make_playlist("Первый", 1, 10, ["один", "два"]))
        self.window._download()
        self._wait(lambda: self.window.dock.mode() == "running")
        self.window._stop()
        self._wait(lambda: self.window.dock.mode() == "paused")
        self.assertFalse(self.window.sidebar.clear_button.isHidden())
        self.window._remove_card("job-1")
        self._pump()
        self.assertEqual(self.window.dock.mode(), "hidden")
        # The removed playlist does not come back as a "not queued" card to be hidden by hand.
        self.assertEqual(self.window.sidebar.card_ids(), [])
        self.assertIs(self.window.pages.currentWidget(), self.window.empty)
        self.assertTrue(self.window.sidebar.clear_button.isHidden())
        self.assertEqual(self.window.sidebar.caption.text(), "пусто")
        self.assertFalse(self.window.model.locked)

    def test_a_list_open_apart_from_the_queue_stays_when_its_job_is_removed(self):
        self._pause_queue()
        playlist = make_playlist("Первый", 1, 10, ["один", "два"])
        self._open(playlist)
        self.window._add_to_queue()
        self._open(playlist)  # opened again: shown as a list, not as the queued job
        self.assertEqual(self.window.sidebar.card_ids(), ["job-1"])
        self.window._remove_card("job-1")
        self.assertEqual(self.window.sidebar.card_ids(), [DRAFT_ID])
        self.assertIs(self.window.pages.currentWidget(), self.window.playlist_page)
        self.assertEqual(self.window.hero.title.text(), "Первый")

    def test_removing_a_job_that_is_not_shown_keeps_the_view(self):
        self._pause_queue()
        self._open(make_playlist("Первый", 1, 10, ["один"]))
        self.window._add_to_queue()
        self._open(make_playlist("Второй", 2, 20, ["два"]))
        self.window._remove_card("job-1")
        self.assertEqual(self.window.sidebar.card_ids(), [DRAFT_ID])
        self.assertEqual(self.window.hero.title.text(), "Второй")

    def test_paused_panel_counts_what_is_left(self):
        self.block = True
        self._open(make_playlist("Первый", 1, 10, ["один"]))
        self.window._download()
        self._wait(lambda: self.window.dock.mode() == "running")
        self._open(make_playlist("Второй", 2, 20, ["два"]))
        self.window._add_to_queue()
        self.window._stop()
        self._wait(lambda: self.window.dock.mode() == "paused")
        self.assertIn("Осталось 2 плейлиста", self.window.dock.status_text())
        self.window._remove_card("job-1")
        self._pump()
        self.assertEqual(self.window.dock.mode(), "paused")
        self.assertIn("Осталось 1 плейлист.", self.window.dock.status_text())
        self.assertEqual(self.window.dock.place.text(), "")

    def test_removing_the_running_playlist_asks_first(self):
        self.block = True
        self._open(make_playlist("Первый", 1, 10, ["один"]))
        self.window._download()
        self._wait(lambda: self.window.dock.mode() == "running")
        with patch.object(self.window, "_ask", return_value=False) as ask:
            self.window._remove_card("job-1")
        ask.assert_called_once()
        self.assertEqual(self.window.sidebar.card_ids(), ["job-1"])
        with patch.object(self.window, "_ask", return_value=True):
            self.window._remove_card("job-1")
        self._wait(lambda: not self.window.controller.downloading)
        self._wait(lambda: self.window.dock.mode() == "hidden")
        self.assertEqual(self.window.sidebar.card_ids(), [])
        self.assertIn("остановлена и убрана из очереди", self.window.log_drawer.text())

    def test_clear_queue_offers_finished_or_everything(self):
        self._open(make_playlist("Первый", 1, 10, ["один"]))
        self.window._add_to_queue()
        self._wait(lambda: self.window.dock.mode() == "finished")
        self._pause_queue()
        self._open(make_playlist("Второй", 2, 20, ["два"]))
        self.window._add_to_queue()
        with patch.object(self.window, "_popup_above") as popup:
            self.window._clear_menu()
        finished, everything = popup.call_args[0][0].menuActions()
        self.assertTrue(finished.isEnabled())
        finished.trigger()
        self.assertEqual(self.window.sidebar.card_ids(), ["job-2"])
        with patch.object(self.window, "_ask", return_value=False):
            everything.trigger()
        self.assertEqual(self.window.sidebar.card_ids(), ["job-2"])
        with patch.object(self.window, "_ask", return_value=True) as ask:
            everything.trigger()
        self.assertIn("Ещё не скачано: 1 плейлист", ask.call_args[0][1])
        self.assertEqual(self.window.sidebar.card_ids(), [])
        self.assertTrue(self.window.sidebar.clear_button.isHidden())

    def test_collection_card_asks_before_it_is_deleted(self):
        self._open(make_playlist("Первый", 1, 10, ["один", "два"]))
        self.window._collect()
        self.assertIn("collection", self.window.sidebar.card_ids())
        with patch.object(self.window, "_ask", return_value=False):
            self.window._remove_card("collection")
        self.assertIn("collection", self.window.sidebar.card_ids())
        with patch.object(self.window, "_ask", return_value=True):
            self.window._remove_card("collection")
        self.assertNotIn("collection", self.window.sidebar.card_ids())

    def test_settings_menu_is_flat_and_opens_above_its_button(self):
        self.window.resize(1180, 780)
        self.window.show()
        self._pump()
        menus = []
        show = self.window._popup_above

        def spy(menu, anchor, **options):
            menus.append(menu)
            show(menu, anchor, **options)

        with patch.object(self.window, "_popup_above", side_effect=spy):
            self.window._settings_menu()
        menu = menus[0]
        labels = [action.text() for action in menu.menuActions()]
        expected = [
            "Тема как в системе",
            "Тёмная тема",
            "Светлая тема",
            "Русский",
            "English",
            "Проверять закладки при запуске",
            "Проверять обновления при запуске",
        ]
        if sys.platform == "win32":
            expected.append("Выключить компьютер после очереди")
        expected.append(f"VKDW {__version__} на GitHub")
        self.assertEqual(labels, expected)
        # Off unless turned on, except the update check, which is on unless turned off.
        switches = [action.isChecked() for action in menu.menuActions()[5:-1]]
        self.assertEqual(switches, [False, True, False][: len(switches)])
        with patch("vkdw.ui.window.QDesktopServices.openUrl") as open_url:
            menu.menuActions()[-1].trigger()
        self.assertEqual(open_url.call_args.args[0].toString(), REPO_URL)
        self.assertEqual(menu._subMenus, [])
        # Items are measured with the font they are drawn with, so the longest one fits.
        widest = max(menu.view.fontMetrics().horizontalAdvance(label) for label in labels)
        self.assertGreaterEqual(menu.view.width(), widest + 60)
        button = self.window.sidebar.settings_button
        corner = button.mapToGlobal(QPoint(0, 0))
        end = menu.aniManager.ani.endValue()
        margins = menu.layout().contentsMargins()
        self.assertLessEqual(end.y() + margins.top() + menu.view.height(), corner.y())
        self.assertEqual(end.x() + margins.left() + menu.view.width(), corner.x() + button.width())
        menu.close()

    def test_shutdown_after_the_queue_can_be_cancelled(self):
        from vkdw.ui.window import SHUTDOWN_DELAY

        self.window._set_shutdown_after(True)
        with patch("vkdw.ui.window.subprocess.run") as run, patch.object(self.window, "_notify"):
            self._open(make_playlist("Первый", 1, 10, ["один"]))
            self.window._download()
            self._wait(lambda: run.called)
            self.assertEqual(run.call_args_list[0].args[0], ["shutdown", "/s", "/t", str(SHUTDOWN_DELAY)])
            self.assertFalse(self.window._shutdown_after)  # once only
            cancel = next(button for button in self.window.panel.findChildren(PushButton) if button.text() == "Не выключать")
            cancel.click()
            self.assertEqual(run.call_args_list[-1].args[0], ["shutdown", "/a"])
        self._pump()
        self.assertIn("Выключение отменено.", [entry.text for entry in self.window.controller.log])

    def test_a_newer_release_shows_a_bar_with_its_page(self):
        from vkdw.updates import Release

        url = "https://github.com/hongseoul/vkdw-vkvideo-and-playlist-downloader/releases/tag/v9.0.0"
        self.window.controller._events.put(("update", Release("9.0.0", url)))
        self._pump()
        self._pump()  # the log line goes through the event queue too
        self.assertIn(f"Вышла версия 9.0.0: {url}", [entry.text for entry in self.window.controller.log])
        bar = next(bar for bar in self.window.panel.findChildren(InfoBar) if bar.title == "Вышла версия 9.0.0")
        button = next(button for button in bar.findChildren(PushButton) if button.text() == "Скачать")
        with patch("vkdw.ui.window.QDesktopServices.openUrl") as open_url:
            button.click()
        self.assertEqual(open_url.call_args.args[0].toString(), url)

    def test_update_check_can_be_turned_off(self):
        with patch("vkdw.ui.window.QTimer.singleShot") as later:
            self.window.start_update_check()
            self.assertEqual(later.call_args.args[1], self.window.controller.check_updates)
            later.reset_mock()
            self.window._set_check_updates(False)
            self.window.start_update_check()
            later.assert_not_called()

    def test_no_shutdown_while_the_queue_has_more(self):
        self.window._set_shutdown_after(True)
        with patch("vkdw.ui.window.subprocess.run") as run, patch.object(self.window.controller, "pending_count", return_value=1):
            self.window._on_idle("готово", "finished")
        run.assert_not_called()
        self.assertTrue(self.window._shutdown_after)

    def test_close_asks_before_discarding_a_pending_queue(self):
        self._pause_queue()
        self._open(make_playlist("Первый", 1, 10, ["один"]))
        self.window._add_to_queue()
        self.window.show()
        with patch.object(self.window, "_confirm_close", return_value=False) as ask:
            self.window.close()
        ask.assert_called_once()
        self.assertTrue(self.window.isVisible())

    def test_minimum_size_keeps_the_list_usable(self):
        self._open(make_playlist("Плейлист", 1, 10, [f"ролик {i}" for i in range(40)]))
        self.window.resize(960, 640)
        self.window.show()
        self._pump()
        self.assertGreater(self.window.list.height(), 150)
        bottom = self.window.actions.mapTo(self.window, self.window.actions.rect().bottomLeft()).y()
        self.assertLessEqual(bottom, self.window.height())
        self.window.list.verticalScrollBar().setValue(300)
        self._pump()
        self.assertTrue(self.window.hero.is_compact())

    def test_notices_fold_away_while_the_list_scrolls(self):
        self._open(make_playlist("Плейлист", 1, 10, [f"ролик {i}" for i in range(40)], restricted=3))
        self.window.resize(960, 640)
        self.window.show()
        self._pump()
        notices = self.window.notices
        self.assertTrue(notices.isVisible())
        self.window.list.verticalScrollBar().setValue(300)
        self._pump()
        self.assertFalse(notices.isVisible())
        self.assertEqual(notices.names(), ["restricted"])
        self.window.list.verticalScrollBar().setValue(0)
        self._pump()
        self.assertTrue(notices.isVisible())

    # Reading a list again

    def test_access_error_on_open_mentions_vpn(self):
        with patch("vkdw.controller.VkClient") as client:
            client.return_value.open_target.side_effect = VkError(15, "VK закрыл доступ.", "access")
            self.window.link.setText("https://vkvideo.ru/playlist/1_10")
            self.window._open_link()
            self._wait(lambda: not self.window.controller.loading)
        self.assertEqual(self.window.link_error.text(), "VK закрыл доступ.")
        self.assertIn("VPN", self.window.link_hint.text())
        self.assertFalse(self.window.link_hint.isHidden())

    def test_refresh_keeps_ticks_and_shows_new_restrictions(self):
        playlist = make_playlist("Плейлист", 1, 10, ["а", "б", "в"])
        self._open(playlist, Target("playlist", 1, 10))
        self.window.model.set_rows([0], False)
        fresh = make_playlist("Плейлист", 1, 10, ["а", "б", "в", "г"], restricted=2)
        with patch("vkdw.controller.VkClient") as client:
            client.return_value.open_target.return_value = fresh
            self.window._refresh_shown()
            self.assertTrue(self.window.hero.refresh_ring.isVisibleTo(self.window.hero))
            self._wait(lambda: not self.window.controller.loading)
        self.assertEqual(self._titles(), ["а", "б", "в", "г"])
        self.assertEqual(self.window.model.checked, {"1_2", "1_3"})
        self.assertEqual(self.window.model.note("1_2"), ("закрыто", "restricted"))
        self.assertIn("restricted", self.window.notices.names())
        self.assertFalse(self.window.hero.refresh_ring.isVisibleTo(self.window.hero))

    def test_failed_refresh_keeps_the_list_and_explains(self):
        playlist = make_playlist("Плейлист", 1, 10, ["а", "б"])
        self._open(playlist, Target("playlist", 1, 10))
        with patch("vkdw.controller.VkClient") as client:
            client.return_value.open_target.side_effect = VkError(15, "VK закрыл доступ.", "access")
            self.window._refresh_shown()
            self._wait(lambda: not self.window.controller.loading)
        self.assertIn("load_failed", self.window.notices.names())
        self.assertIs(self.window.pages.currentWidget(), self.window.playlist_page)
        self.assertEqual(self._titles(), ["а", "б"])

    def test_pick_again_reads_vk_first(self):
        self._pause_queue()
        self._open(make_playlist("Первый", 1, 10, ["а", "б"]), Target("playlist", 1, 10))
        self.window.model.set_rows([0], False)
        self.window._add_to_queue()
        self.assertTrue(self.window.model.locked)
        fresh = make_playlist("Первый", 1, 10, ["а", "б", "новый"])
        with patch("vkdw.controller.VkClient") as client:
            client.return_value.open_target.return_value = fresh
            self.window._new_pick()
            self.assertTrue(self.window.controller.loading)
            self._wait(lambda: not self.window.controller.loading)
        self.assertFalse(self.window.model.locked)
        self.assertEqual(self._titles(), ["а", "б", "новый"])
        self.assertEqual(self.window.model.checked, {"1_2"})
        self.assertEqual(self.window.sidebar.selected_id(), "")

    def test_clicking_a_queued_row_explains_what_to_do(self):
        self.window.resize(1180, 780)
        self.window.show()
        self._pause_queue()
        self._open(make_playlist("Первый", 1, 10, ["а", "б"]))
        self.window._add_to_queue()
        self._pump()
        view = self.window.list
        with patch.object(self.window, "_toast") as toast:
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, view.visualRect(self.window.model.index(0)).center())
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, view.visualRect(self.window.model.index(1)).center())
        toast.assert_called_once()
        self.assertIn("Выбрать заново", toast.call_args.args[2])
        self.assertEqual(self.window.model.checked, {"1_1", "1_2"})

    def test_failed_videos_can_be_retried(self):
        self._open(make_playlist("Первый", 1, 10, ["один", "плохой"]))
        self.window._add_to_queue()
        self._wait(lambda: self.window.dock.mode() == "finished")
        self._pump()
        self.assertFalse(self.window.actions.retry_button.isHidden())
        self.assertIn("failed", self.window.notices.names())
        self._pause_queue()
        self.window._retry_failed()
        job = self.window.controller.job(self.window._view_job_id)
        self.assertEqual(job.job_id, "job-2")
        self.assertEqual([item.title for _index, item in job.items], ["плохой"])
        self.assertEqual(self.window.sidebar.card_ids(), ["job-1", "job-2"])

    # Pages and collections

    def test_owner_page_opens_unticked_with_more_to_load(self):
        videos = tuple(VideoItem(-5, index, f"ролик {index}", 10, "", "") for index in range(1, 4))
        page = PlaylistInfo(-5, None, "Канал", videos, kind="owner", total=50, albums=((7, "Выпуски", 12),))
        self._open(page, Target("owner", -5, 0))
        self.assertEqual(self.window.model.checked, set())
        self.assertEqual(self.window.hero.kind.text(), "ВСЕ ВИДЕО")
        self.assertTrue(self.window.toolbar.more_button.isVisibleTo(self.window.toolbar))
        self.assertTrue(self.window.hero.albums_button.isVisibleTo(self.window.hero))
        self.assertIn("в VK всего 50", self.window.toolbar.counter.text())
        self.window.model.set_rows([1], True)
        more = PlaylistInfo(-5, None, "Канал", videos + (VideoItem(-5, 4, "ролик 4", 10, "", ""),), kind="owner", total=50)
        with patch("vkdw.controller.VkClient") as client:
            client.return_value.open_more.return_value = more
            self.window._load_more()
            self._wait(lambda: not self.window.controller.loading)
        self.assertEqual(len(self._titles()), 4)
        self.assertEqual(self.window.model.checked, {"-5_2"})

    def test_collection_from_links_then_one_more_link(self):
        first = [VideoItem(-1, 2, "первый", 10, "", ""), VideoItem(-3, 4, "второй", 10, "", "")]
        with patch("vkdw.controller.VkClient") as client:
            client.return_value.open_videos.return_value = first
            self.window.link.setText("https://vk.com/video-1_2\nhttps://vk.com/video-3_4")
            self.window._open_link()
            self.assertIs(self.window.pages.currentWidget(), self.window.skeleton)
            self._wait(lambda: not self.window.controller.loading)
        self.assertEqual(self.window.hero.kind.text(), "СБОРНИК")
        self.assertEqual(self._titles(), ["первый", "второй"])
        self.assertEqual(self.window.sidebar.card_ids(), ["collection"])
        self.assertEqual(self.window.sidebar.selected_id(), "collection")
        self.assertEqual(self.window.open_button.text(), "Добавить")
        self.window.model.set_rows([0], False)

        with patch("vkdw.controller.VkClient") as client:
            client.return_value.open_videos.return_value = [VideoItem(-5, 6, "третий", 10, "", "")]
            self.window.link.setText("https://vk.com/video-5_6")
            self.window._open_link()
            self.assertIs(self.window.pages.currentWidget(), self.window.playlist_page)
            self._wait(lambda: not self.window.controller.loading)
        self.assertEqual(self._titles(), ["первый", "второй", "третий"])
        self.assertEqual(self.window.model.checked, {"-3_4", "-5_6"})
        self.assertEqual(self.window.link.text(), "")

    def test_a_dropped_text_file_opens_its_links(self):
        from PySide6.QtCore import QMimeData, QPointF, QUrl
        from PySide6.QtGui import QDropEvent

        path = Path(self.folder.name) / "ссылки.txt"
        path.write_bytes("https://vk.com/video-1_2\r\nhttps://vk.com/video-3_4\r\n".encode("cp1251"))
        data = QMimeData()
        data.setUrls([QUrl.fromLocalFile(str(path)), QUrl.fromLocalFile(str(Path(self.folder.name) / "картинка.png"))])
        event = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, data, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        with patch("vkdw.controller.VkClient") as client:
            client.return_value.open_videos.return_value = [VideoItem(-1, 2, "первый", 10, "", ""), VideoItem(-3, 4, "второй", 10, "", "")]
            self.window.dropEvent(event)
            self._wait(lambda: not self.window.controller.loading)
        client.return_value.open_videos.assert_called_once()
        self.assertEqual(client.return_value.open_videos.call_args.args[0], ["-1_2", "-3_4"])
        self.assertEqual(self.window.hero.kind.text(), "СБОРНИК")

    def test_links_of_ticked_videos_are_copied(self):
        from vkdw.ui.window import RoundMenu

        self._pause_queue()
        self._open(make_playlist("Первый", 1, 10, ["а", "б", "в"]))
        self.window.model.set_rows([1], False)
        menus = []
        with patch.object(RoundMenu, "exec", lambda menu, *args, **kwargs: menus.append(menu)):
            self.window._row_menu(self.window.list.visualRect(self.window.model.index(0)).center())
        action = next(action for action in menus[0].actions() if action.text() == "Копировать ссылки отмеченных (2)")
        action.trigger()
        self.assertEqual(QApplication.clipboard().text(), "https://vkvideo.ru/video1_1\nhttps://vkvideo.ru/video1_3")

    def test_ticked_videos_go_to_the_collection(self):
        self._pause_queue()
        self._open(make_playlist("Первый", 1, 10, ["а", "б", "в"]))
        self.window.model.set_rows([1], False)
        self.window._collect()
        self.assertEqual(self.window.sidebar.card_ids(), ["draft", "collection"])
        collection = self.window.controller.playlists[self.window._collection_key]
        self.assertEqual([item.title for item in collection.videos], ["а", "в"])
        self.window._on_card("collection")
        self.assertEqual(self._titles(), ["а", "в"])
        self.assertFalse(self.window.actions.collect_button.isVisibleTo(self.window.actions))
        self.window._add_to_queue()
        self.assertIsNone(self.window._collection_key)
        job = self.window.controller.job(self.window._view_job_id)
        self.assertEqual(job.naming, "date")
        self.assertEqual(self.window.sidebar.card_ids(), ["job-1", "draft"])

    # Language

    def test_language_switch_is_live(self):
        self._open(make_playlist("Плейлист", 1, 10, ["а", "б"]))
        try:
            self.window.set_language("en")
            self.assertEqual(self.window.windowTitle(), "VK Video Downloader")
            self.assertEqual(self.window.open_button.text(), "Open")
            self.assertEqual(self.window.actions.download_button.text(), "Download 2")
            self.assertEqual(self.window.hero.status.text(), "Opened · not queued")
            self.assertEqual(self.window.sidebar.heading.text(), "Queue")
            self.assertEqual(self.window.sidebar.card(DRAFT_ID).pill.text(), "not queued")
            self.assertIn("2 of 2 selected", self.window.toolbar.counter.text())
            self.assertEqual(self.window.toolbar.search.placeholderText(), "Search by title or author")
        finally:
            self.window.set_language("ru")
        self.assertEqual(self.window.windowTitle(), "Загрузчик VK Видео")
        self.assertEqual(self.window.open_button.text(), "Открыть")


if __name__ == "__main__":
    unittest.main()
