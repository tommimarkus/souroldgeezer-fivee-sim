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
