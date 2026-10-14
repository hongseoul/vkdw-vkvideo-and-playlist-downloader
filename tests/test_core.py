import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from vkdw.downloader import choose_source, output_path
from vkdw.i18n import set_language
from vkdw.jobs import PlaylistQueue, QueueJob
from vkdw.thumbs import ThumbLoader, save_image
from vkdw.vk_client import (
    OWNER_BATCH,
    PlaylistInfo,
    Target,
    VideoItem,
    VkClient,
    VkError,
    parse_target,
    parse_targets,
    pick_image,
    safe_filename,
)


def setUpModule():
    set_language("ru")


class ParseTests(unittest.TestCase):
    def test_vkvideo_playlist(self):
        target = parse_target("https://vkvideo.ru/playlist/-28658784_103")
        self.assertEqual(target.kind, "playlist")
        self.assertEqual(target.owner_id, -28658784)
        self.assertEqual(target.item_id, 103)

    def test_negative_album_id(self):
        target = parse_target("https://vkvideo.ru/playlist/-51890028_-2")
        self.assertEqual((target.owner_id, target.item_id), (-51890028, -2))

    def test_vk_com_playlist(self):
        target = parse_target("https://vk.com/video/playlist/-174476437_2")
        self.assertEqual(target.kind, "playlist")
        self.assertEqual(target.item_id, 2)

    def test_old_album_section(self):
        target = parse_target("https://vk.com/videos-77521?section=album_55889438")
        self.assertEqual(target.kind, "playlist")
        self.assertEqual((target.owner_id, target.item_id), (-77521, 55889438))

    def test_video_link(self):
        target = parse_target("https://vkvideo.ru/video-18403220_456239696")
        self.assertEqual(target.kind, "video")
        self.assertEqual(target.owner_id, -18403220)
        self.assertEqual(target.item_id, 456239696)

    def test_plain_video_without_dash(self):
        target = parse_target("vk.com/video205387401_165548505")
        self.assertEqual(target.kind, "video")
        self.assertEqual(target.owner_id, 205387401)

    def test_rejects_other_sites(self):
        with self.assertRaises(VkError) as caught:
            parse_target("https://example.com/playlist/-1_2")
        self.assertEqual(caught.exception.kind, "input")

    def test_community_pages(self):
        cases = {
            "https://vk.com/videos-123": Target("owner", -123, 0),
            "vk.com/club123": Target("owner", -123, 0),
            "https://vk.com/public77": Target("owner", -77, 0),
            "vk.com/id5": Target("owner", 5, 0),
            "https://vkvideo.ru/@ntv": Target("owner", 0, 0, screen_name="ntv"),
            "https://vkvideo.ru/@ntv/all": Target("owner", 0, 0, screen_name="ntv"),
            "https://vkvideo.ru/@club42": Target("owner", -42, 0),
            "https://vk.com/video/@ntv": Target("owner", 0, 0, screen_name="ntv"),
            "https://m.vk.com/ntv": Target("owner", 0, 0, screen_name="ntv"),
        }
        for link, expected in cases.items():
            with self.subTest(link=link):
                self.assertEqual(parse_target(link), expected)

    def test_section_and_video_win_over_the_page(self):
        self.assertEqual(parse_target("https://vk.com/videos-1?section=album_2").kind, "playlist")
        self.assertEqual(parse_target("https://vk.com/ntv?z=video-1_2").kind, "video")

    def test_service_paths_are_not_pages(self):
        for link in ("https://vk.com/feed", "https://vkvideo.ru/", "https://vk.com/video"):
            with self.subTest(link=link), self.assertRaises(VkError) as caught:
                parse_target(link)
            self.assertEqual(caught.exception.kind, "input")

    def test_embedded_player(self):
        target = parse_target("https://vk.com/video_ext.php?oid=-123&id=456&hash=abc&hd=2")
        self.assertEqual(target, Target("video", -123, 456))
        with self.assertRaises(VkError) as caught:
            parse_target("https://vk.com/video_ext.php?hash=abc")
        self.assertEqual(caught.exception.kind, "input")

    def test_short_links_are_read_when_opened(self):
        self.assertEqual(parse_target("vk.cc/c5QYMV"), Target("short", 0, 0, link="https://vk.cc/c5QYMV"))
        self.assertEqual(parse_target("https://vk.link/name").kind, "short")
        with self.assertRaises(VkError):
            parse_target("https://vk.cc/")

    def test_wall_post_says_what_to_do(self):
        for link in ("https://vk.com/wall-1_2", "https://vk.com/club1?w=wall-1_2"):
            with self.subTest(link=link), self.assertRaises(VkError) as caught:
                parse_target(link)
            self.assertIn("запись на стене", str(caught.exception))
        # A video opened over a post is the video.
        self.assertEqual(parse_target("https://vk.com/wall-1_2?z=video-1_5").kind, "video")

    def test_several_links_in_one_text(self):
        text = "https://vk.com/video-1_2\nсмотри https://vkvideo.ru/video-3_4 , https://vk.com/video-1_2"
        targets = parse_targets(text)
        self.assertEqual([(t.kind, t.owner_id, t.item_id) for t in targets], [("video", -1, 2), ("video", -3, 4)])
        self.assertEqual(len(parse_targets("https://vk.com/club5")), 1)
        with self.assertRaises(VkError):
            parse_targets("просто текст без ссылок")


