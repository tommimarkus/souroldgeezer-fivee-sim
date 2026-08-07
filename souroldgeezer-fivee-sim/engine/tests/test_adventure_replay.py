"""An adventure's replay: the run's frozen fights, composed into one envelope.

**Composition is file work, and that is the whole of the design.**
``encounter.finalize`` already wrote a complete v2 bundle beside the journal;
that file *is* the chapter. Nothing here recovers a session, replays an action,
or asks the kernel anything — so the cases below finalize through the engine and
then hold what was composed against the artifact ``finalize`` itself named. A
composer that re-derived a fight would pass a shallow equality check and still
be wrong: with carry-over, shifting chapter N's ``latest_state`` by a hit point
leaves chapter N+1's recorded starting hit points following from nothing, while
the integrity block hashes the inconsistency happily.

**The envelope is a second format, not a third replay version.**
``validate_replay``'s version-agnostic prefix demands ``seed``,
``initial.creatures``, ``map`` and ``events`` before it ever reaches the v1
early return, and an adventure envelope has none of them. So it says
``fivee-sim-adventure-replay`` and is validated by a sibling function — which is
also what the ``replay.validate`` route dispatches on, and the case at the
bottom of this file is why that dispatch is not dead code.

**The key sets are derived, never listed.** Three times in this feature two
declarations that had to agree were written twice and drifted — ``facing``, the
four carry-over keys, the carried set inside ``adventures.py``. So a chapter's
fields are held against the *document's own member record*, the envelope's
adventure block against the field list ``_parsed`` requires, and the integrity
block against :data:`replay.ADVENTURE_INTEGRITY_KEYS` by tampering with each key
the declaration names in turn.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from fivee_sim.paths import adventures_root, encounters_root, replays_root
from fivee_sim.service import adventures
from fivee_sim.service import replay as replay_service
from fivee_sim.service.errors import NotFoundError, ReplayError, RequestError

from . import api
from .conftest import AMBUSHER, LOOKOUT, MILL, REPLAY_GOBLIN, REPLAY_HERO, SCOUT

#: Envelope keys the integrity block does **not** cover, with the reason each
#: is exempt. Written out so a new *data* block added to the envelope without a
#: hash is a failure here rather than a silent hole: the derived case below
#: subtracts these from the envelope's own keys and holds the remainder against
#: ``ADVENTURE_INTEGRITY_KEYS``.
UNHASHED_KEYS: frozenset[str] = frozenset({
    # What the file says it is. Hashing the discriminator would let a bundle
    # renamed to another format still verify against its own new name.
    "format",
    "format_version",
    # Which engine composed it — a provenance note, not part of the run.
    "engine_version",
    # The block itself; it cannot hash its own hashes.
    "integrity",
})


def artifact_of(encounter_id: str) -> Path:
    """The frozen replay ``encounter.finalize`` wrote for one fight.

    Spelled once here rather than at each of the four cases below that reach
    for one, and spelled out rather than imported from
    ``encounters.replay_path``: these tests want the file on disk, and a helper
    that asked the subject where it put things would still find it after a move
    nobody meant to make. Where the layout itself is the claim is
    ``test_encounter_journal``'s own siblings case.
    """
    return encounters_root() / encounter_id / "replay.json"


def run_of(chapters: int, name: str = "The Sunless Citadel") -> str:
    """An adventure of ``chapters`` linked, finalized encounters."""
    adventure_id = str(api.adventure_create(name)["id"])
    for index in range(chapters):
        linked = api.adventure_encounter(
            adventure_id,
            combatants=(
                [dict(REPLAY_HERO), dict(REPLAY_GOBLIN)] if index == 0 else None
            ),
            seed=700 + index,
        )
        api.encounter_finalize(str(linked["encounter_id"]))
    return adventure_id


def run_of_a_walk_then_a_fight(name: str = "The Drowned Mill") -> str:
    """The two-chapter run this format change exists to let anybody record.

    An interlude on a saved map, the party walking across it, then the ambush on
    that same ground with ``carry_map`` — which is also the run that could not
    compose at all until the bundle said which kind of chapter it was.
    """
    api.map_save("mill", MILL, "*")
    adventure_id = str(api.adventure_create(name)["id"])
    interlude = api.adventure_encounter(
        adventure_id,
        combatants=[dict(SCOUT), dict(LOOKOUT)],
        seed=730,
        map_id="mill",
        mode="exploration",
    )
    walked = str(interlude["encounter_id"])
    api.encounter_act(walked, "move", to_position=[25, 25], actor="Kettle")
    api.encounter_finalize(walked)
    ambush = api.adventure_encounter(
        adventure_id,
        combatants=[dict(AMBUSHER)],
        seed=731,
        carry_map=True,
        mode="combat",
    )
    api.encounter_finalize(str(ambush["encounter_id"]))
    return adventure_id


def composed(adventure_id: str) -> dict[str, Any]:
    """The envelope a composition wrote, read back off the disk it named."""
    result = api.adventure_replay(adventure_id)
    payload = json.loads(Path(str(result["path"])).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def altered(value: Any) -> Any:
    """The same block, changed in a way no other check would notice.

    A mapping gains a key nothing reads; a list loses its last entry, which for
    a two-chapter run leaves a structurally valid envelope. Both are exactly the
    edit an integrity hash exists to catch and nothing else would.
    """
    if isinstance(value, Mapping):
        return {**value, "annotation": "edited by hand"}
    return list(value)[:-1]


class TestComposingTheRun:
    """The chapters are the frozen artifacts, in the order the run linked them."""

    def test_the_finalized_fights_compose_in_member_order(self) -> None:
        adventure_id = run_of(2)

        envelope = composed(adventure_id)

        members = api.adventure_state(adventure_id)["members"]
        assert [chapter["index"] for chapter in envelope["chapters"]] == [0, 1]
        assert [chapter["encounter_id"] for chapter in envelope["chapters"]] == [
            member["encounter_id"] for member in members
        ]

    def test_each_chapter_is_the_artifact_finalize_froze_and_not_a_re_derivation(
        self,
    ) -> None:
        # Held against the file ``finalize`` itself named, so the path is the
        # engine's answer rather than a second spelling of it here. This is the
        # claim the whole design rests on: a fight is composed as it was
        # recorded, never replayed again under whatever kernel is loaded now.
        adventure_id = str(api.adventure_create("Barrow of the Forgotten King")["id"])
        linked = api.adventure_encounter(
            adventure_id, combatants=[dict(REPLAY_HERO), dict(REPLAY_GOBLIN)], seed=710
        )
        finalized = api.encounter_finalize(str(linked["encounter_id"]))
        frozen = json.loads(
            Path(str(finalized["replay_path"])).read_text(encoding="utf-8")
        )

        envelope = composed(adventure_id)

        assert envelope["chapters"][0]["replay"] == frozen
        assert replay_service.validate_replay(envelope["chapters"][0]["replay"]) == []

    def test_a_chapter_carries_the_runs_own_member_record_beside_the_bundle(
        self,
    ) -> None:
        # Derived from the document rather than from a list written here: a
        # chapter is what the adventure recorded about that encounter — when it
        # was linked and who was carried into it — plus the fight itself. A
        # composer that dropped ``carried`` would leave the envelope unable to
        # say why chapter two starts where chapter one ended.
        adventure_id = run_of(2)

        envelope = composed(adventure_id)

        members = api.adventure_state(adventure_id)["members"]
        assert len(members) == 2
        for member, chapter in zip(members, envelope["chapters"], strict=True):
            assert set(chapter) == set(member) | {"replay"}
            assert {key: chapter[key] for key in member} == member
        assert envelope["chapters"][1]["carried"] == ["Thora", "Goblin"]

    def test_a_recovery_boundary_is_frozen_beside_the_chapter_it_precedes(
        self,
    ) -> None:
        adventure_id = str(api.adventure_create("The Sunless Citadel")["id"])
        first = api.adventure_encounter(
            adventure_id,
            combatants=[dict(REPLAY_HERO), dict(REPLAY_GOBLIN)],
            seed=711,
        )
        api.encounter_finalize(str(first["encounter_id"]))
        second = api.adventure_encounter(
            adventure_id,
            recovery={"Thora": {"hp": 20}, "Goblin": {}},
            recovery_note="Long rest beside the sealed door",
            seed=712,
        )
        api.encounter_finalize(str(second["encounter_id"]))

        envelope = composed(adventure_id)

        assert "recovery" not in envelope["chapters"][0]
        assert envelope["chapters"][1]["recovery"] == {
            "Thora": {"hp": 20}, "Goblin": {}
        }
        assert envelope["chapters"][1]["recovery_note"] == (
            "Long rest beside the sealed door"
        )
        assert replay_service.validate_adventure_replay(envelope) == []

    def test_the_envelope_names_the_run_by_every_field_a_document_must_have(
        self,
    ) -> None:
        # ``_parsed`` decides what a stored adventure must say about itself;
        # the envelope says the same things, from one declaration. Written as a
        # subtraction so widening the document without widening the envelope is
        # a failure rather than a quiet omission.
        adventure_id = run_of(1, name="Tomb of Horrors")

        envelope = composed(adventure_id)

        document = api.adventure_state(adventure_id)
        assert set(envelope["adventure"]) == set(adventures.DOCUMENT_FIELDS)
        assert envelope["adventure"] == {
            key: document[key] for key in adventures.DOCUMENT_FIELDS
        }
        assert envelope["adventure"]["name"] == "Tomb of Horrors"

    def test_the_envelope_says_which_format_it_is_and_which_engine_wrote_it(
        self,
    ) -> None:
        from fivee_sim import __version__

        envelope = composed(run_of(1))

        assert envelope["format"] == "fivee-sim-adventure-replay"
        assert envelope["format"] != replay_service.FORMAT, (
            "an adventure envelope that answered to the replay format would be "
            "handed to a validator whose every required field it lacks"
        )
        assert envelope["format_version"] == 1
        assert envelope["engine_version"] == __version__

    def test_every_block_the_envelope_carries_is_one_the_integrity_block_covers(
        self,
    ) -> None:
        # The derived guard. A per-field case would miss the next block added,
        # which is exactly how a key reaches one declaration and not the other.
        envelope = composed(run_of(2))

        assert set(envelope) - UNHASHED_KEYS == set(
            replay_service.ADVENTURE_INTEGRITY_KEYS
        )
        assert set(envelope["integrity"]) == {"algorithm"} | set(
            replay_service.ADVENTURE_INTEGRITY_KEYS
        )
        assert envelope["integrity"]["algorithm"] == "sha256"

    def test_the_result_always_names_a_written_file_and_never_inlines_the_bundle(
        self,
    ) -> None:
        # ``replay_export`` answers a small bundle inline; one realistic v2
        # bundle already exceeds that ceiling, so an envelope of several is
        # never a thing to hand back in a JSON response.
        result = api.adventure_replay(run_of(2))

        assert "bundle" not in result
        written = Path(str(result["path"]))
        assert written.is_file()
        assert result["bytes"] == len(written.read_bytes())
        assert result["chapters"] == 2
        assert result["format"] == "fivee-sim-adventure-replay"

    def test_an_explicit_path_is_where_the_envelope_lands(self, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere" / "run.json"

        result = api.adventure_replay(run_of(1), path=str(target))

        assert result["path"] == str(target)
        assert json.loads(target.read_text(encoding="utf-8"))["format"] == (
            "fivee-sim-adventure-replay"
        )

    def test_the_envelope_is_not_offered_to_the_viewer_as_a_replay(self) -> None:
        # ``list_replays`` filters on the replay format, so an adventure
        # envelope is invisible to it — which is why the result names no viewer
        # link. One handed out would send a reader to a 404.
        result = api.adventure_replay(run_of(1))

        assert "viewer_url" not in result
        assert Path(str(result["path"])).parent == replays_root()
        assert replay_service.list_replays([replays_root()]) == []


class TestWhatCompositionRefuses:
    """Every chapter is a frozen file, and a missing one is never invented."""

    def test_an_encounter_that_was_never_finalized_is_refused_by_name(self) -> None:
        adventure_id = str(api.adventure_create("The Sunless Citadel")["id"])
        first = api.adventure_encounter(
            adventure_id, combatants=[dict(REPLAY_HERO), dict(REPLAY_GOBLIN)], seed=720
        )
        api.encounter_finalize(str(first["encounter_id"]))
        unfinished = api.adventure_encounter(adventure_id, seed=721)

        with pytest.raises(
            RequestError,
            match=f"encounter '{unfinished['encounter_id']}' of adventure "
            f"'{adventure_id}' has no finalized replay",
        ):
            api.adventure_replay(adventure_id)

    def test_a_member_whose_artifact_is_gone_is_refused_rather_than_re_derived(
        self,
    ) -> None:
        # The session is still in memory and the journal is still on disk, so
        # an implementation that fell back to either would silently answer with
        # a fight replayed under today's code instead of the one recorded. It
        # must refuse, and it must write nothing.
        adventure_id = run_of(1)
        member = api.adventure_state(adventure_id)["members"][0]
        artifact = artifact_of(str(member["encounter_id"]))
        assert artifact.is_file()
        artifact.unlink()

        with pytest.raises(RequestError, match="has no finalized replay"):
            api.adventure_replay(adventure_id)

        assert not replays_root().exists() or not list(replays_root().glob("*.json"))

    def test_a_member_artifact_that_is_not_json_is_refused_not_embedded(self) -> None:
        adventure_id = run_of(1)
        member = api.adventure_state(adventure_id)["members"][0]
        artifact = artifact_of(str(member["encounter_id"]))
        artifact.write_text("{ this is not json", encoding="utf-8")

        with pytest.raises(RequestError, match="is not valid JSON"):
            api.adventure_replay(adventure_id)

    def test_a_run_with_no_encounters_has_nothing_to_compose(self) -> None:
        # Refused rather than answered with an empty envelope: a replay of no
        # fights is a file that says nothing, and the validator refuses one for
        # the same reason.
        api.adventure_create("The Sunless Citadel")

        with pytest.raises(
            RequestError, match="adventure 'adv-1' has no encounters to compose"
        ):
            api.adventure_replay("adv-1")

    def test_an_unknown_adventure_names_what_is_actually_there(self) -> None:
        api.adventure_create("The Sunless Citadel")

        with pytest.raises(
            NotFoundError, match="no adventure 'adv-9'; adventures here: adv-1"
        ):
            api.adventure_replay("adv-9")

    def test_a_chapter_the_validator_refuses_stops_the_write(self) -> None:
        # The self-check: the envelope goes through the same validator the
        # ``replay.validate`` route would run before a byte is published, so a
        # corrupted member artifact cannot reach disk inside a run's replay.
        adventure_id = run_of(1)
        member = api.adventure_state(adventure_id)["members"][0]
        artifact = artifact_of(str(member["encounter_id"]))
        broken = json.loads(artifact.read_text(encoding="utf-8"))
        del broken["events"]
        artifact.write_text(json.dumps(broken), encoding="utf-8")

        with pytest.raises(ReplayError, match="is not playable") as refusal:
            api.adventure_replay(adventure_id)

        assert any(
            diagnostic["path"].startswith("chapters.0.replay.")
            for diagnostic in refusal.value.diagnostics
        )
        assert not replays_root().exists() or not list(replays_root().glob("*.json"))


class TestTheEnvelopeValidator:
    """Order, membership, and the integrity block. The chapters check themselves."""

    def test_a_composed_envelope_validates(self) -> None:
        envelope = composed(run_of(2))

        assert replay_service.validate_adventure_replay(envelope) == []
        assert api.replay_validate(envelope) == {
            "valid": True, "error_count": 0, "diagnostics": []
        }

    def test_the_diagnostics_are_the_same_small_shape_a_replays_are(self) -> None:
        broken = composed(run_of(1))
        broken["format"] = "fivee-sim-replay"

        diagnostics = replay_service.validate_adventure_replay(broken)

        assert diagnostics
        assert all(set(one) == {"path", "problem"} for one in diagnostics)
        assert {one["path"] for one in diagnostics} >= {"format"}

    def test_something_that_is_not_an_object_at_all_is_one_diagnostic(self) -> None:
        assert replay_service.validate_adventure_replay([1, 2]) == [
            {"path": "$", "problem": "must be an object"}
        ]

    def test_a_format_version_this_engine_does_not_read_is_named(self) -> None:
        envelope = composed(run_of(1))
        envelope["format_version"] = 2

        paths = {one["path"] for one in replay_service.validate_adventure_replay(envelope)}

        assert "format_version" in paths

    def test_chapters_out_of_order_are_named_at_the_chapter_that_moved(self) -> None:
        envelope = composed(run_of(2))
        envelope["chapters"] = list(reversed(envelope["chapters"]))

        paths = {one["path"] for one in replay_service.validate_adventure_replay(envelope)}

        assert "chapters.0.index" in paths
        assert "chapters.1.index" in paths

    def test_a_repeated_chapter_index_is_named(self) -> None:
        envelope = composed(run_of(2))
        envelope["chapters"][1]["index"] = 0

        paths = {one["path"] for one in replay_service.validate_adventure_replay(envelope)}

        assert "chapters.1.index" in paths

    def test_one_encounter_appearing_twice_is_named(self) -> None:
        # Distinct from the index check: two chapters can be numbered 0 and 1
        # and still be the same fight, which would make the run's own history
        # disagree with the party the carry-over says walked between them.
        envelope = composed(run_of(2))
        envelope["chapters"][1]["encounter_id"] = envelope["chapters"][0]["encounter_id"]

        problems = {
            one["path"]: one["problem"]
            for one in replay_service.validate_adventure_replay(envelope)
        }

        assert "chapters.1.encounter_id" in problems
        assert "repeats" in problems["chapters.1.encounter_id"]

    def test_an_envelope_with_no_chapters_is_refused(self) -> None:
        envelope = composed(run_of(1))
        envelope["chapters"] = []

        problems = {
            one["path"]: one["problem"]
            for one in replay_service.validate_adventure_replay(envelope)
        }

        assert "at least one" in problems["chapters"]

    @pytest.mark.parametrize(
        ("field", "value", "path"),
        [
            ("recovery", [], "chapters.1.recovery"),
            ("recovery", {"Thora": []}, "chapters.1.recovery.Thora"),
            ("recovery_note", "", "chapters.1.recovery_note"),
        ],
    )
    def test_malformed_recovery_metadata_is_named_at_its_chapter(
        self, field: str, value: object, path: str
    ) -> None:
        envelope = composed(run_of(2))
        envelope["chapters"][1][field] = value

        paths = {
            one["path"] for one in replay_service.validate_adventure_replay(envelope)
        }

        assert path in paths

    def test_a_recovery_note_without_a_recovery_is_not_a_boundary(self) -> None:
        envelope = composed(run_of(2))
        envelope["chapters"][1]["recovery_note"] = "Long rest"

        problems = {
            one["path"]: one["problem"]
            for one in replay_service.validate_adventure_replay(envelope)
        }

        assert "requires recovery" in problems["chapters.1.recovery_note"]

    def test_the_first_chapter_cannot_claim_a_preceding_recovery(self) -> None:
        envelope = composed(run_of(2))
        envelope["chapters"][0]["recovery"] = {}

        problems = {
            one["path"]: one["problem"]
            for one in replay_service.validate_adventure_replay(envelope)
        }

        assert "first chapter" in problems["chapters.0.recovery"]

    def test_a_chapter_whose_own_bundle_is_broken_is_named_under_that_chapter(
        self,
    ) -> None:
        envelope = composed(run_of(2))
        del envelope["chapters"][1]["replay"]["latest_state"]

        paths = {one["path"] for one in replay_service.validate_adventure_replay(envelope)}

        assert "chapters.1.replay.latest_state" in paths
        assert not any(path.startswith("chapters.0.replay.") for path in paths), (
            "the intact chapter must not be blamed for its neighbour"
        )

    def test_every_hash_the_declaration_names_is_actually_verified(self) -> None:
        # Derived from ``ADVENTURE_INTEGRITY_KEYS`` rather than written out: a
        # block added to the composer's hash list and not to the validator's
        # checks is the drift this loop exists to catch, and a per-key case
        # would miss the next one.
        envelope = composed(run_of(2))
        assert replay_service.ADVENTURE_INTEGRITY_KEYS, "nothing to check"

        for key in replay_service.ADVENTURE_INTEGRITY_KEYS:
            tampered = deepcopy(envelope)
            tampered[key] = altered(tampered[key])

            paths = {
                one["path"]
                for one in replay_service.validate_adventure_replay(tampered)
            }

            assert f"integrity.{key}" in paths, key

    def test_an_integrity_block_that_is_not_there_is_named(self) -> None:
        envelope = composed(run_of(1))
        del envelope["integrity"]

        paths = {one["path"] for one in replay_service.validate_adventure_replay(envelope)}

        assert "integrity" in paths

    def test_another_digest_algorithm_is_refused_rather_than_trusted(self) -> None:
        envelope = composed(run_of(1))
        envelope["integrity"]["algorithm"] = "md5"

        paths = {one["path"] for one in replay_service.validate_adventure_replay(envelope)}

        assert "integrity.algorithm" in paths


class TestTheValidateRouteDispatches:
    """One route, two formats, chosen by what the document says it is."""

    def test_an_adventure_envelope_goes_to_the_envelope_validator(self) -> None:
        envelope = composed(run_of(2))

        # Not vacuous: the replay validator refuses this document outright,
        # because an envelope carries none of the fields its prefix demands. So
        # a route that did not dispatch would answer `valid: false` here.
        assert replay_service.validate_replay(envelope)
        assert api.replay_validate(envelope)["valid"] is True

    def test_a_replay_bundle_still_goes_to_the_replay_validator(self) -> None:
        adventure_id = run_of(1)
        member = api.adventure_state(adventure_id)["members"][0]
        bundle = json.loads(
            artifact_of(str(member["encounter_id"])).read_text(encoding="utf-8")
        )

        assert api.replay_validate(bundle)["valid"] is True
        del bundle["events"]
        assert api.replay_validate(bundle)["valid"] is False


class TestTheGlobInTheAdventuresRoot:
    """``adv-*.json`` is a wider net than an adventure id, and an envelope is not one.

    A root of their own settled the other half of this — a journal cannot be
    mistaken for an adventure because no listing reaches both — and settled
    none of this one: ``adventure.replay`` writes wherever the caller names,
    and this directory is a perfectly reasonable place to name.
    """

    def test_composing_leaves_no_corrupt_adventure_in_the_listing(self) -> None:
        adventure_id = run_of(1)

        api.adventure_replay(adventure_id)

        listed = api.adventure_list("all")["adventures"]
        assert [entry["adventure_id"] for entry in listed] == [adventure_id]
        assert [entry["status"] for entry in listed] == ["active"]

    def test_an_envelope_written_among_the_adventures_is_not_read_as_one(
        self,
    ) -> None:
        # The trap the default output location sidesteps and an explicit path
        # walks straight into: ``adv-1.replay.json`` matches ``adv-*.json``, so
        # a listing that trusted the glob would report the run's own replay as a
        # corrupt adventure. The id grammar is what decides, not the glob.
        adventure_id = run_of(1)

        api.adventure_replay(
            adventure_id, path=str(adventures_root() / f"{adventure_id}.replay.json")
        )

        listed = api.adventure_list("all")["adventures"]
        assert [entry["adventure_id"] for entry in listed] == [adventure_id]
        assert not any(entry["status"] == "corrupt" for entry in listed)

    def test_the_run_does_not_name_its_own_replay_as_a_neighbouring_adventure(
        self,
    ) -> None:
        # The same listing answers "adventures here", so a file the glob wrongly
        # claimed would be offered to a lost caller as somewhere else to look.
        # Asserted on the refusal's own words because that is where a reader
        # meets the listing, and a count would pass against a name it invented.
        adventure_id = run_of(1)
        api.adventure_replay(
            adventure_id, path=str(adventures_root() / f"{adventure_id}.replay.json")
        )

        with pytest.raises(
            NotFoundError, match="no adventure 'adv-9'; adventures here: adv-1$"
        ):
            api.adventure_state("adv-9")


class TestARunOfWalksAndFights:
    """A chapter says which kind it is, and the envelope's copy is the artifact's.

    Until the bundle carried its mode, an adventure with an interlude in it
    could not compose at all: ``compose_replay`` validates before it publishes,
    and every interlude bundle failed at four ``turn`` paths. So the first case
    here is the one that could not happen, and the rest are about the field that
    made it possible not becoming a second declaration of the same fact.
    """

    def test_a_run_of_a_walk_and_a_fight_composes_and_validates(self) -> None:
        adventure_id = run_of_a_walk_then_a_fight()

        envelope = composed(adventure_id)

        assert replay_service.validate_adventure_replay(envelope) == []
        assert api.replay_validate(envelope) == {
            "valid": True, "error_count": 0, "diagnostics": []
        }
        # Not vacuous: the interlude's own bundle is the half that used to be
        # refused, and it is refused for its ``turn`` paths and nothing else.
        interlude = envelope["chapters"][0]["replay"]
        assert interlude["encounter"]["mode"] == "exploration"
        assert interlude["latest_state"]["turn"] is None
        assert replay_service.validate_replay(interlude) == []

    def test_each_chapter_says_which_kind_it_was_in_the_order_they_were_linked(
        self,
    ) -> None:
        envelope = composed(run_of_a_walk_then_a_fight())

        assert [chapter["mode"] for chapter in envelope["chapters"]] == [
            "exploration", "combat"
        ]

    def test_a_chapters_mode_is_the_frozen_bundles_and_not_the_documents(self) -> None:
        # ``_frozen_bundle`` already reads the artifact, so the chapter's copy
        # comes from there rather than from the run's own record of the link.
        # Proved by making the two disagree: the document is edited on disk to
        # call the interlude a fight, and the composed envelope still says what
        # the artifact says. Re-deriving is exactly what chapter freezing exists
        # to prevent, and this is the same rule applied to one field.
        adventure_id = run_of_a_walk_then_a_fight()
        path = adventures.adventure_path(adventure_id)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["members"][0]["mode"] = "combat"
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

        envelope = composed(adventure_id)

        assert api.adventure_state(adventure_id)["members"][0]["mode"] == "combat"
        assert envelope["chapters"][0]["mode"] == "exploration"
        assert replay_service.validate_adventure_replay(envelope) == []

    def test_a_chapter_mode_the_model_never_declared_is_named_at_that_chapter(
        self,
    ) -> None:
        envelope = composed(run_of_a_walk_then_a_fight())
        envelope["chapters"][1]["mode"] = "wandering"

        problems = {
            one["path"]: one["problem"]
            for one in replay_service.validate_adventure_replay(envelope)
        }

        assert "chapters.1.mode" in problems
        assert "combat, exploration" in problems["chapters.1.mode"]

    def test_a_chapter_that_disagrees_with_the_bundle_it_carries_is_named(self) -> None:
        # The two-declarations defect, closed where it would appear. The
        # envelope's own summary of a run and the artifact it sits beside are
        # the same fact written twice, and a reader that trusted the summary
        # would draw an initiative order for a chapter that never rolled one.
        envelope = composed(run_of_a_walk_then_a_fight())
        envelope["chapters"][0]["mode"] = "combat"

        problems = {
            one["path"]: one["problem"]
            for one in replay_service.validate_adventure_replay(envelope)
        }

        assert "chapters.0.mode" in problems
        assert "its own replay" in problems["chapters.0.mode"]

    def test_a_chapter_older_than_the_field_is_read_rather_than_refused(self) -> None:
        # An envelope composed before chapters said which kind they were is
        # still a playable record of a run of fights, and every chapter in one
        # is a fight. Refusing the key's absence would make every adventure
        # replay already on a user's disk invalid at the version that added it.
        envelope = composed(run_of(2))
        for chapter in envelope["chapters"]:
            del chapter["mode"]
            del chapter["replay"]["encounter"]["mode"]
        envelope["integrity"]["chapters"] = replay_service.canonical_sha256(
            envelope["chapters"]
        )

        assert replay_service.validate_adventure_replay(envelope) == []
