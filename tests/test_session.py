import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vkdw import session
from vkdw.controller import DownloadController
from vkdw.i18n import set_language
from vkdw.vk_client import Cancelled, PlaylistInfo, Target, VideoItem, VkError


def make_playlist(title: str, owner: int, album: int, names: list[str]) -> PlaylistInfo:
    videos = tuple(
        VideoItem(owner, index, name, 10, "author", "", access_key="key" if index == 2 else None, thumb_url="https://t/1.jpg", date=1700000000)
        for index, name in enumerate(names, start=1)
    )
    return PlaylistInfo(owner, album, title, videos, cover_url="https://c/1.jpg")


class SessionFileTests(unittest.TestCase):
    def setUp(self):
        set_language("ru")
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "session.json"

    def tearDown(self):
        self.folder.cleanup()

    def test_everything_comes_back_as_it_was(self):
        ctrl = DownloadController()
        playlist = make_playlist("Сериал", -5, 7, ["один", "два", "три"])
        ctrl.playlists[playlist.key] = playlist
        ctrl.sources[playlist.key] = Target("playlist", -5, 7)
        ctrl.notes[playlist.key] = {"-5_1": ("360p", "done"), "-5_2_key": ("", "active")}
        job = ctrl.make_job(playlist, list(enumerate(playlist.videos, start=1)), Path("D:/Видео"), "720p", True)
        job.status = "running"
        job.failures = (("-5_3", "нет файла", "access"),)
        job.tagged = frozenset({"-5_2"})
        ctrl._queue.add(job)

        text = session.dump(ctrl.session())
        self.assertTrue(session.save(self.path, text))
        self.assertFalse(self.path.with_name("session.json.tmp").exists())

        state = session.load(self.path)
        self.assertEqual(state.playlists, {playlist.key: playlist})
        self.assertEqual(state.sources, {playlist.key: Target("playlist", -5, 7)})
        # The video that was downloading is shown as cut off, not as still running.
        self.assertEqual(state.notes[playlist.key], {"-5_1": ("360p", "done"), "-5_2_key": ("", "stopped")})
        back = state.jobs[0]
        self.assertEqual(back.status, "interrupted")
        self.assertEqual(back.items, job.items)
        self.assertEqual(back.target_dir, job.target_dir)
        self.assertEqual((back.max_height, back.quality, back.naming), (720, "720p", "number"))
        self.assertEqual(back.failures, job.failures)
        self.assertEqual(back.tagged, frozenset({"-5_2"}))
        self.assertEqual(back.playlist_key, playlist.key)

    def test_age_warnings_are_kept_and_older_rows_without_them_are_read(self):
        item = VideoItem(-5, 1, "один", 10, "author", "", None, "", 0, "18+")
        self.assertEqual(session._read_video(session._video(item)), item)
        old_row = session._video(item)[:9]
        self.assertEqual(session._read_video(old_row).warning, "")

    def test_the_adding_time_is_kept_and_older_rows_take_the_upload_time(self):
        item = VideoItem(-5, 1, "один", 10, "author", "", None, "", 1000, "", 2000)
        self.assertEqual(session._read_video(session._video(item)), item)
        self.assertEqual(session._read_video(session._video(item)[:10]).added, 1000)

    def test_single_videos_saved_under_the_old_shared_key_get_their_own(self):
        # Before, a single video was keyed (owner, None): A was queued, then B of the same owner
        # took the list, so the file holds B under that key and a job for A pointing at it.
        a = VideoItem(-7, 1, "A", 10, "o", "")
        b = VideoItem(-7, 2, "B", 10, "o", "")
        ctrl = DownloadController()
        job = ctrl.make_job(PlaylistInfo(-7, None, "A", (a,), kind="video"), [(1, a)], Path("D:/V"), "best", True)
        job.playlist_key = (-7, None)
        old = session.Session(
            jobs=[job],
            playlists={(-7, None): PlaylistInfo(-7, None, "B", (b,), kind="video")},
            sources={(-7, None): Target("video", -7, 2)},
            notes={(-7, None): {"-7_1": ("360p", "done"), "-7_2": ("", "failed")}},
            draft=(-7, None),
            picks={(-7, None): {"-7_2"}},
        )
        state = session.parse(session.dump(old))
        key_a, key_b = (-7, "video", 1), (-7, "video", 2)
        self.assertEqual(state.jobs[0].playlist_key, key_a)
        self.assertEqual(set(state.playlists), {key_a, key_b})
        self.assertEqual(state.playlists[key_a].videos, (a,))
        self.assertEqual(state.sources, {key_a: Target("video", -7, 1), key_b: Target("video", -7, 2)})
        self.assertEqual(state.notes, {key_a: {"-7_1": ("360p", "done")}, key_b: {"-7_2": ("", "failed")}})
        self.assertEqual((state.draft, state.picks), (key_b, {key_b: {"-7_2"}}))

    def test_no_file_is_no_session(self):
        self.assertIsNone(session.load(self.path))

    def test_a_broken_file_is_set_aside(self):
        self.path.write_text('{"version": 1, "jobs": [{"title": ', encoding="utf-8")
        with self.assertRaises(ValueError):
            session.load(self.path)
        self.assertFalse(self.path.exists())
        self.assertTrue(self.path.with_name("session.bad.json").exists())

    def test_another_format_is_not_read(self):
        self.path.write_text('{"version": 99}', encoding="utf-8")
        with self.assertRaises(ValueError):
            session.load(self.path)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        set_language("ru")
        self.order: list[str] = []
        self.download = patch("vkdw.controller.download_video", side_effect=self._fake_download)
        self.download.start()
        self.ctrl = DownloadController()

    def tearDown(self):
        self.ctrl.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.ctrl._lock:
                if not self.ctrl._worker_active:
                    break
            time.sleep(0.02)
        self.download.stop()

    def _fake_download(self, client, item, dest, max_height, cancel, on_progress):
        if cancel.is_set():
            raise Cancelled()
        self.order.append(item.title)
        return "360p"

    def _wait(self, predicate, timeout: float = 5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.ctrl.drain()
            if predicate():
                return
            time.sleep(0.02)
        self.fail(f"timeout, queue={[(job.title, job.status) for job in self.ctrl.jobs()]}")

    def _saved_queue(self) -> session.Session:
        old = DownloadController()
        first = make_playlist("Первый", 1, 10, ["один", "два"])
        second = make_playlist("Второй", 2, 20, ["три"])
        done = make_playlist("Готовый", 3, 30, ["четыре"])
        for playlist in (done, first, second):
            old.playlists[playlist.key] = playlist
            job = old.make_job(playlist, list(enumerate(playlist.videos, start=1)), Path("unused"), "360p", True)
            old._queue.add(job)
        jobs = old._queue.jobs
        jobs[0].status, jobs[1].status = "done", "running"
        return session.parse(session.dump(old.session()))

    def test_the_queue_comes_back_paused_and_resumes_where_it_stopped(self):
        pending = self.ctrl.restore(self._saved_queue())
        self.assertEqual(pending, 2)
        self.assertTrue(self.ctrl.paused)
        self.assertEqual([(job.title, job.status) for job in self.ctrl.jobs()],
                         [("Готовый", "done"), ("Первый", "interrupted"), ("Второй", "waiting")])
        self.ctrl.drain()
        self.assertIn("Восстановлена очередь", "\n".join(entry.text for entry in self.ctrl.log))
        self.assertFalse(self.ctrl.downloading)

        self.ctrl.resume()
        self._wait(lambda: not self.ctrl.downloading and all(job.status == "done" for job in self.ctrl.jobs()))
        self.assertEqual(self.order, ["один", "два", "три"])

    def test_new_jobs_and_collections_do_not_take_restored_numbers(self):
        state = self._saved_queue()
        collection = PlaylistInfo(0, None, "Сборник", make_playlist("x", 4, 1, ["пять"]).videos, kind="collection", collection_id=3)
        state.playlists[collection.key] = collection
        self.ctrl.restore(state)
        playlist = make_playlist("Новый", 9, 90, ["шесть"])
        job = self.ctrl.make_job(playlist, [(1, playlist.videos[0])], Path("unused"), "360p", True)
        self.assertNotIn(job.job_id, [other.job_id for other in self.ctrl.jobs()])
        fresh, _added = self.ctrl.add_to_collection(None, playlist.videos)
        self.assertNotEqual(fresh.key, collection.key)

    def test_a_session_of_an_older_version_with_needs_token_is_read(self):
        text = session.dump(self._saved_queue()).replace('"outcome":', '"needs_token":true,"outcome":')
        self.assertIn("needs_token", text)
        self.assertEqual(len(session.parse(text).jobs), len(self._saved_queue().jobs))


try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    import vkdw.ui  # noqa: F401
except ImportError:  # pragma: no cover - only without the GUI libraries
    QApplication = None


@unittest.skipIf(QApplication is None, "no Qt")
class WindowSessionTests(unittest.TestCase):
    def setUp(self):
        set_language("ru")
        self.app = QApplication.instance() or QApplication([])
        self.folder = tempfile.TemporaryDirectory()
        self.file = Path(self.folder.name) / "session.json"
        self.save = patch("vkdw.ui.window.save_settings")
        self.save.start()
        self.download = patch("vkdw.controller.download_video", return_value="360p")
        self.download.start()
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.controller.close()
            window._confirm_close = lambda: True
            window.close()
            window.deleteLater()
        self.app.processEvents()
        self.download.stop()
        self.save.stop()
        self.folder.cleanup()

    def _window(self):
        from vkdw.ui.window import MainWindow

        window = MainWindow({"folder": self.folder.name}, watch_system_theme=False, session_file=self.file)
        self.windows.append(window)
        return window

    def test_queue_draft_and_collection_survive_a_restart(self):
        window = self._window()
        ctrl = window.controller
        with ctrl._lock:
            ctrl._queue.paused = True  # keep the job waiting
        queued = make_playlist("В очереди", 1, 10, ["а", "б"])
        ctrl._events.put(("loaded", "open", None, queued, Target("playlist", 1, 10)))
        window._pump()
        window._add_to_queue()
        window._collect([queued.videos[0]])
        draft = make_playlist("Черновик", 2, 20, ["в", "г", "д"])
        ctrl._events.put(("loaded", "open", None, draft, Target("playlist", 2, 20)))
        window._pump()
        window.model.uncheck({draft.videos[0].raw_id})
        cards = window.sidebar.card_ids()
        self.assertEqual(len(cards), 3)
        # Saving waits a little, so a burst of changes is written once.
        self.assertTrue(window._save_timer.isActive())
        window._save_session()
        self.assertTrue(self.file.exists())

        again = self._window()
        self.assertEqual(again.sidebar.card_ids(), cards)
        self.assertEqual(again.dock.mode(), "paused")
        self.assertTrue(again.controller.can_resume())
        again._on_card("draft")
        self.assertEqual(again.hero.title.text(), "Черновик")
        self.assertEqual(again.model.checked, {draft.videos[1].raw_id, draft.videos[2].raw_id})

    def test_nothing_is_kept_without_a_session_file(self):
        from vkdw.ui.window import MainWindow

        window = MainWindow({"folder": self.folder.name}, watch_system_theme=False)
        self.windows.append(window)
        window._refresh_queue()
        self.assertFalse(window._save_timer.isActive())
        window._save_session()
        self.assertFalse(self.file.exists())


if __name__ == "__main__":
    unittest.main()
