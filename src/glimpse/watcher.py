"""File system watcher using watchdog.

Watches enabled indexed locations for create/modify/delete events.
Respects file_type_settings (skips disabled categories).
Enqueues jobs to the JobQueue for indexing.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from glimpse.config import FILE_TYPE_CATEGORIES, V01_SUPPORTED_CATEGORIES
from glimpse.extractor.base import get_extractor
from glimpse.queue import JobQueue, JobPriority
from glimpse.store import get_file_type_settings

log = logging.getLogger(__name__)


class IndexEventHandler(FileSystemEventHandler):
    """Watchdog handler that enqueues indexing jobs."""

    def __init__(
        self,
        location_id: int,
        location_path: Path,
        queue: JobQueue,
        get_enabled_categories: Callable[[], set[str]],
    ):
        super().__init__()
        self._location_id = location_id
        self._location_path = location_path.resolve()
        self._queue = queue
        self._get_enabled_categories = get_enabled_categories

    def _should_process(self, path: Path) -> bool:
        """Check if file type is enabled and supported in v0.1."""
        # Get category from extension
        ext = path.suffix.lower()
        category = self._ext_to_category(ext)
        if not category:
            return False

        enabled = self._get_enabled_categories()
        if category not in enabled:
            return False

        # v0.1 only supports text/code/pdf; others return empty but we still
        # respect the toggle (spec: disabling stops both indexing AND watching)
        return True

    def _ext_to_category(self, ext: str) -> str | None:
        """Map file extension to category."""
        if ext in {".txt", ".md", ".markdown", ".rst", ".text"}:
            return "text"
        if ext in {".py", ".pyw", ".pyi", ".js", ".jsx", ".mjs", ".cjs",
                   ".ts", ".tsx", ".go", ".rs", ".java", ".cpp", ".cc", ".cxx",
                   ".hpp", ".h", ".hxx", ".cs", ".rb", ".php"}:
            return "code"
        if ext == ".pdf":
            return "pdf"
        if ext in {".docx", ".doc", ".odt"}:
            return "office"
        if ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"}:
            return "image"
        if ext in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
            return "video"
        return None

    def _enqueue(self, path: Path, priority: JobPriority) -> None:
        """Enqueue a job for the given path."""
        # We need a file_id. For new files we'll use a synthetic negative id
        # and the indexer will assign a real one after upsert.
        # But we need the file to exist in the DB first... 
        # Simpler: the indexer handles the full flow. We just pass the path
        # and let indexer do upsert + enqueue for embeddings.
        # For now, we need a file_id to dedupe. Use a hash of the path.
        file_id = abs(hash(str(path))) % (2**31)
        self._queue.enqueue(file_id, str(path), priority)

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if self._should_process(path):
            log.info("File created: %s", path)
            self._enqueue(path, JobPriority.NORMAL)

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if self._should_process(path):
            log.info("File modified: %s", path)
            self._enqueue(path, JobPriority.HIGH)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        # For deletes, we don't enqueue a job; the indexer will handle
        # cleanup when it sees the file is gone during a rescan.
        # But we can immediately remove from DB if we have the file_id.
        # For v0.1: log it; the periodic rescan will clean up.
        log.info("File deleted: %s", path)

    def on_moved(self, event: FileSystemEvent) -> None:
        # Treat as delete + create
        if event.is_directory:
            return
        dest_path = Path(event.dest_path)
        if self._should_process(dest_path):
            log.info("File moved to: %s", dest_path)
            self._enqueue(dest_path, JobPriority.NORMAL)
        # Note: source deletion handled by periodic rescan


class LocationWatcher:
    """Manages a watchdog Observer for a single indexed location."""

    def __init__(
        self,
        location_id: int,
        location_path: str,
        queue: JobQueue,
        get_enabled_categories: Callable[[], set[str]],
    ):
        self._location_id = location_id
        self._location_path = Path(location_path).resolve()
        self._queue = queue
        self._get_enabled_categories = get_enabled_categories
        self._observer: Observer | None = None
        self._enabled = False

    def start(self) -> None:
        if self._observer is not None:
            return
        if not self._location_path.exists():
            log.warning("Watch path does not exist: %s", self._location_path)
            return

        handler = IndexEventHandler(
            location_id=self._location_id,
            location_path=self._location_path,
            queue=self._queue,
            get_enabled_categories=self._get_enabled_categories,
        )
        self._observer = Observer()
        self._observer.schedule(handler, str(self._location_path), recursive=True)
        self._observer.start()
        self._enabled = True
        log.info("Started watching location %d: %s", self._location_id, self._location_path)

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None
            self._enabled = False
            log.info("Stopped watching location %d", self._location_id)

    def set_enabled(self, enabled: bool) -> None:
        if enabled and not self._enabled:
            self.start()
        elif not enabled and self._enabled:
            self.stop()

    @property
    def is_alive(self) -> bool:
        return self._observer is not None and self._observer.is_alive()


class WatcherManager:
    """Manages watchers for all enabled locations."""

    def __init__(self, queue: JobQueue, db_get_settings_func: Callable[[], dict[str, bool]]):
        self._queue = queue
        self._db_get_settings = db_get_settings_func  # returns {category: enabled}
        self._watchers: dict[int, LocationWatcher] = {}
        self._lock = threading.RLock()

    def _get_enabled_categories(self) -> set[str]:
        settings = self._db_get_settings()
        return {cat for cat, enabled in settings.items() if enabled}

    def add_location(self, location_id: int, path: str, enabled: bool = True) -> None:
        with self._lock:
            if location_id in self._watchers:
                return
            watcher = LocationWatcher(
                location_id=location_id,
                location_path=path,
                queue=self._queue,
                get_enabled_categories=self._get_enabled_categories,
            )
            self._watchers[location_id] = watcher
            if enabled:
                watcher.start()

    def remove_location(self, location_id: int) -> None:
        with self._lock:
            watcher = self._watchers.pop(location_id, None)
            if watcher:
                watcher.stop()

    def set_location_enabled(self, location_id: int, enabled: bool) -> None:
        with self._lock:
            watcher = self._watchers.get(location_id)
            if watcher:
                watcher.set_enabled(enabled)

    def set_file_type_enabled(self, category: str, enabled: bool) -> None:
        # File type changes are picked up automatically by the handler
        # via get_enabled_categories() callback. No action needed here.
        log.info("File type %s enabled=%s (watcher will respect on next event)", category, enabled)

    def refresh_all(self) -> None:
        """Re-evaluate all watchers against current settings."""
        with self._lock:
            for watcher in self._watchers.values():
                # Watcher checks categories on each event; no restart needed
                pass

    def shutdown(self) -> None:
        with self._lock:
            for watcher in self._watchers.values():
                watcher.stop()
            self._watchers.clear()