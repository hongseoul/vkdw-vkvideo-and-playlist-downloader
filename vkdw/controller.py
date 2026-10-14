"""Opening links and the download queue, without any window.

Work runs in background threads that only put events into a queue. The window
calls drain() on its own thread: drain() updates the state kept here and hands
the events on, so the window can redraw what changed.

Events returned by drain():
    ("playlist", PlaylistInfo, mode)                 a list arrived: opened from a link (open),
                                                     read again from VK (refresh), got its next
                                                     batch (more) or got videos added (add)
    ("fatal", text, kind, mode, key)                 a load failed; kind as VkError.kind
    ("load_done", mode, key)                         a load was cancelled
    ("log", LogEntry)
    ("status", text)                                 a line for the download panel
    ("queue",)                                       jobs or their statuses changed
    ("idle", text, outcome)                          the queue stopped running; outcome is
                                                     finished, queue_finished, paused, stopped or idle
    ("row", playlist_key, raw_id, note, state)       one video got a new status
    ("overall", job_id, playlist_key, position, total, VideoItem)
                                                     a video of a job starts downloading
    ("file", job_id, playlist_key, raw_id, loaded, size, speed)
                                                     bytes of the current video
    ("checked", key, PlaylistInfo | None, error)     check_lists read one list (or failed: error)
    ("check_done", read, failed)                     check_lists went through all its lists
    ("covers_saved", [Path], failed)                 save_covers wrote these images; failed did not save
    ("update", Release)                              check_updates found a newer release
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from vkdw import updates
from vkdw.archive import Archive
from vkdw.downloader import (
    EXISTS,
    FILE_ROOM,
    LOW_SPACE,
    QUALITY_CHOICES,
    download_video,
    duplicate_ids,
    fit_name,
    free_space,
    is_saved,
    name_width,
    output_path,
    playlist_file_text,
)
from vkdw.i18n import count, number, tr
from vkdw.jobs import FINISHED, PlaylistQueue, QueueJob
from vkdw.session import Session
from vkdw.settings import fmt_size, write_atomic
from vkdw.thumbs import COVER_LIMIT, fetch_image, save_image
from vkdw.vk_client import (
    Cancelled,
    PlaylistInfo,
    Target,
    VideoItem,
    VkClient,
    VkError,
    parse_targets,
    safe_filename,
    target_raw_id,
)


@dataclass(frozen=True)
class LogEntry:
    time: float
    level: str  # info, ok, warn or error
    text: str


def videos_word(amount: int) -> str:
    return count(amount, "video")


def playlists_word(amount: int) -> str:
    return count(amount, "playlist")


def target_dir(folder: Path, playlist: PlaylistInfo, subfolder: bool) -> Path:
    """Where the videos of a playlist are saved."""
    folder = Path(folder)
    if subfolder and not playlist.single:
        folder = folder / fit_name(folder, safe_filename(playlist.title) or "playlist", FILE_ROOM)
    return folder


def saved_ids(pairs, folder: Path, naming: str, total: int, tagged=None) -> set[str]:
    """Videos, given as (playlist index, video), whose file is already where a download would save it.

    tagged is as QueueJob.tagged; by default it is found from the given videos.
    The folder is listed once, so even a long list or a slow drive is checked quickly; only
    names found there get the same check the download does before it skips a file.
    """
    pairs = list(pairs)
    try:
        with os.scandir(folder) as entries:
            names = {entry.name.casefold() for entry in entries}
    except OSError:
        return set()
    if not names:
        return set()
    width = name_width(total)
    if tagged is None:
        tagged = duplicate_ids(pairs, width, naming)
    found = set()
    for index, item in pairs:
        path = output_path(folder, item, index, width, naming, tagged)
        try:
            if path.name.casefold() in names and is_saved(path):
                found.add(item.raw_id)
        except OSError:
            pass
    return found


def _short_id(raw_id: str) -> str:
    """owner_video without the access key, to match the same video given two ways."""
    return "_".join(raw_id.split("_")[:2])


def _album_cover_url(client: VkClient, playlist: PlaylistInfo) -> str:
    """A playlist's own full-size cover; without one, its first video's, as VK shows it then."""
    try:
        url = client.album_cover(playlist.owner_id, playlist.album_id)
        first = next((item for item in playlist.videos if item.thumb_url), None)
        if not url and first is not None:
            url = client.video_covers([first.raw_id]).get(_short_id(first.raw_id), "")
    except VkError:
        url = ""
    return url or playlist.cover_url


# Pause between the lists check_lists reads, so many bookmarks do not run into VK's rate limit.
CHECK_PAUSE = 0.5


class DownloadController:
    def __init__(self, archive: Archive | None = None) -> None:
        # Where downloaded videos were saved; in memory only unless the window gives a file.
        self.archive = archive or Archive()
        self._events: queue.Queue = queue.Queue()
        self._load_cancel = threading.Event()
        self._check_cancel = threading.Event()
        self.checking = False
        self._download_cancel = threading.Event()
        self._lock = threading.Lock()
        self._queue = PlaylistQueue()
        self._job_seq = 0
        self._collection_seq = 0
        self._worker_gen = 0
        self._worker_active = False
        self._last_outcome = ""
        # The fields below change only on the window's thread, inside drain()
        # or the methods the window calls.
        self.loading = False
        self.load_mode = ""
        self.load_key: tuple | None = None
        self.downloading = False
        self.playlists: dict[tuple, PlaylistInfo] = {}
        # Where each opened list came from, to read it again.
        self.sources: dict[tuple, Target] = {}
        self.notes: dict[tuple, dict[str, tuple[str, str]]] = {}
        self.log: list[LogEntry] = []
        self.last_summary = ""
        self.status_text = tr("status.ready")
        self.active_job_id = ""
        self.job_pos = 0
        self.job_total = 0

    # Opening links

    def open_link(self, url: str) -> None:
        """Start opening a link; several video links become a collection.
        Raises VkError at once if the text holds no VK link."""
        if self.loading:
            return
        targets = parse_targets(url)
        videos = [target for target in targets if target.kind == "video"]
        if len(videos) >= 2:
            self.open_videos(videos)
        else:
            self.open_target(targets[0])

    def open_target(self, target: Target) -> None:
        def work(client: VkClient, cancel):
            # A short link is read once; the list is kept by where it led.
            resolved = client.resolve_link(target) if target.kind == "short" else target
            info = client.open_target(resolved, cancel)
            source = Target("owner", info.owner_id, 0) if info.kind == "owner" else resolved
            return info, source

        self._start("open", None, work)

    def open_videos(self, targets: list[Target], key: tuple | None = None) -> None:
        """Read video links into a collection: the open one named by key, or a new one."""
        raw_ids = [target_raw_id(target) for target in targets]

        def work(client: VkClient, cancel):
            items = client.open_videos(raw_ids, cancel)
            if not items:
                raise VkError(0, tr("err.videos_not_found"), "access")
            return (items, len(raw_ids)), None

        self._start("add", key, work)

    def refresh(self, key: tuple) -> bool:
        """Read an opened list from VK again. Returns False if there is nothing to read it from."""
        info = self.playlists.get(key)
        if info is None or self.loading:
            return False
        if info.kind == "collection":
            raw_ids = [item.raw_id for item in info.videos]

            def work(client: VkClient, cancel):
                fresh = {_short_id(item.raw_id): item for item in client.open_videos(raw_ids, cancel)}
                gone = tr("row.gone")
                videos = tuple(fresh.get(_short_id(item.raw_id)) or replace(item, restriction=gone) for item in info.videos)
                return replace(info, videos=videos), None

            self._start("refresh", key, work)
            return True
        target = self.sources.get(key)
        if target is None:
            return False
        limit = len(info.videos)

        def work(client: VkClient, cancel):
            return client.open_target(target, cancel, limit=limit), target

        self._start("refresh", key, work)
        return True

    def load_more(self, key: tuple) -> bool:
        """Read the next batch of an owner page's videos."""
        info = self.playlists.get(key)
        if info is None or not info.has_more or self.loading:
            return False
        self._start("more", key, lambda client, cancel: (client.open_more(info, cancel), None))
        return True

    def cancel_load(self) -> None:
        if self.loading:
            self._load_cancel.set()

    def _start(self, mode: str, key: tuple | None, work) -> None:
        if self.loading:
            return
        self.loading = True
        self.load_mode = mode
        self.load_key = key
        self._load_cancel.clear()
        if self.downloading and mode == "open":
            self._log(tr("log.opening_while_downloading"))
        threading.Thread(target=self._load_worker, args=(mode, key, work), daemon=True).start()

    def _load_worker(self, mode: str, key, work) -> None:
        client = VkClient()
        try:
            payload, source = work(client, self._load_cancel.is_set)
        except Cancelled:
            self._events.put(("load_done", mode, key))
            return
        except VkError as exc:
            self._events.put(("fatal", str(exc), exc.kind, mode, key))
            return
        except Exception as exc:
            self._events.put(("fatal", tr("err.open_failed", error=exc), "other", mode, key))
            return
        self._events.put(("loaded", mode, key, payload, source))

    def _end_load(self) -> None:
        self.loading = False
        self.load_mode = ""
        self.load_key = None

    # Checking lists for new videos

    def check_lists(self, targets: list[Target]) -> bool:
        """Read lists from VK one after another in the background, beside any other load:
        an owner page only its first batch, the newest videos. Returns False if a check runs."""
        if self.checking or not targets:
            return False
        self.checking = True
        self._check_cancel.clear()
        threading.Thread(target=self._check_worker, args=(list(targets),), daemon=True).start()
        return True

    def cancel_check(self) -> None:
        self._check_cancel.set()

    def _check_worker(self, targets: list[Target]) -> None:
        client = VkClient()
        read = failed = 0
        for position, target in enumerate(targets):
            if self._check_cancel.is_set():
                break
            if position:
                time.sleep(CHECK_PAUSE)
            key = (target.owner_id, "all") if target.kind == "owner" else (target.owner_id, target.item_id)
            try:
                info = client.open_target(target, self._check_cancel.is_set)
            except Cancelled:
                break
            except Exception as exc:  # one list that fails does not stop the others
                failed += 1
                self._events.put(("checked", key, None, str(exc), target))
                continue
            read += 1
            self._events.put(("checked", key, info, "", target))
        self._events.put(("check_done", read, failed))

    # Covers

    def save_covers(self, videos: list[tuple[VideoItem, Path]], album: tuple[PlaylistInfo, Path] | None = None) -> None:
        """Save full-size previews in the background: of each given video and, with album, the
        playlist's own cover. A dest is the file to write; its extension becomes the image's.
        Each image is logged, and ("covers_saved", paths, failed) comes at the end."""
        threading.Thread(target=self._covers_worker, args=(list(videos), album), daemon=True).start()

    def _covers_worker(self, videos: list[tuple[VideoItem, Path]], album) -> None:
        client = VkClient()
        try:
            urls = client.video_covers([item.raw_id for item, _dest in videos]) if videos else {}
        except Exception:
            urls = {}  # each video falls back to the preview its row already shows
        wanted = [(item.title, urls.get(_short_id(item.raw_id)) or item.thumb_url, dest) for item, dest in videos]
        if album is not None:
            playlist, dest = album
            wanted.insert(0, (playlist.title, _album_cover_url(client, playlist), dest))
        saved: list[Path] = []
        failed = 0
        for title, url, dest in wanted:
            try:
                if not url:
                    raise VkError(0, tr("err.no_cover"))
                path = save_image(fetch_image(url, client.ssl_context, COVER_LIMIT), dest)
            except Exception as exc:  # one image that fails does not stop the others
                failed += 1
                self._log(tr("log.cover_failed", title=title, error=exc), "error")
                continue
            saved.append(path)
            self._log(tr("log.cover_saved", path=path), "ok")
        self._events.put(("covers_saved", saved, failed))

    # A newer version

    def check_updates(self) -> None:
        """Ask GitHub in the background whether a newer release is out; ("update", Release) if so."""
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self) -> None:
        release = updates.check()
        if release is not None:
            self._events.put(("update", release))

    def adopt(self, info: PlaylistInfo, source: Target) -> PlaylistInfo:
        """Keep a list read by check_lists as the opened one of its key. An owner page that has
        more of its videos read already keeps them, after the fresh first batch."""
        old = self.playlists.get(info.key)
        if old is not None and info.kind == "owner" and len(old.videos) > len(info.videos):
            have = {_short_id(item.raw_id) for item in info.videos}
            rest = tuple(item for item in old.videos if _short_id(item.raw_id) not in have)
            info = replace(info, videos=info.videos + rest, total=max(info.total, old.total))
        self.playlists[info.key] = info
        self.sources[info.key] = source
        return info

    def new_videos(self, playlist: PlaylistInfo, folder: Path, subfolder: bool) -> list[tuple[int, VideoItem]]:
        """(index, video) of a list that are not saved yet: not blocked by VK, not in the folder
        a download would use now, and not found by the archive somewhere else."""
        pairs = list(enumerate(playlist.videos, start=1))
        saved = saved_ids(pairs, target_dir(folder, playlist, subfolder), playlist.naming, len(pairs))
        open_ = [(index, item) for index, item in pairs if not item.restriction and item.raw_id not in saved]
        elsewhere = self.archive.found((item for _index, item in open_))
        return [(index, item) for index, item in open_ if item.raw_id not in elsewhere]

    # Collections

    def add_to_collection(self, key: tuple | None, items) -> tuple[PlaylistInfo, int]:
        """Add videos to an open collection, or to a new one when key is None.
        Returns the collection and how many of the videos were not in it yet."""
        info = self.playlists.get(key) if key is not None else None
        if info is None or info.kind != "collection":
            info = self._new_collection()
        have = {_short_id(item.raw_id) for item in info.videos}
        fresh = []
        for item in items:
            short = _short_id(item.raw_id)
            if short not in have:
                have.add(short)
                fresh.append(item)
        if fresh:
            videos = info.videos + tuple(fresh)
            cover = info.cover_url or next((item.thumb_url for item in videos if item.thumb_url), "")
            info = replace(info, videos=videos, cover_url=cover)
        self.playlists[info.key] = info
        return info, len(fresh)

    def remove_from_collection(self, key: tuple, raw_ids) -> PlaylistInfo | None:
        info = self.playlists.get(key)
        if info is None or info.kind != "collection":
            return None
        drop = set(raw_ids)
        info = replace(info, videos=tuple(item for item in info.videos if item.raw_id not in drop))
        self.playlists[key] = info
        return info

    def _new_collection(self) -> PlaylistInfo:
        self._collection_seq += 1
        title = tr("collection.title", date=time.strftime(tr("collection.date_format")))
        taken = {info.title for info in self.playlists.values() if info.kind == "collection"}
        base, extra = title, 2
        while title in taken:
            title = f"{base} ({extra})"
            extra += 1
        return PlaylistInfo(0, None, title, (), kind="collection", collection_id=self._collection_seq)

    # Queue

    def make_job(
        self,
        playlist: PlaylistInfo,
        items: list[tuple[int, VideoItem]],
        folder: Path,
        quality: str,
        subfolder: bool,
    ) -> QueueJob:
        """A queue entry for the chosen (index, video) pairs of a playlist."""
        items = sorted(items, key=lambda pair: pair[0])
        where = target_dir(folder, playlist, subfolder)
        everything = list(enumerate(playlist.videos, start=1))
        tagged = duplicate_ids(everything, name_width(len(playlist.videos)), playlist.naming)
        with self._lock:
            self._job_seq += 1
            job_id = f"job-{self._job_seq}"
        return QueueJob(
            job_id=job_id,
            title=playlist.title,
            video_count=len(items),
            quality=quality,
            folder_label=str(where),
            naming=playlist.naming,
            video_total=len(playlist.videos),
            items=tuple(items),
            target_dir=where,
            max_height=dict(QUALITY_CHOICES).get(quality),
            playlist_key=playlist.key,
            cover_url=playlist.cover_url,
            tagged=tagged,
        )

    def retry_failed(self, job_id: str) -> QueueJob | None:
        """A new job with only the videos of a finished job that did not download."""
        with self._lock:
            job = next((job for job in self._queue.jobs if job.job_id == job_id), None)
            if job is None or job.status not in FINISHED:
                return None
            failed = {raw_id for raw_id, _text, _kind in job.failures}
            items = tuple(pair for pair in job.items if pair[1].raw_id in failed)
            if not items:
                return None
            self._job_seq += 1
            return replace(
                job,
                job_id=f"job-{self._job_seq}",
                status="waiting",
                summary="",
                items=items,
                video_count=len(items),
                failures=(),
                outcome="",
            )

    def enqueue(self, job: QueueJob, *, start: bool) -> tuple[str, str]:
        """Put a job in the queue. Returns where it went (now, next, tail) and a message."""
        with self._lock:
            running = self._worker_active
            paused = self._queue.paused
            if start and running:
                self._queue.insert_next(job)
                placed = "next"
            elif start:
                self._queue.paused = False
                self._queue.insert_first(job)
                placed = "now"
            else:
                self._queue.add(job)
                placed = "tail"
            should_start = placed == "now" or (placed == "tail" and not running and not paused)
        count_text = videos_word(job.video_count)
        if placed == "next":
            message = tr("queue.placed_next", title=job.title, count=count_text)
        elif placed == "tail" and running:
            message = tr("queue.placed_after", title=job.title, count=count_text)
        elif placed == "tail" and paused:
            message = tr("queue.placed_paused", title=job.title, count=count_text)
        else:
            message = tr("queue.placed", title=job.title, count=count_text)
        self._log(message)
        if should_start:
            self._ensure_worker()
        return placed, message

    def resume(self) -> None:
        with self._lock:
            self._queue.paused = False
        self._download_cancel.clear()
        self._log(tr("log.resume"))
        self._ensure_worker()

    def stop(self) -> None:
        """Interrupt the current playlist and pause the queue."""
        if not self.downloading:
            return
        with self._lock:
            self._queue.paused = True
        self._download_cancel.set()
        self.status_text = tr("status.stopping")

    def remove(self, job_ids) -> int:
        """Drop jobs in any state. A running one is stopped, and the queue goes on with the next."""
        removed = 0
        running = False
        with self._lock:
            for job_id in job_ids:
                job = self._queue.take(job_id)
                if job is None:
                    continue
                removed += 1
                running = running or job.status == "running"
            if running:
                # Set under the lock, so the worker sees the stop and the missing job together
                # and moves on without pausing the queue.
                self._download_cancel.set()
            elif not self._queue.pending_count():
                # Nothing is left to resume, so a pause would only hold back the next playlist.
                self._queue.paused = False
        if removed:
            self._log(tr("log.removed", count=playlists_word(removed)))
        return removed

    def clear(self) -> int:
        """Empty the whole queue, stopping the current download."""
        return self.remove([job.job_id for job in self.jobs()])

    def clear_finished(self) -> int:
        with self._lock:
            cleared = self._queue.clear_finished()
        if cleared:
            self._log(tr("log.cleared", count=playlists_word(cleared)))
        return cleared

    def pending_count(self) -> int:
        with self._lock:
            return self._queue.pending_count()

    def jobs(self) -> list[QueueJob]:
        """Copies of the queued jobs, safe to read while workers run."""
        with self._lock:
            return [replace(job) for job in self._queue.jobs]

    def job(self, job_id: str) -> QueueJob | None:
        if not job_id:
            return None
        with self._lock:
            for job in self._queue.jobs:
                if job.job_id == job_id:
                    return replace(job)
        return None

    def position(self, job_id: str) -> tuple[int, int]:
        with self._lock:
            return self._queue.position(job_id)

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._queue.paused

    def can_resume(self) -> bool:
        with self._lock:
            return self._queue.paused and not self.downloading and self._queue.pending_count() > 0

    def has_unfinished(self) -> bool:
        with self._lock:
            return self._queue.pending_count() + sum(1 for job in self._queue.jobs if job.status == "running") > 0

    def close(self) -> None:
        self._load_cancel.set()
        self._check_cancel.set()
        self._download_cancel.set()
        with self._lock:
            self._queue.paused = True

    def note(self, text: str, level: str = "info") -> None:
        """A line for the log from the window."""
        self._log(text, level)

    # Keeping the queue between runs

    def session(self, keys=()) -> Session:
        """The queue, and the lists of its jobs and of the given keys, to save to disk."""
        jobs = self.jobs()
        keep = {job.playlist_key for job in jobs} | {key for key in keys if key is not None}
        keep &= set(self.playlists)
        return Session(
            jobs=jobs,
            playlists={key: self.playlists[key] for key in keep},
            sources={key: self.sources[key] for key in keep if key in self.sources},
            notes={key: dict(self.notes[key]) for key in keep if key in self.notes},
        )

    def restore(self, state: Session) -> int:
        """Put back a saved queue before anything else is opened. It stays paused until resumed;
        returns how many of its playlists are left to download."""
        jobs = [job for job in state.jobs if job.playlist_key in state.playlists]
        self.playlists.update(state.playlists)
        self.sources.update(state.sources)
        self.notes.update({key: dict(notes) for key, notes in state.notes.items()})
        collections = [key[1] for key in state.playlists if key[0] == "collection" and isinstance(key[1], int)]
        self._collection_seq = max([self._collection_seq, *collections])
        with self._lock:
            self._queue.jobs = jobs + self._queue.jobs
            numbers = [int(job.job_id[4:]) for job in jobs if job.job_id.startswith("job-") and job.job_id[4:].isdigit()]
            self._job_seq = max([self._job_seq, *numbers])
            pending = self._queue.pending_count()
            if pending:
                self._queue.paused = True
        if pending:
            self._log(tr("log.restored", count=playlists_word(pending)))
        return pending

    # Background download

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker_active or self._queue.paused or self._queue.next_pending() is None:
                return
            self._worker_active = True
            self._worker_gen += 1
            generation = self._worker_gen
        self._download_cancel.clear()
        self.downloading = True
        threading.Thread(target=self._queue_worker, args=(generation,), daemon=True).start()

    def _queue_worker(self, generation: int) -> None:
        # The next playlist starts in this same thread, as soon as the previous
        # one has finished, without waiting for the window to catch up.
        ran = 0
        try:
            while True:
                with self._lock:
                    if self._queue.paused or self._download_cancel.is_set():
                        break
                    job = self._queue.next_pending()
                    if job is None:
                        break
                    job.status = "running"
                    job.failures = ()
                    position, total = self._queue.position(job.job_id)
                self._events.put(("queue",))
                place = tr("log.place", position=position, total=total) if total > 1 else ""
                self._events.put(("log", tr("log.job_start", title=job.title, place=place, count=videos_word(job.video_count)), "info"))
                hard = False
                try:
                    summary, stopped, failed = self._execute(job)
                except Exception as exc:
                    summary = tr("summary.crashed", title=job.title, error=exc)
                    stopped = False
                    failed = 1
                    hard = True
                follow_title = None
                with self._lock:
                    dropped = not self._queue.holds(job)
                    if dropped:
                        # Removed from the queue while it was downloading: that was the
                        # stop, so the queue itself keeps going.
                        self._download_cancel.clear()
                if dropped:
                    self._events.put(("log", tr("log.cancelled", title=job.title), "warn"))
                    self._events.put(("queue",))
                    continue
                with self._lock:
                    job.summary = summary
                    if hard:
                        job.status = "error"
                        job.outcome = "error"
                        self._queue.paused = True
                    elif stopped:
                        job.status = "interrupted"
                        job.outcome = "stopped"
                        self._queue.paused = True
                    elif self._download_cancel.is_set() or self._queue.paused:
                        job.status = "done"
                        job.outcome = "ok"
                        self._queue.paused = True
                    else:
                        job.status = "done"
                        job.outcome = "ok"
                        nxt = self._queue.next_pending()
                        follow_title = nxt.title if nxt is not None else None
                    self._last_outcome = job.outcome
                ran += 1
                if hard:
                    level = "error"
                elif stopped or failed:
                    level = "warn"
                else:
                    level = "ok"
                self._events.put(("job_summary", summary))
                self._events.put(("log", summary, level))
                self._events.put(("queue",))
                if follow_title:
                    self._events.put(("status", tr("status.next", title=job.title, next=follow_title)))
                    self._events.put(("log", tr("log.next", title=job.title, next=follow_title), "info"))
                    continue
                break
        finally:
            with self._lock:
                if self._worker_gen == generation:
                    self._worker_active = False
            self._events.put(("worker_idle", generation, ran))

    def _execute(self, job: QueueJob) -> tuple[str, bool, int]:
        client = VkClient()
        width = name_width(job.video_total or job.video_count)
        done = skipped = failed = 0
        hinted = False
        total = len(job.items)
        stopped = False
        target_dir = job.target_dir or Path()
        key = job.playlist_key
        free = free_space(target_dir)
        if free is not None and free < LOW_SPACE:
            self._events.put(("log", tr("log.low_space", free=fmt_size(free), folder=target_dir), "warn"))
        for position, (index, item) in enumerate(job.items, start=1):
            if self._download_cancel.is_set():
                stopped = True
                break
            self._events.put(("overall", job.job_id, key, position, total, item))
            dest = output_path(target_dir, item, index, width, job.naming, job.tagged)
            self._events.put(("row", key, item.raw_id, "", "active"))

            def on_progress(loaded, size, speed, raw_id=item.raw_id):
                self._events.put(("file", job.job_id, key, raw_id, loaded, size, speed))

            try:
                label = download_video(client, item, dest, job.max_height, self._download_cancel, on_progress)
            except Cancelled:
                stopped = True
                self._events.put(("row", key, item.raw_id, "", "stopped"))
                break
            except VkError as exc:
                if exc.kind == "disk":
                    # The next videos would not fit either: pause, so ▶ goes on once there is room.
                    stopped = True
                    self._events.put(("log", tr("log.no_space", title=item.title, error=exc), "error"))
                    self._events.put(("row", key, item.raw_id, str(exc)[:200], "stopped"))
                    break
                failed += 1
                self._fail(job, item, str(exc), exc.kind)
                self._events.put(("log", tr("log.not_downloaded", title=item.title, error=exc), "error"))
                self._events.put(("row", key, item.raw_id, str(exc)[:200], "failed"))
                if exc.kind == "ffmpeg" and not hinted:
                    hinted = True
                    self._events.put(("log", tr("hint.ffmpeg"), "warn"))
                continue
            except Exception as exc:
                failed += 1
                self._fail(job, item, str(exc), "other")
                self._events.put(("log", tr("log.video_error", title=item.title, error=exc), "error"))
                self._events.put(("row", key, item.raw_id, str(exc)[:200], "failed"))
                continue
            # Only now the file is there under its final name.
            self.archive.add(item.raw_id, dest)
            if label == EXISTS:
                skipped += 1
                self._events.put(("log", tr("log.exists", name=dest.name), "info"))
            else:
                done += 1
                self._events.put(("log", tr("log.saved", label=label, name=dest.name), "ok"))
            self._events.put(("row", key, item.raw_id, label, "done"))
        if job.naming == "number" and done:
            self._write_playlist_file(job, width)
        word = "summary.stopped" if stopped else "summary.done"
        summary = tr(word, title=job.title, done=done, skipped=skipped, failed=failed)
        return summary, stopped, failed

    def _write_playlist_file(self, job: QueueJob, width: int) -> None:
        """«Title.m3u8» beside the files of a playlist: every video of it saved there so far,
        in playlist order, so a player shows the series in its order. Written again after
        each job, so videos added later join it."""
        target_dir = job.target_dir or Path()
        playlist = self.playlists.get(job.playlist_key)
        pairs = list(enumerate(playlist.videos, start=1)) if playlist is not None else list(job.items)
        entries = []
        for index, item in pairs:
            path = output_path(target_dir, item, index, width, job.naming, job.tagged)
            try:
                if is_saved(path):
                    entries.append((path, item.title, item.duration))
            except OSError:
                continue
        if not entries:
            return
        name = fit_name(target_dir, safe_filename(job.title) or "playlist", len(".m3u8.tmp")) + ".m3u8"
        if not write_atomic(target_dir / name, playlist_file_text(entries)):
            self._events.put(("log", tr("log.m3u_failed", name=name), "warn"))

    def _fail(self, job: QueueJob, item: VideoItem, text: str, kind: str) -> None:
        with self._lock:
            job.failures = job.failures + ((item.raw_id, text, kind),)

    # Events

    def drain(self) -> list[tuple]:
        """Apply what the background threads reported. Call on the window's thread."""
        out: list[tuple] = []
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            event = self._handle(event)
            if event is None:
                continue
            if event[0] == "file" and out and out[-1][0] == "file":
                # Only the latest byte count matters for drawing.
                out[-1] = event
            else:
                out.append(event)
        return out

    def _log(self, text: str, level: str = "info") -> None:
        self._events.put(("log", text, level))

    def _handle(self, event: tuple) -> tuple | None:
        kind = event[0]
        if kind == "log":
            entry = LogEntry(time.time(), event[2], event[1])
            self.log.append(entry)
            return ("log", entry)
        if kind == "loaded":
            return self._on_loaded(*event[1:])
        if kind == "fatal":
            self._end_load()
            self._log(event[1], "error")
            return event
        if kind == "load_done":
            self._end_load()
            return event
        if kind == "job_summary":
            self.last_summary = event[1]
            return None
        if kind == "status":
            self.status_text = event[1]
            return event
        if kind == "row":
            _kind, key, raw_id, note, state = event
            self.notes.setdefault(key, {})[raw_id] = (note, state)
            return event
        if kind == "overall":
            self.active_job_id = event[1]
            self.job_pos, self.job_total = event[3], event[4]
            return event
        if kind == "worker_idle":
            return self._on_worker_idle(event[1], event[2])
        if kind == "checked":
            _kind, key, info, error, source = event
            if info is not None:
                info = self.adopt(info, source)
            return ("checked", key, info, error)
        if kind == "check_done":
            self.checking = False
            return event
        return event

    def _on_loaded(self, mode: str, key, payload, source) -> tuple:
        self._end_load()
        if mode == "add":
            items, asked = payload
            info, added = self.add_to_collection(key, items)
            self._log(tr("log.collection_added", count=videos_word(added), title=info.title))
            missing = asked - len(items)
            if missing > 0:
                self._log(tr("log.links_failed", count=count(missing, "link")), "warn")
            return ("playlist", info, mode)
        info: PlaylistInfo = payload
        self.playlists[info.key] = info
        if source is not None:
            self.sources[info.key] = source
        blocked = sum(1 for item in info.videos if item.restriction)
        if mode == "open":
            self._log(tr("log.opened", title=info.title, count=len(info.videos)))
            if blocked and blocked < len(info.videos) and info.kind != "owner":
                self._log(tr("log.restricted_unticked"), "warn")
        elif mode == "refresh":
            self._log(tr("log.refreshed", title=info.title, count=len(info.videos), blocked=blocked))
        elif mode == "more":
            self._log(tr("log.more", shown=number(len(info.videos)), total=number(info.total)))
        return ("playlist", info, mode)

    def _on_worker_idle(self, generation: int, ran: int) -> tuple | None:
        with self._lock:
            if generation != self._worker_gen or self._worker_active:
                return None
            pending = self._queue.pending_count()
            paused = self._queue.paused
            last = self._last_outcome
        self.downloading = False
        self.active_job_id = ""
        if paused and pending:
            text = tr("idle.paused", left=playlists_word(pending))
            outcome = "paused"
        elif paused:
            text = self.last_summary or tr("idle.stopped")
            outcome = "finished" if last == "ok" else "stopped"
        elif ran > 1:
            text = tr("idle.queue_finished")
            outcome = "queue_finished"
        elif ran == 1:
            text = self.last_summary or tr("idle.done")
            outcome = "finished" if last == "ok" else "stopped"
        else:
            text = tr("status.ready")
            outcome = "idle"
        self.status_text = text
        return ("idle", text, outcome)