class QualityTests(unittest.TestCase):
    def test_best_is_highest_mp4(self):
        kind, label, url = choose_source({"mp4_720": "h", "mp4_360": "l", "hls": "s"}, None)
        self.assertEqual((kind, label, url), ("mp4", "720p", "h"))

    def test_caps_at_requested_height(self):
        kind, label, url = choose_source({"mp4_1080": "a", "mp4_720": "b", "mp4_360": "c"}, 480)
        self.assertEqual((kind, label, url), ("mp4", "360p", "c"))

    def test_falls_back_when_nothing_is_lower(self):
        kind, label, url = choose_source({"mp4_1080": "a"}, 360)
        self.assertEqual(url, "a")
        self.assertEqual(label, "1080p")

    def test_hls_when_no_mp4(self):
        kind, label, _url = choose_source({"hls": "stream"}, 720)
        self.assertEqual(kind, "hls")
        self.assertEqual(label, "HLS")


class NameTests(unittest.TestCase):
    def test_filename_strips_forbidden_characters(self):
        self.assertEqual(safe_filename('a<>:"/\\|?*b'), "a b")

    def test_playlist_name_keeps_order(self):
        item = VideoItem(1, 2, "Ролик", 10, "", "")
        path = output_path(Path("out"), item, 3, 3, "number")
        self.assertEqual(path.name, "003 - Ролик.mp4")

    def test_page_and_collection_names_start_with_the_upload_date(self):
        stamp = int(time.mktime((2026, 10, 11, 12, 0, 0, 0, 0, -1)))
        item = VideoItem(1, 2, "Ролик", 10, "", "", date=stamp)
        self.assertEqual(output_path(Path("out"), item, 3, 3, "date").name, "2026-10-11 - Ролик.mp4")
        undated = VideoItem(1, 2, "Ролик", 10, "", "")
        self.assertEqual(output_path(Path("out"), undated, 3, 3, "date").name, "Ролик.mp4")
        self.assertEqual(output_path(Path("out"), item, 3, 3, "title").name, "Ролик.mp4")


class ApiAnswerTests(unittest.TestCase):
    def test_a_connection_cut_while_the_answer_is_read_is_a_network_error(self):
        class Cut:
            headers = None

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                raise ConnectionResetError(10054, "reset")

        with patch("vkdw.vk_client.urllib.request.urlopen", return_value=Cut()):
            with self.assertRaises(VkError) as caught:
                VkClient()._request("https://api.vkvideo.ru/method/video.get")
        self.assertEqual((caught.exception.code, caught.exception.kind), (0, "network"))

    def test_an_answer_that_is_not_json_gets_another_try(self):
        client = VkClient()
        answers = iter(["<html>proxy</html>", '{"response": {"items": []}}'])
        with patch.object(client, "_token", return_value="t"), \
                patch.object(client, "_request", side_effect=lambda *a, **k: next(answers)), \
                patch("vkdw.vk_client.time.sleep"):
            self.assertEqual(client._call("video.get"), {"items": []})

    def test_single_videos_of_one_owner_are_different_lists(self):
        def single(video_id):
            return PlaylistInfo(-7, None, "t", (VideoItem(-7, video_id, "t", 1, "", ""),), kind="video")

        self.assertNotEqual(single(1).key, single(2).key)


