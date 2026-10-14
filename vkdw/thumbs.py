"""Preview images for playlists and videos.

Images are fetched in background threads and kept on disk, so a playlist that
was opened before shows its previews at once. Ready images are handed back as
Pillow images; the window turns them into Tk images on its own thread.
"""

from __future__ import annotations

import collections
import hashlib
import io
import os
import ssl
import threading
import urllib.request
from pathlib import Path

import certifi
from PIL import Image, ImageDraw, ImageOps

from vkdw.vk_client import USER_AGENT

CACHE_LIMIT = 3000
# A full-size cover is a few hundred kilobytes; this only guards against a runaway answer.
COVER_LIMIT = 32 * 1024 * 1024
# Image formats a saved cover keeps as VK sent it; anything else is turned into JPEG.
_KEPT_FORMATS = {"JPEG": ".jpg", "PNG": ".png"}


def default_cache_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(root) / "VKDW" / "thumbs"


def fetch_image(url: str, context: ssl.SSLContext, limit: int = 4 * 1024 * 1024) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": "https://vkvideo.ru/"})
    with urllib.request.urlopen(request, timeout=20, context=context) as response:
        return response.read(limit)


def save_image(data: bytes, dest: Path) -> Path:
    """Write an image to dest with the extension of its format, and return where it went.

    JPEG and PNG are written as they came; another format (say WebP) is saved as JPEG, which
    every viewer and player opens. A file already there is replaced. Raises OSError, or
    ValueError if the data is not an image.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            ext = _KEPT_FORMATS.get(image.format or "")
            if ext is None:
                buffer = io.BytesIO()
                image.convert("RGB").save(buffer, "JPEG", quality=95)
                data, ext = buffer.getvalue(), ".jpg"
    except (OSError, SyntaxError) as exc:  # Pillow's errors for data it cannot read
        raise ValueError(str(exc)) from exc
    path = dest.with_suffix(ext)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".part")
    temp.write_bytes(data)
    os.replace(temp, path)
    return path


def rounded(image: Image.Image, radius: int) -> Image.Image:
    image = image.convert("RGBA")
    if radius <= 0:
        return image
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, image.width - 1, image.height - 1), radius, fill=255)
    image.putalpha(mask)
    return image


def make_thumb(data: bytes, size: tuple[int, int], radius: int = 0) -> Image.Image:
    with Image.open(io.BytesIO(data)) as source:
        image = ImageOps.fit(source.convert("RGB"), size, Image.LANCZOS)
    return rounded(image, radius)


def placeholder(size: tuple[int, int], radius: int, color: str, mark: str = "") -> Image.Image:
    """Empty frame shown until the real image arrives. mark='play' adds a triangle."""
    width, height = size
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius, fill=color)
    if mark == "play":
        side = min(width, height) * 0.32
        cx, cy = width / 2, height / 2
        draw.polygon(
            [(cx - side * 0.4, cy - side / 2), (cx - side * 0.4, cy + side / 2), (cx + side * 0.55, cy)],
            fill="#4a5366",
        )
    return image


class ThumbLoader:
    """Download previews in a few threads.

    Requests carry a group. drop(group) forgets everything still waiting in that
    group, so switching playlists does not leave the old one loading in front.
    """

    def __init__(self, deliver, *, workers: int = 4, cache_dir: Path | None = None, fetch=None):
        self._deliver = deliver
        self._cache_dir = cache_dir if cache_dir is not None else default_cache_dir()
        self._fetch = fetch or (lambda url: fetch_image(url, self._ssl))
        self._workers = workers
        self._pending: collections.deque = collections.deque()
        self._generation: dict[str, int] = {}
        self._cond = threading.Condition()
        # Held while an image is handed over, so that once close() returns nothing more is.
        self._deliver_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._closed = False
        self._ssl = ssl.create_default_context(cafile=certifi.where())

    def request(self, url: str, size: tuple[int, int], tag, group: str = "", radius: int = 0) -> None:
        if not url:
            return
        with self._cond:
            if self._closed:
                return
            generation = self._generation.get(group, 0)
            self._pending.append((url, size, radius, tag, group, generation))
            if not self._threads:
                self._start()
            self._cond.notify()

    def drop(self, group: str) -> None:
        with self._cond:
            self._generation[group] = self._generation.get(group, 0) + 1
            self._pending = collections.deque(job for job in self._pending if job[4] != group)

    def close(self) -> None:
        """Stop; after this the deliver callback is never called again, so its owner may go."""
        with self._deliver_lock, self._cond:
            self._closed = True
            self._pending.clear()
            self._cond.notify_all()

    def _start(self) -> None:
        for index in range(self._workers):
            thread = threading.Thread(target=self._run, name=f"thumbs-{index}", daemon=True)
            self._threads.append(thread)
            thread.start()
        threading.Thread(target=self._prune, name="thumbs-prune", daemon=True).start()

    def _current(self, group: str, generation: int) -> bool:
        with self._cond:
            return not self._closed and self._generation.get(group, 0) == generation

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._pending and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                url, size, radius, tag, group, generation = self._pending.popleft()
                if self._generation.get(group, 0) != generation:
                    continue
            try:
                image = make_thumb(self._load(url), size, radius)
            except Exception:
                # A missing preview is not worth a message: the frame stays empty.
                continue
            with self._deliver_lock:
                if self._current(group, generation):
                    self._deliver(tag, image)

    def _load(self, url: str) -> bytes:
        name = hashlib.sha1(url.encode("utf-8")).hexdigest() + ".jpg"
        path = self._cache_dir / name
        try:
            return path.read_bytes()
        except OSError:
            pass
        data = self._fetch(url)
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(f".{threading.get_ident()}.tmp")
            temp.write_bytes(data)
            temp.replace(path)
        except OSError:
            pass
        return data

    def _prune(self) -> None:
        try:
            files = sorted(self._cache_dir.glob("*.jpg"), key=lambda path: path.stat().st_mtime)
        except OSError:
            return
        for path in files[: max(0, len(files) - CACHE_LIMIT)]:
            try:
                path.unlink()
            except OSError:
                pass
