"""Bookmarks: playlists and pages to come back to. Also the recently opened ones.

Opened again, a bookmark is read fresh from VK, so new videos show up ticked
while those already in the folder are marked as saved. Kept in settings.json.

What is new depends on the kind. A playlist (a series, say) is wanted whole, so every video
of it not saved yet is new. An owner page is followed for what comes out: only videos added
to it after its bookmark's boundary (since) count, and of them those not saved yet. The
boundary stays where it is when the page is checked, so a video seen and not saved stays
new; «mark everything as seen» moves it on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from vkdw.vk_client import PlaylistInfo, Target, VideoItem

# Only lists that live in VK can be read again; a single video or a collection cannot.
KINDS = ("playlist", "owner")

# How many recently opened lists are kept.
RECENT_LIMIT = 12


@dataclass(frozen=True)
class Bookmark:
    kind: str  # playlist or owner
    owner_id: int
    item_id: int  # the playlist id; 0 for an owner page
    title: str
    access_key: str | None = None
    cover_url: str = ""
    count: int = 0  # videos in VK when it was last opened
    # Of an owner page: videos added after this time (seconds since 1970) are new.
    # 0 until it is first read; a playlist does not use it.
    since: int = 0

    @property
    def key(self) -> tuple:
        """The key of the list it opens, as PlaylistInfo.key."""
        return (self.owner_id, "all") if self.kind == "owner" else (self.owner_id, self.item_id)

    @property
    def target(self) -> Target:
        return Target(self.kind, self.owner_id, self.item_id, self.access_key)


def for_playlist(playlist: PlaylistInfo, source: Target | None = None) -> Bookmark | None:
    """A bookmark for an opened list, or None if it is not one that can be kept."""
    if playlist.kind not in KINDS:
        return None
    item_id = 0 if playlist.kind == "owner" else int(playlist.album_id or 0)
    access_key = source.access_key if source is not None and source.kind == "playlist" else None
    return Bookmark(
        playlist.kind,
        playlist.owner_id,
        item_id,
        playlist.title,
        access_key,
        playlist.cover_url,
        playlist.total or len(playlist.videos),
    )


def newest(playlist: PlaylistInfo) -> int:
    """When the latest video of the list was added to it; now, for a list without times."""
    return max((item.added for item in playlist.videos), default=0) or int(time.time())


def started(mark: Bookmark, playlist: PlaylistInfo) -> Bookmark:
    """A new bookmark: of an owner page, what it has now is seen, and only later videos are new."""
    return replace(mark, since=newest(playlist)) if mark.kind == "owner" else mark


def is_new(mark: Bookmark, item: VideoItem) -> bool:
    """Whether a video not saved yet counts as new for the bookmark."""
    return mark.kind != "owner" or item.added > mark.since


def refreshed(mark: Bookmark, playlist: PlaylistInfo) -> Bookmark:
    """The bookmark with the name, cover and size the list has in VK now. An owner page kept
    before bookmarks had a boundary gets one now, so it starts with nothing new."""
    fresh = replace(
        mark,
        title=playlist.title or mark.title,
        cover_url=playlist.cover_url or mark.cover_url,
        count=playlist.total or len(playlist.videos),
    )
    return started(fresh, playlist) if mark.kind == "owner" and not mark.since else fresh


def remember(recent: list[Bookmark], playlist: PlaylistInfo, source: Target | None = None) -> list[Bookmark]:
    """Recently opened lists with this one first; past RECENT_LIMIT the oldest drop off.
    Kept in the same shape as bookmarks, so a list is reopened the same way."""
    mark = for_playlist(playlist, source)
    if mark is None:
        return recent
    return [mark] + [old for old in recent if old.key != mark.key][: RECENT_LIMIT - 1]


def to_data(marks: list[Bookmark]) -> list[dict]:
    return [
        {
            "kind": mark.kind,
            "owner_id": mark.owner_id,
            "item_id": mark.item_id,
            "title": mark.title,
            "access_key": mark.access_key,
            "cover_url": mark.cover_url,
            "count": mark.count,
            "since": mark.since,
        }
        for mark in marks
    ]


def from_data(data) -> list[Bookmark]:
    """Bookmarks from settings; entries that cannot be read are skipped."""
    marks = []
    seen = set()
    for entry in data if isinstance(data, list) else ():
        try:
            mark = Bookmark(
                str(entry["kind"]),
                int(entry["owner_id"]),
                int(entry["item_id"]),
                str(entry.get("title", "")),
                entry.get("access_key") or None,
                str(entry.get("cover_url", "")),
                int(entry.get("count", 0)),
                int(entry.get("since", 0)),
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if mark.kind in KINDS and mark.key not in seen:
            seen.add(mark.key)
            marks.append(mark)
    return marks
