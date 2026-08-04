"""FastAPI backend: all /api endpoints + static file serving.

Endpoints:
- GET  /api/search?q=...           -> hybrid search
- GET  /api/state                  -> app state (queue, governor, embedder, stats)
- GET  /api/locations              -> list indexed locations
- POST /api/locations              -> add location
- DELETE /api/locations/{id}       -> remove location (and its data)
- PATCH  /api/locations/{id}       -> toggle enabled
- GET  /api/filetypes              -> per-category toggles + support status
- PATCH  /api/filetypes/{category} -> toggle category
- GET  /api/perf                   -> performance settings + profile
- PATCH  /api/perf                 -> update performance settings
- POST /api/perf/max_effort        -> toggle max effort
- POST /api/open                   -> open file in OS (validated)
- GET  /static/*                   -> frontend assets
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from glimpse.config import (
    DEFAULT_HOTKEY,
    FILE_TYPE_CATEGORIES,
    HardwareProfile,
    PROFILES,
    RuntimeFlags,
    V01_SUPPORTED_CATEGORIES,
)
from glimpse.db import connect
from glimpse.embedder import get_embedder, HashingEmbedder
from glimpse.governor import GovernorMode, ResourceGovernor
from glimpse.indexer import create_indexer
from glimpse.models import (
    ALL_CATEGORIES,
    SUPPORTED_CATEGORIES_V01,
    AppStateResponse,
    FileTypeStatus,
    FileTypeToggle,
    LocationCreate,
    LocationResponse,
    OpenFileRequest,
    OpenFileResponse,
    PerfSettings,
    PerfSettingsUpdate,
    SearchRequest,
    SearchResponse,
    get_file_type_statuses,
)
from glimpse.queue import JobPriority, JobQueue
from glimpse.store import (
    add_location,
    get_all_settings,
    get_file_type_settings,
    get_index_stats,
    get_locations,
    hybrid_search,
    remove_location,
    set_file_type_enabled,
    set_location_enabled,
    set_setting,
    upsert_file,
)
from glimpse.watcher import WatcherManager

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global state (set by app.py on startup)
# ---------------------------------------------------------------------------

class AppState:
    """Singleton holding cross-component references."""

    db_path: Path | None = None
    queue: JobQueue | None = None
    governor: ResourceGovernor | None = None
    watcher_manager: WatcherManager | None = None
    indexer = None
    http_port: int = 0
    runtime_flags: RuntimeFlags | None = None


STATE = AppState()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: components are initialized by app.py before this runs
    log.info("Backend starting on port %d", STATE.http_port)
    yield
    # Shutdown
    log.info("Backend shutting down")
    if STATE.queue:
        STATE.queue.shutdown()
    if STATE.governor:
        STATE.governor.shutdown()
    if STATE.watcher_manager:
        STATE.watcher_manager.shutdown()


app = FastAPI(title="Glimpse API", lifespan=lifespan)

# Static files (frontend)
STATIC_DIR = Path(__file__).parent / "ui" / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_components():
    if not all([STATE.db_path, STATE.queue, STATE.governor, STATE.watcher_manager, STATE.indexer]):
        raise HTTPException(503, "App not fully initialized")


def _get_enabled_categories() -> set[str]:
    with connect(STATE.db_path) as con:
        return {cat for cat, en in get_file_type_settings(con).items() if en}


def _get_enabled_location_ids() -> list[int]:
    with connect(STATE.db_path) as con:
        locs = get_locations(con)
        return [loc.id for loc in locs if loc.enabled]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@app.get("/api/search", response_model=SearchResponse)
async def search(q: str, top_k: int | None = None):
    _require_components()
    start = time.perf_counter()

    if not q.strip():
        return SearchResponse(hits=[], query=q, took_ms=0)

    # Get settings for weights
    with connect(STATE.db_path) as con:
        settings = get_all_settings(con)
        vector_w = float(settings.get("search_vector_weight", "0.6"))
        keyword_w = float(settings.get("search_keyword_weight", "0.4"))
        default_top_k = int(settings.get("search_top_k", "50"))

    k = top_k or default_top_k

    # Embed query
    embedder = get_embedder(STATE.runtime_flags)
    query_vec = embedder.embed_texts([q])[0]
    from glimpse.embedder import serialize_embedding
    query_emb = serialize_embedding(query_vec)

    # Hybrid search
    with connect(STATE.db_path) as con:
        hits = hybrid_search(
            con,
            query_embedding=query_emb,
            query_text=q,
            vector_weight=vector_w,
            keyword_weight=keyword_w,
            top_k=k,
            location_ids=_get_enabled_location_ids(),
            enabled_categories=list(_get_enabled_categories()),
        )

    took_ms = int((time.perf_counter() - start) * 1000)
    return SearchResponse(hits=hits, query=q, took_ms=took_ms)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@app.get("/api/state", response_model=AppStateResponse)
async def get_state():
    _require_components()

    embedder = get_embedder(STATE.runtime_flags)
    queue_stats = STATE.queue.stats()

    with connect(STATE.db_path) as con:
        stats = get_index_stats(con)

    return AppStateResponse(
        queue_depth=STATE.queue.depth(),
        queue_pending=queue_stats["pending"],
        queue_in_flight=queue_stats["in_flight"],
        governor_mode=STATE.governor.mode.value,
        embedder_ready=embedder.is_ready(),
        embedder_type="sentence_transformers" if not isinstance(embedder, HashingEmbedder) else "hashing",
        stats=stats,
    )


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

@app.get("/api/locations", response_model=list[LocationResponse])
async def list_locations():
    _require_components()
    with connect(STATE.db_path) as con:
        locs = get_locations(con)
    return [LocationResponse(id=l.id, path=l.path, enabled=l.enabled, added_at=l.added_at) for l in locs]


@app.post("/api/locations", response_model=LocationResponse, status_code=201)
async def create_location(loc: LocationCreate):
    _require_components()
    path = Path(loc.path).resolve()
    if not path.exists():
        raise HTTPException(400, "Path does not exist")
    if not path.is_dir():
        raise HTTPException(400, "Path must be a directory")

    with connect(STATE.db_path) as con:
        loc_id = add_location(con, str(path), loc.enabled)
        con.commit()

        STATE.watcher_manager.add_location(loc_id, str(path), loc.enabled)

    return LocationResponse(id=loc_id, path=str(path), enabled=loc.enabled, added_at=time.time())


@app.delete("/api/locations/{loc_id}", status_code=204)
async def delete_location(loc_id: int):
    _require_components()
    with connect(STATE.db_path) as con:
        remove_location(con, loc_id)
        con.commit()

    STATE.watcher_manager.remove_location(loc_id)


@app.patch("/api/locations/{loc_id}", response_model=LocationResponse)
async def update_location(loc_id: int, enabled: bool):
    _require_components()
    with connect(STATE.db_path) as con:
        set_location_enabled(con, loc_id, enabled)
        con.commit()

    STATE.watcher_manager.set_location_enabled(loc_id, enabled)

    with connect(STATE.db_path) as con:
        from glimpse.store import get_locations
        locs = get_locations(con)
        for l in locs:
            if l.id == loc_id:
                return LocationResponse(id=l.id, path=l.path, enabled=l.enabled, added_at=l.added_at)

    raise HTTPException(404, "Location not found")


# ---------------------------------------------------------------------------
# File Types
# ---------------------------------------------------------------------------

@app.get("/api/filetypes", response_model=list[FileTypeStatus])
async def list_filetypes():
    _require_components()
    with connect(STATE.db_path) as con:
        enabled = get_file_type_settings(con)
    return get_file_type_statuses(enabled)


@app.patch("/api/filetypes/{category}", response_model=FileTypeStatus)
async def toggle_filetype(category: str, enabled: bool):
    _require_components()
    if category not in ALL_CATEGORIES:
        raise HTTPException(404, "Unknown category")

    with connect(STATE.db_path) as con:
        set_file_type_enabled(con, category, enabled)
        con.commit()

    STATE.watcher_manager.set_file_type_enabled(category, enabled)

    return FileTypeStatus(category=category, enabled=enabled, supported=category in SUPPORTED_CATEGORIES_V01)


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------

@app.get("/api/perf", response_model=PerfSettings)
async def get_perf():
    _require_components()
    with connect(STATE.db_path) as con:
        settings = get_all_settings(con)

    profile_name = settings.get("profile", HardwareProfile.BALANCED.value)
    profile = PROFILES[HardwareProfile(profile_name)]
    max_effort = settings.get("max_effort", "0") == "1"

    return PerfSettings(
        profile=profile_name,
        cpu_high_pct=profile.cpu_high_pct,
        cpu_pause_pct=profile.cpu_pause_pct,
        batch_size=profile.batch_size,
        idle_gate_min=profile.idle_gate_min,
        battery_pause_below_pct=profile.battery_pause_below_pct,
        max_effort=max_effort,
    )


@app.patch("/api/perf", response_model=PerfSettings)
async def update_perf(update: PerfSettingsUpdate):
    _require_components()
    with connect(STATE.db_path) as con:
        settings = get_all_settings(con)

        if update.profile is not None:
            if update.profile not in {p.value for p in HardwareProfile}:
                raise HTTPException(400, "Invalid profile")
            settings["profile"] = update.profile
            set_setting(con, "profile", update.profile)
            STATE.governor.set_profile(HardwareProfile(update.profile))

        if update.max_effort is not None:
            settings["max_effort"] = "1" if update.max_effort else "0"
            set_setting(con, "max_effort", settings["max_effort"])
            STATE.governor.set_mode(GovernorMode.MAX_EFFORT if update.max_effort else GovernorMode.GOVERNED)
            if STATE.queue:
                STATE.queue.set_max_effort(update.max_effort)

        # Note: other profile params are derived from profile; individual overrides
        # would need a custom profile system. For v0.1, just use profile presets.

        con.commit()

    return await get_perf()


@app.post("/api/perf/max_effort")
async def toggle_max_effort(enabled: bool):
    _require_components()
    with connect(STATE.db_path) as con:
        set_setting(con, "max_effort", "1" if enabled else "0")
        con.commit()

    STATE.governor.set_mode(GovernorMode.MAX_EFFORT if enabled else GovernorMode.GOVERNED)
    if STATE.queue:
        STATE.queue.set_max_effort(enabled)

    return {"max_effort": enabled}


# ---------------------------------------------------------------------------
# Open file (validated)
# ---------------------------------------------------------------------------

@app.post("/api/open", response_model=OpenFileResponse)
async def open_file(req: OpenFileRequest):
    _require_components()
    path = Path(req.path).resolve()

    # Security: only allow opening files under enabled indexed locations
    allowed = False
    for loc_id in _get_enabled_location_ids():
        with connect(STATE.db_path) as con:
            from glimpse.store import get_locations
            for loc in get_locations(con):
                if loc.id == loc_id and loc.enabled:
                    try:
                        path.relative_to(Path(loc.path).resolve())
                        allowed = True
                        break
                    except ValueError:
                        continue
        if allowed:
            break

    if not allowed:
        return OpenFileResponse(success=False, error="Path not in indexed locations")

    if not path.exists():
        return OpenFileResponse(success=False, error="File not found")

    try:
        os.startfile(str(path))  # Windows
        return OpenFileResponse(success=True)
    except Exception as e:
        return OpenFileResponse(success=False, error=str(e))


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})