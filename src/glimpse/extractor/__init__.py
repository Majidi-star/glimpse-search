"""Extractor package — imports register all extractors."""

from glimpse.extractor import base  # noqa: F401
from glimpse.extractor import text  # noqa: F401
from glimpse.extractor import code  # noqa: F401
from glimpse.extractor import pdf  # noqa: F401

__all__ = ["base", "text", "code", "pdf"]