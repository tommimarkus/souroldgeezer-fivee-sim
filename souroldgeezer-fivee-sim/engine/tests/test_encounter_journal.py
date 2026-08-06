"""Durable encounter journals: recovery, discovery, finalization, and idempotency."""

from __future__ import annotations

import dataclasses
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from fivee_sim.content import BuiltinMode, ContentRegistry
from fivee_sim.kernel.grid import MovementMode
from fivee_sim.model.encounter import Action, ActionKind, ActionRecord
from fivee_sim.service import encounter_journal, specs
from fivee_sim.service import sessions as sessions_service
from fivee_sim.service.errors import RequestError

from . import api
from .conftest import (
    REPLAY_GOBLIN,
    REPLAY_HERO,
    advance_encounter_to,
    mapless_fight,
)


def journal_path(root: Path, encounter_id: str) -> Path:
    return root / f"{encounter_id}.jsonl"


def records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _conditions_of(state: dict[str, object], name: str) -> list[str]:
    combatants = state["combatants"]
    assert isinstance(combatants, list)
    return list(next(c for c in combatants if c["name"] == name)["conditions"])


def _items_of(state: dict[str, object], name: str) -> dict[str, int]:
    combatants = state["combatants"]
    assert isinstance(combatants, list)
    return dict(next(c for c in combatants if c["name"] == name)["items"])


def test_creation_and_each_attempt_and_result_are_durably_hash_chained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=101)

    api.encounter_advance(encounter_id, request_id="turn-1")

    saved = records(journal_path(root, encounter_id))
    assert [entry["kind"] for entry in saved] == ["creation", "attempt", "result"]
    previous = ""
    for entry in saved:
        assert entry["previous_sha256"] == previous
        assert len(str(entry["sha256"])) == 64
        previous = str(entry["sha256"])


def test_concurrent_appends_preserve_one_verified_hash_chain() -> None:
    encounter_id = mapless_fight(seed=100)
    writers = 12
    ready = Barrier(writers)

    def add_note(index: int) -> None:
        ready.wait()
        encounter_journal.append(
            encounter_id,
            {
                "kind": "note",
                "timestamp": f"2026-08-01T12:00:{index:02d}Z",
                "index": index,
            },
        )

    with ThreadPoolExecutor(max_workers=writers) as pool:
        list(pool.map(add_note, range(writers)))

    saved, warning = encounter_journal.read(encounter_id)
    assert warning is None
    assert len(saved) == writers + 1


def test_creation_request_ids_are_idempotent_even_after_memory_loss() -> None:
    first = api.encounter_create(
        [dict(REPLAY_HERO), dict(REPLAY_GOBLIN)], seed=102, request_id="create-duel"
    )
    api.STATE.sessions.clear()

    second = api.encounter_create(
        [dict(REPLAY_HERO), dict(REPLAY_GOBLIN)], seed=999, request_id="create-duel"
    )

    assert second["encounter_id"] == first["encounter_id"]
    assert second["seed"] == 102


def test_a_repeated_request_id_returns_the_original_result_without_acting_twice() -> None:
    encounter_id = mapless_fight(seed=103)

    first = api.encounter_advance(encounter_id, request_id="same-turn")
    second = api.encounter_advance(encounter_id, request_id="same-turn")

    assert second == first
    assert api.encounter_log(encounter_id)["total_actions"] == 1


def test_a_refused_action_is_part_of_the_audit_record() -> None:
    encounter_id = mapless_fight(seed=107)

    with pytest.raises(RequestError, match="needs a target"):
        api.encounter_act(encounter_id, "attack", request_id="bad-attack")

    bundle = api.replay_export(encounter_id, format_version=2)["bundle"]
    assert bundle["attempts"][-1]["status"] == "refused"
    assert bundle["attempts"][-1]["request_id"] == "bad-attack"
    assert "needs a target" in bundle["attempts"][-1]["error"]


def test_an_active_encounter_recovers_after_process_memory_is_lost() -> None:
    encounter_id = mapless_fight(seed=109)
    api.encounter_advance(encounter_id, request_id="turn-1")
    before = api.encounter_state(encounter_id)
    api.STATE.sessions.clear()

    recovered = api.encounter_resume(encounter_id)

    assert recovered["recovered"] is True
    assert recovered["state"] == before
    assert api.encounter_state(encounter_id) == before


