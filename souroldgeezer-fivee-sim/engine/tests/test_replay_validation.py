"""Canonical replay validation shared by exports, tools, and the viewer corpus."""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from fivee_sim.model.encounter import EncounterMode
from fivee_sim.service.map_ops import WRITABLE_FORMAT_VERSIONS
from fivee_sim.service.replay import (
    LATEST_FORMAT_VERSION,
    READABLE_FORMAT_VERSIONS,
    canonical_sha256,
    validate_replay,
)

from . import api
from .conftest import REPLAY_HERO, eventful_fight, mapless_fight


def interlude(seed: int = 90) -> str:
    """One scout, one chapter, nobody holding the floor.

    A solo roster on purpose: the arity rule an interlude relaxes is the one
    thing about it a fight cannot imitate, and a scout crossing a room alone is
    the chapter this whole format change exists to let a run record.
    """
    created = api.encounter_create(
        [{**REPLAY_HERO, "name": "Kettle"}], seed=seed, mode="exploration"
    )
    return str(created["encounter_id"])


def bundle_of(encounter_id: str, format_version: int = 2) -> dict[str, Any]:
    exported = api.replay_export(encounter_id, format_version=format_version)
    bundle = exported["bundle"]
    assert isinstance(bundle, dict)
    return bundle


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


def test_the_canonical_validator_accepts_every_readable_version() -> None:
    """Derived from ``READABLE_FORMAT_VERSIONS`` rather than named twice.

    A real bundle is exported and validated for each declared version, not
    merely counted — a derivation that stopped actually exporting one would
    be weaker than the two hardcoded assertions it replaced. The vacuity
    guard sits alongside it: a readable set that shrank to nothing, or to
    one trivial member, would let the loop below pass by covering nothing.
    """
    assert len(READABLE_FORMAT_VERSIONS) >= 2, (
        f"only {len(READABLE_FORMAT_VERSIONS)} readable version(s) declared; "
        "this guard needs a real spread to prove anything"
    )
    encounter_id = mapless_fight(seed=79)

    for version in sorted(READABLE_FORMAT_VERSIONS):
        assert (
            validate_replay(api.replay_export(encounter_id, format_version=version)["bundle"])
            == []
        )


def test_every_version_this_build_writes_is_a_version_it_can_read() -> None:
    """The property that makes the writer-moves-without-the-reader defect
    impossible rather than merely unlikely: a phase that bumps the writer and
    forgets ``READABLE_FORMAT_VERSIONS`` fails this before it fails a user's
    disk.

    ``WRITABLE_FORMAT_VERSIONS`` is the real writable set — every key of the
    dispatch table ``replay_export`` calls through — rather than the
    ``{FORMAT_VERSION, LATEST_FORMAT_VERSION}`` approximation this assertion
    started as. Those two constants are the *bundle builders'* stamps, and a
    build could grow a writer for a version neither of them names; the
    dispatch cannot, because a version with no function in it is a version
    ``replay_export`` refuses.
    """
    assert WRITABLE_FORMAT_VERSIONS, "this build declares no writable versions at all"
    assert LATEST_FORMAT_VERSION in WRITABLE_FORMAT_VERSIONS, (
        "the version every default export writes is not one the dispatch can write"
    )
    assert WRITABLE_FORMAT_VERSIONS <= READABLE_FORMAT_VERSIONS, sorted(
        WRITABLE_FORMAT_VERSIONS - READABLE_FORMAT_VERSIONS
    )



def test_every_writer_stamps_the_version_it_was_dispatched_for() -> None:
    """Each writer names its own number, and the dispatch is where that is checked.

    The trap is one the reader-superset test above cannot see.
    ``replay_bundle_v2`` stamped ``LATEST_FORMAT_VERSION`` rather than a literal
    ``2``, which was correct exactly as long as it *was* the latest — bumping
    that constant for v3 would have had the v2 writer emit v2 checkpoints under
    a v3 banner, a bundle the validator accepts and no reader can reconstruct.
    """
    encounter_id = mapless_fight(seed=83)

    for version in sorted(WRITABLE_FORMAT_VERSIONS):
        bundle = api.replay_export(encounter_id, format_version=version)["bundle"]
        assert bundle["format_version"] == version


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


