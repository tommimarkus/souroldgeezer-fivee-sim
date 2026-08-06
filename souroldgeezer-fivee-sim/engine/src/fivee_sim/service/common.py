"""Cross-cutting service helpers: seeds, filesystem-safe names, hashes, discovery."""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..content import contained_json_files

__all__ = [
    "ID_PATTERN",
    "canonical_json",
    "discover_json_files",
    "resolve_seed",
    "sha256_of",
    "slugify",
]

#: What an id addressing a file under one of our directories may look like: the
#: :func:`slugify` alphabet, nothing else. An id outside this grammar cannot
#: name a file we wrote, so a surface reports it as unknown rather than
#: half-resolving it — traversal attempts land here before any directory is
#: read. It lives beside ``slugify`` because it *is* ``slugify``'s output read
#: back: maps and scenes both address files this way, and a second copy of a
#: containment grammar is a second chance for one of them to drift wider.
ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*")


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


def _expand_exponent(value: str) -> str:
    mantissa, raw_exponent = value.lower().split("e", 1)
    exponent = int(raw_exponent)
    sign = ""
    if mantissa.startswith("-"):
        sign, mantissa = "-", mantissa[1:]
    whole, _, fraction = mantissa.partition(".")
    digits = whole + fraction
    decimal_at = len(whole) + exponent
    if decimal_at <= 0:
        return sign + "0." + ("0" * -decimal_at) + digits
    if decimal_at >= len(digits):
        return sign + digits + ("0" * (decimal_at - len(digits)))
    return sign + digits[:decimal_at] + "." + digits[decimal_at:]


def _javascript_number(value: int | float) -> str:
    """Spell a finite JSON number the way ``JSON.stringify`` does."""
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise ValueError("canonical JSON does not support non-finite numbers")
    if value == 0:
        return "0"
    rendered = repr(value).lower()
    magnitude = abs(value)
    if 1e-6 <= magnitude < 1e21:
        if "e" in rendered:
            return _expand_exponent(rendered)
        if value.is_integer():
            return str(int(value))
        return rendered
    mantissa, raw_exponent = rendered.split("e", 1)
    exponent = int(raw_exponent)
    return f"{mantissa}e{'+' if exponent >= 0 else ''}{exponent}"


def _json_key(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _javascript_number(value)
    raise TypeError(f"canonical JSON object key must be scalar, got {type(value)!r}")


def canonical_json(value: Any) -> str:
    """One JSON rendering of a value, so a digest over it means something.

    It lived in :mod:`fivee_sim.service.replay`, where a bundle's integrity
    hashes are checked in a browser and so had to agree with ``JSON.stringify``
    down to how a float is spelled. It belongs here for the reason
    :func:`sha256_of` does: it is a hash's other half, and a second surface that
    wanted to name a payload by its content would otherwise have written a
    second canonicaliser — at which point two files that hash to different names
    can hold the same bytes, and the whole point of content addressing is gone.
    """
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _javascript_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, Mapping):
        items = [(_json_key(key), item) for key, item in value.items()]
        items.sort(key=lambda pair: pair[0])
        return "{" + ",".join(
            f"{json.dumps(key, ensure_ascii=False)}:{canonical_json(item)}"
            for key, item in items
        ) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    raise TypeError(f"value is not JSON serializable: {type(value)!r}")


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