def spec_map() -> dict[str, Any]:
    """An inline battle-map **spec**, saying as much as a spec can say.

    ``rows``/``legend`` and ``terrain`` are alternatives, so no one spec carries
    both; every other key in :data:`specs.MAP_KEYS` is here, and the features
    exercise every key in :data:`specs.FEATURE_KEYS` — including the pair a
    linked door needs, which is what makes the door hang somewhere rather than
    nowhere.
    """
    return {
        "name": "gatehouse",
        "width": 8,
        "height": 6,
        "default_terrain": "normal",
        "rows": [
            "........",
            "........",
            "........",
            "........",
            "###..###",
            "........",
        ],
        "legend": {".": "normal", "#": "wall"},
        "default_elevation": 0,
        "elevation": [[7, 5, 10]],
        "features": [
            {
                "name": "gate",
                "square": [6, 1],
                "kind": "door",
                "orientation": "vertical",
                "initially_open": False,
                "closed_terrain": "door-closed",
                "open_terrain": "door-open",
            },
            {
                "name": "hall-door-west",
                "square": [3, 4],
                "kind": "door",
                "orientation": "horizontal",
                "initially_open": False,
                "linked_to": "hall-door-east",
            },
            {
                "name": "hall-door-east",
                "square": [4, 4],
                "kind": "door",
                "orientation": "horizontal",
                "initially_open": False,
                "linked_to": "hall-door-west",
            },
        ],
    }


def test_a_fight_created_from_an_inline_map_spec_recovers_from_its_journal() -> None:
    # The journal captures an inline spec as a map *document*, and recovery
    # parses that document back. Anything the spec cannot express is therefore
    # not merely unavailable to the fight — it is missing from the captured
    # document, and a door with no orientation is a document the parser refuses.
    # So a spec key the fight never reads can still be the difference between a
    # fight that survives a dev reload and one that is lost.
    created = api.encounter_create(
        [dict(REPLAY_HERO), dict(REPLAY_GOBLIN)], seed=149, map=spec_map()
    )
    encounter_id = str(created["encounter_id"])
    before = deepcopy(api.encounter_state(encounter_id)["map"])
    assert before["features"]["hall-door-west"]["linked_to"] == "hall-door-east"

    api.STATE.sessions.clear()

    assert api.encounter_state(encounter_id)["map"] == before


def test_a_ruling_condition_survives_the_journal_and_replays_on_resume() -> None:
    # A ruling changes the fight, so recovery has to replay it as it replays an
    # action. Journalled and not replayed, the resume would quietly hand back a
    # creature without the condition — the state would look plausible and be
    # wrong, which is the worst shape a recovery bug can take.
    encounter_id = mapless_fight(seed=131)
    api.encounter_condition(encounter_id, "Thora", "poisoned", request_id="ruling-1")
    before = api.encounter_state(encounter_id)
    # State equality alone cannot tell a faithful replay from a ruling that
    # never applied: both sides would be conditionless and equal. Pin that the
    # condition is actually there before asking whether it survives.
    assert _conditions_of(before, "Thora") == ["poisoned"]
    api.STATE.sessions.clear()

    recovered = api.encounter_resume(encounter_id)

    assert recovered["recovered"] is True
    assert recovered["state"] == before
    assert _conditions_of(recovered["state"], "Thora") == ["poisoned"]


def test_a_lifted_ruling_condition_replays_as_lifted() -> None:
    # The other half: replaying only the apply would leave the condition on.
    encounter_id = mapless_fight(seed=137)
    api.encounter_condition(encounter_id, "Thora", "poisoned", request_id="ruling-on")
    assert _conditions_of(api.encounter_state(encounter_id), "Thora") == ["poisoned"]
    api.encounter_condition(
        encounter_id, "Thora", "poisoned", applied=False, request_id="ruling-off"
    )
    before = api.encounter_state(encounter_id)
    api.STATE.sessions.clear()

    recovered = api.encounter_resume(encounter_id)

    assert recovered["state"] == before
    assert _conditions_of(recovered["state"], "Thora") == []


