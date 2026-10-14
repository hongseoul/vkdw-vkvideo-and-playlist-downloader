import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from vkdw.controller import DownloadController, saved_ids, target_dir
from vkdw.downloader import EXISTS, download_video, name_width, output_path
from vkdw.i18n import set_language
from vkdw.vk_client import Cancelled, PlaylistInfo, Target, VideoItem, VkError


def make_playlist(title: str, owner: int, album: int, names: list[str]) -> PlaylistInfo:
    videos = tuple(VideoItem(owner, index, name, 10, "author", "") for index, name in enumerate(names, start=1))
    return PlaylistInfo(owner, album, title, videos)


class SavedFilesTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def test_only_finished_files_where_the_download_saves_count(self):
        playlist = make_playlist("Сериал", 1, 10, ["один", "два", "три", "четыре"])
        (self.path / "001 - один.mp4").write_bytes(b"x")
        (self.path / "002 - два.mp4").write_bytes(b"")  # empty: not a finished file
        (self.path / "003 - три.mp4.part").write_bytes(b"x")  # still downloading
        (self.path / "4 - четыре.mp4").write_bytes(b"x")  # another numbering
        pairs = list(enumerate(playlist.videos, start=1))
        self.assertEqual(saved_ids(pairs, self.path, "number", len(pairs)), {"1_1"})
        self.assertEqual(saved_ids(pairs, self.path / "нет такой", "number", len(pairs)), set())

    def test_the_check_follows_the_file_names_of_the_download(self):
        playlist = make_playlist("Сериал", 1, 10, ["один"])
        item = playlist.videos[0]
        dest = output_path(self.path, item, 1, name_width(1), "number")
        dest.write_bytes(b"x")
        self.assertEqual(download_video(None, item, dest, None, threading.Event(), None), EXISTS)
        self.assertEqual(saved_ids([(1, item)], self.path, "number", 1), {item.raw_id})
        self.assertEqual(target_dir(self.path, playlist, True), self.path / "Сериал")
        self.assertEqual(target_dir(self.path, playlist, False), self.path)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        set_language("ru")
        self.ctrl = DownloadController()
        self.download = patch("vkdw.controller.download_video", side_effect=self._fake_download)
        self.download.start()
        self.order: list[str] = []
        self.release = threading.Event()
        self.entered = threading.Event()
        self.block_first = False
        self.events: list[tuple] = []

    def tearDown(self):
        self.release.set()
        self.ctrl.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.ctrl._lock:
                if not self.ctrl._worker_active:
                    break
            time.sleep(0.02)
        self.download.stop()

    def _fake_download(self, client, item, dest, max_height, cancel, on_progress):
        self.order.append(item.title)
        if self.block_first and not self.entered.is_set():
            self.entered.set()
            while not self.release.is_set():
                if cancel.is_set():
                    raise Cancelled()
                time.sleep(0.02)
        if cancel.is_set():
            raise Cancelled()
        if item.title == "плохой":
            raise VkError(0, "нет файла", "access")
        if item.title == "большой":
            raise VkError(0, "Не хватает места на диске", "disk")
        if item.title.startswith("hls"):
            raise VkError(0, "нужен ffmpeg", "ffmpeg")
        if item.title == "старый":
            return EXISTS
        on_progress(10, 10, 100)
        return "360p"

    def _queue(self, playlist: PlaylistInfo, *, start: bool) -> tuple[str, str]:
        items = list(enumerate(playlist.videos, start=1))
        job = self.ctrl.make_job(playlist, items, Path("unused"), "360p", True)
        return self.ctrl.enqueue(job, start=start)

    def _drain(self) -> None:
        self.events.extend(self.ctrl.drain())

    def _statuses(self):
        return [(job.title, job.status) for job in self.ctrl.jobs()]

    def _log_text(self) -> str:
        return "\n".join(entry.text for entry in self.ctrl.log)

    def _wait(self, predicate, timeout: float = 5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._drain()
            if predicate():
                return
            time.sleep(0.02)
        self.fail(f"timeout, queue={self._statuses()}, log={self._log_text()!r}")

    def _loaded(self, playlist: PlaylistInfo, source: Target | None = None) -> None:
        self.ctrl._events.put(("loaded", "open", None, playlist, source))
        self._drain()

    def test_next_playlist_starts_after_the_previous_one_finishes(self):
        self.block_first = True
        self._queue(make_playlist("Первый", 1, 10, ["один"]), start=True)
        self._wait(lambda: self.entered.is_set() and self._statuses()[:1] == [("Первый", "running")])
        self.assertTrue(self.ctrl.downloading)

        placed, message = self._queue(make_playlist("Второй", 2, 20, ["два"]), start=False)
        self.assertEqual(placed, "tail")
        self.assertIn("Начнётся после текущей выгрузки", message)
        self.assertEqual(self._statuses(), [("Первый", "running"), ("Второй", "waiting")])

        self.release.set()
        self._wait(lambda: self._statuses() == [("Первый", "done"), ("Второй", "done")] and not self.ctrl.downloading)
        self.assertEqual(self.order, ["один", "два"])
        self.assertIn("Запускаю «Второй»", self._log_text())
        self.assertIn(("idle", "Очередь завершена.", "queue_finished"), self.events)

    def test_download_while_running_goes_next(self):
        self.block_first = True
        self._queue(make_playlist("Первый", 1, 10, ["один"]), start=True)
        self._wait(lambda: self.entered.is_set())
        self._queue(make_playlist("Второй", 2, 20, ["два"]), start=False)
        placed, _message = self._queue(make_playlist("Третий", 3, 30, ["три"]), start=True)
        self.assertEqual(placed, "next")
        self.assertEqual([title for title, _status in self._statuses()], ["Первый", "Третий", "Второй"])

    def test_failed_video_does_not_stop_the_next_playlist(self):
        self._queue(make_playlist("Первый", 1, 10, ["плохой"]), start=False)
        self._wait(lambda: self._statuses()[:1] == [("Первый", "running")] or self.order == ["плохой"])
        self._queue(make_playlist("Второй", 2, 20, ["хороший"]), start=False)
        self._wait(lambda: self._statuses() == [("Первый", "done"), ("Второй", "done")])
        self.assertEqual(self.order, ["плохой", "хороший"])
        self.assertIn("Не скачано «плохой»", self._log_text())
        errors = [entry.text for entry in self.ctrl.log if entry.level == "error"]
        self.assertTrue(any("плохой" in text for text in errors))

    def test_missing_ffmpeg_gets_one_hint_per_playlist(self):
        self._queue(make_playlist("Первый", 1, 10, ["hls 1", "hls 2", "хороший"]), start=True)
        self._wait(lambda: self._statuses() == [("Первый", "done")])
        hints = [entry for entry in self.ctrl.log if "winget install ffmpeg" in entry.text]
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0].level, "warn")
        self.assertEqual([kind for _raw_id, _text, kind in self.ctrl.jobs()[0].failures], ["ffmpeg", "ffmpeg"])

    def test_no_room_on_the_disk_pauses_the_queue(self):
        self._queue(make_playlist("Первый", 1, 10, ["большой", "после"]), start=True)
        self._queue(make_playlist("Второй", 2, 20, ["два"]), start=False)
        self._wait(lambda: not self.ctrl.downloading and self._statuses()[0][1] == "interrupted")
        self.assertEqual(self.order, ["большой"])
        self.assertEqual(self._statuses()[1][1], "waiting")
        self.assertTrue(self.ctrl.paused)
        self.assertEqual(self.ctrl.jobs()[0].failures, ())  # nothing to retry: ▶ goes on with it
        self.assertIn("Освободите место", self._log_text())

    def test_low_space_is_told_before_a_playlist(self):
        with patch("vkdw.controller.free_space", return_value=200 * 1024 * 1024):
            self._queue(make_playlist("Первый", 1, 10, ["один"]), start=True)
            self._wait(lambda: self._statuses() == [("Первый", "done")])
        warnings = [entry.text for entry in self.ctrl.log if entry.level == "warn"]
        self.assertTrue(any("200,0 МБ" in text for text in warnings), warnings)

    def test_stop_pauses_and_resume_continues_then_takes_the_next(self):
        self.block_first = True
        self._queue(make_playlist("Первый", 1, 10, ["один"]), start=True)
        self._wait(lambda: self.entered.is_set())
        self._queue(make_playlist("Второй", 2, 20, ["два"]), start=False)
        self.ctrl.stop()
        self._wait(lambda: not self.ctrl.downloading and self._statuses()[0][1] == "interrupted")
        self.assertEqual(self.order, ["один"])
        self.assertEqual(self._statuses()[1][1], "waiting")
        self.assertTrue(self.ctrl.paused)
        self.assertTrue(self.ctrl.can_resume())
        self.assertNotIn("Запускаю «Второй»", self._log_text())
        idle = [event for event in self.events if event[0] == "idle"]
        self.assertEqual(idle[-1][2], "paused")

        self.release.set()
        self.ctrl.resume()
        self._wait(lambda: self._statuses() == [("Первый", "done"), ("Второй", "done")])
        self.assertEqual(self.order, ["один", "один", "два"])

    def test_remove_waiting_playlist_and_keep_the_running_one(self):
        self.block_first = True
        self._queue(make_playlist("Первый", 1, 10, ["один"]), start=True)
        self._wait(lambda: self.entered.is_set())
        self._queue(make_playlist("Второй", 2, 20, ["два"]), start=False)
        second = self.ctrl.jobs()[1].job_id
        self.assertEqual(self.ctrl.remove([second]), 1)
        self.assertEqual(self._statuses(), [("Первый", "running")])
        self.release.set()
        self._wait(lambda: self._statuses() == [("Первый", "done")] and not self.ctrl.downloading)
        self.assertEqual(self.order, ["один"])

    def test_removing_the_running_playlist_stops_it_and_the_queue_goes_on(self):
        self.block_first = True
        self._queue(make_playlist("Первый", 1, 10, ["один", "лишний"]), start=True)
        self._wait(lambda: self.entered.is_set())
        self._queue(make_playlist("Второй", 2, 20, ["два"]), start=False)
        first = self.ctrl.jobs()[0].job_id
        self.assertEqual(self.ctrl.remove([first]), 1)
        self._wait(lambda: self._statuses() == [("Второй", "done")] and not self.ctrl.downloading)
        self.assertEqual(self.order, ["один", "два"])
        self.assertFalse(self.ctrl.paused)
        self.assertIn("Загрузка «Первый» остановлена и убрана из очереди", self._log_text())
        self.assertEqual([event for event in self.events if event[0] == "idle"][-1][2], "finished")

    def test_removing_the_only_running_playlist_leaves_the_queue_idle(self):
        self.block_first = True
        self._queue(make_playlist("Первый", 1, 10, ["один"]), start=True)
        self._wait(lambda: self.entered.is_set())
        self.assertEqual(self.ctrl.clear(), 1)
        self._wait(lambda: not self.ctrl.downloading)
        self.assertEqual(self.ctrl.jobs(), [])
        self.assertEqual([event for event in self.events if event[0] == "idle"][-1][2], "idle")
        # The next playlist starts as usual.
        self._queue(make_playlist("Второй", 2, 20, ["два"]), start=False)
        self._wait(lambda: self._statuses() == [("Второй", "done")])

    def test_removing_the_paused_playlist_lifts_the_pause(self):
        self.block_first = True
        self._queue(make_playlist("Первый", 1, 10, ["один"]), start=True)
        self._wait(lambda: self.entered.is_set())
        self.ctrl.stop()
        self._wait(lambda: not self.ctrl.downloading and self._statuses() == [("Первый", "interrupted")])
        self.assertTrue(self.ctrl.paused)
        self.ctrl.remove([self.ctrl.jobs()[0].job_id])
        self.assertFalse(self.ctrl.paused)
        self.assertEqual(self.ctrl.pending_count(), 0)
        self._queue(make_playlist("Второй", 2, 20, ["два"]), start=False)
        self._wait(lambda: self._statuses() == [("Второй", "done")])

    def test_unfinished_queue_is_reported(self):
        with self.ctrl._lock:
            self.ctrl._queue.paused = True
        self.assertFalse(self.ctrl.has_unfinished())
        self._queue(make_playlist("Первый", 1, 10, ["один"]), start=False)
        self.assertTrue(self.ctrl.has_unfinished())

    def test_video_statuses_are_kept_per_playlist(self):
        playlist = make_playlist("Первый", 1, 10, ["один", "плохой", "старый"])
        self._queue(playlist, start=False)
        self._wait(lambda: "Готово «Первый»" in self._log_text())
        notes = self.ctrl.notes[playlist.key]
        self.assertEqual(notes[playlist.videos[0].raw_id], ("360p", "done"))
        self.assertEqual(notes[playlist.videos[1].raw_id], ("нет файла", "failed"))
        self.assertEqual(notes[playlist.videos[2].raw_id], (EXISTS, "done"))
        files = [event for event in self.events if event[0] == "file"]
        self.assertTrue(files)
        self.assertEqual(files[-1][2:4], (playlist.key, playlist.videos[0].raw_id))
        self.assertIn(("idle", self.ctrl.last_summary, "finished"), self.events)

    def test_job_takes_quality_folder_and_sorted_items(self):
        playlist = make_playlist("Мой: плейлист", 1, 10, ["а", "б", "в"])
        items = [(3, playlist.videos[2]), (1, playlist.videos[0])]
        job = self.ctrl.make_job(playlist, items, Path("root"), "720p", True)
        self.assertEqual([index for index, _item in job.items], [1, 3])
        self.assertEqual(job.max_height, 720)
        self.assertEqual(job.target_dir, Path("root") / "Мой плейлист")
        self.assertEqual(job.video_count, 2)
        self.assertEqual(job.naming, "number")
        flat = self.ctrl.make_job(playlist, items, Path("root"), "best", False)
        self.assertIsNone(flat.max_height)
        self.assertEqual(flat.target_dir, Path("root"))

    def test_opened_playlist_is_remembered_and_logged(self):
        playlist = PlaylistInfo(
            1,
            10,
            "Плейлист",
            (VideoItem(1, 1, "а", 10, "", ""), VideoItem(1, 2, "б", 10, "", "закрыто")),
        )
        self.ctrl.loading = True
        self._loaded(playlist, Target("playlist", 1, 10))
        self.assertFalse(self.ctrl.loading)
        self.assertIs(self.ctrl.playlists[playlist.key], playlist)
        self.assertEqual(self.ctrl.sources[playlist.key], Target("playlist", 1, 10))
        self.assertIn("Открыт «Плейлист», роликов: 2.", self._log_text())
        self.assertIn("недоступные", self._log_text())
        self.assertIn(("playlist", playlist, "open"), self.events)

    def test_bad_link_is_refused_at_once(self):
        with self.assertRaises(VkError):
            self.ctrl.open_link("https://example.com/video")
        self.assertFalse(self.ctrl.loading)

    def test_refresh_reads_the_same_link_again(self):
        old = make_playlist("Плейлист", 1, 10, ["а", "б"])
        self._loaded(old, Target("playlist", 1, 10))
        fresh = PlaylistInfo(1, 10, "Плейлист", (old.videos[0], VideoItem(1, 2, "б", 10, "author", "недоступно в регионе")))
        with patch("vkdw.controller.VkClient") as client:
            client.return_value.open_target.return_value = fresh
            self.assertTrue(self.ctrl.refresh(old.key))
            self.assertEqual(self.ctrl.load_mode, "refresh")
            self._wait(lambda: not self.ctrl.loading)
        self.assertEqual(client.return_value.open_target.call_args.args[0], Target("playlist", 1, 10))
        self.assertIs(self.ctrl.playlists[old.key], fresh)
        self.assertIn(("playlist", fresh, "refresh"), self.events)
        self.assertIn("недоступны 1", self._log_text())

    def test_refresh_failure_names_the_kind_of_problem(self):
        old = make_playlist("Плейлист", 1, 10, ["а"])
        self._loaded(old, Target("playlist", 1, 10))
        with patch("vkdw.controller.VkClient") as client:
            client.return_value.open_target.side_effect = VkError(15, "закрыто", "access")
            self.ctrl.refresh(old.key)
            self._wait(lambda: not self.ctrl.loading)
        self.assertIn(("fatal", "закрыто", "access", "refresh", old.key), self.events)
        self.assertIs(self.ctrl.playlists[old.key], old)

    def test_refresh_without_a_source_is_refused(self):
        playlist = make_playlist("Плейлист", 1, 10, ["а"])
        self._loaded(playlist)
        self.assertFalse(self.ctrl.refresh(playlist.key))
        self.assertFalse(self.ctrl.loading)

    def test_retry_takes_only_the_failed_videos(self):
        playlist = make_playlist("Первый", 1, 10, ["один", "плохой", "два"])
        self._queue(playlist, start=False)
        self._wait(lambda: self._statuses() == [("Первый", "done")])
        first = self.ctrl.jobs()[0]
        self.assertEqual([(raw_id, kind) for raw_id, _text, kind in first.failures], [("1_2", "access")])
        retry = self.ctrl.retry_failed(first.job_id)
        self.assertEqual([index for index, _item in retry.items], [2])
        self.assertEqual(retry.target_dir, first.target_dir)
        self.assertNotEqual(retry.job_id, first.job_id)
        self.assertIsNone(self.ctrl.retry_failed("job-404"))

    def test_two_video_links_become_a_collection(self):
        items = [VideoItem(-1, 2, "первый", 10, "", ""), VideoItem(-3, 4, "второй", 10, "", "")]
        with patch("vkdw.controller.VkClient") as client:
            client.return_value.open_videos.return_value = items
            self.ctrl.open_link("https://vk.com/video-1_2 https://vk.com/video-3_4 https://vk.com/video-5_6")
            self._wait(lambda: not self.ctrl.loading)
        client.return_value.open_videos.assert_called_once()
        self.assertEqual(client.return_value.open_videos.call_args.args[0], ["-1_2", "-3_4", "-5_6"])
        playlist = next(event[1] for event in self.events if event[0] == "playlist")
        self.assertEqual(playlist.kind, "collection")
        self.assertEqual([item.title for item in playlist.videos], ["первый", "второй"])
        self.assertEqual(playlist.naming, "date")
        self.assertIn("Не открылись: 1 ссылка", self._log_text())

    def test_collection_skips_videos_it_already_has(self):
        first, added = self.ctrl.add_to_collection(None, [VideoItem(1, 1, "а", 1, "", "")])
        self.assertEqual(added, 1)
        same, added = self.ctrl.add_to_collection(first.key, [VideoItem(1, 1, "а", 1, "", "", access_key="k"), VideoItem(1, 2, "б", 1, "", "")])
        self.assertEqual(added, 1)
        self.assertEqual(same.key, first.key)
        self.assertEqual([item.title for item in same.videos], ["а", "б"])
        other, _added = self.ctrl.add_to_collection(None, [VideoItem(1, 3, "в", 1, "", "")])
        self.assertNotEqual(other.key, first.key)
        self.assertNotEqual(other.title, first.title)
        left = self.ctrl.remove_from_collection(first.key, ["1_1"])
        self.assertEqual([item.title for item in left.videos], ["б"])


