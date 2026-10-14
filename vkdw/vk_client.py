"""Read public VK Video playlists, community videos and direct file links."""

from __future__ import annotations

import http.client
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace

import certifi

from vkdw.i18n import language, tr

# Public web-client id of vkvideo.ru. The site uses it to mint a short-lived
# anonymous token. This is not a user credential.
_ANON_CLIENT_ID = "52461373"
_ANON_CLIENT_SECRET = "o557NLIkAErNhakXrQ7A"
_ANON_SCOPES = "video_anonymous,photos_anonymous,profile_anonymous"

API_URL = "https://api.vkvideo.ru/method/"
TOKEN_URL = "https://login.vk.ru/"
API_VERSION = "5.269"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
PAGE_SIZE = 100
# Videos of a community are read this many at a time; "Load more" asks for the next batch.
OWNER_BATCH = 1000
# Widths of the preview images to ask VK for: a row thumbnail and a playlist cover.
THUMB_WIDTH = 320
COVER_WIDTH = 320
# Wider than any VK image: pick_image then gives the largest, for a cover saved to disk.
FULL_WIDTH = 100_000

_PLAYLIST_RE = re.compile(
    r"playlist/(-?\d+)_(-?\d+)(?:_([A-Za-z0-9]+))?",
    re.IGNORECASE,
)
_SECTION_RE = re.compile(r"videos(-?\d+).*?section=album_(-?\d+)", re.IGNORECASE)
_VIDEO_RE = re.compile(
    r"(?:(?:^|[/=?&])(?:video|clip)|z=video)(-?\d+)_(\d+)(?:_([A-Za-z0-9]+))?",
    re.IGNORECASE,
)
# Pages whose videos can be listed: vk.com/videos-123, vk.com/club123, vkvideo.ru/@name, vk.com/name.
_VIDEOS_PAGE_RE = re.compile(r"^/videos(-?\d+)/?$", re.IGNORECASE)
_NUMBERED_PAGE_RE = re.compile(r"^/(club|public|event|id)(\d+)/?$", re.IGNORECASE)
_AT_PAGE_RE = re.compile(r"^/(?:video/)?@([A-Za-z0-9_.]+)", re.IGNORECASE)
_NAMED_PAGE_RE = re.compile(r"^/([A-Za-z0-9_.]{2,64})/?$")
_NOT_PAGES = {
    "video", "videos", "clips", "feed", "im", "search", "music", "audio", "groups", "friends",
    "settings", "login", "catalog", "playlist", "playlists", "away.php", "share.php",
}
_VK_HOSTS = ("vkvideo.ru", "vk.com", "vk.ru", "vk.me", "vkontakte.ru")
# Short links: where they lead is known only from their redirect, read when the link is opened.
_SHORT_HOSTS = ("vk.cc", "vk.link")
_WALL_RE = re.compile(r"(?:^|[/=?&])wall(-?\d+)_(\d+)", re.IGNORECASE)


class VkError(Exception):
    """kind tells the window what may help: input (a bad link), access (VK refused,
    perhaps for this region), network (no connection), ffmpeg (needed and not found),
    disk (no room for the file) or other."""

    def __init__(self, code: int, message: str, kind: str = "other"):
        self.code = code
        self.kind = kind
        super().__init__(message)


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class Target:
    # "playlist", "video", "owner" (all videos of a community or a person) or "short" (a vk.cc
    # link, resolved into one of the others when opened)
    kind: str
    owner_id: int
    item_id: int
    access_key: str | None = None
    screen_name: str = ""  # an owner page given by its short name, resolved when opened
    link: str = ""  # the whole short link


