from __future__ import annotations

import json
from pathlib import Path

import pytest

from fivee_sim.service import runs, sessions


def test_create_allocates_a_manifest_backed_workspace(tmp_path: Path) -> None:
    created = runs.create(request_id="open-1", runs_dir=tmp_path)

    assert created["id"] == "run-1"
    assert isinstance(created["version"], str)
    root = tmp_path / "run-1"
    assert root.is_dir()
    assert {path.name for path in root.iterdir()} == {
        "run.json",
        "maps",
        "scenes",
        "replays",
        "encounters",
        "adventures",
        "blobs",
    }
    manifest = json.loads((root / "run.json").read_text(encoding="utf-8"))
    assert manifest | {"created_at": ""} == {
        "format": "fivee-sim-run",
        "format_version": 1,
        "id": "run-1",
        "created_at": "",
        "adventure_id": None,
        "request_ids": {
            "open-1": {
                "operation": "run.create",
                "idempotency_fingerprint": sessions.idempotency_fingerprint(
                    "run.create", {}
                ),
            }
        },
    }


def test_create_is_idempotent_and_refuses_a_reused_key(tmp_path: Path) -> None:
    first = runs.create(request_id="open-1", runs_dir=tmp_path)

    assert runs.create(request_id="open-1", runs_dir=tmp_path) == first
    with pytest.raises(runs.IdempotencyConflictError, match="idempotency key 'open-1'"):
        runs.create(
            request_id="open-1", request_identity={"different": True}, runs_dir=tmp_path
        )
    assert [entry["id"] for entry in runs.list_runs(runs_dir=tmp_path)["runs"]] == ["run-1"]


def test_state_and_list_skip_incomplete_staging_and_keep_scratch_run_adventure_free(
    tmp_path: Path,
) -> None:
    (tmp_path / ".run-9.stage").mkdir(parents=True)
    created = runs.create(runs_dir=tmp_path)

    assert runs.list_runs(runs_dir=tmp_path) == {"runs": [
        {"id": "run-1", "adventure_id": None}
    ]}
    assert runs.state_of("run-1", runs_dir=tmp_path) == {
        "id": "run-1",
        "adventure_id": None,
        "format": "fivee-sim-run",
        "format_version": 1,
        "created_at": created["created_at"],
        "request_ids": {},
        "version": created["version"],
    }
    assert created["adventure_id"] is None