def test_a_leveled_pack_condition_reaches_level_three_and_survives_the_journal(
) -> None:
    # Acceptance check for the level machinery (srd-parity T10b): a
    # pack-declared cumulative condition — never an SRD one — reaches level 3
    # through three separate impositions, and that level survives
    # encounter.state and a resume-from-journal round trip.
    pack = Path(__file__).parent / "packs" / "01-ashfall-reach.json"
    api.content_configure([str(pack)], add=True)
    encounter_id = mapless_fight(seed=151)

    for index in range(3):
        api.encounter_condition(
            encounter_id, "Thora", "ashfall-ember-marked",
            request_id=f"mark-{index}",
        )

    before = api.encounter_state(encounter_id)
    thora = next(c for c in before["combatants"] if c["name"] == "Thora")
    assert thora["condition_levels"] == {"ashfall-ember-marked": 3}
    assert "ashfall-ember-marked" in thora["conditions"]
    api.STATE.sessions.clear()

    recovered = api.encounter_resume(encounter_id)

    assert recovered["recovered"] is True
    assert recovered["state"] == before
    recovered_thora = next(
        c for c in recovered["state"]["combatants"] if c["name"] == "Thora"
    )
    assert recovered_thora["condition_levels"] == {"ashfall-ember-marked": 3}


def test_a_bonus_action_survives_the_journal_and_replays_on_resume() -> None:
    # The read side of the Action round-trip. encounters.act records
    # movement_mode and as_bonus_action in the journal, but action_from_journal
    # rebuilt neither, so a Dash taken as a bonus action came back as an
    # ordinary one — and the action that legitimately followed it was then
    # refused as a second action, out of a resume the caller cannot retry.
    hero = dict(REPLAY_HERO)
    hero["bonus_actions"] = ["dash"]
    encounter_id = str(
        api.encounter_create([hero, dict(REPLAY_GOBLIN)], seed=115)["encounter_id"]
    )
    api.encounter_act(encounter_id, "dash", as_bonus_action=True, request_id="ba-dash")
    api.encounter_act(encounter_id, "dodge", request_id="then-dodge")
    before = api.encounter_state(encounter_id)
    api.STATE.sessions.clear()

    recovered = api.encounter_resume(encounter_id)

    assert recovered["recovered"] is True
    assert recovered["state"] == before


def test_every_action_field_survives_the_journal_round_trip() -> None:
    # The trap the two hand-written lists set for each other. ``Action.as_dict``
    # names every optional scalar to write; ``action_from_journal`` names every
    # one to read; and a field added to the dataclass but missed by either is
    # dropped silently from a record that promises to replay the call exactly.
    # That is how ``to_level`` went missing from cross-storey moves, and how
    # ``movement_mode`` and ``as_bonus_action`` were missing from the read side
    # until this test was written. Derived from the dataclass rather than
    # restated, so the next field added is covered without touching this test.
    every_field = Action(
        kind=ActionKind.MOVE,
        target="Goblin", attack="Longsword", item="Potion", spell="Firebolt",
        slot_level=2, to_position=(3, 4), targets=("Goblin", "Thora"),
        center=(5, 6), path=((1, 1), (2, 2)), direction=(0, -1), toward="Goblin",
        feature="door-1", set_open=True, to_level=1,
        movement_mode=MovementMode.FLY, as_bonus_action=True,
    )
    recorded = ActionRecord(
        index=0, round=1, actor="Thora", action=every_field,
        first_event=0, event_count=0,
    ).as_dict()["action"]

    rebuilt = specs.action_from_journal(recorded)

    for field in dataclasses.fields(Action):
        assert getattr(rebuilt, field.name) == getattr(every_field, field.name), (
            f"{field.name} did not survive the journal round trip: add it to "
            f"Action.as_dict, to action_from_journal, or to both"
        )


def test_an_attempt_interrupted_before_its_result_is_audited_and_safe_to_retry() -> None:
    encounter_id = mapless_fight(seed=110)
    before = api.encounter_state(encounter_id)
    encounter_journal.append(
        encounter_id,
        {
            "kind": "attempt",
            "timestamp": "2026-08-01T12:00:00Z",
            "index": 0,
            "operation": "encounter_advance",
            "request_id": "interrupted-turn",
            "arguments": {},
        },
    )
    api.STATE.sessions.clear()

    api.encounter_resume(encounter_id)
    interrupted = api.replay_export(encounter_id, format_version=2)["bundle"][
        "attempts"
    ][-1]
    retried = api.encounter_advance(encounter_id, request_id="interrupted-turn")

    assert interrupted["status"] == "interrupted"
    assert api.encounter_state(encounter_id) != before
    assert retried["state"]["turn"] == api.encounter_state(encounter_id)["turn"]
    assert api.encounter_log(encounter_id)["total_actions"] == 1