@dataclass(frozen=True)
class VideoItem:
    owner_id: int
    video_id: int
    title: str
    duration: int
    owner_name: str
    restriction: str  # why VK does not give the video, or "" if it can be saved
    access_key: str | None = None
    thumb_url: str = ""
    date: int = 0  # upload time, seconds since 1970
    # VK's warning on a video it still gives, such as an age limit; "" if none.
    warning: str = ""
    # When it was added to the list it was read from: the upload time for an owner's own
    # video, later for one added from elsewhere. Bookmarks of owner pages count new ones by it.
    added: int = 0

    @property
    def raw_id(self) -> str:
        base = f"{self.owner_id}_{self.video_id}"
        if self.access_key:
            return f"{base}_{self.access_key}"
        return base


@dataclass(frozen=True)
class PlaylistInfo:
    owner_id: int
    album_id: int | None
    title: str
    videos: tuple[VideoItem, ...]
    cover_url: str = ""
    owner_name: str = ""
    # playlist, video (one video), owner (all videos of a page) or collection (gathered by hand)
    kind: str = "playlist"
    total: int = 0  # how many videos the page has in VK; more than len(videos) until all are read
    albums: tuple = ()  # (album_id, title, count) of an owner page's playlists
    collection_id: int = 0

    @property
    def key(self) -> tuple:
        if self.kind == "owner":
            return (self.owner_id, "all")
        if self.kind == "collection":
            return ("collection", self.collection_id)
        if self.kind == "video" and self.videos:
            # Two single videos of one owner are two lists, so the video's id is part of the key.
            return video_key(self.owner_id, self.videos[0].video_id)
        return (self.owner_id, self.album_id)

    @property
    def single(self) -> bool:
        return self.kind == "video"

    @property
    def naming(self) -> str:
        """How files are named: numbered in playlist order, by upload date, or by title alone."""
        if self.kind == "playlist":
            return "number"
        if self.kind == "video":
            return "title"
        return "date"

    @property
    def has_more(self) -> bool:
        return self.kind == "owner" and self.total > len(self.videos)


def video_key(owner_id: int, video_id: int) -> tuple:
    """PlaylistInfo.key of a single opened video."""
    return (owner_id, "video", video_id)


def parse_target(raw_url: str) -> Target:
    text = (raw_url or "").strip()
    if not text:
        raise VkError(0, tr("err.empty_link"), "input")
    if not re.match(r"https?://", text, re.IGNORECASE):
        text = "https://" + text
    parsed = urllib.parse.urlparse(text)
    host = _host(parsed)
    if host in _SHORT_HOSTS:
        if not parsed.path.strip("/"):
            raise VkError(0, tr("err.no_target"), "input")
        return Target("short", 0, 0, link=text)
    if not _is_vk_host(host):
        raise VkError(0, tr("err.not_vk"), "input")

    if "/playlist/" in parsed.path:
        match = _PLAYLIST_RE.search(parsed.path)
        if not match:
            raise VkError(0, tr("err.bad_playlist_link"), "input")
        return Target("playlist", int(match.group(1)), int(match.group(2)), match.group(3))

    if parsed.path.lower().endswith("/video_ext.php"):
        # The player embedded on other sites: video_ext.php?oid=-123&id=456&hash=…
        query = urllib.parse.parse_qs(parsed.query)
        try:
            return Target("video", int(query["oid"][0]), int(query["id"][0]))
        except (KeyError, IndexError, ValueError):
            raise VkError(0, tr("err.bad_embed_link"), "input") from None

    section = _SECTION_RE.search(text)
    if section:
        return Target("playlist", int(section.group(1)), int(section.group(2)))

    match = _VIDEO_RE.search(text)
    if match:
        return Target("video", int(match.group(1)), int(match.group(2)), match.group(3))

    if _WALL_RE.search(text):
        # wall.getById is not open to the anonymous token, and the post page shows no videos
        # without signing in.
        raise VkError(0, tr("err.wall_post"), "input")

    page = _owner_page(parsed.path)
    if page is not None:
        return page
    raise VkError(0, tr("err.no_target"), "input")


def _host(parsed) -> str:
    host = (parsed.netloc or "").lower().split(":")[0]
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def _is_vk_host(host: str) -> bool:
    return any(host == name or host.endswith("." + name) for name in _VK_HOSTS)


