"""Durable append-only encounter journals.

Each JSON line is hash-chained to the one before it and fsynced before return.
A final unterminated line is treated as a crash tail: it is preserved byte for
byte beside the journal, then the valid prefix is restored for recovery.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Any

from ..paths import ENCOUNTERS_ENV, ENCOUNTERS_SUBDIR, encounters_root
from . import durable
from .errors import StaleWriteError

__all__ = [
    "ENCOUNTERS_ENV",
    "ENCOUNTERS_SUBDIR",
    "JournalError",
    "StaleWriteError",
    "append",
    "claim",
    "encounters_root",
    "journal_path",
    "list_journals",
    "read",
]

_SAFE_ID = re.compile(r"^enc-[A-Za-z0-9_-]+$")
_JOURNAL_LOCK = RLock()


class JournalError(ValueError):
    """A journal cannot be trusted or written."""


def journal_path(encounter_id: str) -> Path:
    if not _SAFE_ID.fullmatch(encounter_id):
        raise JournalError(f"invalid encounter id {encounter_id!r}")
    return encounters_root() / f"{encounter_id}.jsonl"


def claim(encounter_id: str) -> bool:
    """Take this id by creating its journal, or report that someone else has.

    Allocation cannot be a look followed by a decision. Every engine server on a
    host resolves the same encounters root, and the counter in one process's
    ``EngineState`` says nothing about what another has taken — so an id that
    tests free stays free only by luck, and ``create`` holds one across an
    initial state and a content-snapshot deepcopy before it writes anything.

    ``O_EXCL`` collapses the test and the taking into one syscall, which is the
    same reason ``atomic_write`` publishes through ``os.replace``: the kernel
    arbitrates, so there is no window to lose. A caller that loses tries the
    next name.

    The empty journal left behind *is* the claim, which is why nothing cleans it
    up on a crash: an id that was handed out once must never be handed out
    again, and a reader cannot tell a crashed creation from a stolen one. Every
    read path already tolerates it — ``read`` returns no records, ``list_journals``
    and ``creation_request`` skip it, and ``recover_session`` refuses it by name.
    """
    path = journal_path(encounter_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 0o666 rather than 0o600: the umask applies, so a claimed journal has
        # the permissions `append`'s own ``open(..., "ab")`` would have given it.
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    except FileExistsError:
        return False
    except OSError as error:
        raise JournalError(f"cannot claim {path}: {error}") from error
    os.close(handle)
    return True


def _canonical_bytes(value: Any) -> bytes:
    """The hash chain's own rendering, and deliberately not
    :func:`~fivee_sim.service.common.canonical_json`.

    The two agree on objects, arrays, strings, ``null``, booleans and integers,
    and disagree on **every** float: this one spells ``1.0`` and ``-0.0``,
    that one ``1`` and ``0``. The difference is not an oversight — a bundle's
    hashes are recomputed in a browser, so that one has to match
    ``JSON.stringify``, a constraint a chain link checked only against itself by
    this module does not carry.

    So unifying them is not the tidying it looks like. Every journal already on
    a disk was chained under *this* function, and a rendering that spells one
    number differently rewrites every hash after the record holding it — turning
    a hash-valid file into a corrupt-looking one, which is precisely the reading
    ``verify`` gives an edited journal. If a later phase does unify them, the
    honest move is a `journal_version` bump, not a quiet swap.
    """
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _record_hash(record: Mapping[str, Any]) -> str:
    unhashed = dict(record)
    unhashed.pop("sha256", None)
    return sha256(_canonical_bytes(unhashed)).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read(
    encounter_id: str, *, repair_partial: bool = False
) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    """Read and verify a journal, optionally preserving a partial crash tail."""
    with _JOURNAL_LOCK:
        return _read_unlocked(encounter_id, repair_partial=repair_partial)


def _read_unlocked(
    encounter_id: str, *, repair_partial: bool = False
) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    path = journal_path(encounter_id)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise JournalError(f"unknown encounter {encounter_id!r}") from None
    except OSError as error:
        raise JournalError(f"cannot read {path}: {error}") from error

    warning: dict[str, str] | None = None
    valid_raw = raw
    if raw and not raw.endswith(b"\n"):
        split = raw.rfind(b"\n") + 1
        tail = raw[split:]
        if not repair_partial:
            raise JournalError(f"{path} has a partial final record")
        tail_path = path.with_suffix(".corrupt-tail")
        try:
            with tail_path.open("wb") as handle:
                handle.write(tail)
                handle.flush()
                os.fsync(handle.fileno())
            with path.open("wb") as handle:
                handle.write(raw[:split])
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(path.parent)
        except OSError as error:
            raise JournalError(f"cannot preserve partial tail for {path}: {error}") from error
        valid_raw = raw[:split]
        warning = {
            "problem": "partial final record was removed from the journal",
            "preserved_tail": str(tail_path),
        }

    records: list[dict[str, Any]] = []
    previous = ""
    for line_number, line in enumerate(valid_raw.splitlines(), start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise JournalError(
                f"{path} line {line_number} is not valid JSON: {error.msg}"
            ) from error
        if not isinstance(record, dict):
            raise JournalError(f"{path} line {line_number} must be an object")
        if record.get("previous_sha256") != previous:
            raise JournalError(f"{path} line {line_number} breaks the hash chain")
        actual = _record_hash(record)
        if record.get("sha256") != actual:
            raise JournalError(f"{path} line {line_number} has an invalid sha256")
        records.append(record)
        previous = actual
    return records, warning


def append(
    encounter_id: str,
    payload: Mapping[str, Any],
    *,
    expected_head: str | None = None,
) -> dict[str, Any]:
    """Append and fsync one hash-chained record.

    ``expected_head`` is the chain head the caller last saw. Pass it and a
    second writer is refused with
    :class:`~fivee_sim.service.errors.StaleWriteError` instead of chaining onto
    a head that has moved; omit it and the append still cannot corrupt the file,
    it simply takes its turn.

    The two locks are not redundant. The ``flock`` excludes other *processes* —
    every engine server on a host shares this directory — while the ``RLock``
    keeps this process's own threads from interleaving, and keeps ``read``
    reentrant inside the critical section.
    """
    path = journal_path(encounter_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _JOURNAL_LOCK, durable.file_lock(path):
        return _append_unlocked(encounter_id, payload, expected_head=expected_head)


def _append_unlocked(
    encounter_id: str, payload: Mapping[str, Any], *, expected_head: str | None = None
) -> dict[str, Any]:
    path = journal_path(encounter_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = ""
    if path.exists():
        records, _ = read(encounter_id)
        if records:
            previous = str(records[-1]["sha256"])
    if expected_head is not None and expected_head != previous:
        raise StaleWriteError(
            f"encounter {encounter_id!r}", expected=expected_head, current=previous
        )
    record = {**dict(payload), "previous_sha256": previous}
    record["sha256"] = _record_hash(record)
    encoded = _canonical_bytes(record) + b"\n"
    existed = path.exists()
    try:
        with path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            _fsync_directory(path.parent)
    except OSError as error:
        raise JournalError(f"cannot append {path}: {error}") from error
    return record


def list_journals() -> list[Path]:
    root = encounters_root()
    if not root.is_dir():
        return []
    return sorted(root.glob("enc-*.jsonl"))
