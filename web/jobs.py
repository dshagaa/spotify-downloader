"""Background job queue that shells out to the spotify-dl CLI."""

from __future__ import annotations

import os
import queue
import secrets
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .config import Settings
from .db import JobStore, utcnow
from .progress import ProgressTracker

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI_ENTRYPOINT = PROJECT_ROOT / "spotify-dl.py"


class JobManager:
    """Runs one download at a time and records its output to a log file."""

    def __init__(self, settings: Settings, store: JobStore) -> None:
        self.settings = settings
        self.store = store
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._process: subprocess.Popen[bytes] | None = None
        self._process_lock = threading.Lock()
        self._cancelled: set[str] = set()
        self._cancel_lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="download-worker",
            daemon=True,
        )

    def start(self) -> None:
        interrupted = self.store.mark_interrupted()
        if interrupted:
            print(f"[web] marked {interrupted} job(s) as interrupted")
        self._worker.start()

    def shutdown(self) -> None:
        """Stop an in-flight download when the server shuts down."""

        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            _terminate_tree(process)

    def submit(
        self,
        *,
        playlist_url: str,
        playlist_label: str,
        audio_format: str,
        title_first: bool,
        max_concurrent: int,
        download_archive: bool,
        subfolder: str,
    ) -> dict[str, Any]:
        job_id = secrets.token_hex(4)
        output_dir = self.settings.download_dir
        if subfolder:
            output_dir = output_dir / subfolder
        output_dir.mkdir(parents=True, exist_ok=True)

        self.store.create(
            id=job_id,
            playlist_url=playlist_url,
            playlist_label=playlist_label,
            audio_format=audio_format,
            title_first=int(title_first),
            max_concurrent=max_concurrent,
            download_archive=int(download_archive),
            subfolder=subfolder,
            output_dir=str(output_dir),
            status="queued",
            created_at=utcnow(),
        )
        self._queue.put(job_id)
        job = self.store.get(job_id)
        assert job is not None
        return job

    def cancel(self, job_id: str) -> bool:
        job = self.store.get(job_id)
        if job is None:
            return False

        if job["status"] == "queued":
            with self._cancel_lock:
                self._cancelled.add(job_id)
            self.store.update(
                job_id,
                status="cancelled",
                finished_at=utcnow(),
                error="Cancelled before it started",
            )
            return True

        if job["status"] != "running":
            return False

        with self._cancel_lock:
            self._cancelled.add(job_id)
        self.store.update(job_id, status="cancelling")

        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            _terminate_tree(process)
        return True

    def generate_playlist(self, job_id: str) -> bool:
        """Write the M3U8 playlist for a finished job from files on disk."""

        job = self.store.get(job_id)
        if job is None or job["status"] != "completed":
            return False

        command = [
            sys.executable,
            str(CLI_ENTRYPOINT),
            job["playlist_url"],
            "-o",
            job["output_dir"],
            "--flat",
            "--playlist-only",
        ]
        environment = dict(
            os.environ,
            PYTHONUNBUFFERED="1",
            PYTHONIOENCODING="utf-8",
        )
        try:
            result = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=120,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _is_cancelled(self, job_id: str) -> bool:
        with self._cancel_lock:
            return job_id in self._cancelled

    def _forget(self, job_id: str) -> None:
        with self._cancel_lock:
            self._cancelled.discard(job_id)

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._run(job_id)
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[web] job {job_id} crashed: {exc!r}")
                try:
                    self.store.update(
                        job_id,
                        status="failed",
                        finished_at=utcnow(),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                except Exception as db_exc:  # keep the worker alive
                    print(f"[web] could not record failure: {db_exc!r}")
                self._forget(job_id)
            finally:
                self._queue.task_done()

    def _run(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None or job["status"] != "queued":
            # Cancelled before it reached the front of the queue.
            self._forget(job_id)
            return

        log_path = self.settings.log_dir / f"{job_id}.log"
        command = self._build_command(job)
        environment = dict(
            os.environ,
            PYTHONUNBUFFERED="1",
            PYTHONIOENCODING="utf-8",
        )

        self.store.update(job_id, status="running", started_at=utcnow())

        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            options: dict[str, Any] = {}
            if os.name == "nt":
                options["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                options["start_new_session"] = True

            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=environment,
                **options,
            )
            with self._process_lock:
                self._process = process
            try:
                exit_code = process.wait()
            finally:
                with self._process_lock:
                    self._process = None

        cancelled = self._is_cancelled(job_id)
        self._forget(job_id)

        tracker = ProgressTracker()
        try:
            tracker.feed(
                log_path.read_text(encoding="utf-8", errors="replace")
            )
        except FileNotFoundError:
            pass

        if cancelled:
            status = "cancelled"
            error = "Cancelled by user"
        elif exit_code == 0:
            status = "completed"
            error = None
        else:
            status = "failed"
            error = f"spotify-dl exited with code {exit_code}"

        fields: dict[str, Any] = {
            "status": status,
            "finished_at": utcnow(),
            "exit_code": exit_code,
            "total_tracks": tracker.total,
            "matched": tracker.matched,
            "no_match": tracker.no_match,
            "downloaded": tracker.downloaded,
            "error": error,
        }
        if tracker.output_dir:
            fields["output_dir"] = tracker.output_dir
        self.store.update(job_id, **fields)

    def _build_command(self, job: dict[str, Any]) -> list[str]:
        command = [
            sys.executable,
            str(CLI_ENTRYPOINT),
            job["playlist_url"],
            "-o",
            str(self.settings.download_dir),
            "-f",
            job["audio_format"],
            "-c",
            str(job["max_concurrent"]),
        ]
        if job["title_first"]:
            command.append("--title-first")
        if job["subfolder"]:
            command.extend(["--subfolder", job["subfolder"]])
        if job["download_archive"]:
            command.extend(
                [
                    "--download-archive",
                    str(self.settings.data_dir / "archive.txt"),
                ]
            )
        return command


def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
        return

    try:
        os.killpg(  # type: ignore[attr-defined]
            os.getpgid(process.pid),  # type: ignore[attr-defined]
            signal.SIGTERM,
        )
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(  # type: ignore[attr-defined]
                os.getpgid(process.pid),  # type: ignore[attr-defined]
                signal.SIGKILL,  # type: ignore[attr-defined]
            )
        except ProcessLookupError:
            pass
