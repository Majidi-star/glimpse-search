# Glimpse — Local Semantic File Search

<p align="center">
  <strong>Spotlight/Everything, but it understands meaning, not just filenames.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Windows-10%2F11-blue?logo=windows" alt="Windows">
  <img src="https://img.shields.io/badge/Python-3.11-blue?logo=python" alt="Python">
  <img src="https://img.shields.io/badge/privacy-first-green?logo=lock" alt="Privacy">
  <img src="https://img.shields.io/badge/offline-capable-orange?logo=wifi-off" alt="Offline">
  <img src="https://img.shields.io/badge/license-Apache%202.0-blue" alt="Apache 2.0 License">
</p>

---

## Why Glimpse?

| Traditional Search | Glimpse |
|-------------------|---------|
| Matches filenames & exact text | **Understands meaning & context** |
| Indexes everything by default | **You choose what to index** |
| Sends data to cloud for AI | **100% local — never leaves your machine** |
| Heavy background indexing | **Resource governor = invisible at idle** |
| No semantic understanding | **Hybrid: vector + keyword (BM25)** |

> **Privacy first.** No telemetry. No cloud. No accounts. Your files, your embeddings, your machine.

---

## Status: v0.1 (Windows)

✅ **Working today:**
- Text, Markdown, source code (10+ languages), PDF
- Hybrid semantic + keyword search (vector + BM25)
- System tray + global hotkey (`Ctrl+Space`) search popup
- Settings: Locations (add/remove/enable), File Types (per-category toggles)
- Resource governor (CPU, battery, idle, GPU) + Max Effort mode
- Deterministic offline embedder (no download) **or** optional `bge-small-en-v1.5` (CPU, ~33MB)

🚧 **Coming soon:**
- v0.2 — Images (CLIP + captioning + OCR), Office docs (.docx)
- v0.3 — Model providers UI, local model downloader, optional LLM answer synthesis
- v0.4 — Video (keyframes + Whisper transcription)
- v0.5 — Linux support

---

## Quickstart

