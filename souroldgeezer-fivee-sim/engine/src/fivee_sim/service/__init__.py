"""Transport-neutral service layer: the operation bodies behind every adapter.

Modules here hold the logic the REST server exposes. The
rule that makes two thin adapters possible is that nothing in this package may
import HTTP or any transport's error type: functions take plain values,
raise plain :class:`ValueError` family errors, and return JSON-ready primitives
or engine dataclasses. Each adapter maps those errors into its own vocabulary
(``ToolError`` on one side, RFC 9457 problem+json on the other) and does nothing
else — which is why :class:`~fivee_sim.service.errors.NotFoundError` exists as a
distinct member of the family rather than as a phrase inside a message: the two
adapters part company exactly there, and nowhere else.
"""

from .common import resolve_seed, sha256_of, slugify
from .errors import MapEditError, MapError, NotFoundError, RequestError

__all__ = [
    "MapEditError",
    "MapError",
    "NotFoundError",
    "RequestError",
    "resolve_seed",
    "sha256_of",
    "slugify",
]
