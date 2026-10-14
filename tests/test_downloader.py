import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vkdw.controller import DownloadController, saved_ids
from vkdw.downloader import (
    choose_source,
    choose_variant,
    download_video,
    duplicate_ids,
    ffmpeg_command,
    find_ffmpeg,
    hls_variants,
    name_width,
    output_path,
    run_ffmpeg,
)
from vkdw.i18n import set_language
from vkdw.vk_client import Cancelled, PlaylistInfo, VideoItem, VkError

FFMPEG = shutil.which("ffmpeg")


def setUpModule():
    set_language("ru")


class FakeClient:
    """Gives download_video the links a test server serves, as VK's video.get would."""

    ssl_context = None

    def __init__(self, files: dict):
        self.files = files
        self.calls = 0

    def video_files(self, item):
        self.calls += 1
        return dict(self.files)


class FileServer:
    """A local HTTP server for one file, with or without Range, that can cut a response short."""

    def __init__(self, data: bytes, ranges: bool = True, root: Path | None = None, pause: float = 0.0):
        self.data = data
        self.ranges = ranges
        self.cut_next = False
        # Seconds to wait after each 16 KB, so a download takes long enough to stop it midway.
        self.pause = pause
        self.ranges_asked: list[str] = []
        self.lock = threading.Lock()
        owner = self

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(root or "."), **kwargs)

            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path != "/file.mp4":
                    return super().do_GET()
                asked = self.headers.get("Range", "")
                with owner.lock:
                    owner.ranges_asked.append(asked)
                data, start, end = owner.data, 0, len(owner.data) - 1
                if asked and owner.ranges:
                    first, _dash, last = asked.removeprefix("bytes=").partition("-")
                    start, end = int(first), min(int(last), end) if last else end
                    if start >= len(data):
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(data)}")
                        self.end_headers()
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                else:
                    self.send_response(200)
                body = data[start : end + 1]
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes" if owner.ranges else "none")
                self.end_headers()
                with owner.lock:
                    cut = owner.cut_next and len(body) > 1
                    if cut:
                        owner.cut_next = False
                if cut:
                    body = body[: len(body) // 2]
                    self.close_connection = True
                try:
                    for at in range(0, len(body), 16 * 1024):
                        self.wfile.write(body[at : at + 16 * 1024])
                        if owner.pause:
                            time.sleep(owner.pause)
                    self.wfile.flush()
                except OSError:
                    pass  # the download stopped and closed its connection

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path: str = "file.mp4") -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/{path}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


ITEM = VideoItem(-1, 2, "Ролик", 2, "", "")
PART = 512 * 1024


