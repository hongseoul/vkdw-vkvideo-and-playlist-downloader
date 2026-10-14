"""The queue and the lists behind it, kept on disk between runs.

If the window is closed or the computer stops, the next start shows the same
queue, paused: «Resume» goes on, and files already saved are skipped.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from vkdw.jobs import QueueJob
from vkdw.settings import write_atomic
from vkdw.vk_client import PlaylistInfo, Target, VideoItem, video_key

VERSION = 1


def session_path() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(root) / "VKDW" / "session.json"


@dataclass
class Session:
    jobs: list[QueueJob] = field(default_factory=list)
    playlists: dict[tuple, PlaylistInfo] = field(default_factory=dict)
    # Where each list came from, to read it again.
    sources: dict[tuple, Target] = field(default_factory=dict)
    # Statuses of videos, as DownloadController.notes.
    notes: dict[tuple, dict[str, tuple[str, str]]] = field(default_factory=dict)
    # The window's last opened list and collection, and the ticks of unqueued lists.
    draft: tuple | None = None
    collection: tuple | None = None
    picks: dict[tuple, set[str]] = field(default_factory=dict)


# Writing

def _video(item: VideoItem) -> list:
    return [item.owner_id, item.video_id, item.title, item.duration, item.owner_name,
            item.restriction, item.access_key, item.thumb_url, item.date, item.warning, item.added]


def _playlist(info: PlaylistInfo) -> dict:
    return {
        "owner_id": info.owner_id,
        "album_id": info.album_id,
        "title": info.title,
        "videos": [_video(item) for item in info.videos],
        "cover_url": info.cover_url,
        "owner_name": info.owner_name,
        "kind": info.kind,
        "total": info.total,
        "albums": [list(album) for album in info.albums],
        "collection_id": info.collection_id,
    }


def _job(job: QueueJob) -> dict:
    # A job that was downloading when the program ended goes on from where it stopped.
    status = "interrupted" if job.status == "running" else job.status
    return {
        "job_id": job.job_id,
        "title": job.title,
        "video_count": job.video_count,
        "quality": job.quality,
        "folder_label": job.folder_label,
        "status": status,
        "summary": job.summary,
        "naming": job.naming,
        "video_total": job.video_total,
        "items": [[index, _video(item)] for index, item in job.items],
        "target_dir": str(job.target_dir) if job.target_dir is not None else None,
        "max_height": job.max_height,
        "playlist_key": list(job.playlist_key),
        "cover_url": job.cover_url,
        "tagged": sorted(job.tagged),
        "failures": [list(failure) for failure in job.failures],
        "outcome": job.outcome,
    }


def _target(target: Target) -> dict:
    return {
        "kind": target.kind,
        "owner_id": target.owner_id,
        "item_id": target.item_id,
        "access_key": target.access_key,
        "screen_name": target.screen_name,
    }


def _notes(notes: dict[str, tuple[str, str]]) -> dict:
    # A video that was downloading when the program ended was cut off.
    return {raw_id: ["", "stopped"] if state == "active" else [note, state] for raw_id, (note, state) in notes.items()}


def dump(state: Session) -> str:
    data = {
        "version": VERSION,
        "jobs": [_job(job) for job in state.jobs],
        "playlists": [_playlist(info) for info in state.playlists.values()],
        "sources": [[list(key), _target(target)] for key, target in state.sources.items()],
        "notes": [[list(key), _notes(notes)] for key, notes in state.notes.items() if notes],
        "draft": list(state.draft) if state.draft is not None else None,
        "collection": list(state.collection) if state.collection is not None else None,
        "picks": [[list(key), sorted(ids)] for key, ids in state.picks.items()],
    }
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def save(path: Path, text: str) -> bool:
    return write_atomic(path, text)


# Reading

def _key(value) -> tuple | None:
    return tuple(value) if isinstance(value, list) else None


def _read_video(row: list) -> VideoItem:
    # Rows written before warnings were kept have nine fields, before the adding time ten.
    owner_id, video_id, title, duration, owner_name, restriction, access_key, thumb_url, date, *rest = row
    warning = rest[0] if rest else ""
    added = rest[1] if len(rest) > 1 else date
    return VideoItem(int(owner_id), int(video_id), str(title), int(duration), str(owner_name),
                     str(restriction), access_key or None, str(thumb_url), int(date), str(warning or ""),
                     int(added or 0))


def _read_playlist(data: dict) -> PlaylistInfo:
    return PlaylistInfo(
        owner_id=int(data["owner_id"]),
        album_id=data["album_id"],
        title=str(data["title"]),
        videos=tuple(_read_video(row) for row in data["videos"]),
        cover_url=str(data.get("cover_url", "")),
        owner_name=str(data.get("owner_name", "")),
        kind=str(data.get("kind", "playlist")),
        total=int(data.get("total", 0)),
        albums=tuple(tuple(album) for album in data.get("albums", ())),
        collection_id=int(data.get("collection_id", 0)),
    )


def _read_job(data: dict) -> QueueJob:
    target = data.get("target_dir")
    return QueueJob(
        job_id=str(data["job_id"]),
        title=str(data["title"]),
        video_count=int(data.get("video_count", 0)),
        quality=str(data.get("quality", "")),
        folder_label=str(data.get("folder_label", "")),
        status=str(data.get("status", "waiting")),
        summary=str(data.get("summary", "")),
        naming=str(data.get("naming", "number")),
        video_total=int(data.get("video_total", 0)),
        items=tuple((int(index), _read_video(row)) for index, row in data["items"]),
        target_dir=Path(target) if target else None,
        max_height=data.get("max_height"),
        playlist_key=_key(data.get("playlist_key")) or (),
        cover_url=str(data.get("cover_url", "")),
        tagged=frozenset(str(short) for short in data.get("tagged", ())),
        failures=tuple(tuple(failure) for failure in data.get("failures", ())),
        outcome=str(data.get("outcome", "")),
    )


def _read_target(data: dict) -> Target:
    return Target(str(data["kind"]), int(data["owner_id"]), int(data["item_id"]),
                  data.get("access_key") or None, str(data.get("screen_name", "")))


def _old_video_key(key) -> bool:
    return isinstance(key, tuple) and len(key) == 2 and key[1] is None


def _migrate_video_keys(state: Session) -> Session:
    """Sessions written before a single video's key held its id keyed it as (owner, None),
    so two videos of one owner shared one list. Each job gets the key of its own video back,
    with the list rebuilt from the job when the file kept only the other video."""
    renamed = {(info.owner_id, None): key for key, info in state.playlists.items() if info.kind == "video"}
    old_notes = {key: notes for key, notes in state.notes.items() if _old_video_key(key)}
    for job in state.jobs:
        if not _old_video_key(job.playlist_key) or not job.items:
            continue
        item = job.items[0][1]
        key = video_key(item.owner_id, item.video_id)
        if key not in state.playlists:
            state.playlists[key] = PlaylistInfo(
                item.owner_id, None, job.title, (item,), cover_url=job.cover_url, owner_name=item.owner_name, kind="video"
            )
            state.sources[key] = Target("video", item.owner_id, item.video_id, item.access_key)
        notes = old_notes.get(job.playlist_key, {})
        if item.raw_id in notes:
            state.notes.setdefault(key, {})[item.raw_id] = notes[item.raw_id]
        job.playlist_key = key

    def move(key):
        return renamed.get(key, key) if _old_video_key(key) else key

    state.sources = {move(key): target for key, target in state.sources.items()}
    for key, notes in old_notes.items():
        del state.notes[key]
        if key in renamed:
            new = renamed[key]
            own = {item.raw_id for item in state.playlists[new].videos}
            state.notes.setdefault(new, {}).update({raw_id: note for raw_id, note in notes.items() if raw_id in own})
    state.picks = {move(key): ids for key, ids in state.picks.items()}
    state.draft = move(state.draft)
    state.collection = move(state.collection)
    return state


def parse(text: str) -> Session:
    """Raises ValueError if the text is not a session this version can read."""
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("version") != VERSION:
            raise ValueError("unknown session format")
        playlists = [_read_playlist(item) for item in data.get("playlists", ())]
        return _migrate_video_keys(Session(
            jobs=[_read_job(item) for item in data.get("jobs", ())],
            playlists={info.key: info for info in playlists},
            sources={_key(key): _read_target(target) for key, target in data.get("sources", ())},
            notes={
                _key(key): {raw_id: (str(note), str(state)) for raw_id, (note, state) in notes.items()}
                for key, notes in data.get("notes", ())
            },
            draft=_key(data.get("draft")),
            collection=_key(data.get("collection")),
            picks={_key(key): set(ids) for key, ids in data.get("picks", ())},
        ))
    except ValueError:
        raise
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"broken session: {exc}") from exc


def load(path: Path) -> Session | None:
    """The saved session, or None if there is none.

    Raises ValueError if the file cannot be read; it is then kept aside as
    session.bad.json, so the next save does not wipe what may still be fixed by hand.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(str(exc)) from exc
    try:
        return parse(text)
    except ValueError:
        try:
            path.replace(path.with_name("session.bad.json"))
        except OSError:
            pass
        raise
