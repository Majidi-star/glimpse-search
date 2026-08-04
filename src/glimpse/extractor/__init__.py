"""Extractor package — imports register all extractors."""

from glimpse.extractor import (
    base,  # noqa: F401
    code,  # noqa: F401
    pdf,  # noqa: F401
    text,  # noqa: F401
)

__all__ = ["base", "text", "code", "pdf"]
