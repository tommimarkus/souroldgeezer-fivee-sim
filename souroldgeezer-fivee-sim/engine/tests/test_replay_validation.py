"""Canonical replay validation shared by exports, tools, and the viewer corpus."""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from fivee_sim.service.replay import canonical_sha256, validate_replay

from . import api
from .conftest import mapless_fight


def set_path(target: object, dotted: str, value: object) -> None:
    """Replace one existing mapping/list path in a JSON-compatible fixture."""
    path = dotted.split(".")
    cursor = target
    for key in path[:-1]:
        if isinstance(cursor, dict):
            cursor = cursor[key]
        elif isinstance(cursor, list):
            cursor = cursor[int(key)]
        else:
            raise AssertionError(f"{dotted!r} stops before {key!r}")
    if isinstance(cursor, dict):
        cursor[path[-1]] = value
    elif isinstance(cursor, list):
        cursor[int(path[-1])] = value
    else:
        raise AssertionError(f"{dotted!r} has no replaceable target")


def test_the_canonical_validator_accepts_v1_and_v2_exports() -> None:
    encounter_id = mapless_fight(seed=79)

    assert validate_replay(
        api.replay_export(encounter_id, format_version=1)["bundle"]
    ) == []
    assert validate_replay(
        api.replay_export(encounter_id, format_version=2)["bundle"]
    ) == []


def test_replay_validate_reports_all_diagnostics_without_loading_a_session() -> None:
    result = api.replay_validate(
        {
            "format": "wrong",
            "format_version": 7,
            "seed": "not an integer",
            "initial": {},
            "events": {},
        }
    )

    assert result["valid"] is False
    assert result["error_count"] >= 4
    assert any(item["path"] == "format" for item in result["diagnostics"])


def test_v2_checkpoint_hashes_and_authoritative_latest_state_are_verified() -> None:
    bundle = api.replay_export(
        mapless_fight(seed=81), format_version=2
    )["bundle"]
    bundle["checkpoints"][-1]["state"]["round"] = 99
    bundle["integrity"]["checkpoints"] = canonical_sha256(bundle["checkpoints"])

    found = validate_replay(bundle)

    assert any(item["path"].endswith("state_hash") for item in found)
    assert any(item["path"] == "latest_state" for item in found)


@pytest.mark.parametrize(
    ("value", "javascript_json"),
    [
        (24.0, b'{"value":24}'),
        (24, b'{"value":24}'),
        (1e-6, b'{"value":0.000001}'),
        (1e-7, b'{"value":1e-7}'),
        (1e20, b'{"value":100000000000000000000}'),
        (1e21, b'{"value":1e+21}'),
    ],
)
def test_canonical_hashes_use_javascript_json_number_spelling(
    value: float | int, javascript_json: bytes
) -> None:
    assert canonical_sha256({"value": value}) == sha256(javascript_json).hexdigest()


def test_canonical_hashes_sort_object_keys_by_unicode_code_point() -> None:
    expected = '{"\ue000":1,"😀":2}'.encode()

    assert canonical_sha256({"😀": 2, "\ue000": 1}) == sha256(expected).hexdigest()


def test_the_validator_reports_non_json_numbers_instead_of_crashing() -> None:
    bundle = api.replay_export(mapless_fight(seed=82), format_version=2)["bundle"]
    bundle["content"]["not_json"] = float("nan")

    found = validate_replay(bundle)

    assert any(item["path"] == "integrity.content" for item in found)


@pytest.mark.parametrize("case", json.loads(
    (Path(__file__).parent / "fixtures" / "replay-invalid.json").read_text(
        encoding="utf-8"
    )
))
def test_the_invalid_corpus_is_refused(case: dict[str, object]) -> None:
    encounter_id = mapless_fight(seed=83)
    bundle = api.replay_export(encounter_id, format_version=2)["bundle"]
    broken = deepcopy(bundle)
    set_path(broken, str(case["path"]), case["value"])

    found = validate_replay(broken)

    assert any(item["path"] == case["diagnostic_path"] for item in found)