class OwnerPageTests(unittest.TestCase):
    """An owner page is read in batches through video.get, with its playlists alongside."""

    def setUp(self):
        self.total = 2500
        self.calls = []

    def _fake_call(self, method, **params):
        self.calls.append((method, params))
        if method == "utils.resolveScreenName":
            return {"type": "group", "object_id": 28}
        if method == "video.getAlbums":
            if not params.get("extended"):
                return {"items": [{"id": 5, "title": "Выпуски"}]}  # VK gives no counts then
            return {"items": [{"id": 5, "title": "Выпуски", "count": 10}, {"id": 6, "title": "Пустой", "count": 0}]}
        if method == "video.get":
            offset, size = params["offset"], params["count"]
            items = [
                {"owner_id": -28, "id": number, "title": f"ролик {number}", "date": 1000 + number}
                for number in range(offset, min(offset + size, self.total))
            ]
            return {"count": self.total, "items": items, "groups": [{"id": 28, "name": "Канал"}]}
        raise AssertionError(method)

    def test_first_batch_then_more(self):
        client = VkClient()
        with patch.object(client, "_call", side_effect=self._fake_call):
            page = client.open_target(Target("owner", 0, 0, screen_name="channel"))
            self.assertEqual(page.kind, "owner")
            self.assertEqual(page.key, (-28, "all"))
            self.assertEqual(page.title, "Канал")
            self.assertEqual(len(page.videos), OWNER_BATCH)
            self.assertEqual(page.total, 2500)
            self.assertTrue(page.has_more)
            self.assertEqual(page.albums, ((5, "Выпуски", 10),))
            self.assertEqual(page.naming, "date")
            more = client.open_more(page)
            self.assertEqual(len(more.videos), 2000)
            self.assertEqual(more.videos[1000].video_id, 1000)
            last = client.open_more(more)
        self.assertEqual(len(last.videos), 2500)
        self.assertFalse(last.has_more)
        resolved = [call for call in self.calls if call[0] == "utils.resolveScreenName"]
        self.assertEqual(len(resolved), 1)

    def test_refresh_reads_as_many_as_were_shown(self):
        client = VkClient()
        with patch.object(client, "_call", side_effect=self._fake_call):
            page = client.open_target(Target("owner", -28, 0), limit=2000)
        self.assertEqual(len(page.videos), 2000)

    def test_closed_page_is_an_access_problem(self):
        client = VkClient()
        with patch.object(client, "_call", side_effect=VkError(15, "закрыто", "access")):
            with self.assertRaises(VkError) as caught:
                client.open_target(Target("owner", -5, 0))
        self.assertEqual(caught.exception.kind, "access")

    def test_unknown_short_name(self):
        client = VkClient()
        with patch.object(client, "_call", return_value=[]):
            with self.assertRaises(VkError) as caught:
                client.open_target(Target("owner", 0, 0, screen_name="nobody"))
        self.assertEqual(caught.exception.kind, "input")

    def test_videos_by_id_keep_the_asked_order(self):
        client = VkClient()
        answer = {
            "items": [{"owner_id": -3, "id": 4, "title": "второй"}, {"owner_id": -1, "id": 2, "title": "первый"}],
            "groups": [],
        }
        with patch.object(client, "_call", return_value=answer):
            items = client.open_videos(["-1_2", "-9_9", "-3_4_key"])
        self.assertEqual([item.title for item in items], ["первый", "второй"])
        self.assertEqual(items[1].access_key, "key")


class ShortLinkTests(unittest.TestCase):
    """A short link is read by its redirect, through VK's away.php, without following it."""

    def _resolve(self, *hops: str) -> Target:
        client = VkClient()
        with patch.object(client, "_redirect", side_effect=list(hops)):
            return client.resolve_link(parse_target("vk.cc/abc"))

    def test_leads_to_a_playlist_through_away(self):
        away = "https://vkontakte.ru/away.php?cc_key=abc&to=https%3A%2F%2Fvk.com%2Fvideo%2Fplaylist%2F-5_7"
        self.assertEqual(self._resolve(away), Target("playlist", -5, 7))

    def test_short_link_to_a_short_link(self):
        self.assertEqual(self._resolve("https://vk.cc/def", "https://vkvideo.ru/video-1_2"), Target("video", -1, 2))

    def test_another_site_or_nowhere(self):
        with self.assertRaises(VkError) as caught:
            self._resolve("https://vkontakte.ru/away.php?to=https%3A%2F%2Fexample.com%2F")
        self.assertIn("example.com", str(caught.exception))
        for hops in ([""], ["https://vk.com/"]):
            with self.subTest(hops=hops), self.assertRaises(VkError) as caught:
                self._resolve(*hops)
            self.assertIn("никуда не ведёт", str(caught.exception))


