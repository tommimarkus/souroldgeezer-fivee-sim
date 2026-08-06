"""Content-addressed, immutable, shared payloads.

The fourth storage kind, and the one defined entirely by its name: a blob's
filename is the SHA-256 of its own canonical bytes, so everything else follows
without machinery. Publishing is a rename, because the winner of a race writes
what the loser was about to. Freshness needs no stamp, because a file that
exists already holds the only content that name can mean. Deduplication needs
no index, because two fights capturing identical content compute identical
names. Integrity needs no chain and no version precondition, because a blob
cannot legally change — so reading one back and hashing it is the whole check,
and :func:`get` does exactly that.

This is the idiom the launcher's ``src/<source-id>`` copy already uses for
engine source, applied to the payloads a journal used to carry inline. A
creation record holding a content snapshot by value stored 14 KB per fight to
say a thing every fight on the machine was saying identically; it names a blob
instead.

**Blobs are never deleted.** A journal names one for as long as the journal
exists, and nothing here can know which other process is mid-recovery on a
fight it has not been told about — the same reasoning that retains old launcher
source copies and the empty journal a ``claim`` leaves behind. Reaping them is
``encounter.prune``'s question, alongside the journals that would license it,
and deliberately not this module's.

Nothing here imports a transport or raises a transport's error: a caller passes
a payload or a digest and gets a plain :class:`BlobError` back, like every other
module in ``service/``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..paths import BLOBS_ENV, BLOBS_SUBDIR, blobs_root
from .common import canonical_json, sha256_of
from .durable import atomic_write

__all__ = [
    "BLOBS_ENV",
    "BLOBS_SUBDIR",
    "MAX_BLOB_BYTES",
    "BlobError",
    "blob_path",
    "blobs_root",
    "get",
    "put",
]

#: The largest file :func:`get` will read. A blob holds one of two things, and
#: the engine already bounds both on their own read paths —
#: :data:`~fivee_sim.content.MAX_PACK_BYTES` and
#: :data:`~fivee_sim.map_document.MAX_MAP_BYTES`, 4 MiB each. Arriving by digest
#: is not a reason to drop the bound: this module's stated posture is that it is
#: reading bytes it did not write in this process, and its integrity check runs
#: *after* the read, so an unbounded read is a hole upstream of the thing built
#: to close it.
#:
#: Four times the single-file cap rather than equal to it, because a content
#: blob is a whole registry where a pack file is one member of one — a session
#: that loads several large packs must not find its own snapshot refused. The
#: bundled SRD slice renders at about 14 KB, so the headroom is three orders of
#: magnitude and this bounds a runaway, not a real payload.
MAX_BLOB_BYTES = 16 * 1024 * 1024

#: What a blob reference may look like: a lowercase SHA-256 hex digest, nothing
#: else. The same containment reasoning as :data:`~fivee_sim.service.common.
#: ID_PATTERN`, and sharper here — a blob id is not a name anybody chose, so a
#: reference outside this grammar cannot address a file we wrote and is refused
#: before any directory is read.
_REFERENCE = re.compile(r"[0-9a-f]{64}")


class BlobError(ValueError):
    """A blob cannot be named, found, or trusted."""


def blob_path(reference: str) -> Path:
    """Where the blob ``reference`` names lives, or a refusal that it is not one."""
    if not _REFERENCE.fullmatch(reference):
        raise BlobError(f"not a blob reference: {reference!r}")
    return blobs_root() / f"{reference}.json"


def put(payload: Mapping[str, Any]) -> str:
    """Store ``payload`` and return the reference that names it.

    Idempotent by construction rather than by check: the same payload always
    computes the same name, so a second caller finds the file already there and
    writes nothing. The existence test is not a race — a loser would write
    byte-identical content to a byte-identical name — and skipping the write is
    only an 11 KB saving, not a correctness claim.
    """
    try:
        text = canonical_json(payload)
    except (TypeError, ValueError) as error:
        # ``canonical_json`` raises ``TypeError`` on a value ``json`` cannot
        # spell. Nothing in ``service/`` may hand a caller a refusal outside the
        # ``ValueError`` family, and ``TypeError`` is outside it — so an engine
        # change that ever put an unserialisable value in a snapshot would
        # escape this module rather than being refused by it.
        raise BlobError(f"cannot render a blob from this payload: {error}") from error
    reference = sha256_of(text)
    path = blobs_root() / f"{reference}.json"
    if path.exists():
        return reference
    try:
        atomic_write(path, text)
    except OSError as error:
        raise BlobError(f"cannot write blob {reference!r}: {error}") from error
    return reference


def get(reference: str) -> dict[str, Any]:
    """The payload ``reference`` names, checked against the name it came under.

    The verification is what a hash chain is to a journal and a version
    precondition is to a document, and it costs one hash of a file already
    read. A journal that named a blob is trusting bytes it did not write in
    this process; a swapped or truncated snapshot is then a named refusal
    rather than a fight quietly recovered under content nobody chose.
    """
    path = blob_path(reference)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise BlobError(f"no blob {reference!r}") from None
    except OSError as error:
        raise BlobError(f"cannot read blob {reference!r}: {error}") from error
    if size > MAX_BLOB_BYTES:
        raise BlobError(
            f"blob {reference!r} is {size} bytes, over the {MAX_BLOB_BYTES} byte limit"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise BlobError(f"no blob {reference!r}") from None
    except (OSError, UnicodeDecodeError) as error:
        raise BlobError(f"cannot read blob {reference!r}: {error}") from error
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise BlobError(f"blob {reference!r} is not valid JSON: {error.msg}") from error
    except RecursionError as error:
        raise BlobError(f"blob {reference!r} is nested too deeply to parse") from error
    if not isinstance(payload, dict):
        raise BlobError(f"blob {reference!r} is not a JSON object")
    try:
        actual = sha256_of(canonical_json(payload))
    except RecursionError as error:
        # The bound the size cap cannot give, and the sharper of the two: the
        # check *itself* is what recurses. ``canonical_json`` is pure Python, so
        # it exhausts the interpreter's limit at a few hundred levels of nesting
        # in a three-kilobyte file, where the C parser above survives twenty
        # thousand. ``RecursionError`` is not a ``ValueError``, so before this it
        # left ``service/`` entirely and reached the adapter's catch-all as a
        # bare 500 rather than a named refusal.
        raise BlobError(f"blob {reference!r} is nested too deeply to verify") from error
    if actual != reference:
        # The one refusal here nothing in the engine can heal on its own: ``put``
        # declines to rewrite a name that already exists, and nothing reaps a
        # blob. So this names the file and the single action that fixes it,
        # rather than a digest under a root the caller may never have set.
        raise BlobError(
            f"blob {reference!r} does not match its name; remove {path} to let it be rewritten"
        )
    return payload