def _owner_page(path: str) -> Target | None:
    match = _VIDEOS_PAGE_RE.match(path)
    if match:
        return Target("owner", int(match.group(1)), 0)
    match = _NUMBERED_PAGE_RE.match(path)
    if match:
        number = int(match.group(2))
        return Target("owner", number if match.group(1).lower() == "id" else -number, 0)
    match = _AT_PAGE_RE.match(path)
    if match:
        return _named_page(match.group(1))
    match = _NAMED_PAGE_RE.match(path)
    if match and match.group(1).lower() not in _NOT_PAGES:
        return _named_page(match.group(1))
    return None


def _named_page(name: str) -> Target:
    numbered = _NUMBERED_PAGE_RE.match("/" + name)
    if numbered:
        return _owner_page("/" + name)
    return Target("owner", 0, 0, screen_name=name)


def parse_targets(text: str) -> list[Target]:
    """Every VK link in a text, in order and without repeats. One link works as parse_target."""
    pieces = [piece for piece in re.split(r"[\s,;]+", text or "") if piece]
    if len(pieces) <= 1:
        return [parse_target(text)]
    targets: list[Target] = []
    first_error: VkError | None = None
    for piece in pieces:
        try:
            target = parse_target(piece)
        except VkError as exc:
            first_error = first_error or exc
            continue
        if target not in targets:
            targets.append(target)
    if not targets:
        raise first_error or VkError(0, tr("err.no_target"), "input")
    return targets


def _away_target(url: str) -> str:
    """The address behind VK's away.php?to=…, which short links lead through; else url itself."""
    parsed = urllib.parse.urlparse(url)
    if parsed.path.lower().endswith("/away.php"):
        to = urllib.parse.parse_qs(parsed.query).get("to")
        if to and to[0]:
            return to[0]
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def target_raw_id(target: Target) -> str:
    raw = f"{target.owner_id}_{target.item_id}"
    return f"{raw}_{target.access_key}" if target.access_key else raw


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    # Again after the cut: Windows drops spaces and dots at the end of a folder name.
    return cleaned[:150].rstrip(" .")


