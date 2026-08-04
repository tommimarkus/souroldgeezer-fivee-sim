"""Transport-neutral service layer: the tool bodies behind every adapter.

Modules here hold the logic the MCP tools — and, later, the REST editor
server — expose. The rule that makes two thin adapters possible is that
nothing in this package may import MCP, HTTP, or any transport's error type:
functions take plain values, raise plain :class:`ValueError` family errors,
and return JSON-ready primitives or engine dataclasses. Each adapter maps
those errors into its own vocabulary (``ToolError`` on one side,
problem+json on the other) and does nothing else.
"""

from .common import resolve_seed, sha256_of, slugify
from .errors import MapEditError, MapError, RequestError

__all__ = [
    "MapEditError",
    "MapError",
    "RequestError",
    "resolve_seed",
    "sha256_of",
    "slugify",
]
