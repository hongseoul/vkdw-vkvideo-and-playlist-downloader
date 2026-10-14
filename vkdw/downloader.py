"""Save a VK video file to disk."""

from __future__ import annotations

import collections
import errno
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from vkdw.i18n import tr
from vkdw.settings import fmt_size, write_atomic
from vkdw.vk_client import USER_AGENT, Cancelled, VideoItem, VkClient, VkError, safe_filename

MP4_HEIGHTS = (2160, 1440, 1080, 720, 480, 360, 240, 144)
# Quality ids and the height they allow; "best" takes the largest file there is.
QUALITY_CHOICES = (
    ("best", None),
    ("1080p", 1080),
    ("720p", 720),
    ("480p", 480),
    ("360p", 360),
)
# Returned instead of a quality label when the file was saved before.
EXISTS = "exists"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://vkvideo.ru/",
    "Origin": "https://vkvideo.ru",
}
# VkError codes worth another try: network trouble, VK's rate limit and server errors.
RETRY_CODES = (0, 6, 9, 10, 429, 500, 502, 503, 504)
FFMPEG_LIMIT = 60 * 60 * 6
# An MP4 comes in this many connections at once. Measured through a VPN on a 866 MB video:
# 1 connection 1.1 MB/s, 4 connections 7 MB/s, 8 connections 11 MB/s.
CONNECTIONS = 8
# Smaller files come in one connection: the quick start of each connection covers them anyway.
MULTI_MIN = 16 * 1024 * 1024
# A part whose connection drops is opened again this many times before the whole try fails.
PART_RETRIES = 3
# Bytes read at a time, and the least a free connection takes over from a part still coming.
CHUNK = 256 * 1024
STEAL_MIN = 2 * 1024 * 1024
# A playlist starts with a warning in the log when its drive has less free space than this.
LOW_SPACE = 1024 ** 3
# Errors another try cannot help: the drive is full, or ffmpeg is missing.
NO_RETRY_KINDS = ("disk", "ffmpeg")


def quality_label(quality: str) -> str:
    return tr("quality.best") if quality == "best" else quality


def choose_source(files: dict, max_height: int | None) -> tuple[str, str, str]:
    """Return (kind, label, url). kind is 'mp4', 'hls' or 'dash'; the last two go through ffmpeg."""
    available = []
    for height in MP4_HEIGHTS:
        url = files.get(f"mp4_{height}")
        if url:
            available.append((height, url))
    if available:
        if max_height is None:
            height, url = available[0]
        else:
            under = [item for item in available if item[0] <= max_height]
            height, url = max(under) if under else min(available)
        return "mp4", f"{height}p", url
    for key, kind, label in (("hls", "hls", "HLS"), ("hls_fmp4", "hls", "HLS"), ("dash_sep", "dash", "DASH")):
        url = files.get(key)
        if url:
            return kind, label, url
    raise VkError(0, tr("err.no_saveable_file"), "access")


# HLS

@dataclass(frozen=True)
class HlsVariant:
    height: int  # the shorter side, as VK names its MP4 files: 720 for 1280x720 and for 720x1280
    bandwidth: int
    url: str
    audio_url: str = ""  # the sound, when the variant keeps it in a separate playlist


_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
_RESOLUTION_RE = re.compile(r"(\d+)x(\d+)")


def _attributes(line: str) -> dict[str, str]:
    text = line.split(":", 1)[1] if ":" in line else ""
    return {key: value.strip('"') for key, value in _ATTR_RE.findall(text)}


def hls_variants(text: str, base_url: str) -> list[HlsVariant]:
    """The variants of an HLS master playlist. Empty if the text is not one, or if a variant
    has no RESOLUTION: then the quality cannot be chosen and ffmpeg gets the master as it is."""
    if not text.lstrip().startswith("#EXTM3U"):
        return []
    audio: dict[str, str] = {}
    found = []
    pending: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = _attributes(line)
            if attrs.get("TYPE") == "AUDIO" and attrs.get("URI"):
                group = attrs.get("GROUP-ID", "")
                if group not in audio or attrs.get("DEFAULT") == "YES":
                    audio[group] = urllib.parse.urljoin(base_url, attrs["URI"])
        elif line.startswith("#EXT-X-STREAM-INF:"):
            pending = _attributes(line)
        elif line and not line.startswith("#") and pending is not None:
            match = _RESOLUTION_RE.fullmatch(pending.get("RESOLUTION", ""))
            if not match:
                return []
            height = min(int(match.group(1)), int(match.group(2)))
            bandwidth = pending.get("BANDWIDTH", "")
            bandwidth = int(bandwidth) if bandwidth.isdigit() else 0
            found.append((height, bandwidth, urllib.parse.urljoin(base_url, line), pending.get("AUDIO", "")))
            pending = None
    return [HlsVariant(height, bandwidth, url, audio.get(group, "")) for height, bandwidth, url, group in found]