class Mp4PartsTests(unittest.TestCase):
    """An MP4 comes in several connections, each its own part, and is resumed part by part."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.dest = Path(self.folder.name) / "Ролик.mp4"
        self.partial = self.dest.with_name("Ролик.mp4.part")
        self.meta = self.dest.with_name("Ролик.mp4.part.json")
        self.data = os.urandom(4 * PART + 123)
        for name, value in (("CONNECTIONS", 4), ("MULTI_MIN", PART)):
            patcher = patch(f"vkdw.downloader.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.folder.cleanup()

    def _serve(self, ranges=True) -> FileServer:
        server = FileServer(self.data, ranges=ranges)
        self.addCleanup(server.close)
        return server

    def _download(self, server, key="mp4_720", cancel=None, progress=None, client=None):
        client = client or FakeClient({key: server.url()})
        report = (lambda *args: progress.append(args)) if progress is not None else (lambda *_: None)
        return download_video(client, ITEM, self.dest, None, cancel or threading.Event(), report)

    def _stop_midway(self, server: FileServer) -> list:
        server.pause = 0.02  # each part takes over half a second
        cancel = threading.Event()
        with self.assertRaises(Cancelled):
            download_video(FakeClient({"mp4_720": server.url()}), ITEM, self.dest, None, cancel, lambda *_: cancel.set())
        server.pause = 0.0
        self.assertEqual(self.partial.stat().st_size, len(self.data))  # the file has its full size from the start
        parts = json.loads(self.meta.read_text(encoding="utf-8"))["parts"]
        server.ranges_asked.clear()
        return parts

    def test_the_parts_come_in_together(self):
        server = self._serve()
        self.assertEqual(self._download(server), "720p")
        self.assertEqual(self.dest.read_bytes(), self.data)
        self.assertEqual(server.ranges_asked[0], "bytes=0-0")  # the full size first
        size = -(-len(self.data) // 4)
        expected = {f"bytes={first}-{min(first + size, len(self.data)) - 1}" for first in range(0, len(self.data), size)}
        self.assertEqual(set(server.ranges_asked[1:]), expected)
        self.assertEqual(len(server.ranges_asked), 5)
        self.assertEqual(list(Path(self.folder.name).iterdir()), [self.dest])

    def test_stop_then_each_part_goes_on_where_it_stopped(self):
        server = self._serve()
        parts = self._stop_midway(server)
        self.assertEqual(len(parts), 4)
        for first, last, done in parts:
            self.assertTrue(0 < done < last - first + 1, (first, last, done))
        progress = []
        self.assertEqual(self._download(server, progress=progress), "720p")
        self.assertEqual(self.dest.read_bytes(), self.data)
        self.assertEqual(sorted(server.ranges_asked), sorted(f"bytes={first + done}-{last}" for first, last, done in parts))
        self.assertEqual(progress[0][1], len(self.data))  # the whole size, not what is left
        self.assertGreaterEqual(progress[0][0], sum(done for _first, _last, done in parts))
        self.assertFalse(self.meta.exists())

    def test_a_cut_connection_is_opened_again_for_its_part_alone(self):
        server = self._serve()
        server.cut_next = True
        client = FakeClient({"mp4_720": server.url()})
        self._download(server, client=client)
        self.assertEqual(self.dest.read_bytes(), self.data)
        self.assertEqual(client.calls, 1)  # no new link was needed
        self.assertEqual(len(server.ranges_asked), 6)

    def test_server_without_ranges_sends_the_whole_file(self):
        server = self._serve(ranges=False)
        self._download(server)
        self.assertEqual(self.dest.read_bytes(), self.data)
        self.assertEqual(server.ranges_asked, ["bytes=0-0"])
        self.dest.unlink()
        server.pause = 0.002
        cancel = threading.Event()
        with self.assertRaises(Cancelled):
            download_video(FakeClient({"mp4_720": server.url()}), ITEM, self.dest, None, cancel, lambda *_: cancel.set())
        self.assertFalse(self.meta.exists())  # nothing to resume from
        server.pause = 0.0
        self._download(server)
        self.assertEqual(self.dest.read_bytes(), self.data)

    def test_a_file_bigger_than_the_free_space_is_not_started(self):
        server = self._serve()
        client = FakeClient({"mp4_720": server.url()})
        with patch("vkdw.downloader.free_space", return_value=len(self.data) - 1):
            with self.assertRaises(VkError) as caught:
                self._download(server, client=client)
        self.assertEqual(caught.exception.kind, "disk")
        self.assertEqual(client.calls, 1)  # another try would not make room
        self.assertFalse(self.partial.exists())
        self.assertEqual(server.ranges_asked, ["bytes=0-0"])

    def test_a_full_disk_while_writing_keeps_what_came(self):
        server = self._serve()
        self._stop_midway(server)
        full = OSError(28, "No space left on device")
        with patch("vkdw.downloader._fetch_parts", side_effect=full):
            with self.assertRaises(VkError) as caught:
                self._download(server)
        self.assertEqual(caught.exception.kind, "disk")
        self.assertTrue(self.meta.exists())  # ▶ goes on from here once there is room
        self._download(server)
        self.assertEqual(self.dest.read_bytes(), self.data)

    def test_another_file_behind_the_link_starts_over(self):
        server = self._serve()
        self._stop_midway(server)
        server.data = self.data = os.urandom(len(self.data) + 5000)
        client = FakeClient({"mp4_720": server.url()})
        self._download(server, client=client)
        self.assertEqual(self.dest.read_bytes(), self.data)
        self.assertEqual(client.calls, 2)  # the try that found another file, then a fresh one
        self.assertIn("bytes=0-0", server.ranges_asked)

    def test_another_quality_starts_over(self):
        server = self._serve()
        self._stop_midway(server)
        self._download(server, key="mp4_480")
        self.assertEqual(self.dest.read_bytes(), self.data)
        self.assertEqual(server.ranges_asked[0], "bytes=0-0")

    def test_a_finished_part_file_is_only_renamed(self):
        server = self._serve()
        parts = self._stop_midway(server)
        self.partial.write_bytes(self.data)
        state = {"total": len(self.data), "quality": "720p", "parts": [[first, last, last - first + 1] for first, last, _done in parts]}
        self.meta.write_text(json.dumps(state), encoding="utf-8")
        self._download(server)
        self.assertEqual(self.dest.read_bytes(), self.data)
        self.assertEqual(server.ranges_asked, [])

    def test_a_part_file_of_the_one_connection_version_goes_on(self):
        server = self._serve()
        self.partial.write_bytes(self.data[:1000])
        self.meta.write_text(json.dumps({"total": len(self.data), "quality": "720p"}), encoding="utf-8")
        self._download(server)
        self.assertEqual(self.dest.read_bytes(), self.data)
        self.assertEqual(server.ranges_asked, [f"bytes=1000-{len(self.data) - 1}"])

    def _split_small(self):
        for name, value in (("CHUNK", 16 * 1024), ("STEAL_MIN", 64 * 1024)):
            patcher = patch(f"vkdw.downloader.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_free_connections_take_over_halves_of_a_big_part(self):
        self._split_small()
        server = self._serve()
        server.pause = 0.005
        # One part left from the one-connection version: the other connections cut it up.
        self.partial.write_bytes(self.data[:1000])
        self.meta.write_text(json.dumps({"total": len(self.data), "quality": "720p"}), encoding="utf-8")
        self._download(server)
        self.assertEqual(self.dest.read_bytes(), self.data)
        self.assertGreaterEqual(len(server.ranges_asked), 4)
        starts = sorted(int(asked.removeprefix("bytes=").split("-")[0]) for asked in server.ranges_asked)
        self.assertEqual(starts[0], 1000)

    def test_split_parts_stop_and_go_on(self):
        self._split_small()
        server = self._serve()
        self.partial.write_bytes(self.data[:1000])
        self.meta.write_text(json.dumps({"total": len(self.data), "quality": "720p"}), encoding="utf-8")
        parts = self._stop_midway(server)
        self.assertGreater(len(parts), 1)
        self.assertEqual(parts[0][0], 0)
        for before, after in zip(parts, parts[1:]):
            self.assertEqual(before[1] + 1, after[0])  # still end to end
        self.assertEqual(parts[-1][1], len(self.data) - 1)
        self._download(server)
        self.assertEqual(self.dest.read_bytes(), self.data)

    def test_a_small_file_comes_in_one_connection(self):
        server = self._serve()
        with patch("vkdw.downloader.MULTI_MIN", len(self.data) + 1):
            self._download(server)
        self.assertEqual(self.dest.read_bytes(), self.data)
        self.assertEqual(server.ranges_asked, ["bytes=0-0", f"bytes=0-{len(self.data) - 1}"])


MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=640x360,CODECS="avc1.4d401e,mp4a.40.2"
360/index.m3u8?sig=1
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
https://cdn.example/1080/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720
720/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=854x480
/abs/480/index.m3u8
"""


