import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from vkdw import bookmarks
from vkdw.archive import Archive
from vkdw.controller import DownloadController
from vkdw.i18n import set_language
from vkdw.vk_client import PlaylistInfo, Target, VideoItem, VkError


def setUpModule():
    set_language("ru")


def make_playlist(title: str, owner: int, album, names: list[str], kind: str = "playlist", restricted=()) -> PlaylistInfo:
    videos = tuple(
        VideoItem(owner, index, name, 10, "author", "закрыто" if index in restricted else "")
        for index, name in enumerate(names, start=1)
    )
    return PlaylistInfo(owner, album, title, videos, kind=kind, total=len(videos))


def wait_for(ctrl: DownloadController, predicate, events: list, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events.extend(ctrl.drain())
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timeout, events={events!r}")


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def test_a_saved_file_is_found_until_it_is_deleted(self):
        store = self.path / "archive.json"
        video = self.path / "old" / "Ролик.mp4"
        video.parent.mkdir()
        video.write_bytes(b"x")
        Archive(store).add("-1_2_ключ", video)
        archive = Archive(store)  # read back from the file
        self.assertEqual(archive.where("-1_2"), video.resolve())
        item = VideoItem(-1, 2, "Ролик", 1, "", "", access_key="другой")
        self.assertEqual(archive.found([item]), {item.raw_id: video.resolve()})
        self.assertEqual(archive.found([item], skip={item.raw_id}), {})
        video.unlink()
        self.assertIsNone(archive.where("-1_2"))
        self.assertEqual(archive.found([item]), {})

    def test_an_empty_or_broken_file_is_no_archive(self):
        store = self.path / "archive.json"
        store.write_text("не json", encoding="utf-8")
        self.assertIsNone(Archive(store).where("-1_2"))
        (self.path / "empty.mp4").write_bytes(b"")
        archive = Archive()
        archive.add("-1_2", self.path / "empty.mp4")
        self.assertIsNone(archive.where("-1_2"))

    def test_the_queue_writes_a_video_only_when_its_file_is_there(self):
        ctrl = DownloadController(Archive(self.path / "archive.json"))
        playlist = make_playlist("Сериал", -5, 7, ["один", "плохой"])

        def fake_download(client, item, dest, max_height, cancel, on_progress):
            if item.title == "плохой":
                raise VkError(0, "нет файла", "access")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"video")
            return "360p"

        events = []
        with patch("vkdw.controller.download_video", side_effect=fake_download):
            job = ctrl.make_job(playlist, list(enumerate(playlist.videos, start=1)), self.path, "360p", True)
            ctrl.enqueue(job, start=True)
            wait_for(ctrl, lambda: any(event[0] == "idle" for event in events), events)
        again = Archive(self.path / "archive.json")
        self.assertEqual(again.where("-5_1"), (self.path / "Сериал" / "001 - один.mp4").resolve())
        self.assertIsNone(again.where("-5_2"))


class NewVideosTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)
        self.ctrl = DownloadController()

    def tearDown(self):
        self.folder.cleanup()

    def test_new_means_not_blocked_not_in_the_folder_and_not_in_the_archive(self):
        playlist = make_playlist("Сериал", -5, 7, ["один", "два", "три", "четыре"], restricted={4})
        where = self.path / "Сериал"
        where.mkdir()
        (where / "001 - один.mp4").write_bytes(b"x")
        # «два» was renamed in VK since it was saved: only the archive knows the file.
        old = where / "002 - старое имя.mp4"
        old.write_bytes(b"x")
        self.ctrl.archive.add("-5_2", old)
        new = self.ctrl.new_videos(playlist, self.path, True)
        self.assertEqual([item.title for _index, item in new], ["три"])
        old.unlink()  # deleted: it may download again
        self.assertEqual([item.title for _index, item in self.ctrl.new_videos(playlist, self.path, True)], ["два", "три"])


class FakeClient:
    """VkClient for check_lists: each target gives a list, or VkError."""

    answers: dict = {}
    asked: list = []

    def __init__(self):
        pass

    def open_target(self, target, cancel=None, limit=0):
        FakeClient.asked.append(target)
        answer = FakeClient.answers[(target.owner_id, target.item_id)]
        if isinstance(answer, Exception):
            raise answer
        return answer