def choose_variant(variants: list[HlsVariant], max_height: int | None) -> HlsVariant:
    """The same rule as choose_source: the best one not above max_height, else the lowest."""
    def rank(variant: HlsVariant) -> tuple[int, int]:
        return variant.height, variant.bandwidth

    if max_height is None:
        return max(variants, key=rank)
    under = [variant for variant in variants if variant.height <= max_height]
    return max(under, key=rank) if under else min(variants, key=rank)


def resolve_hls(url: str, max_height: int | None, ssl_context) -> tuple[str, list[str]]:
    """The quality label and the inputs for ffmpeg: the chosen variant (with its sound, if that
    is separate), or the master itself when its variants cannot be read."""
    try:
        request = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(request, timeout=30, context=ssl_context) as response:
            base = response.geturl() or url
            text = response.read(1024 * 1024).decode("utf-8", "replace")
    except (OSError, ValueError, http.client.HTTPException):
        return "HLS", [url]
    variants = hls_variants(text, base)
    if not variants:
        return "HLS", [url]
    variant = choose_variant(variants, max_height)
    inputs = [variant.url] + ([variant.audio_url] if variant.audio_url else [])
    return f"{variant.height}p", inputs


# File names

# A Windows path is at most 259 characters (MAX_PATH with its closing NUL). Long paths can be
# turned on, but Explorer and many players still fail on them, so names are cut to fit.
MAX_PATH_CHARS = 259
# The longest name a download writes beside its video: the .part's progress file while
# write_atomic replaces it.
LONGEST_SUFFIX = ".mp4.part.json.tmp"
# What a playlist folder leaves for the names of its files: a separator, «001 - », the start
# of a title, the id a cut name gets and LONGEST_SUFFIX.
FILE_ROOM = 80
# A cut name keeps at least this much, even where the chosen folder itself is too deep.
MIN_NAME = 20


def fit_name(folder: Path, name: str, after: int) -> str:
    """name, cut so that folder, a separator, name and after more characters stay within
    MAX_PATH_CHARS. A cut name loses the spaces and dots at its end: Windows drops them from
    a folder name, and the path with them would lead nowhere."""
    room = MAX_PATH_CHARS - len(str(folder)) - 1 - after
    if len(name) <= room:
        return name
    return name[: max(room, MIN_NAME)].rstrip(" .")


def name_width(total: int) -> int:
    """Digits of the number in file names: 001 for up to 999 videos, more for longer playlists."""
    return max(3, len(str(total)))


def is_saved(dest: Path) -> bool:
    """A finished file: downloads go to .part and get this name only when complete."""
    return dest.exists() and dest.stat().st_size > 0


def _base_name(item: VideoItem, index: int, width: int, naming: str) -> str:
    title = safe_filename(item.title) or f"video_{item.video_id}"
    if naming == "number":
        return f"{index:0{width}d} - {title}"
    if naming == "date" and item.date > 0:
        return f"{time.strftime('%Y-%m-%d', time.localtime(item.date))} - {title}"
    return title


def _short(item: VideoItem) -> str:
    return f"{item.owner_id}_{item.video_id}"


def duplicate_ids(pairs, width: int, naming: str) -> frozenset[str]:
    """Videos, given as (playlist index, video), that would get a file name another video of
    the list also gets: two videos of a page with one title on one day, say. They are returned
    as owner_video, for output_path to add that id to the name.

    Of each group of equal names, the oldest video (the smallest id) keeps the plain name. Ids
    do not change, so the rule gives the same names whatever the order of the list, and a file
    saved before the rule existed stays with the video it was most likely saved for.

    Only the given videos are compared: an owner page read in batches of 1000 does not see a
    match in a batch it has not read yet, which is fine for so rare a case.
    """
    groups: dict[str, list[VideoItem]] = {}
    for index, item in pairs:
        groups.setdefault(_base_name(item, index, width, naming).casefold(), []).append(item)
    tagged: set[str] = set()
    for items in groups.values():
        shorts = {_short(item) for item in items}
        if len(shorts) < 2:
            continue
        keep = min(items, key=lambda item: (item.video_id, item.owner_id))
        tagged |= shorts - {_short(keep)}
    return frozenset(tagged)