class LiveTests(unittest.TestCase):
    def _files(self, video: dict) -> dict:
        client = VkClient()
        with patch.object(client, "_call", return_value={"items": [video]}):
            return client.video_files(VideoItem(1, 2, "эфир", 0, "", ""))

    def test_a_stream_that_is_on_or_coming_cannot_be_saved(self):
        for video, words in (
            ({"live": 1, "live_status": "started", "files": {"hls_live": "x"}}, "ещё идёт"),
            ({"live": 1, "files": {"hls": "x"}}, "ещё идёт"),
            ({"live": 1, "live_status": "upcoming", "files": {}}, "не начался"),
        ):
            with self.subTest(video=video), self.assertRaises(VkError) as caught:
                self._files(video)
            self.assertIn(words, str(caught.exception))

    def test_a_recording_of_a_past_stream_is_a_video(self):
        self.assertEqual(self._files({"live": 1, "live_status": "finished", "files": {"hls": "x"}}), {"hls": "x"})
        self.assertEqual(self._files({"live": 1, "files": {"mp4_720": "m"}}), {"mp4_720": "m"})


class RestrictionTests(unittest.TestCase):
    AGE = {"blur": 1, "can_play": 1, "can_preview": 1, "title": "Видео с возрастным ограничением"}

    def _item(self, video: dict) -> VideoItem:
        client = VkClient()
        with patch.object(client, "_call", return_value={"items": [dict(video, id=2, owner_id=1)]}):
            return client.open_videos(["1_2"])[0]

    def test_an_age_limit_is_a_warning_and_the_video_downloads(self):
        item = self._item({"restriction": self.AGE, "files": {"mp4_720": "m"}})
        self.assertEqual((item.restriction, item.warning), ("", "Видео с возрастным ограничением"))
        client = VkClient()
        with patch.object(client, "_call", return_value={"items": [{"restriction": self.AGE, "files": {"mp4_720": "m"}}]}):
            self.assertEqual(client.video_files(item), {"mp4_720": "m"})

    def test_a_video_vk_does_not_play_stays_blocked(self):
        for video in (
            {"content_restricted": 1, "content_restricted_message": "Недоступно в вашей стране"},
            {"restriction": {"can_play": 0, "title": "Видео скрыто"}},
            {"content_restricted": 1, "restriction": self.AGE},
        ):
            with self.subTest(video=video):
                item = self._item(video)
                self.assertTrue(item.restriction)
                self.assertEqual(item.warning, "")


class QueueTests(unittest.TestCase):
    def test_next_starts_only_when_the_queue_is_running(self):
        queue = PlaylistQueue()
        first = QueueJob("a", "Первый")
        second = QueueJob("b", "Второй")
        queue.add(first)
        queue.add(second)
        self.assertEqual(queue.next_pending().job_id, "a")
        first.status = "done"
        self.assertEqual(queue.next_pending().title, "Второй")

    def test_pause_holds_the_interrupted_playlist_in_front(self):
        queue = PlaylistQueue()
        current = QueueJob("a", "Текущий", status="interrupted")
        nxt = QueueJob("b", "Следующий")
        queue.add(current)
        queue.add(nxt)
        queue.paused = True
        self.assertIsNone(queue.next_pending())
        queue.paused = False
        self.assertEqual(queue.next_pending().job_id, "a")

    def test_insert_next_keeps_the_running_job_first(self):
        queue = PlaylistQueue()
        running = QueueJob("a", "Идёт", status="running")
        waiting = QueueJob("b", "Ждёт")
        queue.add(running)
        queue.add(waiting)
        queue.insert_next(QueueJob("c", "Сразу после"))
        self.assertEqual([job.job_id for job in queue.jobs], ["a", "c", "b"])

    def test_insert_first_goes_before_a_paused_playlist(self):
        queue = PlaylistQueue()
        queue.add(QueueJob("a", "Пауза", status="interrupted"))
        queue.add(QueueJob("b", "Ждёт"))
        queue.insert_first(QueueJob("c", "Сейчас"))
        self.assertEqual([job.job_id for job in queue.jobs], ["c", "a", "b"])

    def test_take_any_job_and_clear_only_finished(self):
        queue = PlaylistQueue()
        queue.add(QueueJob("run", "Идёт", status="running"))
        queue.add(QueueJob("wait", "Ждёт"))
        queue.add(QueueJob("hold", "Пауза", status="interrupted"))
        queue.add(QueueJob("done", "Готово", status="done"))
        queue.add(QueueJob("bad", "Ошибка", status="error"))
        self.assertEqual(queue.take("wait").title, "Ждёт")
        self.assertIsNone(queue.take("wait"))
        self.assertEqual(queue.take("hold").job_id, "hold")
        self.assertEqual(queue.clear_finished(), 2)
        self.assertEqual([job.job_id for job in queue.jobs], ["run"])
        running = queue.jobs[0]
        self.assertTrue(queue.holds(running))
        queue.take("run")
        self.assertFalse(queue.holds(running))