class CheckListsTests(unittest.TestCase):
    def setUp(self):
        self.ctrl = DownloadController()
        self.patches = [patch("vkdw.controller.VkClient", FakeClient), patch("vkdw.controller.CHECK_PAUSE", 0)]
        for item in self.patches:
            item.start()
        FakeClient.asked = []

    def tearDown(self):
        self.ctrl.close()
        for item in self.patches:
            item.stop()

    def test_every_list_is_read_and_one_failure_does_not_stop_the_rest(self):
        series = make_playlist("Сериал", -5, 7, ["один", "два"])
        page = make_playlist("Канал", -9, None, ["новый"], kind="owner")
        FakeClient.answers = {(-5, 7): series, (-1, 1): VkError(15, "закрыто", "access"), (-9, 0): page}
        targets = [Target("playlist", -5, 7), Target("playlist", -1, 1), Target("owner", -9, 0)]
        events = []
        self.assertTrue(self.ctrl.check_lists(targets))
        self.assertFalse(self.ctrl.check_lists(targets))  # one check at a time
        wait_for(self.ctrl, lambda: not self.ctrl.checking, events)
        checked = [(event[1], event[3]) for event in events if event[0] == "checked"]
        self.assertEqual(checked, [((-5, 7), ""), ((-1, 1), "закрыто"), ((-9, "all"), "")])
        self.assertEqual(events[-1], ("check_done", 2, 1))
        self.assertIs(self.ctrl.playlists[(-5, 7)], series)
        self.assertEqual(self.ctrl.sources[(-9, "all")], Target("owner", -9, 0))

    def test_an_owner_page_keeps_the_videos_read_before(self):
        old = make_playlist("Канал", -9, None, ["а", "б", "в"], kind="owner")
        self.ctrl.playlists[old.key] = old
        fresh_items = (VideoItem(-9, 10, "свежий", 10, "", ""), old.videos[0])
        fresh = PlaylistInfo(-9, None, "Канал", fresh_items, kind="owner", total=4)
        merged = self.ctrl.adopt(fresh, Target("owner", -9, 0))
        self.assertEqual([item.title for item in merged.videos], ["свежий", "а", "б", "в"])
        self.assertEqual(merged.total, 4)


try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    import vkdw.ui  # noqa: F401
except ImportError:  # pragma: no cover - only without the GUI libraries
    QApplication = None