def test_a_recovered_fight_reproduces_the_ammunition_the_shot_spent() -> None:
    # A quiver is only ever *derived*: nothing writes the count to the journal,
    # so recovery reproduces it by replaying the shot through the stepper. That
    # makes the arrow a live check on the whole chain — the creation payload
    # carrying ``ammunition`` and ``items``, ``attack_from_spec`` reading them
    # back, and the stepper spending one on the replayed action. Any link
    # missing and the recovered archer stands there with a full quiver.
    archer = {
        "name": "Sylvi",
        "team": "party",
        "ac": 14,
        "max_hp": 20,
        "position": [0, 0],
        "items": {"Arrow": 3},
        "attacks": [
            {
                "name": "Shortbow",
                "attack_bonus": 5,
                "damage": "1d6+3",
                "damage_type": "piercing",
                "kind": "ranged",
                "normal_range": 80,
                "long_range": 320,
                "ammunition": "Arrow",
            }
        ],
    }
    encounter_id = str(
        api.encounter_create([archer, dict(REPLAY_GOBLIN)], seed=137)["encounter_id"]
    )
    advance_encounter_to(encounter_id, "Sylvi")
    api.encounter_act(encounter_id, "attack", target="Goblin", attack="Shortbow")
    before = api.encounter_state(encounter_id)
    api.STATE.sessions.clear()

    recovered = api.encounter_resume(encounter_id)

    assert _items_of(before, "Sylvi") == {"Arrow": 2}
    assert _items_of(recovered["state"], "Sylvi") == {"Arrow": 2}
    assert recovered["state"] == before


def test_recovery_uses_captured_content_when_the_live_registry_has_changed() -> None:
    encounter_id = mapless_fight(seed=111)
    before = api.encounter_state(encounter_id)
    api.STATE.sessions.clear()
    api.STATE.content = sessions_service.Content(
        registry=ContentRegistry(builtin=BuiltinMode.EXCLUDE), generation=9
    )

    recovered = api.encounter_resume(encounter_id)

    assert recovered["state"] == before


def test_discovery_and_idempotent_finalization_keep_the_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=113)
    assert [entry["encounter_id"] for entry in api.encounter_list()["encounters"]] == [
        encounter_id
    ]

    first = api.encounter_finalize(encounter_id)
    second = api.encounter_finalize(encounter_id)

    assert second == first
    assert Path(first["replay_path"]).is_file()
    assert journal_path(root, encounter_id).is_file()
    assert api.encounter_list(status="finalized")["encounters"][0]["encounter_id"] == encounter_id


def test_finalization_is_idempotent_after_process_memory_is_lost() -> None:
    encounter_id = mapless_fight(seed=117)
    first = api.encounter_finalize(encounter_id)
    api.STATE.sessions.clear()

    second = api.encounter_finalize(encounter_id)

    assert second == first
    assert Path(second["replay_path"]).read_bytes()


def test_a_partial_tail_is_preserved_and_the_valid_prefix_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=127)
    path = journal_path(root, encounter_id)
    with path.open("ab") as handle:
        handle.write(b'{"partial"')
    api.STATE.sessions.clear()

    result = api.encounter_resume(encounter_id)

    tail = Path(result["recovery_warning"]["preserved_tail"])
    assert tail.read_bytes() == b'{"partial"'
    assert result["state"]["round"] == 1
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["kind"] == "creation"


def test_hash_chain_tampering_is_refused_instead_of_silently_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=149)
    path = journal_path(root, encounter_id)
    line = json.loads(path.read_text(encoding="utf-8"))
    line["seed"] = 150
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    api.STATE.sessions.clear()

    with pytest.raises(RequestError, match="invalid sha256"):
        api.encounter_resume(encounter_id)