### Prerequisites
- **Windows 10/11**
- **Python 3.11** (not 3.12+ yet)
- **[uv](https://github.com/astral-sh/uv)** (fast Python package manager)

### Install (Lean — No Torch, Works Offline)
```bash
git clone https://github.com/Majidi-star/glimpse-search.git
cd glimpse-search
uv sync --extra dev
uv run python -m glimpse
```

### Install (Full — Real Semantic Embeddings)
```bash
# Run with VPN OFF (torch ~190MB download)
uv sync --extra models --extra dev
uv run python -m glimpse
```

> **First launch:** Tray icon appears. Press `Ctrl+Space` to open search. Right-click tray for menu.

---

## How It Works

### Architecture
```
┌──────────────┐     ┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│   Watcher    │ ──▶ │  Extractors │ ──▶ │  Chunker +   │ ──▶ │   Vector Store  │
│  (watchdog)  │     │ (per type)  │     │  Embedder    │     │ (SQLite + vec)  │
└──────────────┘     └─────────────┘     └──────────────┘     └─────────────────┘
       ▲                   ▲                    ▲                       ▲
       │                   │                    │                       │
┌──────────────┐     ┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│  Resource    │     │   File      │     │   Hybrid     │     │   Query API     │
│  Governor    │     │   Types     │     │  Search      │     │  (FastAPI)      │
│  (throttles) │     │  (toggles)  │     │ (vec + BM25) │     │  + Tray + Popup │
└──────────────┘     └─────────────┘     └──────────────┘     └─────────────────┘
```

### What Gets Stored (Per File)
| Item | Size | Purpose |
|------|------|---------|
| **Embeddings** | 384-dim float32 per chunk (~1.5KB) | Semantic search |
| **Snippets** | ~150 chars per chunk | Result previews |
| **Gist** | ~300 chars per file | File summary |
| **Metadata** | Path, hash, mtime, size, type | Deduplication + filters |

**Never stored:** Raw file content, full text, images, personal data.

---

## Usage

### Search Popup (`Ctrl+Space`)
- Type naturally: `"meeting notes about budget"` → finds semantically related files
- Exact matches: `"function parse_json"` → BM25 keyword boost
- `↑/↓` navigate, `Enter` open file, `Esc` close

### Settings Window (Tray → Settings)
| Tab | What It Does |
|-----|--------------|
| **Locations** | Add folders to index, enable/disable, remove (purges data) |
| **File Types** | Toggle categories: Text, Code, PDF (Office/Image/Video stubbed) |
| **Performance** | Hardware profile (Minimal/Balanced/Performance), Max Effort toggle |
| **Models** | *(v0.3)* Local downloader, remote providers (OpenAI/Ollama/Anthropic) |

### Max Effort Mode
- **Normal:** Governor throttles by CPU, battery, idle, GPU. Invisible at idle.
- **Max Effort:** Suspends governor → full CPU/GPU, ignores idle, drains queue fast. Auto-reverts when queue empty.

---

## Installation Details

### Lean Mode (Default)
```bash
uv sync --extra dev
```
- **No torch, no sentence-transformers** (~0MB extra)
- Uses `HashingEmbedder` — deterministic, offline, no semantic meaning
- Perfect for testing, CI, air-gapped machines

### Full Mode (Semantic Search)
```bash
uv sync --extra models --extra dev
```
- Downloads `torch` (CPU, ~190MB) + `sentence-transformers`
- Loads `BAAI/bge-small-en-v1.5` (384-dim, CPU, ~33MB)
- Real semantic similarity — "budget" matches "financial planning"

> **Switch anytime:** Re-run `uv sync --extra models` to enable, or remove to disable. No data migration needed.

---

## Configuration

### App Data Location
```
%LOCALAPPDATA%\Glimpse\
  ├── glimpse.sqlite       # Main database (ignored by git)
  └── models/              # (v0.3) Local model cache
```

### Hardware Profiles (Settings → Performance)
| Profile | Threads | CPU High | CPU Pause | Batch | Idle Gate | Battery Pause | Best For |
|---------|---------|----------|-----------|-------|-----------|---------------|----------|
| Minimal | 1 | 60% | 85% | 8 | 5 min | 40% | Old laptops, battery saver |
| Balanced | 2 | 70% | 92% | 24 | 2 min | 25% | **Default** — most machines |
| Performance | 4 | 80% | 97% | 64 | 30 sec | 15% | Desktops, plugged in |

### Settings Persisted in SQLite (`settings` table)
- `profile` — hardware profile
- `max_effort` — governor suspended
- `paused` — queue paused
- `search_vector_weight` / `search_keyword_weight` — hybrid weights (default 0.6/0.4)
- `search_top_k` — results limit (default 50)

---

## Development

### Project Structure
```
glimpse-search/
├── pyproject.toml          # uv config, deps, ruff, pytest
├── .gitignore              # No sqlite, no .venv, no model cache
├── AGENTS.md               # Instructions for AI agents
├── README.md
├── src/glimpse/
│   ├── app.py              # Entry point, lifecycle, tray+hotkey+webview
│   ├── backend.py          # FastAPI endpoints
│   ├── config.py           # Paths, defaults, profiles
│   ├── db.py               # SQLite schema + sqlite-vec + FTS5
│   ├── store.py            # Data access, hybrid search
│   ├── embedder.py         # Embedder ABC + Hashing + ST implementations
│   ├── extractor/          # Text, Code, PDF + base protocol
│   ├── governor.py         # ResourceGovernor (CPU, battery, idle, GPU)
│   ├── queue.py            # JobQueue (priority, dedupe, Max Effort)
│   ├── indexer.py          # Extract → embed → store orchestration
│   ├── watcher.py          # watchdog observers per location
│   ├── models.py           # Pydantic API models
│   ├── tray.py             # pystray icon + menu
│   ├── hotkey.py           # keyboard global hotkey
│   └── ui/static/          # HTML/JS/CSS (search + settings)
└── tests/
    ├── test_db.py
    ├── test_extractors.py
    ├── test_governor.py
    └── test_search.py
```

### Commands
```bash
# Install (lean)
uv sync --extra dev

# Install (full semantic)
uv sync --extra models --extra dev

# Run app
uv run python -m glimpse

# Run tests (38 pass, 1 skipped)
uv run pytest

# Lint + format
uv run ruff check src tests
uv run ruff format src tests

# Type check (if you add mypy)
# uv run mypy src
```

### Adding a File Type (e.g., `.rs` for Rust)
1. Add extension to `extractor/code.py` → `LANGUAGE_PATTERNS`
2. Test: `uv run python -c "from glimpse.extractor import get_extractor; print(get_extractor('code').can_handle(Path('test.rs')))"`
3. That's it — registry auto-discovers via `extractor/__init__.py`

---

## Privacy & Security

| Guarantee | Implementation |
|-----------|----------------|
| **No content leaves machine** | Only embeddings + snippets stored locally; optional LLM synthesis (v0.3) sends *only* retrieved snippets, never raw files |
| **No default indexing** | Empty "Indexed Locations" on first run; you add folders explicitly |
| **No telemetry** | Zero network calls unless you configure remote LLM provider |
| **Encrypted at rest** | SQLite file in `%LOCALAPPDATA%`; consider BitLocker/VPN for disk encryption |
| **No API keys in repo** | `.gitignore` excludes `*.sqlite`, `.venv`, model cache; settings stored in DB, not code |

> **Threat model:** Local attacker with filesystem access can read the index. Mitigate with disk encryption and OS permissions.

---

## Roadmap

| Version | Target |
|---------|--------|
| **v0.1** ✅ | Text/Code/PDF, hybrid search, tray+popup, governor, Max Effort |
| **v0.2** 🚧 | Images (CLIP + caption + OCR), Office (.docx), improved code chunking (tree-sitter) |
| **v0.3** 📋 | Model Providers UI, local model downloader (progress bars), remote LLM (OpenAI/Ollama/Anthropic), optional answer synthesis |
| **v0.4** 📋 | Video (keyframes + Whisper transcription), scene detection |
| **v0.5** 📋 | Linux (tray via libappindicator, hotkey via udev/input, idle via X11/Wayland) |

---

## Contributing

1. Fork → branch → PR
2. Run `uv run pytest && uv run ruff check src tests` before pushing
3. Follow existing patterns: dataclasses, type hints, `slots=True`, structured logging
4. No breaking changes to DB schema without migration plan

---

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.

---

## Acknowledgments

- **sqlite-vec** — Vector search in SQLite
- **BAAI/bge-small-en-v1.5** — Excellent small embedding model
- **pypdf, watchdog, pystray, pywebview** — Solid Windows desktop foundations
- **uv** — Blazing fast Python package management

---

<p align="center">
  <strong>Built for people who want search that understands — without giving up their data.</strong>
</p>