class HlsTests(unittest.TestCase):
    def test_variants_of_a_master_playlist(self):
        variants = hls_variants(MASTER, "https://vk.example/video/master.m3u8?x=1")
        self.assertEqual([variant.height for variant in variants], [360, 1080, 720, 480])
        self.assertEqual(variants[0].url, "https://vk.example/video/360/index.m3u8?sig=1")
        self.assertEqual(variants[1].url, "https://cdn.example/1080/index.m3u8")
        self.assertEqual(variants[3].url, "https://vk.example/abs/480/index.m3u8")

    def test_choice_follows_the_mp4_rule(self):
        variants = hls_variants(MASTER, "https://vk.example/m.m3u8")
        self.assertEqual(choose_variant(variants, None).height, 1080)
        self.assertEqual(choose_variant(variants, 720).height, 720)
        self.assertEqual(choose_variant(variants, 600).height, 480)
        self.assertEqual(choose_variant(variants, 144).height, 360)  # nothing that low: the lowest

    def test_vertical_video_is_named_by_its_shorter_side(self):
        text = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=720x1280\nv.m3u8\n"
        self.assertEqual(hls_variants(text, "https://a/")[0].height, 720)

    def test_unreadable_masters_give_nothing(self):
        self.assertEqual(hls_variants("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n", "https://a/"), [])
        self.assertEqual(hls_variants("#EXTM3U\n#EXTINF:4,\nseg1.ts\n", "https://a/"), [])
        self.assertEqual(hls_variants("<html>", "https://a/"), [])

    def test_separate_sound_goes_with_its_variant(self):
        text = (
            "#EXTM3U\n"
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="ru",DEFAULT=YES,URI="audio/ru.m3u8"\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1280x720,AUDIO="aud"\n'
            "v720.m3u8\n"
        )
        variant = hls_variants(text, "https://a/b/master.m3u8")[0]
        self.assertEqual(variant.audio_url, "https://a/b/audio/ru.m3u8")
        command = ffmpeg_command("ffmpeg", [variant.url, variant.audio_url], Path("x.mp4.part"))
        self.assertEqual(command.count("-i"), 2)
        self.assertIn("1:a", command)

    def test_hls_and_dash_go_through_ffmpeg(self):
        self.assertEqual(choose_source({"hls": "s"}, 720), ("hls", "HLS", "s"))
        self.assertEqual(choose_source({"dash_sep": "d"}, 720)[0], "dash")


