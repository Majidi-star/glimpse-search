"""Job queue for background indexing work (spec §5, §9).

Single in-process queue that all background workers drain through the ResourceGovernor.
Features:
- Priority: rescans (modified existing) > new files
- Deduplication by file_id
- Pause/resume
- Max Effort flag (bypasses governor)
- Depth counter for ETA/progress
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from glimpse.governor import GovernorMode, ResourceGovernor

log = logging.getLogger(__name__)


class JobPriority(Enum):
    LOW = 0  # initial scan of new location
    NORMAL = 1  # new file created
    HIGH = 2  # modified file (rescan)


@dataclass(slots=True, order=True)
class IndexJob:
    """A single file indexing job.

    sort_index ensures priority queue orders by (priority desc, time asc).
    """

    sort_index: tuple[int, float] = field(init=False, compare=True)
    file_id: int = field(compare=False)
    path: str = field(compare=False)
    priority: JobPriority = field(compare=False, default=JobPriority.NORMAL)
    created_at: float = field(compare=False, default_factory=time.time)
    retries: int = field(compare=False, default=0)

    def __post_init__(self):
        # Negative priority for max-heap behavior (higher priority first)
        # Earlier created_at first for same priority
        self.sort_index = (-self.priority.value, self.created_at)


class JobQueue:
    """Thread-safe priority queue with deduplication and governor integration."""

    def __init__(self, governor: ResourceGovernor):
        self._governor = governor
        self._queue: queue.PriorityQueue[IndexJob] = queue.PriorityQueue()
        self._in_flight: set[int] = set()  # file_ids currently being processed
        self._pending_set: set[int] = set()  # file_ids waiting in queue
        self._lock = threading.RLock()
        self._paused = False
        self._shutdown = False

        # Stats for UI
        self._total_enqueued = 0
        self._total_completed = 0
        self._total_failed = 0

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------

    def enqueue(
        self,
        file_id: int,
        path: str,
        priority: JobPriority = JobPriority.NORMAL,
    ) -> bool:
        """Add a job to the queue.

        Returns True if enqueued, False if deduplicated (already pending/in-flight).
        """
        with self._lock:
            if self._shutdown:
                return False
            if file_id in self._in_flight or file_id in self._pending_set:
                return False  # dedupe

            job = IndexJob(file_id=file_id, path=path, priority=priority)
            self._queue.put(job)
            self._pending_set.add(file_id)
            self._total_enqueued += 1
            log.debug("Enqueued job: file_id=%d path=%s priority=%s", file_id, path, priority.name)
            return True

    def dequeue(self, timeout: float | None = None) -> IndexJob | None:
        """Get the next job respecting the governor.

        Blocks until a job is available and governor allows it, or shutdown.
        Returns None on shutdown, or on timeout (if specified).
        """
        start_time = time.time()

        def timed_out() -> bool:
            return timeout is not None and (time.time() - start_time) >= timeout

        while not self._shutdown:
            if timed_out():
                return None

            if self._paused:
                time.sleep(0.5)
                continue

            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            with self._lock:
                # Double-check dedupe (could have been added while waiting)
                if job.file_id in self._in_flight:
                    self._pending_set.discard(job.file_id)
                    continue

                # Ask governor
                decision = self._governor.poll()
                if not decision.should_run:
                    # Re-queue and sleep
                    self._queue.put(job)
                    time.sleep(decision.sleep_ms / 1000.0)
                    continue

                # Governor approved - move to in-flight
                self._pending_set.discard(job.file_id)
                self._in_flight.add(job.file_id)
                return job

        return None

    def complete(self, file_id: int, success: bool = True) -> None:
        """Mark a job as done (success or failure)."""
        with self._lock:
            self._in_flight.discard(file_id)
            if success:
                self._total_completed += 1
            else:
                self._total_failed += 1
                # Optionally re-queue with retry logic
                # For v0.1: just log
                log.warning("Job failed: file_id=%d", file_id)

    def requeue(self, file_id: int, path: str, priority: JobPriority = JobPriority.HIGH) -> bool:
        """Re-queue a failed or modified file (higher priority)."""
        with self._lock:
            self._in_flight.discard(file_id)
            self._pending_set.discard(file_id)
            return self.enqueue(file_id, path, priority)

    def pause(self) -> None:
        with self._lock:
            self._paused = True
            log.info("Job queue paused")

    def resume(self) -> None:
        with self._lock:
            self._paused = False
            log.info("Job queue resumed")

    def set_max_effort(self, enabled: bool) -> None:
        """Toggle Max Effort mode (delegates to governor)."""
        self._governor.set_mode(GovernorMode.MAX_EFFORT if enabled else GovernorMode.GOVERNED)

    def depth(self) -> int:
        """Total pending + in-flight jobs."""
        with self._lock:
            return self._queue.qsize() + len(self._in_flight)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "pending": self._queue.qsize(),
                "in_flight": len(self._in_flight),
                "total_enqueued": self._total_enqueued,
                "total_completed": self._total_completed,
                "total_failed": self._total_failed,
            }

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            self._paused = False

    # ---------------------------------------------------------------------
    # Batch processing helper (used by worker thread)
    # ---------------------------------------------------------------------

    def process_batch(
        self,
        worker_fn: Callable[[IndexJob], bool],
        batch_size: int,
    ) -> int:
        """Process up to batch_size jobs sequentially.

        Returns number of jobs processed. If queue is empty, returns 0 without blocking.
        """
        processed = 0
        for _ in range(batch_size):
            # Non-blocking dequeue: if queue is empty, return immediately
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                break

            with self._lock:
                # Double-check dedupe
                if job.file_id in self._in_flight:
                    self._pending_set.discard(job.file_id)
                    continue

                # Ask governor
                decision = self._governor.poll()
                if not decision.should_run:
                    # Re-queue and stop batch
                    self._queue.put(job)
                    break

                # Governor approved - move to in-flight
                self._pending_set.discard(job.file_id)
                self._in_flight.add(job.file_id)

            try:
                success = worker_fn(job)
                self.complete(job.file_id, success)
                processed += 1
            except Exception as e:
                log.exception("Worker error on file_id=%d: %s", job.file_id, e)
                self.complete(job.file_id, False)
                processed += 1

            # Sleep between batches per governor decision
            if decision.sleep_ms > 0:
                time.sleep(decision.sleep_ms / 1000.0)

        return processed
