"""Whether a newer release of the app is out on GitHub.

VK can change what the app relies on any day; then only a new version helps,
and this is how people learn there is one.
"""

from __future__ import annotations

import http.client
import json
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

import certifi

from vkdw import __version__

REPO_URL = "https://github.com/hongseoul/vkdw-vkvideo-and-playlist-downloader"
LATEST_URL = "https://api.github.com/repos/hongseoul/vkdw-vkvideo-and-playlist-downloader/releases/latest"
# A release description is a few kilobytes; this only guards against a runaway answer.
ANSWER_LIMIT = 1024 * 1024


@dataclass(frozen=True)
class Release:
    version: str
    url: str


def version_key(text: str) -> tuple[int, ...] | None:
    """A version as numbers to compare: "v1.2" -> (1, 2, 0, 1).

    The last number is 0 for a pre-release such as 1.0.0-rc1, so it comes
    before 1.0.0 itself. None if the text is not a version.
    """
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)(.*)", text.strip())
    if match is None:
        return None
    numbers = [int(part) for part in match.group(1).split(".")]
    numbers += [0] * (3 - len(numbers))
    return (*numbers, 0 if match.group(2) else 1)


def newer_release(data: dict, current: str = __version__) -> Release | None:
    """The release GitHub described, if it is newer than the running version."""
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name")
    if not isinstance(tag, str):
        return None
    latest, running = version_key(tag), version_key(current)
    if latest is None or running is None or latest <= running:
        return None
    url = data.get("html_url")
    # Only a page of this project opens in the browser, whatever the answer says.
    if not isinstance(url, str) or not url.startswith(REPO_URL + "/"):
        url = REPO_URL + "/releases/latest"
    return Release(tag.strip().removeprefix("v"), url)


def fetch_latest(url: str = LATEST_URL, timeout: float = 15) -> dict:
    """GitHub's description of the latest release. Raises OSError (404 too: no release yet) or ValueError."""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"VKDW/{__version__}"},
    )
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return json.loads(response.read(ANSWER_LIMIT))


def check(current: str = __version__, url: str = LATEST_URL) -> Release | None:
    """A newer release, or None: also when GitHub cannot be reached, since then there is nothing to say."""
    try:
        return newer_release(fetch_latest(url), current)
    except urllib.error.HTTPError as exc:
        exc.close()  # it holds the connection of the refused answer
        return None
    except (OSError, ValueError, http.client.HTTPException):
        return None
