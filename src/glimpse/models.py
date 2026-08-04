"""Pydantic models for API boundaries (request/response)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    q: str = Field(..., min_length=1, description="Search query")
    top_k: int | None = Field(default=None, ge=1, le=200)


class SearchHit(BaseModel):
    file_id: int
    path: str
    file_type: str
    snippet: str
    mtime: float
    score: float
    gist: str | None = None
    chunk_type: str
    position_meta: str | None = None


class SearchResponse(BaseModel):
    hits: list[SearchHit]
    query: str
    took_ms: int


class LocationCreate(BaseModel):
    path: str
    enabled: bool = True


class LocationResponse(BaseModel):
    id: int
    path: str
    enabled: bool
    added_at: float


class FileTypeToggle(BaseModel):
    category: str
    enabled: bool


class FileTypeStatus(BaseModel):
    category: str
    enabled: bool
    supported: bool  # whether this category is implemented in current version


class PerfSettings(BaseModel):
    profile: str
    cpu_high_pct: float
    cpu_pause_pct: float
    batch_size: int
    idle_gate_min: float
    battery_pause_below_pct: int
    max_effort: bool


class PerfSettingsUpdate(BaseModel):
    profile: str | None = None
    cpu_high_pct: float | None = None
    cpu_pause_pct: float | None = None
    batch_size: int | None = None
    idle_gate_min: float | None = None
    battery_pause_below_pct: int | None = None
    max_effort: bool | None = None


class AppStateResponse(BaseModel):
    queue_depth: int
    queue_pending: int
    queue_in_flight: int
    governor_mode: str  # governed | max_effort
    embedder_ready: bool
    embedder_type: str  # hashing | sentence_transformers
    stats: dict[str, int]


class OpenFileRequest(BaseModel):
    path: str


class OpenFileResponse(BaseModel):
    success: bool
    error: str | None = None


# v0.1: only text/code/pdf are supported
SUPPORTED_CATEGORIES_V01 = {"text", "code", "pdf"}
ALL_CATEGORIES = {"text", "code", "pdf", "office", "image", "video"}


def get_file_type_statuses(enabled_map: dict[str, bool]) -> list[FileTypeStatus]:
    """Build file type status list for API."""
    result = []
    for cat in sorted(ALL_CATEGORIES):
        result.append(FileTypeStatus(
            category=cat,
            enabled=enabled_map.get(cat, False),
            supported=cat in SUPPORTED_CATEGORIES_V01,
        ))
    return result