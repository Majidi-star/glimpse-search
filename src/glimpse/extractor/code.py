"""Extractor for source code files.

Language-aware chunking by function/class boundaries using regex heuristics.
Fallback: fixed-size chunks when heuristics don't match.

Supports: Python, JavaScript/TypeScript, Go, Rust, Java, C/C++, C#, Ruby, PHP.
Add more by extending LANGUAGE_PATTERNS.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import NamedTuple

from glimpse.extractor.base import (
    Chunk,
    ExtractionResult,
    Extractor,
    GIST_MAX_CHARS,
    SNIPPET_MAX_CHARS,
    TEXT_CHUNK_CAP,
    merge_chunks_hierarchical,
    register,
    truncate,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Language patterns: (start_re, end_re) for function/class definitions
# We use simple regex heuristics (no tree-sitter) for v0.1.
# ---------------------------------------------------------------------------

class LangPattern(NamedTuple):
    name: str
    extensions: tuple[str, ...]
    # Regex to match function/class/struct/interface/etc. definitions
    # Should capture the "header" line. We'll chunk from header to next header.
    def_pattern: str


LANGUAGE_PATTERNS: list[LangPattern] = [
    LangPattern(
        name="python",
        extensions=(".py", ".pyw", ".pyi"),
        def_pattern=r"^\s*(?:async\s+)?def\s+\w+|^\s*class\s+\w+",
    ),
    LangPattern(
        name="javascript",
        extensions=(".js", ".jsx", ".mjs", ".cjs"),
        def_pattern=r"^\s*(?:export\s+)?(?:async\s+)?function\s+\w+|^\s*(?:export\s+)?class\s+\w+|^\s*(?:const|let|var)\s+\w+\s*=\s*(?:async\s+)?function",
    ),
    LangPattern(
        name="typescript",
        extensions=(".ts", ".tsx"),
        def_pattern=r"^\s*(?:export\s+)?(?:async\s+)?function\s+\w+|^\s*(?:export\s+)?class\s+\w+|^\s*(?:export\s+)?interface\s+\w+|^\s*(?:export\s+)?type\s+\w+",
    ),
    LangPattern(
        name="go",
        extensions=(".go",),
        def_pattern=r"^\s*func\s+(?:\(\w+\s+\w+\)\s+)?\w+|^\s*type\s+\w+\s+struct|^\s*type\s+\w+\s+interface",
    ),
    LangPattern(
        name="rust",
        extensions=(".rs",),
        def_pattern=r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+\w+|^\s*(?:pub\s+)?struct\s+\w+|^\s*(?:pub\s+)?enum\s+\w+|^\s*(?:pub\s+)?trait\s+\w+|^\s*impl\s+(?:<\w+>)?\s*\w+",
    ),
    LangPattern(
        name="java",
        extensions=(".java",),
        def_pattern=r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:class|interface|enum)\s+\w+|^\s*(?:public|private|protected)?\s*(?:static\s+)?\w+\s+\w+\s*\(",
    ),
    LangPattern(
        name="cpp",
        extensions=(".cpp", ".cc", ".cxx", ".hpp", ".h", ".hxx"),
        def_pattern=r"^\s*(?:template\s*<[^>]+>\s*)?(?:class|struct)\s+\w+|^\s*(?:virtual\s+)?\w+\s+\w+\s*\([^)]*\)\s*(?:const)?\s*[{;]",
    ),
    LangPattern(
        name="csharp",
        extensions=(".cs",),
        def_pattern=r"^\s*(?:public|private|protected|internal)?\s*(?:static\s+)?(?:class|interface|struct|enum)\s+\w+|^\s*(?:public|private|protected|internal)?\s*(?:static\s+)?\w+\s+\w+\s*\(",
    ),
    LangPattern(
        name="ruby",
        extensions=(".rb",),
        def_pattern=r"^\s*def\s+\w+|^\s*class\s+\w+|^\s*module\s+\w+",
    ),
    LangPattern(
        name="php",
        extensions=(".php",),
        def_pattern=r"^\s*function\s+\w+|^\s*class\s+\w+|^\s*interface\s+\w+|^\s*trait\s+\w+",
    ),
]


def detect_language(path: Path) -> LangPattern | None:
    ext = path.suffix.lower()
    for lang in LANGUAGE_PATTERNS:
        if ext in lang.extensions:
            return lang
    return None


class CodeExtractor:
    category = "code"

    def can_handle(self, path: Path) -> bool:
        return detect_language(path) is not None

    def extract(self, path: Path) -> ExtractionResult:
        lang = detect_language(path)
        if not lang:
            return ExtractionResult(gist="", chunks=[])

        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            log.warning("Failed to read %s: %s", path, e)
            return ExtractionResult(gist="", chunks=[])

        lines = content.splitlines()
        if not lines:
            return ExtractionResult(gist="", chunks=[])

        # Find definition boundaries using the language pattern
        def_re = re.compile(lang.def_pattern, re.MULTILINE)

        # Get all match positions (line numbers)
        matches = [(m.start(), m.end(), m.group(0)) for m in def_re.finditer(content)]

        if not matches:
            # Fallback: fixed-size chunks
            return self._fallback_chunk(content, lang.name)

        chunks: list[Chunk] = []
        for i, (start, end, header) in enumerate(matches):
            # Determine chunk end: start of next match, or end of file
            next_start = matches[i + 1][0] if i + 1 < len(matches) else len(content)
            chunk_text = content[start:next_start].rstrip()

            # Truncate to snippet size
            snippet = truncate(chunk_text, SNIPPET_MAX_CHARS)

            # Position meta: line number of header
            line_no = content[:start].count("\n") + 1
            import json
            position_meta = json.dumps({"language": lang.name, "line": line_no, "header": header.strip()[:80]})

            chunks.append(Chunk(chunk_type="text", snippet=snippet, position_meta=position_meta))

        # Cap + hierarchical merge
        if len(chunks) > TEXT_CHUNK_CAP:
            chunks = merge_chunks_hierarchical(chunks, TEXT_CHUNK_CAP)

        # Gist: first substantial function/class header + body preview
        gist_text = content[:GIST_MAX_CHARS * 3]  # read a bit more for context
        gist = truncate(gist_text, GIST_MAX_CHARS)

        return ExtractionResult(gist=gist, chunks=chunks)

    def _fallback_chunk(self, content: str, lang_name: str) -> ExtractionResult:
        """Fixed-size chunking fallback when no definitions found."""
        chunks: list[Chunk] = []
        chunk_size = 1000  # chars
        for i in range(0, len(content), chunk_size):
            piece = content[i:i + chunk_size]
            if not piece.strip():
                continue
            snippet = truncate(piece, SNIPPET_MAX_CHARS)
            import json
            position_meta = json.dumps({"language": lang_name, "offset": i})
            chunks.append(Chunk(chunk_type="text", snippet=snippet, position_meta=position_meta))
            if len(chunks) >= TEXT_CHUNK_CAP:
                break

        gist = truncate(content, GIST_MAX_CHARS)
        return ExtractionResult(gist=gist, chunks=chunks)


register(CodeExtractor())