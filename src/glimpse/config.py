"""Application configuration: paths, defaults, hardware profiles.

Everything runtime-writable lives under the app-data directory (never inside a
watched/indexed location). Settings that the user adjusts via the Settings window are
persisted in the ``settings`` table (see store.py), not here — this module only holds
sane defaults and resolves filesystem locations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import platformdirs

_APP_NAME = "Glimpse"
_APP_AUTHOR = "Glimpse"

log = logging.getLogger(__name__)


class HardwareProfile(str, Enum):
    MINIMAL = "minimal"
    BALANCED = "balanced"
    PERFORMANCE = "performance"


@dataclass(frozen=True)
class ProfileDefaults:
    """Governor + worker defaults tied to a hardware profile (spec §8)."""

    worker_threads: int
    cpu_high_pct: float  # above this, throttle batches
    cpu_pause_pct: float  # above this, pause entirely
    batch_size: int  # chunks processed per governor-approved batch
    idle_gate_min: float  # only go full background rate after N min idle (0 = no gate)
    battery_pause_below_pct: int  # pause background work below this battery %
    video_default_on: bool
    larger_models: bool


PROFILES: dict[HardwareProfile, ProfileDefaults] = {
    HardwareProfile.MINIMAL: ProfileDefaults(
        worker_threads=1,
        cpu_high_pct=60.0,
        cpu_pause_pct=85.0,
        batch_size=8,
        idle_gate_min=5.0,
        battery_pause_below_pct=40,
        video_default_on=False,
        larger_models=False,
    ),
    HardwareProfile.BALANCED: ProfileDefaults(
        worker_threads=2,
        cpu_high_pct=70.0,
        cpu_pause_pct=92.0,
        batch_size=24,
        idle_gate_min=2.0,
        battery_pause_below_pct=25,
        video_default_on=False,  # v0.1: no video anyway
        larger_models=False,
    ),
    HardwareProfile.PERFORMANCE: ProfileDefaults(
        worker_threads=4,
        cpu_high_pct=80.0,
        cpu_pause_pct=97.0,
        batch_size=64,
        idle_gate_min=0.5,
        battery_pause_below_pct=15,
        video_default_on=True,
        larger_models=True,
    ),
}


# File-type categories the app understands (spec §3 file_type_settings).
# v0.1 only enables text/code/pdf; the others are present so toggles are stable
# across milestones and disables-immediately works as specified.
FILE_TYPE_CATEGORIES = (
    "text",  # .txt, .md
    "code",  # code files (language-aware chunking)
    "pdf",  # .pdf
    "office",  # .docx etc.   (v0.2)
    "image",  # jpg/png/...   (v0.2)
    "video",  # mp4/mov/...   (v0.4)
)

# Categories that are actually runnable in v0.1. The rest stay disabled by default
# and report "not supported in v0.1" through the API rather than indexing.
V01_SUPPORTED_CATEGORIES = frozenset({"text", "code", "pdf"})


# Default category enabled state. text/code/pdf on, the rest off.
DEFAULT_FILE_TYPE_ENABLED: dict[str, bool] = {
    cat: (cat in V01_SUPPORTED_CATEGORIES) for cat in FILE_TYPE_CATEGORIES
}


@dataclass(frozen=True)
class Paths:
    """Resolved filesystem locations for app runtime data."""

    app_data: Path
    db_path: Path
    models_dir: Path
    log_path: Path

    @classmethod
    def resolve(cls, override: Path | None = None) -> Paths:
        base = (
            override
            if override is not None
            else Path(platformdirs.user_data_dir(_APP_NAME, _APP_AUTHOR))
        )
        base = Path(base)
        models = base / "models"
        base.mkdir(parents=True, exist_ok=True)
        models.mkdir(parents=True, exist_ok=True)
        return cls(
            app_data=base,
            db_path=base / "glimpse.sqlite",
            models_dir=models,
            log_path=base / "glimpse.log",
        )


# Default key/value settings persisted into the ``settings`` table on first run.
DEFAULT_SETTINGS: dict[str, str] = {
    "profile": HardwareProfile.BALANCED.value,
    "max_effort": "0",  # governor active at startup
    "paused": "0",  # worker not paused at startup
    "search_vector_weight": "0.6",  # hybrid rerank weights
    "search_keyword_weight": "0.4",
    "search_top_k": "50",
}


# Hotkey / UI defaults
DEFAULT_HOTKEY = "ctrl+space"
HTTP_HOST = "127.0.0.1"
HTTP_PORT = 0  # 0 = pick a free ephemeral port at startup


@dataclass
class RuntimeFlags:
    """Flags read from environment or CLI by app.py.

    The key one for v0.1: ``GLIMPSE_NO_VENV_DOWNLOAD=1`` makes the embedder skip any
    network model fetch and immediately use the local hashing fallback — useful in
    offline / no-VPN-for-torch situations.
    """

    headless: bool = field(default=False)  # skip tray+webview (for tests / CI)
    embed_offline: bool = field(default=False)  # never fetch the real model


def suggest_profile() -> HardwareProfile:
    """Fingerprint the machine and suggest a profile (spec §8).

    Conservative: anything weak -> MINIMAL. No GPU or small RAM -> BALANCED at most.
    """
    try:
        import psutil
    except Exception:  # pragma: no cover - psutil is a hard dep
        return HardwareProfile.BALANCED

    try:
        cores = psutil.cpu_count(logical=False) or psutil.cpu_count(logical=True) or 1
        ram_gb = (psutil.virtual_memory().total / (1024**3)) if psutil.virtual_memory() else 0.0
    except Exception:  # pragma: no cover
        cores, ram_gb = 1, 0.0

    if cores <= 2 or ram_gb <= 4:
        return HardwareProfile.MINIMAL
    if cores >= 8 and ram_gb >= 16:
        return HardwareProfile.PERFORMANCE
    return HardwareProfile.BALANCED