@unittest.skipIf(QApplication is None, "no Qt")
class WindowArchiveTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)
        self.save = patch("vkdw.ui.window.save_settings")
        self.save.start()
        self.patches = [patch("vkdw.controller.VkClient", FakeClient), patch("vkdw.controller.CHECK_PAUSE", 0)]
        for item in self.patches:
            item.start()
        self.window = None

    def tearDown(self):
        if self.window is not None:
            self.window.controller.close()
            self.window._confirm_close = lambda: True
            self.window.close()
            self.window.deleteLater()
        self.app.processEvents()
        for item in self.patches:
            item.stop()
        self.save.stop()
        self.folder.cleanup()

    def _window(self, settings):
        from vkdw.ui.window import MainWindow

        self.window = MainWindow({"folder": str(self.path / "new"), **settings}, watch_system_theme=False)
        return self.window

    def _wait(self, predicate, timeout: float = 5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            self.window._pump()
            if predicate():
                return
            time.sleep(0.02)
        self.fail("timeout")

    def test_a_video_saved_to_another_folder_is_marked_saved_before(self):
        window = self._window({})
        old = self.path / "old" / "001 - один.mp4"
        old.parent.mkdir()
        old.write_bytes(b"x")
        window.controller.archive.add("-5_1", old)
        playlist = make_playlist("Сериал", -5, 7, ["один", "два"])
        window.controller._events.put(("loaded", "open", None, playlist, Target("playlist", -5, 7)))
        window._pump()
        self.assertEqual(window.model.checked, {"-5_2"})
        self.assertEqual(window.model.note("-5_1"), ("elsewhere", "done"))
        tip = window.model.index(0).data(0x03)  # ToolTipRole
        self.assertIn(str(old.resolve()), tip)
        _level, title, text = window.notices._shown["saved"]
        self.assertEqual(title, "Уже скачаны: 1 из 2")
        self.assertIn("в другую папку", text)

    def test_check_all_bookmarks_counts_new_videos_and_queues_them(self):
        series = make_playlist("Сериал", -5, 7, ["один", "два", "три"])
        other = make_playlist("Другой", -6, 8, ["старый"])
        marks = [bookmarks.for_playlist(series), bookmarks.for_playlist(other)]
        FakeClient.answers = {(-5, 7): series, (-6, 8): other}
        window = self._window({"bookmarks": bookmarks.to_data(marks)})
        (self.path / "new" / "Сериал").mkdir(parents=True)
        (self.path / "new" / "Сериал" / "001 - один.mp4").write_bytes(b"x")
        (self.path / "new" / "Другой").mkdir(parents=True)
        (self.path / "new" / "Другой" / "001 - старый.mp4").write_bytes(b"x")

        # Real toasts would outlive the window and fire their timers in later tests.
        quiet = patch.object(window, "_toast")
        toast = quiet.start()
        self.addCleanup(quiet.stop)
        window._show_bookmarks()
        window._bookmarks_view.checkAll.emit()
        self._wait(lambda: not window.controller.checking and window._bookmark_checked.keys() == {(-5, 7), (-6, 8)})
        self.assertIn("2 ролика", toast.call_args[0][2])
        rows = {row.title: row.status for row in window._bookmark_rows()}
        self.assertEqual(rows, {"Сериал": "+2 новых", "Другой": "нет новых"})
        self.assertEqual(window.bookmarks_button.text(), "Закладки +2")
        view = window._bookmarks_view
        self.assertEqual(view.download_button.text(), "Скачать новое (2)")
        self.assertFalse(view.download_button.isHidden())

        with window.controller._lock:
            window.controller._queue.paused = True  # keep the queue from downloading for real
        view.downloadAll.emit()
        self.assertEqual(toast.call_args[0][2], "В очереди: 2 ролика. Закладок с новым: 1.")
        jobs = window.controller.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual([item.title for _index, item in jobs[0].items], ["два", "три"])
        self.assertEqual([index for index, _item in jobs[0].items], [2, 3])
        # Queued now, so no longer new.
        self.assertEqual({row.title: row.status for row in window._bookmark_rows()}["Сериал"], "нет новых")
        # The flyout goes with the window. Closed here, its fade-out would run past the window
        # deleted in tearDown and crash a later test.

    def test_new_videos_of_bookmarks_are_counted_again_only_when_files_may_have_changed(self):
        series = make_playlist("Сериал", -5, 7, ["один", "два"])
        FakeClient.answers = {(-5, 7): series}
        window = self._window({"bookmarks": bookmarks.to_data([bookmarks.for_playlist(series)])})
        quiet = patch.object(window, "_toast")
        quiet.start()
        self.addCleanup(quiet.stop)
        window._check_bookmarks()
        self._wait(lambda: not window.controller.checking and (-5, 7) in window._bookmark_checked)
        self.assertEqual(window.bookmarks_button.text(), "Закладки +2")

        counted = patch.object(window.controller, "new_videos", wraps=window.controller.new_videos)
        calls = counted.start()
        self.addCleanup(counted.stop)
        for _ in range(3):
            window._update_bookmarks_view()
        self.assertEqual(calls.call_count, 0)

        (self.path / "new" / "Сериал").mkdir(parents=True)
        (self.path / "new" / "Сериал" / "001 - один.mp4").write_bytes(b"x")
        window._recheck_saved()  # as when the window is switched back to
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(window.bookmarks_button.text(), "Закладки +1")


    def test_a_bookmarked_community_counts_what_came_after_the_bookmark(self):
        def video(number, added):
            return VideoItem(-9, number, f"выпуск {number}", 10, "канал", "", date=added, added=added)

        before = PlaylistInfo(-9, None, "Канал", (video(2, 2000), video(1, 1000)), kind="owner", total=2)
        mark = bookmarks.started(bookmarks.for_playlist(before), before)
        after = replace(before, videos=(video(3, 3000),) + before.videos, total=3)
        FakeClient.answers = {(-9, 0): after}
        window = self._window({"bookmarks": bookmarks.to_data([mark])})
        quiet = patch.object(window, "_toast")
        toast = quiet.start()
        self.addCleanup(quiet.stop)
        window._check_bookmarks()
        self._wait(lambda: not window.controller.checking and (-9, "all") in window._bookmark_checked)
        # The old videos are not saved either, but they were there before the bookmark.
        self.assertEqual(window.bookmarks_button.text(), "Закладки +1")
        self.assertEqual([item.video_id for _index, item in window._bookmark_new((-9, "all"))], [3])

        # Opened, the community has its new video ticked and says so.
        window.controller._events.put(("loaded", "open", None, after, Target("owner", -9, 0)))
        window._pump()
        self.assertEqual(window.model.checked, {"-9_3"})
        self.assertIn("Новых в «Канал»: 1 ролик", toast.call_args[0][2])

        window._mark_seen((-9, "all"))
        self.assertEqual(window._bookmark((-9, "all")).since, 3000)
        self.assertEqual(window.bookmarks_button.text(), "Закладки")


if __name__ == "__main__":
    unittest.main()