def format_duration(seconds: int) -> str:
    if not seconds or seconds < 0:
        return ""
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class VkClient:
    def __init__(self):
        self._anon_token = ""
        self._anon_expires = 0.0
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    def open_target(self, target: Target, cancel=None, limit: int = OWNER_BATCH) -> PlaylistInfo:
        if target.kind == "video":
            return self._open_video(target)
        if target.kind == "owner":
            return self._open_owner(target, cancel, None, limit)
        return self._open_playlist(target, cancel)

    def open_more(self, page: PlaylistInfo, cancel=None) -> PlaylistInfo:
        """The next batch of an owner page's videos, added after the ones already read."""
        return self._open_owner(Target("owner", page.owner_id, 0), cancel, page, OWNER_BATCH)

    def video_files(self, item: VideoItem) -> dict:
        response = self._call("video.get", videos=item.raw_id)
        items = response.get("items") or []
        if not items:
            raise VkError(0, tr("err.video_not_found"), "access")
        video = items[0]
        message = _restriction_text(video)
        files = video.get("files") or {}
        live = _live_text(video, files)
        if live:
            raise VkError(0, live)
        if message and not _has_file(files):
            raise VkError(0, message, "access")
        if not files:
            raise VkError(0, tr("err.no_file_link"), "access")
        return files

    def open_videos(self, raw_ids: list[str], cancel=None) -> list[VideoItem]:
        """Videos by id, in the order asked. Ones VK does not return are left out."""
        found: dict[str, VideoItem] = {}
        for start in range(0, len(raw_ids), PAGE_SIZE):
            _check_cancel(cancel)
            chunk = raw_ids[start : start + PAGE_SIZE]
            response = self._call("video.get", videos=",".join(chunk), extended=1)
            names = _owner_names(response)
            for entry in response.get("items") or []:
                video = _video_item(entry, names)
                found[f"{video.owner_id}_{video.video_id}"] = video
        ordered = []
        for raw in raw_ids:
            owner, video_id = raw.split("_")[:2]
            video = found.get(f"{owner}_{video_id}")
            if video is not None:
                if not video.access_key and raw.count("_") == 2:
                    video = replace(video, access_key=raw.split("_")[2])
                ordered.append(video)
        return ordered

    def video_covers(self, raw_ids: list[str], cancel=None) -> dict[str, str]:
        """Full-size preview URL of each video, by owner_video. Videos VK does not return,
        or returns without a preview, are left out."""
        found: dict[str, str] = {}
        for start in range(0, len(raw_ids), PAGE_SIZE):
            _check_cancel(cancel)
            response = self._call("video.get", videos=",".join(raw_ids[start : start + PAGE_SIZE]))
            for entry in response.get("items") or []:
                url = _video_image(entry, FULL_WIDTH)
                if url:
                    found[f"{entry.get('owner_id')}_{entry.get('id')}"] = url
        return found

    def album_cover(self, owner_id: int, album_id: int) -> str:
        """Full-size cover URL of a playlist, or "" if it has none of its own."""
        album = self._call("video.getAlbumById", owner_id=owner_id, album_id=album_id)
        return pick_image(album.get("image"), FULL_WIDTH) if isinstance(album, dict) else ""

    def _open_video(self, target: Target) -> PlaylistInfo:
        response = self._call("video.get", videos=target_raw_id(target), extended=1)
        items = response.get("items") or []
        if not items:
            raise VkError(0, tr("err.video_hidden"), "access")
        names = _owner_names(response)
        video = _video_item(items[0], names)
        title = video.title or f"video{video.raw_id}"
        cover = _video_image(items[0], COVER_WIDTH) or video.thumb_url
        return PlaylistInfo(
            target.owner_id, None, title, (video,), cover_url=cover, owner_name=video.owner_name, kind="video"
        )

    def _open_playlist(self, target: Target, cancel) -> PlaylistInfo:
        title, cover = self._album_meta(target)
        owner_name = ""
        videos: list[VideoItem] = []
        seen: set[str] = set()
        offset = 0
        total = None
        while total is None or offset < total:
            _check_cancel(cancel)
            params = {
                "owner_id": target.owner_id,
                "album_id": target.item_id,
                "count": PAGE_SIZE,
                "offset": offset,
                "extended": 1,
            }
            if target.access_key:
                params["access_key"] = target.access_key
            try:
                response = self._call("video.getFromAlbum", **params)
            except VkError as exc:
                if exc.code in (15, 204) and not videos:
                    raise VkError(exc.code, tr("err.playlist_closed"), "access") from exc
                raise
            batch = response.get("items") or []
            if not batch:
                break
            names = _owner_names(response)
            owner_name = owner_name or names.get(target.owner_id, "")
            fresh = 0
            for entry in batch:
                video = _video_item(entry, names)
                if video.raw_id in seen:
                    continue
                seen.add(video.raw_id)
                videos.append(video)
                fresh += 1
            if fresh == 0:
                break
            total = int(response.get("count") or len(videos))
            offset += len(batch)
            if len(videos) > 5000:
                break
        if not videos:
            raise VkError(0, tr("err.playlist_empty"), "access")
        if not cover:
            cover = next((video.thumb_url for video in videos if video.thumb_url), "")
        return PlaylistInfo(
            target.owner_id,
            target.item_id,
            title,
            tuple(videos),
            cover_url=cover,
            owner_name=owner_name,
        )

    def _open_owner(self, target: Target, cancel, have: PlaylistInfo | None, limit: int) -> PlaylistInfo:
        owner_id = self.resolve_owner(target)
        videos = list(have.videos) if have is not None else []
        seen = {video.raw_id for video in videos}
        names: dict[int, str] = {}
        offset = len(videos)
        stop_at = offset + max(limit, PAGE_SIZE)
        total = have.total if have is not None else 0
        while offset < stop_at:
            _check_cancel(cancel)
            try:
                response = self._call("video.get", owner_id=owner_id, count=PAGE_SIZE, offset=offset, extended=1)
            except VkError as exc:
                if exc.code in (15, 30, 204) and not videos:
                    raise VkError(exc.code, tr("err.owner_closed"), "access") from exc
                raise
            batch = response.get("items") or []
            total = int(response.get("count") or 0)
            if not batch:
                break
            names.update(_owner_names(response))
            for entry in batch:
                video = _video_item(entry, names)
                if video.raw_id not in seen:
                    seen.add(video.raw_id)
                    videos.append(video)
            offset += len(batch)
            if offset >= total:
                break
        if not videos:
            raise VkError(0, tr("err.owner_empty"))
        if have is not None:
            owner_name, albums, cover = have.owner_name, have.albums, have.cover_url
        else:
            owner_name = names.get(owner_id) or self._owner_name(owner_id)
            albums = self._owner_albums(owner_id)
            cover = next((video.thumb_url for video in videos if video.thumb_url), "")
        return PlaylistInfo(
            owner_id,
            None,
            owner_name or tr("owner.fallback_title", id=owner_id),
            tuple(videos),
            cover_url=cover,
            owner_name=owner_name,
            kind="owner",
            total=max(total, len(videos)),
            albums=albums,
        )

    def resolve_link(self, target: Target) -> Target:
        """Where a short link leads, read from its redirect without following it."""
        url = target.link
        for _hop in range(4):
            location = _away_target(self._redirect(url))
            if not location:
                break
            parsed = urllib.parse.urlparse(location)
            host = _host(parsed)
            if host not in _SHORT_HOSTS and not _is_vk_host(host):
                raise VkError(0, tr("err.short_elsewhere", url=location[:200]), "input")
            if not parsed.path.strip("/"):
                break  # an unknown short link leads to VK's main page
            found = parse_target(location)  # a VK link with no video says why itself
            if found.kind != "short":
                return found
            url = found.link
        raise VkError(0, tr("err.short_dead"), "input")

    def _redirect(self, url: str) -> str:
        """The Location of a redirect at url, or "" when the server answers without one."""
        opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=self.ssl_context))
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with opener.open(request, timeout=30):
                return ""
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") or ""
            if 300 <= exc.code < 400 and location:
                return urllib.parse.urljoin(url, location)
            if exc.code == 404:
                return ""
            raise VkError(exc.code, f"HTTP {exc.code}", "other") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise VkError(0, tr("err.no_connection", reason=getattr(exc, "reason", exc)), "network") from exc

    def resolve_owner(self, target: Target) -> int:
        """The numeric id of an owner page, asking VK when the link had a short name."""
        if target.owner_id or not target.screen_name:
            return target.owner_id
        response = self._call("utils.resolveScreenName", screen_name=target.screen_name)
        if not isinstance(response, dict) or not response.get("object_id"):
            raise VkError(0, tr("err.page_not_found", name=target.screen_name), "input")
        kind = response.get("type")
        object_id = int(response["object_id"])
        if kind == "user":
            return object_id
        if kind in ("group", "page", "event"):
            return -object_id
        raise VkError(0, tr("err.page_not_found", name=target.screen_name), "input")

    def _owner_name(self, owner_id: int) -> str:
        try:
            if owner_id < 0:
                response = self._call("groups.getById", group_ids=-owner_id)
                groups = response.get("groups") if isinstance(response, dict) else response
                return str((groups or [{}])[0].get("name") or "")
            users = self._call("users.get", user_ids=owner_id) or [{}]
            return f"{users[0].get('first_name') or ''} {users[0].get('last_name') or ''}".strip()
        except (VkError, AttributeError, IndexError, TypeError):
            return ""

    def _owner_albums(self, owner_id: int) -> tuple:
        try:
            # Without extended VK leaves out how many videos each playlist has.
            response = self._call("video.getAlbums", owner_id=owner_id, count=100, extended=1)
        except VkError:
            return ()
        albums = []
        for album in response.get("items") or []:
            try:
                albums.append((int(album["id"]), str(album.get("title") or ""), int(album.get("count") or 0)))
            except (KeyError, TypeError, ValueError):
                continue
        return tuple(album for album in albums if album[2] > 0)

    def _album_meta(self, target: Target) -> tuple[str, str]:
        """Album title and cover image URL."""
        fallback = tr("playlist.fallback_title", id=f"{target.owner_id}_{target.item_id}")
        try:
            album = self._call(
                "video.getAlbumById",
                owner_id=target.owner_id,
                album_id=target.item_id,
            )
        except VkError:
            return fallback, ""
        title = (album.get("title") or "").strip()
        return title or fallback, pick_image(album.get("image"), COVER_WIDTH)

    def _token(self) -> str:
        now = time.time()
        if self._anon_token and now < self._anon_expires:
            return self._anon_token
        query = urllib.parse.urlencode(
            {
                "act": "get_anonym_token",
                "client_id": _ANON_CLIENT_ID,
                "client_secret": _ANON_CLIENT_SECRET,
                "scopes": _ANON_SCOPES,
            }
        )
        payload = _json(self._request(TOKEN_URL + "?" + query))
        data = payload.get("data") or {}
        token = data.get("access_token") or ""
        if not token:
            raise VkError(0, tr("err.anon_token"), "access")
        expires = float(data.get("expires") or data.get("expired_at") or 0)
        self._anon_token = token
        self._anon_expires = expires - 60 if expires else now + 3600
        return token

    def _call(self, method: str, **params):
        last_error: VkError | None = None
        for attempt in range(4):
            query = {key: value for key, value in params.items() if value is not None}
            query["v"] = API_VERSION
            query["lang"] = language()
            try:
                query["access_token"] = self._token()
                payload = _json(self._request(API_URL + method, data=query))
            except VkError as exc:
                last_error = exc
                if attempt < 3 and exc.code in (0, 6, 9, 10, 29):
                    time.sleep(0.6 * (attempt + 1))
                    continue
                raise
            if "error" not in payload:
                return payload.get("response")
            err = payload["error"]
            code = int(err.get("error_code") or 0)
            message = str(err.get("error_msg") or tr("err.api"))
            last_error = _api_error(code, message)
            if code == 5:
                self._anon_token = ""
                continue
            if code == 6 and attempt < 3:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise last_error
        if last_error:
            raise last_error
        raise VkError(0, tr("err.api_silent"), "network")

    def _request(self, url: str, data: dict | None = None, timeout: float = 40) -> str:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://vkvideo.ru/",
            "Origin": "https://vkvideo.ru",
        }
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self.ssl_context) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:240]
            raise VkError(exc.code, detail or f"HTTP {exc.code}", "access" if exc.code in (403, 451) else "other") from exc
        except urllib.error.URLError as exc:
            raise VkError(0, tr("err.no_connection", reason=exc.reason), "network") from exc
        except (OSError, http.client.HTTPException) as exc:
            # Cut while the answer was read (a reset, a timeout, an incomplete body): urlopen
            # wraps only what goes wrong before the answer starts.
            raise VkError(0, tr("err.no_connection", reason=exc), "network") from exc
        return raw.decode(charset, "replace")


