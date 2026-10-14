"""Playlist download queue.

A job starts only after the previous one has finished unloading. Stopping the
current job pauses the queue: the interrupted playlist is retried on resume,
and playlists that are still waiting stay behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vkdw.i18n import tr

PENDING = ("waiting", "interrupted")
FINISHED = ("done", "error")


def status_label(status: str) -> str:
    return tr(f"job.{status}")


@dataclass
class QueueJob:
    job_id: str
    title: str
    video_count: int = 0
    quality: str = ""
    folder_label: str = ""
    status: str = "waiting"
    summary: str = ""
    naming: str = "number"
    video_total: int = 0
    items: tuple = ()
    target_dir: Path | None = None
    max_height: int | None = None
    playlist_key: tuple = ()
    cover_url: str = ""
    # Videos (owner_video) whose file names get their id, as found by duplicate_ids when the
    # job was made: the whole playlist is compared, not only the videos chosen for the job.
    tagged: frozenset = frozenset()
    # Videos that did not download: (raw_id, message, kind of VkError).
    failures: tuple = ()
    # How the last run ended: ok, stopped or error.
    outcome: str = ""


class PlaylistQueue:
    def __init__(self) -> None:
        self.jobs: list[QueueJob] = []
        self.paused = False

    def add(self, job: QueueJob) -> None:
        self.jobs.append(job)

    def insert_first(self, job: QueueJob) -> None:
        """Put the job ahead of everything that has not finished."""
        for index, existing in enumerate(self.jobs):
            if existing.status in PENDING:
                self.jobs.insert(index, job)
                return
        self.jobs.append(job)

    def insert_next(self, job: QueueJob) -> None:
        """Run the job immediately after the one that is downloading now."""
        for index, existing in enumerate(self.jobs):
            if existing.status == "running":
                self.jobs.insert(index + 1, job)
                return
        self.insert_first(job)

    def next_pending(self) -> QueueJob | None:
        if self.paused:
            return None
        for job in self.jobs:
            if job.status in PENDING:
                return job
        return None

    def pending_count(self) -> int:
        return sum(1 for job in self.jobs if job.status in PENDING)

    def position(self, job_id: str) -> tuple[int, int]:
        for index, job in enumerate(self.jobs, start=1):
            if job.job_id == job_id:
                return index, len(self.jobs)
        return 0, len(self.jobs)

    def take(self, job_id: str) -> QueueJob | None:
        """Drop a job whatever its state and return it, or None if it is not here."""
        for index, job in enumerate(self.jobs):
            if job.job_id == job_id:
                return self.jobs.pop(index)
        return None

    def holds(self, job: QueueJob) -> bool:
        return any(existing is job for existing in self.jobs)

    def clear_finished(self) -> int:
        before = len(self.jobs)
        self.jobs = [job for job in self.jobs if job.status not in FINISHED]
        return before - len(self.jobs)
