"""Durable, atomically published workspaces for automatic opening.

A run is deliberately smaller than an adventure.  It owns a workspace and may
name zero or one adventure; compound opening is the later operation that fills
that optional reference.  This module owns that identity and publication seam,
leaving the existing adventure document service independent until callers are
moved across.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

from . import durable, sessions
from .common import sha256_of
from .errors import IdempotencyConflictError, NotFoundError, RequestError

__all__ = [
    "FORMAT",
    "FORMAT_VERSION",
    "IdempotencyConflictError",
    "RequestError",
    "create",
    "list_runs",
    "state_of",
]

FORMAT = "fivee-sim-run"
FORMAT_VERSION = 1
MANIFEST = "run.json"
_SAFE_ID = re.compile(r"^run-[1-9][0-9]*$")
_WORKSPACE_DIRS = ("maps", "scenes", "replays", "encounters", "adventures", "blobs")


def _render(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _version(text: str) -> str:
    return sha256_of(text)


def _response(document: dict[str, Any], version: str) -> dict[str, Any]:
    return {**deepcopy(document), "version": version}


def _manifest_path(runs_dir: Path, run_id: str) -> Path:
    if _SAFE_ID.fullmatch(run_id) is None:
        raise NotFoundError(f"no run {run_id!r}")
    return runs_dir / run_id / MANIFEST


def _read(path: Path) -> tuple[dict[str, Any], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise NotFoundError(f"no run {path.parent.name!r}") from None
    except (OSError, UnicodeDecodeError) as error:
        raise RequestError(f"cannot read {path}: {error}") from error
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise RequestError(f"{path} is not valid JSON: {error.msg}") from error
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise RequestError(f"{path} is not a {FORMAT} manifest")
    if document.get("format_version") != FORMAT_VERSION:
        raise RequestError(f"{path} is not format_version {FORMAT_VERSION}")
    if not isinstance(document.get("id"), str) or document["id"] != path.parent.name:
        raise RequestError(f"{path} has no matching run id")
    if not isinstance(document.get("created_at"), str):
        raise RequestError(f"{path} has no created_at")
    if document.get("adventure_id") is not None and not isinstance(document["adventure_id"], str):
        raise RequestError(f"{path} adventure_id must be a string or null")
    if not isinstance(document.get("request_ids"), dict):
        raise RequestError(f"{path} has no request_ids")
    return document, _version(text)


def _run_paths(runs_dir: Path) -> list[Path]:
    if not runs_dir.is_dir():
        return []
    return sorted(
        path / MANIFEST
        for path in runs_dir.iterdir()
        if (
            path.is_dir()
            and _SAFE_ID.fullmatch(path.name) is not None
            and (path / MANIFEST).is_file()
        )
    )


def _existing_request(
    runs_dir: Path, request_id: str, identity: dict[str, Any], operation: str
) -> dict[str, Any] | None:
    for path in _run_paths(runs_dir):
        try:
            document, version = _read(path)
        except RequestError:
            continue
        recorded = document["request_ids"].get(request_id)
        if not isinstance(recorded, dict):
            continue
        sessions.ensure_idempotency_identity(request_id, recorded, operation, identity)
        return _response(document, version)
    return None


def _next_id(runs_dir: Path) -> str:
    used = {path.parent.name for path in _run_paths(runs_dir)}
    index = 1
    while f"run-{index}" in used:
        index += 1
    return f"run-{index}"


def create(
    request_id: str | None = None,
    request_identity: dict[str, Any] | None = None,
    initializer: Callable[[Path, str], tuple[str, Any]] | None = None,
    operation: str = "run.create",
    *,
    runs_dir: Path,
) -> dict[str, Any]:
    """Allocate and publish an empty run workspace.

    The hidden staging directory is never a candidate for a list or allocation;
    one rename makes a complete manifest and all required roots visible together.
    """
    identity = {} if request_identity is None else deepcopy(request_identity)
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        with durable.file_lock(runs_dir / ".allocation"):
            if request_id is not None:
                existing = _existing_request(runs_dir, request_id, identity, operation)
                if existing is not None:
                    return existing
            run_id = _next_id(runs_dir)
            root = runs_dir / run_id
            staging = Path(tempfile.mkdtemp(prefix=f".{run_id}.stage-", dir=runs_dir))
            try:
                adventure_id = None
                initialized: Any = None
                if initializer is None:
                    for name in _WORKSPACE_DIRS:
                        (staging / name).mkdir()
                else:
                    adventure_id, initialized = initializer(staging, run_id)
                document: dict[str, Any] = {
                    "format": FORMAT,
                    "format_version": FORMAT_VERSION,
                    "id": run_id,
                    "created_at": sessions.utc_now(),
                    "adventure_id": adventure_id,
                    "request_ids": (
                        {}
                        if request_id is None
                        else {
                            request_id: {
                                "operation": operation,
                                "idempotency_fingerprint": sessions.idempotency_fingerprint(
                                    operation, identity
                                ),
                            }
                        }
                    ),
                }
                text = _render(document)
                durable.atomic_write(staging / MANIFEST, text)
                os.replace(staging, root)
                durable.fsync_directory(runs_dir)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
    except OSError as error:
        raise RequestError(f"cannot create a run under {runs_dir}: {error}") from error
    result = _response(document, _version(text))
    if initializer is not None:
        result["initialized"] = initialized
    return result


def list_runs(*, runs_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """List published manifests without treating hidden staging as a run."""
    entries: list[dict[str, Any]] = []
    for path in _run_paths(runs_dir):
        try:
            document, _version_value = _read(path)
        except RequestError:
            continue
        entries.append({"id": document["id"], "adventure_id": document["adventure_id"]})
    return {"runs": entries}


def state_of(run_id: str, *, runs_dir: Path) -> dict[str, Any]:
    """Return one complete published run manifest and its durable version."""
    document, version = _read(_manifest_path(runs_dir, run_id))
    return _response(document, version)