class PreviewTests(unittest.TestCase):
    def test_pick_image_prefers_no_bars_and_enough_width(self):
        images = [
            {"url": "pad320", "width": 320, "with_padding": 1},
            {"url": "130", "width": 130},
            {"url": "320", "width": 320},
            {"url": "800", "width": 800},
        ]
        self.assertEqual(pick_image(images, 300), "320")
        self.assertEqual(pick_image(images, 100), "130")
        self.assertEqual(pick_image(images, 2000), "800")
        self.assertEqual(pick_image(None, 100), "")

    def test_full_size_covers_of_videos_and_of_a_playlist(self):
        def fake_call(method, **params):
            if method == "video.get":
                self.assertEqual(params["videos"], "-1_2,-1_3_key")
                return {
                    "items": [
                        {"owner_id": -1, "id": 2, "image": [{"url": "small", "width": 320}, {"url": "big", "width": 1280}]},
                        {"owner_id": -1, "id": 3, "image": []},
                    ]
                }
            if method == "video.getAlbumById":
                return {"image": [{"url": "a320", "width": 320}, {"url": "a1920", "width": 1920}]}
            raise AssertionError(method)

        client = VkClient()
        with patch.object(client, "_call", side_effect=fake_call):
            self.assertEqual(client.video_covers(["-1_2", "-1_3_key"]), {"-1_2": "big"})
            self.assertEqual(client.album_cover(-1, 5), "a1920")

    def test_saved_cover_keeps_jpeg_and_png_and_turns_others_into_jpeg(self):
        def encoded(fmt: str) -> bytes:
            buffer = io.BytesIO()
            Image.new("RGB", (64, 36), "red").save(buffer, fmt)
            return buffer.getvalue()

        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "нет папки" / "001 - Серия 1.5.mp4"
            jpeg = encoded("JPEG")
            path = save_image(jpeg, dest)
            self.assertEqual(path.name, "001 - Серия 1.5.jpg")
            self.assertEqual(path.read_bytes(), jpeg)
            self.assertEqual(save_image(encoded("PNG"), dest).suffix, ".png")
            path = save_image(encoded("WEBP"), dest)  # replaces the JPEG saved before
            with Image.open(path) as image:
                self.assertEqual((path.name, image.format), ("001 - Серия 1.5.jpg", "JPEG"))
            self.assertEqual(sorted(item.name for item in path.parent.iterdir()), ["001 - Серия 1.5.jpg", "001 - Серия 1.5.png"])
            with self.assertRaises(ValueError):
                save_image(b"not an image", dest)

    def test_loader_caches_on_disk_and_drops_stale_requests(self):
        buffer = io.BytesIO()
        Image.new("RGB", (320, 180), "blue").save(buffer, "JPEG")
        data = buffer.getvalue()
        fetched = []
        delivered = []
        done = threading.Event()

        def fetch(url):
            fetched.append(url)
            return data

        def deliver(tag, image):
            delivered.append((tag, image.size))
            done.set()

        with tempfile.TemporaryDirectory() as folder:
            loader = ThumbLoader(deliver, workers=1, cache_dir=Path(folder), fetch=fetch)
            loader.request("https://img/1", (64, 36), "first", "rows", radius=4)
            self.assertTrue(done.wait(5))
            done.clear()
            loader.request("https://img/1", (32, 18), "again", "rows")
            self.assertTrue(done.wait(5))
            loader.close()
        self.assertEqual(fetched, ["https://img/1"])
        self.assertEqual(delivered, [("first", (64, 36)), ("again", (32, 18))])

    def test_drop_forgets_waiting_requests(self):
        loader = ThumbLoader(lambda tag, image: None, workers=0, cache_dir=Path("unused"))
        loader._threads.append(threading.current_thread())  # keep workers from starting
        loader.request("https://img/1", (10, 10), "a", "rows")
        loader.request("https://img/2", (10, 10), "b", "covers")
        loader.drop("rows")
        self.assertEqual([job[3] for job in loader._pending], ["b"])


if __name__ == "__main__":
    unittest.main()