class TestTheV3CheckpointChain:
    """A keyframe and a chain of deltas, graded by rebuilding it.

    v3 is where validating a checkpoint stops being a comparison and becomes a
    reconstruction: the state a checkpoint stands for is not in the file, so
    ``state_hash`` can only be checked against a payload the validator builds
    for itself. That is worth having rather than working around — a validator
    that shrugged at a delta it could not apply would accept a bundle no viewer
    can play, which is the one thing this operation exists to rule out.

    Every case below tampers with the *chain* rather than the hashes, and then
    repairs ``integrity.checkpoints`` so the array-level digest is not what
    fails. What is under test is that a broken reconstruction is caught by the
    checkpoint's own claim about itself.
    """

    def chained(self, seed: int = 63) -> dict[str, Any]:
        bundle = bundle_of(eventful_fight(seed=seed, turns=6), format_version=3)
        assert len(bundle["checkpoints"]) > 2, "a chain this short proves nothing"
        return bundle

    def reseal(self, bundle: dict[str, Any]) -> dict[str, Any]:
        bundle["integrity"]["checkpoints"] = canonical_sha256(bundle["checkpoints"])
        return bundle

    def test_a_clean_chain_is_accepted(self) -> None:
        assert validate_replay(self.chained()) == []

    def test_a_tampered_delta_is_caught_by_the_hash_it_leads_to(self) -> None:
        bundle = self.chained(seed=65)
        set_path(bundle, "checkpoints.2.state_delta.round", 99)

        found = validate_replay(self.reseal(bundle))

        assert {"path": "checkpoints.2.state_hash", "problem": "does not match state"} in found

    def test_a_checkpoint_with_no_delta_is_named_rather_than_skipped(self) -> None:
        bundle = self.chained(seed=67)
        del bundle["checkpoints"][1]["state_delta"]

        found = validate_replay(self.reseal(bundle))

        assert {
            "path": "checkpoints.1.state_delta",
            "problem": "must be an object",
        } in found

    def test_the_keyframe_must_still_carry_a_whole_state(self) -> None:
        """Index 0 is what every later checkpoint is expressed against."""
        bundle = self.chained(seed=69)
        bundle["checkpoints"][0] = {
            **{
                key: value
                for key, value in bundle["checkpoints"][0].items()
                if key != "state"
            },
            "state_delta": {},
        }

        found = validate_replay(self.reseal(bundle))

        assert {"path": "checkpoints.0.state", "problem": "must be an object"} in found

    def test_a_latest_state_the_chain_does_not_reach_is_named(self) -> None:
        bundle = self.chained(seed=71)
        bundle["latest_state"]["round"] = 99
        bundle["integrity"]["latest_state"] = canonical_sha256(bundle["latest_state"])

        found = validate_replay(bundle)

        assert {
            "path": "latest_state",
            "problem": "must equal the final authoritative checkpoint",
        } in found

    def test_a_v2_bundle_still_reads_its_checkpoints_whole(self) -> None:
        """The clause that matters most: an older bundle keeps validating.

        Reconstruction is v3's rule, not the validator's. A bundle already on
        somebody's disk carries every checkpoint in full and is graded the way
        it was written.
        """
        bundle = bundle_of(eventful_fight(seed=73, turns=6), format_version=2)

        assert validate_replay(bundle) == []
        assert all("state" in one for one in bundle["checkpoints"])


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


