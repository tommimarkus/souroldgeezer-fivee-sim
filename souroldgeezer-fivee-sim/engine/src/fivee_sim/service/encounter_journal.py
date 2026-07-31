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

from ..content import CLAUDE_PROJECT_ENV, PROJECT_ENV

__all__ = [
    "ENCOUNTERS_ENV",
    "JournalError",
    "append",
    "encounters_root",
    "journal_path",
    "list_journals",
    "read",
]

ENCOUNTERS_ENV = "FIVEE_SIM_ENCOUNTERS"
ENCOUNTERS_SUBDIR = Path(".fivee-sim") / "encounters"
_SAFE_ID = re.compile(r"^enc-[A-Za-z0-9_-]+$")
_JOURNAL_LOCK = RLock()


class JournalError(ValueError):
    """A journal cannot be trusted or written."""


def encounters_root(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    configured = environ.get(ENCOUNTERS_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    project = (
        environ.get(PROJECT_ENV, "").strip()
        or environ.get(CLAUDE_PROJECT_ENV, "").strip()
    )
    return Path(project or Path.cwd()) / ENCOUNTERS_SUBDIR


def journal_path(encounter_id: str) -> Path:
    if not _SAFE_ID.fullmatch(encounter_id):
        raise JournalError(f"invalid encounter id {encounter_id!r}")
    return encounters_root() / f"{encounter_id}.jsonl"


def _canonical_bytes(value: Any) -> bytes:
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


def append(encounter_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Append and fsync one hash-chained record."""
    with _JOURNAL_LOCK:
        return _append_unlocked(encounter_id, payload)


def _append_unlocked(
    encounter_id: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    path = journal_path(encounter_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = ""
    if path.exists():
        records, _ = read(encounter_id)
        if records:
            previous = str(records[-1]["sha256"])
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
