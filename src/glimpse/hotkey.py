"""Global hotkey handling (keyboard library)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import keyboard

log = logging.getLogger(__name__)


class HotkeyManager:
    """Manages a global hotkey to trigger the search popup."""

    def __init__(self, hotkey: str, on_activate: Callable[[], None]):
        self._hotkey = hotkey
        self._on_activate = on_activate
        self._registered = False
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """Register the global hotkey. Returns True on success."""
        try:
            # keyboard.add_hotkey runs the callback in a separate thread
            keyboard.add_hotkey(self._hotkey, self._on_activate, suppress=False)
            self._registered = True
            log.info("Global hotkey registered: %s", self._hotkey)
            return True
        except Exception as e:
            log.warning("Failed to register global hotkey %s: %s", self._hotkey, e)
            return False

    def stop(self) -> None:
        if self._registered:
            try:
                keyboard.remove_hotkey(self._hotkey)
                self._registered = False
                log.info("Global hotkey unregistered")
            except Exception as e:
                log.warning("Error unregistering hotkey: %s", e)

    def set_hotkey(self, new_hotkey: str) -> bool:
        """Change the hotkey at runtime."""
        self.stop()
        self._hotkey = new_hotkey
        return self.start()
