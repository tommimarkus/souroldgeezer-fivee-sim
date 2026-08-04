"""Cross-cutting service helpers: seeds, filesystem-safe names, hashes, discovery."""

from __future__ import annotations

import hashlib
import random
import re
from collections.abc import Sequence
from pathlib import Path

from ..content import contained_json_files

__all__ = ["discover_json_files", "resolve_seed", "sha256_of", "slugify"]


def resolve_seed(seed: int | None) -> int:
    """Use the given seed, or pick one and report it so the result stays replayable."""
    if seed is not None:
        if not -(2**53 - 1) <= seed <= 2**53 - 1:
            raise ValueError(
                "seed must be a JavaScript safe integer "
                f"between {-(2**53 - 1)} and {2**53 - 1}"
            )
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


def discover_json_files(roots: Sequence[str | Path]) -> list[Path]:
    """Every ``*.json`` the roots name, with the content loader's containment
    rule: a named file is taken at its word, a directory refuses symlinks that
    escape it. Unreadable entries are skipped — this feeds a listing, and a
    listing's job is to show what is usable.

    Maps and replays are both directories of JSON the user points us at, so
    they share this rather than each carrying a copy. The containment rule is
    the reason that matters: two copies of a security check are two chances for
    one of them to drift, which is the same argument
    :func:`~fivee_sim.content.contained_json_files` makes for owning the walk.
    """
    found: list[Path] = []
    for entry in roots:
        try:
            root = Path(entry).expanduser().resolve()
        except OSError:
            continue
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix.lower() == ".json":
                found.append(root)
            continue
        found.extend(contained_json_files(root))
    return found