def _json(text: str) -> dict:
    """An answer of VK's API. Something else (say, a page of a proxy) is a network error,
    so it gets another try like a dropped connection."""
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise VkError(0, tr("err.api_garbled"), "network")
    return payload


def _check_cancel(cancel) -> None:
    if cancel is not None and cancel():
        raise Cancelled()


def _owner_names(response: dict) -> dict[int, str]:
    names: dict[int, str] = {}
    for group in response.get("groups") or []:
        try:
            names[-int(group["id"])] = str(group.get("name") or "")
        except (KeyError, TypeError, ValueError):
            continue
    for profile in response.get("profiles") or []:
        try:
            owner_id = int(profile["id"])
        except (KeyError, TypeError, ValueError):
            continue
        full = f"{profile.get('first_name') or ''} {profile.get('last_name') or ''}".strip()
        names[owner_id] = full
    return names


def _video_item(entry: dict, names: dict[int, str]) -> VideoItem:
    video = entry.get("video") if isinstance(entry.get("video"), dict) else entry
    owner_id = int(video.get("owner_id") or 0)
    video_id = int(video.get("id") or 0)
    title = str(video.get("title") or "").strip() or f"video{owner_id}_{video_id}"
    return VideoItem(
        owner_id=owner_id,
        video_id=video_id,
        title=title,
        duration=int(video.get("duration") or 0),
        owner_name=names.get(owner_id, ""),
        restriction=_restriction_text(video),
        access_key=(video.get("access_key") or None),
        thumb_url=_video_image(video, THUMB_WIDTH),
        date=int(video.get("date") or 0),
        warning=_warning_text(video),
        added=int(video.get("adding_date") or video.get("date") or 0),
    )


