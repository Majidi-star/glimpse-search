"""Main application: wires all components together.

Lifecycle:
1. Initialize config, DB, embedder
2. Start FastAPI + uvicorn in background thread
3. Start watcher manager
4. Start indexer worker thread (drains queue through governor)
5. Start tray icon + hotkey (main thread)
6. On quit: shutdown all cleanly
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import uvicorn
from pathlib import Path

import webview

from glimpse.config import (
    DEFAULT_HOTKEY,
    HTTP_HOST,
    HTTP_PORT,
    Paths,
    RuntimeFlags,
    suggest_profile,
    HardwareProfile,
)
from glimpse.db import init_db, connect
from glimpse.embedder import get_embedder
from glimpse.governor import GovernorMode, ResourceGovernor
from glimpse.indexer import create_indexer
from glimpse.queue import JobQueue, JobPriority
from glimpse.store import (
    add_location,
    get_all_settings,
    get_file_type_settings,
    get_index_stats,
    get_locations,
    set_file_type_enabled,
)
from glimpse.watcher import WatcherManager
from glimpse.backend import app as fastapi_app, STATE
from glimpse.tray import TrayApp
from glimpse.hotkey import HotkeyManager

log = logging.getLogger(__name__)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Suppress noisy loggers
logging.getLogger("watchdog").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class GlimpseApp:
    """Main application controller."""

    def __init__(self, flags: RuntimeFlags | None = None):
        self._flags = flags or RuntimeFlags()
        self._paths = Paths.resolve()
        self._db_path = self._paths.db_path

        # Components (initialized in start())
        self._governor: ResourceGovernor | None = None
        self._queue: JobQueue | None = None
        self._watcher_manager: WatcherManager | None = None
        self._indexer = None
        self._worker_thread: threading.Thread | None = None
        self._uvicorn_thread: threading.Thread | None = None
        self._tray: TrayApp | None = None
        self._hotkey: HotkeyManager | None = None
        self._search_window: webview.Window | None = None
        self._settings_window: webview.Window | None = None
        self._http_port = 0
        self._running = False

        # State accessors for tray/hotkey callbacks
        self._max_effort = False
        self._paused = False

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------

    def run(self) -> None:
        """Start the application (blocks until quit)."""
        self._startup()
        self._running = True

        # Tray runs on main thread (blocks)
        if not self._flags.headless:
            self._tray.run()
        else:
            # Headless: just wait
            try:
                while self._running:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass

        self._shutdown()

    # ---------------------------------------------------------------------
    # Startup
    # ---------------------------------------------------------------------

    def _startup(self) -> None:
        log.info("Starting Glimpse v0.1...")
        log.info("App data: %s", self._paths.app_data)
        log.info("Database: %s", self._db_path)

        # 1. Initialize DB
        init_db(self._db_path)
        self._load_settings()

        # 2. Create governor + queue
        self._governor = ResourceGovernor(self._current_profile, max_effort=self._max_effort)
        self._queue = JobQueue(self._governor)

        # 3. Create indexer
        self._indexer = create_indexer(self._db_path, self._flags, batch_size=self._governor._profile.batch_size)

        # 4. Start FastAPI backend
        self._start_backend()

        # 5. Start watcher manager
        self._start_watchers()

        # 6. Start worker thread
        self._start_worker()

        # 7. Initialize embedder (triggers download if needed, in background)
        self._warm_embedder()

        # 8. Setup tray + hotkey
        if not self._flags.headless:
            self._setup_tray_and_hotkey()

        log.info("Glimpse started on http://%s:%d", HTTP_HOST, self._http_port)

    def _load_settings(self) -> None:
        with connect(self._db_path) as con:
            settings = get_all_settings(con)
            self._max_effort = settings.get("max_effort", "0") == "1"
            self._paused = settings.get("paused", "0") == "1"
            profile_name = settings.get("profile", "balanced")
            self._current_profile = HardwareProfile(profile_name)

    def _start_backend(self) -> None:
        """Start uvicorn in a daemon thread on a free port."""
        # Find free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((HTTP_HOST, 0))
            self._http_port = s.getsockname()[1]

        # Wire global STATE for backend
        STATE.db_path = self._db_path
        STATE.queue = self._queue
        STATE.governor = self._governor
        STATE.watcher_manager = self._watcher_manager
        STATE.indexer = self._indexer
        STATE.http_port = self._http_port
        STATE.runtime_flags = self._flags

        config = uvicorn.Config(
            fastapi_app,
            host=HTTP_HOST,
            port=self._http_port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)

        self._uvicorn_thread = threading.Thread(target=server.run, daemon=True)
        self._uvicorn_thread.start()

        # Wait for server to be ready
        for _ in range(50):
            try:
                with socket.create_connection((HTTP_HOST, self._http_port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.1)

    def _start_watchers(self) -> None:
        self._watcher_manager = WatcherManager(self._queue, self._get_file_type_settings)

        with connect(self._db_path) as con:
            for loc in get_locations(con):
                self._watcher_manager.add_location(loc.id, loc.path, loc.enabled)

    def _get_file_type_settings(self) -> dict[str, bool]:
        with connect(self._db_path) as con:
            return get_file_type_settings(con)

    def _start_worker(self) -> None:
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

    def _worker_loop(self) -> None:
        """Background worker: drains queue through governor."""
        log.info("Indexer worker started")
        while self._running:
            if self._paused:
                time.sleep(1.0)
                continue

            # Process a batch (governor decides batch size internally)
            # The queue's process_batch handles governor polling
            processed = self._queue.process_batch(
                worker_fn=self._process_job,
                batch_size=self._governor._profile.batch_size,
            )
            if processed == 0:
                # No work available, sleep a bit
                time.sleep(0.5)

        log.info("Indexer worker stopped")

    def _process_job(self, job) -> bool:
        """Process a single job via indexer."""
        return self._indexer.process(job)

    def _warm_embedder(self) -> None:
        """Trigger embedder load in background (non-blocking)."""
        def _warm():
            try:
                emb = get_embedder(self._flags)
                if hasattr(emb, '_ensure_loaded'):
                    emb._ensure_loaded()  # type: ignore
                log.info("Embedder ready: %s", type(emb).__name__)
            except Exception as e:
                log.warning("Embedder warm-up failed: %s", e)

        threading.Thread(target=_warm, daemon=True).start()

    def _setup_tray_and_hotkey(self) -> None:
        self._tray = TrayApp(
            on_search=self._show_search,
            on_settings=self._show_settings,
            on_toggle_max_effort=self._toggle_max_effort,
            on_pause_resume=self._toggle_pause,
            on_quit=self._quit,
            get_max_effort=lambda: self._max_effort,
            get_paused=lambda: self._paused,
        )

        self._hotkey = HotkeyManager(DEFAULT_HOTKEY, self._show_search)
        self._hotkey.start()

    # ---------------------------------------------------------------------
    # Tray / Hotkey callbacks
    # ---------------------------------------------------------------------

    def _show_search(self) -> None:
        """Open or focus the search popup window."""
        if self._search_window is None:
            self._search_window = webview.create_window(
                "Glimpse Search",
                f"http://{HTTP_HOST}:{self._http_port}/static/index.html",
                width=640,
                height=480,
                resizable=True,
                frameless=False,
                easy_drag=True,
                on_top=True,
            )
            # Handle window close
            def on_closed():
                self._search_window = None
            self._search_window.events.closed += on_closed
        else:
            # Bring to front
            try:
                self._search_window.restore()
                self._search_window.show()
            except Exception:
                pass

    def _show_settings(self) -> None:
        """Open or focus the settings window."""
        if self._settings_window is None:
            self._settings_window = webview.create_window(
                "Glimpse Settings",
                f"http://{HTTP_HOST}:{self._http_port}/static/settings.html",
                width=560,
                height=520,
                resizable=True,
                frameless=False,
                easy_drag=True,
            )
            def on_closed():
                self._settings_window = None
            self._settings_window.events.closed += on_closed
        else:
            try:
                self._settings_window.restore()
                self._settings_window.show()
            except Exception:
                pass

    def _toggle_max_effort(self, enabled: bool) -> None:
        self._max_effort = enabled
        with connect(self._db_path) as con:
            from glimpse.store import set_setting
            set_setting(con, "max_effort", "1" if enabled else "0")
            con.commit()

        if self._governor:
            self._governor.set_mode(GovernorMode.MAX_EFFORT if enabled else GovernorMode.GOVERNED)
        if self._queue:
            self._queue.set_max_effort(enabled)
        if self._tray:
            self._tray.update_menu()

    def _toggle_pause(self, paused: bool) -> None:
        self._paused = paused
        with connect(self._db_path) as con:
            from glimpse.store import set_setting
            set_setting(con, "paused", "1" if paused else "0")
            con.commit()

        if paused:
            self._queue.pause()
        else:
            self._queue.resume()
        if self._tray:
            self._tray.update_menu()

    def _quit(self) -> None:
        log.info("Quit requested")
        self._running = False
        if self._tray:
            self._tray.stop()

    # ---------------------------------------------------------------------
    # Shutdown
    # ---------------------------------------------------------------------

    def _shutdown(self) -> None:
        log.info("Shutting down...")
        self._running = False

        if self._queue:
            self._queue.shutdown()
        if self._worker_thread:
            self._worker_thread.join(timeout=5.0)
        if self._watcher_manager:
            self._watcher_manager.shutdown()
        if self._governor:
            self._governor.shutdown()
        if self._hotkey:
            self._hotkey.stop()
        if self._tray:
            self._tray.stop()

        log.info("Glimpse stopped")


def main():
    """Entry point: python -m glimpse"""
    import argparse

    parser = argparse.ArgumentParser(description="Glimpse - Local Semantic File Search")
    parser.add_argument("--headless", action="store_true", help="Run without UI (for testing)")
    parser.add_argument("--embed-offline", action="store_true", help="Never download embedding model")
    args = parser.parse_args()

    flags = RuntimeFlags(
        headless=args.headless,
        embed_offline=args.embed_offline,
    )

    app = GlimpseApp(flags)
    app.run()


if __name__ == "__main__":
    main()