class FfmpegCommandTests(unittest.TestCase):
    def test_format_is_given_since_the_file_ends_in_part(self):
        command = ffmpeg_command("ffmpeg", ["https://a/master.m3u8"], Path("out.mp4.part"))
        self.assertEqual(command[-3:], ["-f", "mp4", "out.mp4.part"])
        for flag in ("-nostats", "-progress"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("-loglevel") + 1], "error")


class FindFfmpegTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.local = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": str(self.local), "PATH": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _put(self, *parts: str) -> Path:
        path = self.local.joinpath(*parts, "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x")
        return path

    def test_nothing_found_without_ffmpeg(self):
        self.assertIsNone(find_ffmpeg())

    def test_ffmpeg_installed_by_winget_is_found_before_a_restart(self):
        links = self._put("Microsoft", "WinGet", "Links")
        self.assertEqual(find_ffmpeg(), str(links))

    def test_the_app_folder_comes_before_winget(self):
        self._put("Microsoft", "WinGet", "Links")
        own = self._put("VKDW")
        self.assertEqual(find_ffmpeg(), str(own))


def _script(code: str) -> list[str]:
    return [sys.executable, "-c", code]


class RunFfmpegTests(unittest.TestCase):
    """run_ffmpeg with a stand-in written in Python."""

    def _run(self, command, duration=0, cancel=None):
        progress = []
        outcome = {}

        def work():
            try:
                run_ffmpeg(command, cancel or threading.Event(), lambda *args: progress.append(args), duration)
                outcome["ok"] = True
            except BaseException as exc:  # noqa: BLE001
                outcome["error"] = exc

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        thread.join(30)
        self.assertFalse(thread.is_alive(), "run_ffmpeg hung")
        return progress, outcome

    def test_lots_of_stderr_does_not_hang(self):
        code = "import sys\nfor _ in range(40000): sys.stderr.write('x' * 60 + '\\n')\nprint('progress=end')"
        progress, outcome = self._run(_script(code))
        self.assertEqual(outcome, {"ok": True})

    def test_progress_estimates_the_size_from_the_duration(self):
        code = "print('out_time_us=5000000'); print('total_size=1000000'); print('progress=end')"
        progress, outcome = self._run(_script(code), duration=10)
        self.assertEqual(outcome, {"ok": True})
        loaded, size, _speed = progress[-1]
        self.assertEqual((loaded, size), (1000000, 2000000))

    def test_without_duration_there_is_no_size(self):
        code = "print('out_time_us=5000000'); print('total_size=1000'); print('progress=end')"
        progress, _outcome = self._run(_script(code), duration=0)
        self.assertEqual(progress[-1][:2], (1000, 0))

    def test_the_last_error_line_is_the_message(self):
        code = "import sys\nsys.stderr.write('first\\nsecond problem\\n')\nsys.exit(1)"
        _progress, outcome = self._run(_script(code))
        self.assertIsInstance(outcome["error"], VkError)
        self.assertEqual(str(outcome["error"]), "second problem")

    def test_cancel_stops_the_process(self):
        cancel = threading.Event()
        cancel.set()
        started = time.monotonic()
        _progress, outcome = self._run(_script("import time; time.sleep(30)"), cancel=cancel)
        self.assertIsInstance(outcome["error"], Cancelled)
        self.assertLess(time.monotonic() - started, 10)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class RealFfmpegTests(unittest.TestCase):
    """An HLS stream made by ffmpeg in two qualities, served over HTTP and saved as a .part first."""

    @classmethod
    def setUpClass(cls):
        encoders = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30).stdout
        if "libx264" not in encoders:
            raise unittest.SkipTest("this ffmpeg has no libx264 to make a test stream")
        cls.folder = tempfile.TemporaryDirectory()
        root = Path(cls.folder.name) / "site"
        for name, size in (("v240", "320x240"), ("v144", "256x144")):
            (root / name).mkdir(parents=True)
            subprocess.run(
                [
                    FFMPEG, "-v", "error", "-f", "lavfi", "-i", f"testsrc=duration=2:size={size}:rate=25",
                    "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "libx264", "-c:a", "aac", "-shortest",
                    "-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod", str(root / name / "index.m3u8"),
                ],
                check=True,
                timeout=60,
            )
        (root / "master.m3u8").write_text(
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=320x240\nv240/index.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=256x144\nv144/index.m3u8\n",
            encoding="utf-8",
        )
        cls.server = FileServer(b"", root=root)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        cls.folder.cleanup()

    def _probe_height(self, path: Path) -> int:
        out = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True, errors="replace", timeout=30
        ).stderr
        for size in ("320x240", "256x144"):
            if size in out:
                return int(size.split("x")[1])
        return 0

    def test_hls_is_saved_in_the_chosen_quality(self):
        for max_height, label, height in ((None, "240p", 240), (200, "144p", 144)):
            with self.subTest(max_height=max_height):
                dest = Path(self.folder.name) / f"out-{label}.mp4"
                progress = []
                result = download_video(
                    FakeClient({"hls": self.server.url("master.m3u8")}),
                    ITEM,
                    dest,
                    max_height,
                    threading.Event(),
                    lambda *args: progress.append(args),
                )
                self.assertEqual(result, label)
                self.assertGreater(dest.stat().st_size, 0)
                self.assertFalse(dest.with_name(dest.name + ".part").exists())
                self.assertEqual(self._probe_height(dest), height)
                self.assertGreater(progress[-1][0], 0)

    def test_a_media_playlist_is_given_as_it_is(self):
        dest = Path(self.folder.name) / "media.mp4"
        result = download_video(
            FakeClient({"hls": self.server.url("v144/index.m3u8")}), ITEM, dest, None, threading.Event(), lambda *_: None
        )
        self.assertEqual(result, "HLS")
        self.assertGreater(dest.stat().st_size, 0)


