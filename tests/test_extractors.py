"""Tests for extractors."""

import tempfile
from pathlib import Path

from glimpse.config import V01_SUPPORTED_CATEGORIES
from glimpse.extractor.base import (
    Chunk,
    get_all_extractors,
    get_extractor,
    merge_chunks_hierarchical,
)


class TestExtractors:
    def test_registry_has_all_categories(self):
        extractors = get_all_extractors()
        categories = [e.category for e in extractors]
        assert set(categories) == {"text", "code", "pdf", "office", "image", "video"}

    def test_v01_supported_categories(self):
        for cat in V01_SUPPORTED_CATEGORIES:
            ext = get_extractor(cat)
            assert ext is not None
            assert ext.category == cat

    def test_v01_unsupported_are_stubs(self):
        for cat in {"office", "image", "video"}:
            ext = get_extractor(cat)
            assert ext is not None
            # Stubs return empty results
            result = ext.extract(Path("/nonexistent"))
            assert result.gist == ""
            assert result.chunks == []


class TestTextExtractor:
    def setup_method(self):
        self.extractor = get_extractor("text")
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)

    def teardown_method(self):
        self.tmpdir.cleanup()

    def test_markdown_paragraphs(self):
        p = self.tmp / "test.md"
        p.write_text("# Header\n\nParagraph one.\n\nParagraph two with more content.\n\nThird.")
        result = self.extractor.extract(p)
        assert len(result.chunks) == 4  # header + 3 paragraphs
        assert "Header" in result.chunks[0].snippet
        assert result.gist.startswith("# Header")

    def test_chunk_cap_and_merge(self):
        # Create content with many paragraphs
        paras = "\n\n".join([f"Paragraph {i} with some text content here." for i in range(200)])
        p = self.tmp / "long.md"
        p.write_text(paras)
        result = self.extractor.extract(p)
        # Should be capped at 150
        assert len(result.chunks) <= 150

    def test_empty_file(self):
        p = self.tmp / "empty.txt"
        p.write_text("")
        result = self.extractor.extract(p)
        assert result.chunks == []


class TestCodeExtractor:
    def setup_method(self):
        self.extractor = get_extractor("code")
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)

    def teardown_method(self):
        self.tmpdir.cleanup()

    def test_python_functions_and_classes(self):
        p = self.tmp / "test.py"
        p.write_text("""def foo():
    return 1

class Bar:
    def baz(self):
        return 2

async def qux():
    pass
""")
        result = self.extractor.extract(p)
        assert len(result.chunks) == 4  # foo, Bar, baz, qux
        headers = [c.position_meta for c in result.chunks]
        assert any('"header": "def foo"' in h for h in headers)
        assert any('"header": "class Bar"' in h for h in headers)

    def test_fallback_chunking(self):
        # No recognizable definitions
        p = self.tmp / "weird.py"
        p.write_text("x = 1\n" * 50)
        result = self.extractor.extract(p)
        # Should still produce chunks (fallback)
        assert len(result.chunks) > 0


class TestPdfExtractor:
    def setup_method(self):
        self.extractor = get_extractor("pdf")
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)

    def teardown_method(self):
        self.tmpdir.cleanup()

    def test_empty_pdf(self):
        from pypdf import PdfWriter

        p = self.tmp / "empty.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        with open(p, "wb") as f:
            writer.write(f)

        result = self.extractor.extract(p)
        # Empty pages produce no chunks
        assert result.chunks == []


class TestHierarchicalMerge:
    def test_merge_reduces_chunks(self):
        chunks = [Chunk("text", f"Chunk {i}", f'{{"para": {i}}}') for i in range(200)]
        merged = merge_chunks_hierarchical(chunks, 150)
        assert len(merged) <= 150
        assert len(merged) > 0

    def test_under_cap_unchanged(self):
        chunks = [Chunk("text", f"Chunk {i}", f'{{"para": {i}}}') for i in range(50)]
        merged = merge_chunks_hierarchical(chunks, 150)
        assert len(merged) == 50


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