def output_path(folder: Path, item: VideoItem, index: int, width: int, naming: str, tagged=frozenset()) -> Path:
    """naming: number (001 - title, playlist order), date (2026-10-11 - title) or title.
    A video in tagged (see duplicate_ids) gets its id added: «title [-1_2].mp4».

    A name too long for the folder (see MAX_PATH_CHARS) is cut and then always gets the id,
    so two titles cut to the same start stay two files: «001 - Long tit… [-1_2].mp4»."""
    name = _base_name(item, index, width, naming)
    tag = f" [{_short(item)}]" if _short(item) in tagged else ""
    room = MAX_PATH_CHARS - len(str(folder)) - 1 - len(LONGEST_SUFFIX)
    if len(name) + len(tag) > room:
        tag = f" [{_short(item)}]"
        name = name[: max(room - len(tag) - 1, MIN_NAME)].rstrip(" .") + "…"
    return folder / f"{name}{tag}.mp4"


def playlist_file_text(entries) -> str:
    """An .m3u8 list of (file, title, duration) for players: names only, the files lie beside it."""
    lines = ["#EXTM3U"]
    for path, title, duration in entries:
        lines.append(f"#EXTINF:{int(duration) if duration > 0 else -1},{' '.join(title.split())}")
        lines.append(Path(path).name)
    return "\n".join(lines) + "\n"


# Downloading

def download_video(
    client: VkClient,
    item: VideoItem,
    dest: Path,
    max_height: int | None,
    cancel: threading.Event,
    on_progress,
) -> str:
    """Download one video. Returns a short quality label, or EXISTS. Raises Cancelled or VkError."""
    if cancel.is_set():
        raise Cancelled()
    if is_saved(dest):
        return EXISTS
    partial = dest.with_suffix(dest.suffix + ".part")
    last_error: VkError | None = None
    for attempt in range(3):
        if cancel.is_set():
            raise Cancelled()
        kind = ""
        try:
            # The links are signed and soon expire, so each try asks VK for them again.
            files = client.video_files(item)
            kind, label, url = choose_source(files, max_height)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if kind == "mp4":
                # What an earlier try or run saved stays in .part, and the download goes on from there.
                _download_mp4(url, partial, client.ssl_context, cancel, on_progress, label)
            else:
                _drop_partial(partial)
                inputs = [url]
                if kind == "hls":
                    label, inputs = resolve_hls(url, max_height, client.ssl_context)
                _download_with_ffmpeg(inputs, partial, cancel, on_progress, item.duration)
            partial.replace(dest)
            return label
        except Cancelled:
            if kind in ("hls", "dash"):
                _drop_partial(partial)  # ffmpeg cannot go on with a file it did not finish
            raise
        except OSError as exc:
            if exc.errno != errno.ENOSPC:
                raise
            if kind in ("hls", "dash"):
                _drop_partial(partial)
            raise VkError(0, tr("err.disk_full"), "disk") from exc
        except VkError as exc:
            last_error = exc
            if kind in ("hls", "dash"):
                _drop_partial(partial)
            if attempt == 2 or exc.code not in RETRY_CODES or exc.kind in NO_RETRY_KINDS:
                raise
            time.sleep(1 + attempt)
    if last_error:
        raise last_error
    raise VkError(0, tr("err.download_failed"))


def free_space(folder: Path) -> int | None:
    """Free bytes on the drive of folder, or of its nearest parent that exists; None if unknown."""
    path = Path(folder)
    while not path.exists() and path.parent != path:
        path = path.parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _check_space(folder: Path, size: int) -> None:
    """Refuse a file the drive has no room for, before any of it is written."""
    free = free_space(folder)
    if free is not None and size > free:
        raise VkError(0, tr("err.no_space", size=fmt_size(size), free=fmt_size(free)), "disk")


def _meta_path(partial: Path) -> Path:
    """Beside a .part of an MP4: its full size, its quality, and how far each part got."""
    return partial.with_name(partial.name + ".json")