class CoverTests(unittest.TestCase):
    """Covers are read from VK in full size and saved under the name given, in the background."""

    def setUp(self):
        set_language("ru")
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)
        self.ctrl = DownloadController()
        self.fetched: list[str] = []

    def tearDown(self):
        self.ctrl.close()
        self.folder.cleanup()

    def _fetch(self, url, context, limit):
        self.fetched.append(url)
        if url == "broken":
            raise OSError("HTTP Error 404")
        buffer = io.BytesIO()
        Image.new("RGB", (16, 9), "green").save(buffer, "JPEG")
        return buffer.getvalue()

    def _save(self, videos, album=None, album_cover="", covers=None) -> tuple:
        client = MagicMock()
        client.album_cover.return_value = album_cover
        client.video_covers.side_effect = lambda raw_ids: {key: url for key, url in (covers or {}).items() if key in raw_ids}
        with patch("vkdw.controller.VkClient", return_value=client), patch("vkdw.controller.fetch_image", side_effect=self._fetch):
            self.ctrl.save_covers(videos, album)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                for event in self.ctrl.drain():
                    if event[0] == "covers_saved":
                        return event
                time.sleep(0.02)
        self.fail("no covers_saved event")

    def test_videos_get_their_full_preview_beside_their_files(self):
        one = VideoItem(1, 1, "один", 10, "", "", thumb_url="small1")
        two = VideoItem(1, 2, "два", 10, "", "", thumb_url="small2")
        bad = VideoItem(1, 3, "без картинки", 10, "", "")
        dests = [(item, self.path / f"00{item.video_id} - {item.title}.mp4") for item in (one, two, bad)]
        _kind, saved, failed = self._save(dests, covers={"1_1": "big1"})
        self.assertEqual([path.name for path in saved], ["001 - один.jpg", "002 - два.jpg"])
        self.assertEqual(failed, 1)
        self.assertEqual(self.fetched, ["big1", "small2"])  # without a full one, the row's preview
        self.assertIn("без картинки", " ".join(entry.text for entry in self.ctrl.log))

    def test_a_playlist_cover_falls_back_to_its_first_video(self):
        playlist = PlaylistInfo(1, 10, "Сериал", (VideoItem(1, 1, "один", 10, "", "", thumb_url="small"),))
        _kind, saved, failed = self._save([], (playlist, self.path / "Сериал" / "Сериал.jpg"), covers={"1_1": "big"})
        self.assertEqual((failed, [path.relative_to(self.path) for path in saved]), (0, [Path("Сериал/Сериал.jpg")]))
        self.assertEqual(self.fetched, ["big"])
        _kind, saved, failed = self._save([], (playlist, self.path / "Сериал.jpg"), album_cover="broken")
        self.assertEqual((saved, failed), ([], 1))


