"""System tray icon and menu (pystray)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import pystray
from PIL import Image, ImageDraw

log = logging.getLogger(__name__)


def _create_tray_icon() -> Image.Image:
    """Create a simple tray icon programmatically (magnifying glass)."""
    # 64x64 RGBA
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Draw a simple magnifying glass
    # Circle
    draw.ellipse([8, 8, 40, 40], outline=(0, 120, 255, 255), width=4)
    # Handle
    draw.line([32, 32, 52, 52], fill=(0, 120, 255, 255), width=4)

    return img


class TrayApp:
    """Manages the system tray icon and menu."""

    def __init__(
        self,
        on_search: Callable[[], None],
        on_settings: Callable[[], None],
        on_toggle_max_effort: Callable[[bool], None],
        on_pause_resume: Callable[[bool], None],
        on_quit: Callable[[], None],
        get_max_effort: Callable[[], bool],
        get_paused: Callable[[], bool],
    ):
        self._on_search = on_search
        self._on_settings = on_settings
        self._on_toggle_max_effort = on_toggle_max_effort
        self._on_pause_resume = on_pause_resume
        self._on_quit = on_quit
        self._get_max_effort = get_max_effort
        self._get_paused = get_paused

        self._icon: pystray.Icon | None = None
        self._thread: threading.Thread | None = None

    def run(self) -> None:
        """Run the tray icon on the MAIN thread (legacy - for compatibility)."""
        self._icon = pystray.Icon(
            "Glimpse",
            icon=_create_tray_icon(),
            title="Glimpse - Semantic Search",
            menu=self._build_menu(),
        )
        # Run on the main thread (blocks)
        log.info("Tray icon started (main thread)")
        self._icon.run()

    def start_in_background(self) -> None:
        """Start the tray icon on a daemon thread (non-blocking)."""
        if self._icon is not None:
            return
        self._icon = pystray.Icon(
            "Glimpse",
            icon=_create_tray_icon(),
            title="Glimpse - Semantic Search",
            menu=self._build_menu(),
        )
        self._thread = threading.Thread(target=self._icon.run, daemon=True, name="tray-icon")
        self._thread.start()
        # Wait briefly for icon to become visible
        for _ in range(50):
            if getattr(self._icon, "visible", False):
                break
            time.sleep(0.05)
        log.info("Tray icon started (background thread)")

    def _build_menu(self) -> pystray.Menu:
        def make_max_effort_item():
            return pystray.MenuItem(
                "Max Effort",
                lambda: self._on_toggle_max_effort(not self._get_max_effort()),
                checked=lambda item: self._get_max_effort(),
            )

        def make_pause_item():
            return pystray.MenuItem(
                "Pause Indexing",
                lambda: self._on_pause_resume(not self._get_paused()),
                checked=lambda item: self._get_paused(),
            )

        return pystray.Menu(
            pystray.MenuItem("Search", lambda: self._on_search(), default=True),
            pystray.MenuItem("Settings", lambda: self._on_settings()),
            pystray.Menu.SEPARATOR,
            make_max_effort_item(),
            make_pause_item(),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self._on_quit()),
        )

    def update_menu(self) -> None:
        """Refresh menu (e.g., after max_effort/paused change)."""
        if self._icon:
            self._icon.menu = self._build_menu()
            self._icon.update_menu()

    def stop(self) -> None:
        if self._icon:
            self._icon.stop()
            self._icon = None
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        log.info("Tray icon stopped")