def _drop_partial(partial: Path) -> None:
    for path in (partial, _meta_path(partial)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _split(total: int) -> list[list[int]]:
    """Parts of a file of total bytes, as [first byte, last byte, bytes done]."""
    count = CONNECTIONS if total >= MULTI_MIN else 1
    size = -(-total // count)
    return [[start, min(start + size, total) - 1, 0] for start in range(0, total, size)]


def _resume_state(partial: Path, quality: str) -> tuple[int, list[list[int]]] | None:
    """The full size and the parts of an earlier download into partial, or None to start over."""
    try:
        info = json.loads(_meta_path(partial).read_text(encoding="utf-8"))
        size = partial.stat().st_size
        total = int(info["total"])
        if info.get("quality") != quality or total <= 0:
            return None
        if "parts" not in info:
            # Saved by a version that downloaded in one connection: the file holds its first bytes.
            return (total, [[0, total - 1, size]]) if 0 < size <= total else None
        parts = [[int(first), int(last), int(done)] for first, last, done in info["parts"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    # The parts must cover the file one after another, in a file of its full size.
    expected = 0
    for first, last, done in parts:
        if first != expected or last < first or not 0 <= done <= last - first + 1:
            return None
        expected = last + 1
    if expected != total or size != total:
        return None
    return total, parts


def _save_state(partial: Path, total: int, quality: str, parts: list[list[int]]) -> None:
    """Write how far each part got. The bytes go to disk first, so the record never runs ahead."""
    try:
        with partial.open("r+b") as handle:
            os.fsync(handle.fileno())
    except OSError:
        pass
    data = {"total": total, "quality": quality, "parts": [list(part) for part in parts]}
    write_atomic(_meta_path(partial), json.dumps(data))


def _content_range(value: str) -> tuple[int, int]:
    """(first byte, full size) from «bytes 100-199/1000», or (-1, -1)."""
    match = re.fullmatch(r"\s*bytes\s+(\d+)-\d+/(\d+)\s*", value or "")
    if not match:
        return -1, -1
    return int(match.group(1)), int(match.group(2))


def _open_file(url: str, ssl_context, first: int | None = None, last: int | None = None):
    """GET the file, or only bytes first..last of it (to its end when last is None)."""
    headers = dict(HEADERS)
    if first is not None:
        headers["Range"] = f"bytes={first}-{'' if last is None else last}"
    request = urllib.request.Request(url, headers=headers)
    try:
        return urllib.request.urlopen(request, timeout=60, context=ssl_context)
    except urllib.error.HTTPError as exc:
        kind = "access" if exc.code in (403, 451) else "other"
        raise VkError(exc.code, tr("err.file_http", code=exc.code), kind) from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise VkError(0, tr("err.file_failed", reason=reason), "network") from exc


class _OtherFile(Exception):
    """The server answered a part with another range or another full size than the download has."""


def _download_mp4(url: str, partial: Path, ssl_context, cancel: threading.Event, on_progress, quality: str) -> None:
    """Download into partial in several connections at once, each one its own part of the file.

    VK's servers limit the speed of each connection (to about 1 MB/s past a quick start), not
    of the whole address, so CONNECTIONS parts come in about that many times faster. How far
    each part got is kept beside the file, and the next try or run goes on from there.
    """
    state = _resume_state(partial, quality)
    if state is None:
        _drop_partial(partial)
        # One byte tells the full size, and whether the server gives parts of the file at all.
        with _open_file(url, ssl_context, 0, 0) as probe:
            first, total = _content_range(probe.headers.get("Content-Range", ""))
            if probe.status != 206 or first != 0 or total <= 0:
                _stream_whole(probe, partial, cancel, on_progress)
                return
        _check_space(partial.parent, total)
        parts = _split(total)
        with partial.open("wb") as handle:
            handle.truncate(total)  # every part writes at its own place in the file
    else:
        total, parts = state
        if partial.stat().st_size < total:
            _check_space(partial.parent, total - partial.stat().st_size)
            with partial.open("r+b") as handle:
                handle.truncate(total)
    try:
        _fetch_parts(url, partial, ssl_context, cancel, on_progress, quality, total, parts)
    except _OtherFile:
        # A different file behind the new link, or a server that got the range wrong: start over.
        _drop_partial(partial)
        raise VkError(0, tr("err.file_changed"), "network") from None
    _meta_path(partial).unlink(missing_ok=True)


def _left(part: list[int]) -> int:
    return part[1] - part[0] + 1 - part[2]


def _fetch_parts(url, partial: Path, ssl_context, cancel, on_progress, quality: str, total: int, parts) -> None:
    """Up to CONNECTIONS threads take the parts one by one. A thread left without a part takes
    the far half of the biggest one still coming, so the last megabytes do not come in one
    connection. Parts stay in file order and cover it end to end, as the record beside it wants."""
    lock = threading.Lock()
    stop = threading.Event()
    errors: list[BaseException] = []
    taken: set[int] = set()

    def next_part() -> list[int] | None:
        with lock:
            for part in parts:
                if _left(part) > 0 and id(part) not in taken:
                    taken.add(id(part))
                    return part
            busy = [part for part in parts if _left(part) > CHUNK + 2 * STEAL_MIN]
            if not busy:
                return None
            part = max(busy, key=_left)
            # Its thread may be reading one more chunk right now: the cut goes past that.
            reading = part[0] + part[2] + CHUNK
            middle = reading + (part[1] + 1 - reading) // 2
            half = [middle, part[1], 0]
            part[1] = middle - 1
            parts.insert(parts.index(part) + 1, half)
            taken.add(id(half))
            return half

    def fetch(handle, part: list[int]) -> None:
        tries = 0
        while _left(part) > 0 and not (stop.is_set() or cancel.is_set()):
            position = part[0] + part[2]
            try:
                with _open_file(url, ssl_context, position, part[1]) as response:
                    first, size = _content_range(response.headers.get("Content-Range", ""))
                    if response.status != 206 or first != position or size != total:
                        raise _OtherFile()
                    handle.seek(position)
                    while not (stop.is_set() or cancel.is_set()):
                        with lock:
                            amount = min(CHUNK, _left(part))  # the part may have been cut meanwhile
                        if amount <= 0:
                            break
                        try:
                            chunk = response.read(amount)
                        except (OSError, http.client.HTTPException) as exc:
                            raise VkError(0, tr("err.file_failed", reason=exc), "network") from exc
                        if not chunk:
                            raise VkError(0, tr("err.file_cut"), "network")
                        handle.write(chunk)
                        with lock:
                            part[2] += len(chunk)
            except VkError as exc:
                # A dropped connection is opened again for this part alone; the rest go on.
                tries += 1
                if exc.kind != "network" or tries > PART_RETRIES:
                    raise
                time.sleep(tries)

    def work() -> None:
        try:
            with partial.open("r+b", buffering=0) as handle:
                while not (stop.is_set() or cancel.is_set()):
                    part = next_part()
                    if part is None:
                        return
                    fetch(handle, part)
        except BaseException as exc:
            errors.append(exc)
            stop.set()

    count = CONNECTIONS if total >= MULTI_MIN else 1
    workers = [threading.Thread(target=work, daemon=True) for _ in range(count)]
    started = time.monotonic()
    before = sum(part[2] for part in parts)
    for worker in workers:
        worker.start()
    saved = started
    try:
        while any(worker.is_alive() for worker in workers):
            time.sleep(0.2)
            with lock:
                done = sum(part[2] for part in parts)
                snapshot = [list(part) for part in parts]
            on_progress(done, total, (done - before) / max(time.monotonic() - started, 0.001))
            if time.monotonic() - saved >= 2:
                _save_state(partial, total, quality, snapshot)
                saved = time.monotonic()
    finally:
        stop.set()
        for worker in workers:
            worker.join(10)
        if not any(isinstance(exc, _OtherFile) for exc in errors):
            with lock:
                snapshot = [list(part) for part in parts]
            _save_state(partial, total, quality, snapshot)
    if errors:
        raise errors[0]
    if any(_left(part) > 0 for part in parts):
        if cancel.is_set():
            raise Cancelled()
        raise VkError(0, tr("err.file_cut"), "network")


def _stream_whole(response, partial: Path, cancel: threading.Event, on_progress) -> None:
    """A server that does not give parts sends the whole file in one go, and cannot resume it."""
    total = int(response.headers.get("Content-Length") or 0)
    _check_space(partial.parent, total)
    with partial.open("wb") as handle:
        done = 0
        started = time.monotonic()
        while True:
            if cancel.is_set():
                raise Cancelled()
            try:
                chunk = response.read(256 * 1024)
            except (OSError, http.client.HTTPException) as exc:
                raise VkError(0, tr("err.file_failed", reason=exc), "network") from exc
            if not chunk:
                break
            handle.write(chunk)
            done += len(chunk)
            on_progress(done, total, done / max(time.monotonic() - started, 0.001))
    if total and done < total:
        raise VkError(0, tr("err.file_cut"), "network")


# ffmpeg

def ffmpeg_command(ffmpeg: str, inputs: list[str], dest: Path) -> list[str]:
    """Copy the streams of the inputs into an MP4 at dest.

    -f mp4 is needed because dest ends in .part, and ffmpeg picks the format by the extension.
    Only errors go to stderr; progress goes to stdout as key=value lines (-progress pipe:1).
    """
    command = [ffmpeg, "-y", "-nostdin", "-nostats", "-loglevel", "error", "-progress", "pipe:1"]
    headers = "".join(f"{name}: {value}\r\n" for name, value in HEADERS.items())
    for url in inputs:
        command += ["-headers", headers, "-i", url]
    if len(inputs) > 1:
        command += ["-map", "0:v", "-map", "1:a"]
    command += ["-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(dest)]
    return command


def ffmpeg_places() -> list[Path]:
    """Folders searched for ffmpeg.exe before PATH: beside the program, the app's own folder,
    and the folder of winget's links. winget adds that one to PATH only for programs started
    after it, so without this ffmpeg installed while the window is open would not be found."""
    local = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home()))
    places = [Path(__file__).resolve().parent.parent, local / "VKDW", local / "Microsoft" / "WinGet" / "Links"]
    if getattr(sys, "frozen", False):
        places.insert(0, Path(sys.executable).resolve().parent)
    return places


def find_ffmpeg() -> str | None:
    name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    for folder in ffmpeg_places():
        path = folder / name
        if path.is_file():
            return str(path)
    return shutil.which("ffmpeg")


def _download_with_ffmpeg(inputs: list[str], dest: Path, cancel: threading.Event, on_progress, duration: int) -> None:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise VkError(0, tr("err.need_ffmpeg"), "ffmpeg")
    run_ffmpeg(ffmpeg_command(ffmpeg, inputs, dest), cancel, on_progress, duration)


def run_ffmpeg(command: list[str], cancel: threading.Event, on_progress, duration: int) -> None:
    """Run ffmpeg until it ends, reporting (bytes written, estimated size, bytes per second).

    The size is estimated from how much of the video's duration is written already, and is 0
    until that says enough (or when the duration is unknown). Both pipes are read all the time
    by their own threads: a full pipe would make ffmpeg wait forever.
    """
    # Without a console of its own (a windowed exe) Windows would open one for ffmpeg.
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    errors: collections.deque[str] = collections.deque(maxlen=50)
    state = {"time_us": 0, "size": 0}

    def read_errors():
        for line in process.stderr:
            text = line.decode("utf-8", "replace").strip()
            if text:
                errors.append(text)

    def read_progress():
        for line in process.stdout:
            key, _sep, value = line.decode("ascii", "replace").strip().partition("=")
            if key == "out_time_us" and value.isdigit():
                state["time_us"] = int(value)
            elif key == "total_size" and value.isdigit():
                state["size"] = int(value)

    readers = [threading.Thread(target=target, daemon=True) for target in (read_errors, read_progress)]
    for reader in readers:
        reader.start()
    started = time.monotonic()

    def report():
        size = state["size"]
        elapsed = max(time.monotonic() - started, 0.001)
        share = state["time_us"] / (duration * 1_000_000) if duration > 0 else 0.0
        estimate = max(int(size / min(share, 1.0)), size) if share >= 0.01 and size else 0
        on_progress(size, estimate, size / elapsed)

    try:
        while process.poll() is None:
            if cancel.is_set():
                raise Cancelled()
            if time.monotonic() - started > FFMPEG_LIMIT:
                raise VkError(0, tr("err.ffmpeg_too_long"))
            report()
            time.sleep(0.4)
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        raise
    finally:
        for reader in readers:
            reader.join(timeout=5)
        process.stdout.close()
        process.stderr.close()
    if process.returncode != 0:
        tail = errors[-1] if errors else tr("err.ffmpeg")
        raise VkError(0, tail[-300:])
    report()