class TestWhichKindOfChapterTheBundleRecords:
    """``encounter.mode``, and the one state rule that follows from it.

    An interlude has nobody holding the floor, so its every state payload
    reports ``turn`` as null — and a validator that demanded a string refused
    four paths of every bundle a chapter with no fight in it produces. The fix
    is *conditioned on the mode the bundle itself declares*, never a relaxation:
    a fight whose turn went missing is still a broken bundle, and the control
    below is what says so.
    """

    def test_a_v2_bundle_says_which_kind_of_chapter_it_was(self) -> None:
        assert bundle_of(mapless_fight(seed=91))["encounter"]["mode"] == "combat"
        assert bundle_of(interlude(seed=92))["encounter"]["mode"] == "exploration"

    def test_an_interlude_bundle_validates_with_nobody_holding_the_floor(self) -> None:
        # The blocker this phase clears. Every one of these four paths was a
        # diagnostic before the mode reached the state rule, which is why an
        # adventure containing an interlude could not compose at all.
        bundle = bundle_of(interlude(seed=93))

        assert bundle["initial"]["state"]["turn"] is None
        assert bundle["latest_state"]["turn"] is None
        assert [checkpoint["state"]["turn"] for checkpoint in bundle["checkpoints"]] == [
            None for _ in bundle["checkpoints"]
        ]
        assert validate_replay(bundle) == []

    def test_a_fight_that_lost_its_turn_is_still_a_broken_bundle(self) -> None:
        # The control. Without it the case above would also pass against a
        # validator that had simply stopped checking ``turn`` at all.
        bundle = bundle_of(mapless_fight(seed=94))
        for path in ("initial.state.turn", "latest_state.turn", "checkpoints.0.state.turn"):
            broken = deepcopy(bundle)
            set_path(broken, path, None)

            found = validate_replay(broken)

            assert any(
                one["path"] == path and one["problem"] == "must be a string"
                for one in found
            ), path

    def test_an_interlude_that_claims_somebody_holds_the_floor_is_named(self) -> None:
        # The mirror of the control, and the reason the rule is a condition
        # rather than "null is fine anywhere": an interlude rolls no initiative,
        # so a turn in one is a fact nothing in the chapter could have produced.
        broken = deepcopy(bundle_of(interlude(seed=95)))
        set_path(broken, "latest_state.turn", "Kettle")

        found = validate_replay(broken)

        assert any(
            one["path"] == "latest_state.turn" and "interlude" in one["problem"]
            for one in found
        ), found

    @pytest.mark.parametrize("mode", [mode.value for mode in EncounterMode])
    def test_every_mode_the_model_declares_is_a_mode_the_format_accepts(
        self, mode: str
    ) -> None:
        # Derived from the model's own enum rather than listed here: a third
        # kind of chapter added to ``EncounterMode`` and not to the format is a
        # failure at this line rather than a bundle nobody can validate.
        bundle = bundle_of(mapless_fight(seed=96))
        bundle["encounter"]["mode"] = mode
        if mode != EncounterMode.COMBAT.value:
            for path in (
                "initial.state.turn", "latest_state.turn",
                *(f"checkpoints.{index}.state.turn"
                  for index in range(len(bundle["checkpoints"]))),
            ):
                set_path(bundle, path, None)

        assert not [
            one for one in validate_replay(bundle) if one["path"] == "encounter.mode"
        ]

    def test_a_mode_the_model_never_declared_is_named_at_encounter_mode(self) -> None:
        bundle = bundle_of(mapless_fight(seed=97))
        bundle["encounter"]["mode"] = "wandering"

        found = validate_replay(bundle)

        assert any(one["path"] == "encounter.mode" for one in found), found

    def test_a_bundle_written_before_there_was_a_second_kind_is_read_as_a_fight(
        self,
    ) -> None:
        # Absence is not a diagnostic: every v2 bundle frozen before interludes
        # existed was a fight, and reading it as one is both what it is and the
        # strict half of the turn rule. Refusing the key's absence would make
        # every replay on a user's disk unplayable at the version that added it.
        bundle = bundle_of(mapless_fight(seed=98))
        del bundle["encounter"]["mode"]

        assert validate_replay(bundle) == []

        set_path(bundle, "latest_state.turn", None)
        assert any(
            one["path"] == "latest_state.turn" for one in validate_replay(bundle)
        )

    def test_a_v1_export_is_graded_without_a_mode_it_has_no_place_to_carry(
        self,
    ) -> None:
        # The v1 answer, and it is settled by the format rather than chosen. A
        # v1 bundle has no ``encounter`` block to declare a mode in *and* no
        # state block to apply one to — ``initial`` there is creatures and open
        # features — so an interlude exports and validates as v1 with nothing
        # about the mode to say either way.
        bundle = bundle_of(interlude(seed=99), format_version=1)

        assert "encounter" not in bundle
        assert "state" not in bundle["initial"]
        assert validate_replay(bundle) == []


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
