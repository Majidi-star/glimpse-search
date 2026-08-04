"""System tray icon and menu (pystray)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

import pystray
from PIL import Image, ImageDraw

from glimpse.governor import GovernorMode

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

        self._icon: Optional[pystray.Icon] = None

    def run(self) -> None:
        """Run the tray icon (blocks until quit)."""
        self._icon = pystray.Icon(
            "Glimpse",
            icon=_create_tray_icon(),
            title="Glimpse - Semantic Search",
            menu=self._build_menu(),
        )
        # Run on the main thread (blocks until quit)
        log.info("Tray icon started")
        self._icon.run()

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
        log.info("Tray icon stopped")