def _video_image(entry: dict, width: int) -> str:
    video = entry.get("video") if isinstance(entry.get("video"), dict) else entry
    return pick_image(video.get("image"), width) or pick_image(video.get("first_frame"), width)


def pick_image(images, width: int) -> str:
    """URL of the smallest VK image at least `width` wide, preferring ones without black bars."""
    usable = [img for img in images or [] if isinstance(img, dict) and img.get("url")]
    if not usable:
        return ""
    plain = [img for img in usable if not img.get("with_padding")] or usable
    plain.sort(key=lambda img: int(img.get("width") or 0))
    for img in plain:
        if int(img.get("width") or 0) >= width:
            return str(img["url"])
    return str(plain[-1]["url"])


def _playable(video: dict) -> bool:
    """A restriction VK only warns about, such as an age limit: the video plays after a click,
    and its files are given as usual."""
    restriction = video.get("restriction")
    return not video.get("content_restricted") and isinstance(restriction, dict) and bool(restriction.get("can_play"))


def _warning_text(video: dict) -> str:
    if not _playable(video):
        return ""
    return str(video["restriction"].get("title") or tr("row.age")).strip()


def _restriction_text(video: dict) -> str:
    if not video.get("content_restricted") and not video.get("restriction"):
        return ""
    if _playable(video):
        return ""
    message = video.get("content_restricted_message")
    if not message and isinstance(video.get("restriction"), dict):
        message = video["restriction"].get("title") or video["restriction"].get("text")
    return str(message or tr("err.video_unavailable")).strip()


def _live_text(video: dict, files: dict) -> str:
    """Why a live stream cannot be saved, or "" for a video (a recording of a past stream too)."""
    status = str(video.get("live_status") or "")
    if status in ("waiting", "upcoming") or video.get("upcoming"):
        return tr("err.live_upcoming")
    if status == "started" or (video.get("live") and status != "finished" and not _has_mp4(files)):
        return tr("err.live")
    return ""


def _has_mp4(files: dict) -> bool:
    return any(str(key).startswith("mp4_") for key in files)


def _has_file(files: dict) -> bool:
    return any(str(key).startswith("mp4_") or key in ("hls", "hls_fmp4", "dash_sep") for key in files)


def _api_error(code: int, message: str) -> VkError:
    if code in (15, 204):
        return VkError(code, tr("err.access_denied"), "access")
    if code == 30:
        return VkError(code, tr("err.private_profile"), "access")
    if code == 5:
        return VkError(code, tr("err.anon_token"), "access")
    if code == 6:
        return VkError(code, tr("err.rate_limit"))
    return VkError(code, message)