class TestAJournalThatWillNotRebuildIsRefusedRatherThanRaised:
    """Recovery re-runs the whole of ``Encounter.__init__``, refusals included.

    ``create`` wraps that same call and translates its ``EncounterError`` into a
    ``RequestError``; ``recover_session`` called it bare, so the identical
    refusal arrived at ``web/http_server.py``'s final ``except Exception`` as a
    500 with a traceback in the log instead of problem+json. It is reached from
    ``sessions.session_for``, which every operation on an encounter goes
    through, so the fight becomes unopenable rather than merely unrecoverable.

    **What this is not.** No single well-formed API call reaches it today:
    ``create`` refuses both of these documents before a journal exists, and a
    journal written by this build recovers under the rules that wrote it. What
    reaches it is a journal this build did not write — one repaired by hand,
    one written by a different build whose rules have since moved (the
    documented ``FIVEE_SIM_RELOAD`` workflow re-derives a fight under new
    code), or one shared with another engine on the same encounters root. The
    journals below are built the way those arrive: a real creation record,
    amended, appended under an id of its own.
    """

    def rebuilt(
        self, root: Path, encounter_id: str, amended: dict[str, Any]
    ) -> str:
        """A second journal holding one real creation record, amended."""
        recorded = "enc-9001"
        amended["encounter_id"] = recorded
        encounter_journal.append(recorded, amended)
        assert journal_path(root, recorded).exists()
        return recorded

    def test_a_roster_the_fight_refuses_arrives_as_a_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "journal"
        monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
        source = mapless_fight(seed=131)
        creation = deepcopy(records(journal_path(root, source))[0])
        combatants = creation["combatants"]
        assert isinstance(combatants, list) and len(combatants) == 2
        combatants[1]["name"] = combatants[0]["name"]

        recorded = self.rebuilt(root, source, creation)

        with pytest.raises(
            RequestError,
            match=r"cannot recover 'enc-9001''s fight: combatant names must be "
            r"unique; duplicated: Thora",
        ):
            api.encounter_resume(recorded)

    def test_a_map_rule_broken_by_the_roster_arrives_as_a_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The other half of ``__init__``'s validation, and the half a roster
        # alone cannot express: placement is checked against the map, so this
        # refusal comes out of ``_adopt_map`` rather than the name check.
        root = tmp_path / "journal"
        monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
        source = str(
            api.encounter_create(
                [dict(REPLAY_HERO), dict(REPLAY_GOBLIN)], seed=151, map=spec_map()
            )["encounter_id"]
        )
        creation = deepcopy(records(journal_path(root, source))[0])
        combatants = creation["combatants"]
        assert isinstance(combatants, list) and len(combatants) == 2
        combatants[1]["position"] = list(combatants[0]["position"])

        recorded = self.rebuilt(root, source, creation)

        with pytest.raises(
            RequestError,
            match=r"cannot recover 'enc-9001''s fight: .* both start in square",
        ):
            api.encounter_resume(recorded)


#: An interlude's roster: two of the party, nobody opposing them. What makes it
#: a chapter rather than a fight is the mode, and the mode is the thing every
#: case below is really about — it is written in exactly one place, the creation
#: record, and everything a recovered interlude can still do depends on it
#: being read back.
INTERLUDE_PARTY: list[dict[str, object]] = [
    {"name": "Kettle", "team": "party", "ac": 13, "max_hp": 9, "position": [0, 0]},
    {"name": "Thora", "team": "party", "ac": 16, "max_hp": 30, "position": [30, 0]},
]


def test_an_interlude_records_its_mode_in_the_creation_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one place the mode is durable, and what the rest of this file rests on.

    A journal that did not say which kind of chapter it was would recover as a
    fight — and a fight refuses every act an interlude records, so the failure
    would arrive as a refusal about initiative rather than as anything naming
    the missing field.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = str(
        api.encounter_create(
            [dict(one) for one in INTERLUDE_PARTY], seed=211, mode="exploration"
        )["encounter_id"]
    )

    saved = records(journal_path(root, encounter_id))

    assert saved[0]["kind"] == "creation"
    assert saved[0]["mode"] == "exploration"


def test_a_fight_records_the_mode_it_never_had_to_ask_for() -> None:
    encounter_id = mapless_fight(seed=213)
    assert api.encounter_state(encounter_id)["mode"] == "combat"


