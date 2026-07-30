"""Cross-cutting service helpers: seeds, filesystem-safe names, and hashes."""

from __future__ import annotations

import hashlib
import random
import re

__all__ = ["resolve_seed", "sha256_of", "slugify"]


def resolve_seed(seed: int | None) -> int:
    """Use the given seed, or pick one and report it so the result stays replayable."""
    if seed is not None:
        return seed
    return random.SystemRandom().randrange(2**31)


def slugify(name: str) -> str:
    """A filesystem-safe rendering of a name: lowercase, hyphens, nothing else.

    Runs of anything that is not a letter or digit collapse to one hyphen, so
    ``"dungeon 42"`` becomes ``dungeon-42`` and a name of pure punctuation
    still yields something usable rather than an empty filename.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return slug or "map"


def sha256_of(text: str) -> str:
    """The SHA-256 hex digest of ``text`` as UTF-8. The identity of a document."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
