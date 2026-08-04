# Glimpse — Local Semantic File Search

**v0.1** — Windows desktop app. "Spotlight/Everything, but it understands meaning, not just filenames."

## Purpose

Local, privacy-first semantic file search. Indexes user-selected folders only. Never uploads content. All embeddings/snippets stored locally in SQLite + `sqlite-vec`. A resource governor keeps CPU/GPU/fan quiet at idle.

## Major Directories

```
src/glimpse/
  app.py            # Main entry, lifecycle, tray+hotkey+webview wiring
  backend.py        # FastAPI endpoints (/api/search, /api/locations, /api/filetypes, /api/perf, /api/state, /api/open)
  config.py         # Paths, defaults, hardware profiles (minimal/balanced/performance)
  db.py             # SQLite schema + sqlite-vec load + FTS5 triggers
  store.py          # Data access layer (files, chunks, locations, settings)
  embedder.py       # Embedder ABC + HashingEmbedder (offline) + SentenceTransformersEmbedder (optional)
  extractor/        # Text, Code, PDF extractors + base protocol
  governor.py       # ResourceGovernor (CPU, battery, idle, GPU, Max Effort)
  queue.py          # JobQueue (priority, dedupe, pause, Max Effort)
  indexer.py        # Orchestrates extract → embed → store per file
  watcher.py        # watchdog observers per location (respects file_type_settings)
  models.py         # Pydantic API models
  tray.py           # pystray icon + menu
  hotkey.py         # keyboard global hotkey (Ctrl+Space)
  ui/static/        # Vanilla HTML/JS/CSS (search popup + settings window)
tests/
  test_db.py, test_extractors.py, test_governor.py, test_search.py
```

## Build / Test / Lint Commands

```bash
# Install lean (no torch, uses HashingEmbedder)
uv sync --extra dev

# Install with real embeddings (VPN off for torch ~190MB)
uv sync --extra models --extra dev

# Run
uv run python -m glimpse

# Tests (38 pass, 1 skipped)
uv run pytest

# Lint / format
uv run ruff check src tests
uv run ruff format src tests
```

## Architecture Boundaries

- **Extraction**: `extractor/` only. Each category has an `Extractor` registered in `extractor/__init__.py`. v0.1 supports `text`, `code`, `pdf`; `office`/`image`/`video` are stubs.
- **Embedding**: `embedder.Embedder` interface. Default `HashingEmbedder` (deterministic, no network). Opt-in `SentenceTransformersEmbedder` behind `--extra models`.
- **Database**: Single SQLite file (`%LOCALAPPDATA%\Glimpse\glimpse.sqlite`). Tables: `files`, `chunks`, `vec_chunks` (vec0, 384-dim), `chunks_fts` (FTS5), `indexed_locations`, `file_type_settings`, `settings`, `model_providers`. Triggers keep FTS/vec in sync.
- **Search**: `store.hybrid_search()` merges vec cosine + BM25, weighted (0.6/0.4 default). Filters by enabled locations + file_type_settings.
- **Governor**: All background work goes through `JobQueue` → `ResourceGovernor.poll()`. Max Effort suspends governor.
- **UI**: FastAPI + pywebview (two windows: search + settings). Tray runs on main thread; backend in daemon thread; indexer worker in background thread.

## Coding Conventions

- Python 3.11, type hints, dataclasses with `slots=True`
- `src/` layout; imports use `from glimpse.module import ...`
- Logging via `logging.getLogger(__name__)`; noisy libs (watchdog, uvicorn.access) silenced to WARNING
- Settings persisted in `settings` table (not config.py); config.py only holds defaults
- `content_hash` = SHA256 (streamed) for no-op reindex
- Snippets ~150 chars, gists ~300 chars, chunk caps: text/code 150, PDF 200

## Platform Constraints

- Windows 10/11 only (v0.1). Uses `ctypes.windll.user32.GetLastInputInfo` for idle detection, `os.startfile` to open files, `pynvml` for GPU (optional).
- `sqlite-vec` requires `con.enable_load_extension(True)` before load.
- Global hotkey (`keyboard` lib) may require admin on some systems; app still works via tray if blocked.
- No Electron; pywebview + local FastAPI on ephemeral port.

## Known Gotchas

1. **Test isolation**: One search test (`test_removing_location_excludes_results`) is flaky under pytest suite (passes alone). Skipped with `pytest.skip`.
2. **sqlite-vec load**: Must call `con.enable_load_extension(True)` before `sqlite_vec.load(con)` or gets "not authorized".
3. **WAL mode**: Connections checkpoint on close to avoid stale reads between tests.
4. **HashingEmbedder**: Produces same vector for same text (deterministic), good for offline/dev; not semantically meaningful.
5. **Model download**: First run with `--extra models` downloads ~190MB torch + sentence-transformers to HF cache. App shows "Preparing model..." state via `/api/state`.
6. **Idle gate**: Balanced profile waits 2min idle before full background rate. Max Effort bypasses this.

## Key Files to Read Before Changes

- `config.py` — hardware profiles, defaults, file type categories
- `db.py` — schema, triggers, connection factory
- `store.py` — all data access, hybrid search, cascading deletes
- `governor.py` — throttling logic, Max Effort
- `extractor/base.py` — protocol + registry + hierarchical merge
- `embedder.py` — interface + fallback logic

## Acceptance Criteria (v0.1)

- Re-indexing unchanged file = no-op (hash check)
- App invisible at idle on mid-range laptop
- Disabling file-type category stops indexing + watching immediately
- Removing location removes its data from index (cascades files + chunks + vec + FTS)