"""Download progress reconstructed from the CLI log output."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

TOTAL_MARK = "[TOTAL] "
OUTPUT_MARK = "[OUTPUT] "
FOUND_MARK = "[FOUND] "
NO_MATCH_MARK = "[NO MATCH] "
DONE_MARK = "[DONE] "

_LINE_BREAK = re.compile(r"[\r\n]")

TERMINAL_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled")


@dataclass(frozen=True)
class Progress:
    """Snapshot of a job's progress, ready for rendering."""

    label: str
    detail: str
    percent: int
    indeterminate: bool = False


class ProgressTracker:
    """Incrementally counts progress markers in CLI log output.

    The tracker accepts the log in arbitrary chunks (as read by the SSE
    stream), keeping the trailing partial line buffered so markers split
    across chunks are still counted exactly once.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self.total = 0
        self.matched = 0
        self.no_match = 0
        self.downloaded = 0
        self.output_dir: str | None = None

    def feed(self, text: str) -> None:
        if not text:
            return
        parts = _LINE_BREAK.split(self._buffer + text)
        self._buffer = parts.pop()
        for line in parts:
            self._consume(line)

    def _consume(self, line: str) -> None:
        if TOTAL_MARK in line:
            _, _, value = line.rpartition(TOTAL_MARK)
            try:
                self.total = int(value.split()[0])
            except (IndexError, ValueError):
                pass
        if OUTPUT_MARK in line:
            _, _, value = line.rpartition(OUTPUT_MARK)
            self.output_dir = value.strip() or None
        self.matched += line.count(FOUND_MARK)
        self.no_match += line.count(NO_MATCH_MARK)
        self.downloaded += line.count(DONE_MARK)

    @property
    def processed(self) -> int:
        return self.matched + self.no_match

    def percent(self) -> int:
        """Overall completion, counting matching and downloading equally."""

        denominator = self.total + self.matched
        if denominator <= 0:
            return 0
        done = self.processed + self.downloaded
        percent = 100 * done / denominator
        return min(99, int(round(percent)))

    def snapshot(self, status: str) -> Progress:
        if status == "queued":
            return Progress("Queued", "Waiting for a free worker", 0)

        if status == "completed":
            return Progress("Completed", self._summary(), 100)

        if status in ("failed", "cancelled"):
            return Progress(
                status.capitalize(), self._summary(), self.percent()
            )

        # running / cancelling
        if self.total == 0:
            return Progress(
                "Fetching playlist",
                "Reading tracks from Spotify",
                0,
                indeterminate=True,
            )
        if self.processed < self.total:
            return Progress(
                "Matching tracks",
                f"{self.processed} of {self.total} tracks matched",
                self.percent(),
            )
        if self.matched and self.downloaded < self.matched:
            return Progress(
                "Downloading",
                f"{self.downloaded} of {self.matched} downloads finished",
                self.percent(),
            )
        return Progress(
            "Finishing up",
            "Writing metadata and artwork",
            self.percent(),
        )

    def _summary(self) -> str:
        parts: list[str] = []
        if self.total:
            parts.append(f"{self.total} tracks")
        if self.matched:
            parts.append(f"{self.downloaded} downloaded")
        if self.no_match:
            parts.append(f"{self.no_match} not found")
        return " · ".join(parts) if parts else "No track data recorded"


def progress_from_counts(
    status: str,
    *,
    total: int,
    matched: int,
    no_match: int,
    downloaded: int,
) -> Progress:
    """Build progress from the counters persisted in the database."""

    tracker = ProgressTracker()
    tracker.total = total
    tracker.matched = matched
    tracker.no_match = no_match
    tracker.downloaded = downloaded
    return tracker.snapshot(status)


def scan_log(path: Path, status: str) -> Progress:
    """Replay a log file to compute progress for a job."""

    tracker = ProgressTracker()
    try:
        tracker.feed(path.read_text(encoding="utf-8", errors="replace"))
    except (FileNotFoundError, OSError):
        pass
    return tracker.snapshot(status)
