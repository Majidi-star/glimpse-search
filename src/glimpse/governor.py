"""Resource governor: throttles background work based on system load (spec §5).

Signals polled every few seconds:
- System CPU load (psutil)
- GPU load (pynvml if NVIDIA present, else skipped)
- User idle time (Windows GetLastInputInfo)
- Battery state (psutil)
- Process priority (below-normal CPU + idle I/O)

Decision: run or pause, and if run, what batch size.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum

import psutil

from glimpse.config import ProfileDefaults, HardwareProfile, PROFILES

log = logging.getLogger(__name__)


class GovernorMode(Enum):
    GOVERNED = "governed"      # normal: respects all signals
    MAX_EFFORT = "max_effort"  # suspended: ignores idle/battery, normal priority


@dataclass(slots=True)
class GovernorDecision:
    """Result of a governor poll."""
    should_run: bool
    batch_size: int
    sleep_ms: int
    reason: str  # human-readable for UI/debugging


class ResourceGovernor:
    """Throttles background workers based on system state."""

    def __init__(
        self,
        profile: HardwareProfile = HardwareProfile.BALANCED,
        *,
        max_effort: bool = False,
    ):
        self._profile: ProfileDefaults = PROFILES[profile]
        self._mode = GovernorMode.MAX_EFFORT if max_effort else GovernorMode.GOVERNED
        self._last_poll = 0.0
        self._poll_interval = 2.0  # seconds between polls

        # Idle tracking (Windows)
        self._idle_seconds = 0.0
        self._last_input_time = self._get_last_input_time()

        # GPU availability
        self._has_gpu = False
        self._nvml_handle = None
        self._init_gpu()

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------

    @property
    def mode(self) -> GovernorMode:
        return self._mode

    def set_mode(self, mode: GovernorMode) -> None:
        self._mode = mode
        log.info("Governor mode changed to %s", mode.value)

    def set_profile(self, profile: HardwareProfile) -> None:
        self._profile = PROFILES[profile]
        log.info("Governor profile changed to %s", profile.value)

    def poll(self) -> GovernorDecision:
        """Check system state and return a scheduling decision.

        Called by the worker before each batch.
        """
        now = time.time()
        if now - self._last_poll < self._poll_interval:
            # Return cached-ish decision if polled too frequently
            # (worker should sleep between batches anyway)
            pass
        self._last_poll = now

        # Max Effort: run at full speed, ignore idle/battery
        if self._mode == GovernorMode.MAX_EFFORT:
            return GovernorDecision(
                should_run=True,
                batch_size=self._profile.batch_size * 2,  # double batch in max effort
                sleep_ms=0,
                reason="max_effort",
            )

        # ----- GOVERNED MODE -----
        reasons = []

        # 1. CPU load
        cpu_pct = psutil.cpu_percent(interval=0.05)
        if cpu_pct >= self._profile.cpu_pause_pct:
            return GovernorDecision(
                should_run=False,
                batch_size=0,
                sleep_ms=5000,
                reason=f"cpu_pause ({cpu_pct:.0f}% >= {self._profile.cpu_pause_pct:.0f}%)",
            )
        if cpu_pct >= self._profile.cpu_high_pct:
            # Throttle batch size proportionally
            factor = 1.0 - (cpu_pct - self._profile.cpu_high_pct) / (100 - self._profile.cpu_high_pct)
            batch_size = max(1, int(self._profile.batch_size * factor))
            reasons.append(f"cpu_high ({cpu_pct:.0f}%)")
        else:
            batch_size = self._profile.batch_size

        # 2. Battery
        try:
            battery = psutil.sensors_battery()
            if battery and not battery.power_plugged:
                if battery.percent <= self._profile.battery_pause_below_pct:
                    return GovernorDecision(
                        should_run=False,
                        batch_size=0,
                        sleep_ms=10000,
                        reason=f"battery_low ({battery.percent:.0f}% <= {self._profile.battery_pause_below_pct}%)",
                    )
                # Reduce batch on battery
                batch_size = max(1, int(batch_size * 0.5))
                reasons.append(f"battery ({battery.percent:.0f}%)")
        except Exception:
            pass  # no battery info (desktop)

        # 3. User idle
        idle_sec = self._get_idle_seconds()
        if idle_sec < self._profile.idle_gate_min * 60:
            # Not idle enough - only process tiny batches or pause
            if idle_sec < 10:
                return GovernorDecision(
                    should_run=False,
                    batch_size=0,
                    sleep_ms=5000,
                    reason=f"idle_gate ({idle_sec:.0f}s < {self._profile.idle_gate_min}min)",
                )
            # Some idle but not enough - very small batches
            batch_size = max(1, int(batch_size * 0.25))
            reasons.append(f"idle_partial ({idle_sec:.0f}s)")
        else:
            reasons.append(f"idle_ok ({idle_sec:.0f}s)")

        # 4. GPU load (if available)
        if self._has_gpu:
            gpu_load = self._get_gpu_load()
            if gpu_load is not None and gpu_load > 80:
                # GPU busy - could reduce batch or note it
                batch_size = max(1, int(batch_size * 0.5))
                reasons.append(f"gpu_busy ({gpu_load:.0f}%)")

        # Sleep proportional to load: busier = longer sleep
        # Base sleep 100ms, scale up to 2000ms at high load
        sleep_ms = 100 + int(1900 * (cpu_pct / 100))

        reason_str = "; ".join(reasons) if reasons else "ok"
        return GovernorDecision(
            should_run=True,
            batch_size=batch_size,
            sleep_ms=sleep_ms,
            reason=reason_str,
        )

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------

    def _get_idle_seconds(self) -> float:
        """Get user idle time in seconds (Windows)."""
        try:
            last_input = self._get_last_input_time()
            if last_input > self._last_input_time:
                self._last_input_time = last_input
                self._idle_seconds = 0.0
            else:
                self._idle_seconds += self._poll_interval
        except Exception:
            # If we can't detect idle, assume idle (permissive)
            self._idle_seconds = self._profile.idle_gate_min * 60 + 1
        return self._idle_seconds

    def _get_last_input_time(self) -> float:
        """Windows GetLastInputInfo via ctypes. Returns seconds since boot."""
        try:
            import ctypes
            from ctypes import wintypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                # dwTime is milliseconds since boot
                return lii.dwTime / 1000.0
        except Exception:
            pass
        return time.time()  # fallback: assume not idle

    def _init_gpu(self) -> None:
        """Try to initialize NVML for GPU monitoring."""
        try:
            import pynvml
            pynvml.nvmlInit()
            self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self._has_gpu = True
            log.info("GPU monitoring enabled (NVIDIA)")
        except Exception:
            self._has_gpu = False
            log.debug("GPU monitoring not available (no NVIDIA or pynvml)")

    def _get_gpu_load(self) -> float | None:
        """Get GPU utilization % (0-100)."""
        if not self._has_gpu or self._nvml_handle is None:
            return None
        try:
            import pynvml
            util = pynvml.nvmlDeviceGetUtilizationRates(self._nvml_handle)
            return float(util.gpu)
        except Exception:
            return None

    def shutdown(self) -> None:
        """Clean up NVML."""
        if self._has_gpu:
            try:
                import pynvml
                pynvml.nvmlShutdown()
            except Exception:
                pass