if __name__ == "__main__":
    unittest.main()


class PlaylistFileTests(unittest.TestCase):
    """A numbered playlist gets «Title.m3u8» with every video saved in its folder, in order."""

    def setUp(self):
        set_language("ru")
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)
        self.ctrl = DownloadController()
        self.download = patch("vkdw.controller.download_video", side_effect=self._save)
        self.download.start()

    def tearDown(self):
        self.ctrl.close()
        self.download.stop()
        self.folder.cleanup()

    def _save(self, client, item, dest, max_height, cancel, on_progress):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")
        return "360p"

    def _run(self, playlist: PlaylistInfo, pairs) -> None:
        self.ctrl.playlists[playlist.key] = playlist
        job = self.ctrl.make_job(playlist, pairs, self.path, "360p", True)
        self.ctrl._execute(job)

    def test_saved_videos_are_listed_in_playlist_order(self):
        playlist = make_playlist("Сериал: сезон 1", 1, 10, ["первая", "вторая", "третья"])
        videos = list(enumerate(playlist.videos, start=1))
        self._run(playlist, [videos[2]])
        self._run(playlist, [videos[0]])
        text = (self.path / "Сериал сезон 1" / "Сериал сезон 1.m3u8").read_text(encoding="utf-8")
        self.assertEqual(
            text.splitlines(),
            ["#EXTM3U", "#EXTINF:10,первая", "001 - первая.mp4", "#EXTINF:10,третья", "003 - третья.mp4"],
        )

    def test_pages_and_collections_get_no_list(self):
        page = PlaylistInfo(1, None, "Канал", make_playlist("x", 1, 0, ["а"]).videos, kind="owner")
        self._run(page, [(1, page.videos[0])])
        self.assertEqual([path.suffix for path in (self.path / "Канал").iterdir()], [".mp4"])
