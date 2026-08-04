# Glimpse

Local, privacy-first semantic file search. "Spotlight/Everything, but it understands
meaning, not just filenames."

- All file content stays on your machine. Only small embeddings and short snippets/gists
  are stored — never raw file content.
- No default system-wide indexing. You add the folders you want indexed.
- A resource governor keeps the app invisible at idle — never pressure the machine.

## Status

**v0.1** — Windows, text/Markdown/code/PDF only, hybrid (semantic + keyword) search,
tray + search popup, Locations + File Types settings, resource governor with Max Effort.

Images, Office docs, video, OCR, transcription, model-provider UI and answer synthesis
arrive in later milestones (v0.2+).

## Quickstart

Requires Python 3.11 and [uv](https://github.com/astral-sh/uv).

```bash
uv sync                 # create venv + install deps
uv run python -m glimpse
```

On first launch the embedding model (`bge-small-en-v1.5`, ~33MB, CPU) is downloaded to
the HuggingFace cache. The search popup opens with `Ctrl+Space`; right-click the tray icon
for Search, Max Effort, Pause, Settings and Quit.

## Layout

```
src/glimpse/        python backend (db, watcher, governor, search, fastapi app)
  ui/static/        vanilla HTML/JS/CSS frontend
tests/              pytest suite
```

## Dev

```bash
uv sync --extra dev
uv run pytest
uv run ruff check src tests
```