def _video(video_id: int, title: str, date: int = 0, owner: int = -5) -> VideoItem:
    return VideoItem(owner, video_id, title, 10, "", "", date=date)


class SameNameTests(unittest.TestCase):
    def setUp(self):
        self.day = int(time.mktime((2026, 10, 11, 12, 0, 0, 0, 0, -1)))
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def _names(self, items, naming="date"):
        pairs = list(enumerate(items, start=1))
        width = name_width(len(pairs))
        tagged = duplicate_ids(pairs, width, naming)
        return [output_path(self.path, item, index, width, naming, tagged).name for index, item in pairs]

    def test_same_title_and_day_give_two_files(self):
        names = self._names([_video(20, "Выпуск", self.day), _video(10, "Выпуск", self.day + 60)])
        self.assertEqual(names, ["2026-10-11 - Выпуск [-5_20].mp4", "2026-10-11 - Выпуск.mp4"])

    def test_only_the_matching_ones_get_the_id(self):
        names = self._names([_video(1, "А", self.day), _video(2, "Б", self.day), _video(3, "а", self.day)])
        self.assertEqual(names, ["2026-10-11 - А.mp4", "2026-10-11 - Б.mp4", "2026-10-11 - а [-5_3].mp4"])

    def test_the_rule_does_not_depend_on_the_order(self):
        first = _video(1, "Выпуск", self.day)
        second = _video(2, "Выпуск", self.day)
        self.assertEqual(sorted(self._names([first, second])), sorted(self._names([second, first])))

    def test_numbered_playlists_never_match(self):
        self.assertEqual(duplicate_ids(enumerate([_video(1, "А"), _video(2, "А")], start=1), 3, "number"), frozenset())

    def test_saved_ids_finds_the_file_with_the_id(self):
        items = [_video(1, "Выпуск", self.day), _video(2, "Выпуск", self.day)]
        (self.path / "2026-10-11 - Выпуск [-5_2].mp4").write_bytes(b"x")
        self.assertEqual(saved_ids(enumerate(items, start=1), self.path, "date", 2), {"-5_2"})
        (self.path / "2026-10-11 - Выпуск.mp4").write_bytes(b"x")
        self.assertEqual(saved_ids(enumerate(items, start=1), self.path, "date", 2), {"-5_1", "-5_2"})

    def test_a_job_of_some_videos_names_them_as_the_whole_list_does(self):
        items = (_video(1, "Выпуск", self.day), _video(2, "Выпуск", self.day))
        page = PlaylistInfo(-5, None, "Канал", items, kind="owner")
        job = DownloadController().make_job(page, [(2, items[1])], self.path, "best", False)
        self.assertEqual(job.tagged, frozenset({"-5_2"}))
        self.assertEqual(saved_ids([(2, items[1])], self.path, "date", 2, job.tagged), set())
        (self.path / "2026-10-11 - Выпуск [-5_2].mp4").write_bytes(b"x")
        self.assertEqual(saved_ids([(2, items[1])], self.path, "date", 2, job.tagged), {"-5_2"})


if __name__ == "__main__":
    unittest.main()
