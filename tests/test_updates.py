import http.server
import json
import socket
import threading
import unittest

from vkdw import updates
from vkdw.updates import REPO_URL, Release, newer_release, version_key


def release(tag: str, **fields) -> dict:
    return {"tag_name": tag, "html_url": f"{REPO_URL}/releases/tag/{tag}", "draft": False, "prerelease": False, **fields}


class VersionTests(unittest.TestCase):
    def test_versions_compare_as_numbers(self):
        self.assertLess(version_key("1.0.0"), version_key("1.0.1"))
        self.assertLess(version_key("1.9.0"), version_key("1.10.0"))
        self.assertEqual(version_key("v1.2"), version_key("1.2.0"))

    def test_a_pre_release_comes_before_its_version(self):
        self.assertLess(version_key("1.0.0-rc1"), version_key("1.0.0"))
        self.assertGreater(version_key("1.0.1-rc1"), version_key("1.0.0"))

    def test_text_that_is_not_a_version(self):
        for text in ("", "latest", "версия 2"):
            with self.subTest(text=text):
                self.assertIsNone(version_key(text))


class NewerReleaseTests(unittest.TestCase):
    def test_a_newer_tag_gives_its_page(self):
        self.assertEqual(
            newer_release(release("v1.0.1"), "1.0.0"),
            Release("1.0.1", f"{REPO_URL}/releases/tag/v1.0.1"),
        )

    def test_the_same_or_an_older_version_is_not_offered(self):
        self.assertIsNone(newer_release(release("v1.0.0"), "1.0.0"))
        self.assertIsNone(newer_release(release("v0.9.0"), "1.0.0"))

    def test_the_final_version_is_newer_than_its_pre_release(self):
        self.assertIsNotNone(newer_release(release("v1.0.0"), "1.0.0-rc1"))

    def test_drafts_pre_releases_and_odd_answers_are_skipped(self):
        for data in (
            release("v2.0.0", draft=True),
            release("v2.0.0", prerelease=True),
            {"message": "Not Found"},
            {"tag_name": 2},
            release("nightly"),
            [],
        ):
            with self.subTest(data=data):
                self.assertIsNone(newer_release(data, "1.0.0"))

    def test_only_a_page_of_this_project_is_opened(self):
        found = newer_release(release("v1.1.0", html_url="https://example.com/vkdw.exe"), "1.0.0")
        self.assertEqual(found.url, f"{REPO_URL}/releases/latest")


class CheckTests(unittest.TestCase):
    """check() asks the server and says nothing when the answer is not a newer release."""

    def setUp(self):
        self.status = 200
        self.body = b""
        self.headers: dict = {}
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.headers = dict(self.headers)
                self.send_response(owner.status)
                self.send_header("Content-Length", str(len(owner.body)))
                self.end_headers()
                self.wfile.write(owner.body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/releases/latest"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_a_newer_release_is_found(self):
        self.body = json.dumps(release("v1.2.0")).encode()
        self.assertEqual(updates.check("1.0.0", self.url), Release("1.2.0", f"{REPO_URL}/releases/tag/v1.2.0"))
        self.assertTrue(self.headers.get("User-Agent", "").startswith("VKDW/"))  # GitHub refuses requests without one

    def test_no_release_yet(self):
        self.status = 404
        self.body = b'{"message": "Not Found"}'
        self.assertIsNone(updates.check("1.0.0", self.url))

    def test_an_answer_that_is_not_json(self):
        self.body = b"<html>rate limited</html>"
        self.assertIsNone(updates.check("1.0.0", self.url))

    def test_no_connection(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.assertIsNone(updates.check("1.0.0", f"http://127.0.0.1:{port}/"))


if __name__ == "__main__":
    unittest.main()