def test_an_act_in_an_interlude_records_the_actor_it_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = str(
        api.encounter_create(
            [dict(one) for one in INTERLUDE_PARTY], seed=217, mode="exploration"
        )["encounter_id"]
    )

    api.encounter_act(encounter_id, "move", to_position=[10, 0], actor="Kettle")

    acted = [
        entry for entry in records(journal_path(root, encounter_id))
        if entry["kind"] == "result"
    ]
    assert [entry["arguments"]["actor"] for entry in acted] == ["Kettle"]  # type: ignore[index]


def test_a_recovered_interlude_still_takes_its_next_beat() -> None:
    """The sharpest edge in the phase: recovery is how a chapter survives a reload.

    Replay is where an interlude and a fight differ most — every recorded act
    names its actor, and a recovered chapter that came back as a fight would
    refuse all of them. The last line is what makes this more than a recovery
    test: a chapter that recovers and then cannot be *played* is recovered in
    name only, and the refusal a caller would see says nothing about a journal.
    """
    encounter_id = str(
        api.encounter_create(
            [dict(one) for one in INTERLUDE_PARTY], seed=223, mode="exploration"
        )["encounter_id"]
    )
    api.encounter_act(encounter_id, "move", to_position=[10, 0], actor="Kettle")
    before = api.encounter_state(encounter_id)
    api.STATE.sessions.clear()

    recovered = api.encounter_resume(encounter_id)

    assert recovered["recovered"] is True
    assert recovered["state"] == before
    after = api.encounter_act(
        encounter_id, "move", to_position=[20, 0], actor="Kettle"
    )
    kettle = next(
        one for one in after["state"]["combatants"] if one["name"] == "Kettle"
    )
    assert kettle["position"] == [20, 0]


def test_a_recovered_interlude_still_stands_on_its_own_map() -> None:
    """The combination that actually ships: an interlude is a walk somewhere.

    The two cases either side of this one are mapless, and the ground is where
    an interlude's beats mean anything — a move that pays for terrain, a wall
    that refuses one. Recovery rebuilds the map from the payload the creation
    record captured, by the same path a fight uses, so this is the combination
    rather than a new mechanism; it is here because nothing else drives it and
    because the beat's own budget is what a recovered move would spend.
    """
    encounter_id = str(
        api.encounter_create(
            [dict(one) for one in INTERLUDE_PARTY],
            seed=229,
            mode="exploration",
            # Dimensions are in squares; positions are in feet. One row deep so
            # the wall at square 4 is a barrier rather than something to walk
            # around, which is what makes the refusal below the wall's and not
            # a movement budget's.
            map={
                "name": "mill floor",
                "width": 30,
                "height": 1,
                "default_terrain": "normal",
                "terrain": [{"kind": "wall", "squares": [[4, 0]]}],
            },
        )["encounter_id"]
    )
    api.encounter_act(encounter_id, "move", to_position=[10, 0], actor="Kettle")
    before = api.encounter_state(encounter_id)
    api.STATE.sessions.clear()

    recovered = api.encounter_resume(encounter_id)

    assert recovered["state"] == before
    assert recovered["state"]["map"]["name"] == "mill floor"
    # The ground still refuses what it refused: the wall at [4, 0] stands
    # between Kettle and the far side, and a recovered chapter that had lost
    # its map would happily walk through it.
    with pytest.raises(RequestError, match="no route"):
        api.encounter_act(encounter_id, "move", to_position=[25, 0], actor="Kettle")


def test_a_solo_interlude_recovers_from_a_journal_holding_one_combatant() -> None:
    """The arity rule reaches recovery too, and only the mode says which one.

    A journal is read back through the same spec translation that built it, so
    a solo chapter recovers only if the count rule knows which mode it is
    counting for. Without it the refusal is "an encounter needs at least two
    combatants" — a complaint about a roster nobody can now change.
    """
    encounter_id = str(
        api.encounter_create(
            [dict(INTERLUDE_PARTY[0])], seed=227, mode="exploration"
        )["encounter_id"]
    )
    before = api.encounter_state(encounter_id)
    api.STATE.sessions.clear()

    recovered = api.encounter_resume(encounter_id)

    assert recovered["state"] == before
