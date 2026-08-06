"""Durable encounter journals: recovery, discovery, finalization, and idempotency."""

from __future__ import annotations

import ast
import dataclasses
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from fivee_sim import __version__
from fivee_sim.content import BuiltinMode, ContentRegistry
from fivee_sim.kernel.grid import MovementMode
from fivee_sim.model.encounter import Action, ActionKind, ActionRecord
from fivee_sim.paths import SOURCE_ID_ENV
from fivee_sim.service import blobs, durable, encounter_journal, specs
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
    return root / encounter_id / "journal.jsonl"


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

    # ``view="full"`` on the first call, not on the retry: a retry is answered
    # whole whatever it asked for, so this is what puts the two in one shape.
    first = api.encounter_advance(encounter_id, request_id="same-turn", view="full")
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
    # The interrupted attempt never recorded a result, so this is a real turn
    # rather than a replay of one, and it answers ``delta`` like any other.
    retried = api.encounter_advance(
        encounter_id, request_id="interrupted-turn", view="full"
    )

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


def test_the_creation_record_names_the_source_this_launch_was_started_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Beside ``engine_version``, and in the creation record alone.

    The release number cannot tell two checkouts of one release apart, and that
    is precisely the ``FIVEE_SIM_RELOAD`` case a journal outlives. Whole rather
    than truncated, because the journal is the archive; the refusal that quotes
    it is the thing that shortens it.

    One record, not every record: this says which build *started* the fight,
    which is the only build whose rules the whole journal was written under. A
    per-record copy would be a second thing to keep in step for a value that
    cannot change without the process changing.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    monkeypatch.setenv(SOURCE_ID_ENV, "d" * 64)

    encounter_id = mapless_fight(seed=229)

    creation = records(journal_path(root, encounter_id))[0]
    assert creation["kind"] == "creation"
    assert creation["engine_version"] == __version__
    assert creation["source_id"] == "d" * 64


def test_a_launch_that_names_no_source_records_an_empty_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset is recorded as unset, not omitted and not guessed at.

    The launcher exports the digest only when it was asked to watch the source,
    so an ordinary run has none. The key is still written, because a reader
    telling "this build had no id" from "this journal predates the field" is
    the difference between a fact and an absence.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    monkeypatch.delenv(SOURCE_ID_ENV, raising=False)

    encounter_id = mapless_fight(seed=233)

    assert records(journal_path(root, encounter_id))[0]["source_id"] == ""


def test_a_journal_written_before_the_source_id_existed_recovers_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every journal on disk today lacks the key, and none of them may break.

    ``recover_session`` reads it with ``.get``, so an older creation record
    recovers to exactly the state it would have before the field existed. This
    is the case that would fail if the identity were ever promoted from a
    diagnostic into a precondition.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    monkeypatch.setenv(SOURCE_ID_ENV, "e" * 64)
    source = mapless_fight(seed=239)
    advance_encounter_to(source, "Thora")
    api.encounter_act(source, "attack", target="Goblin", attack="Longsword")
    expected = api.encounter_state(source)
    saved = deepcopy(records(journal_path(root, source)))
    assert saved[0].pop("source_id") == "e" * 64
    saved[0]["encounter_id"] = "enc-9001"
    rechained("enc-9001", saved)
    api.STATE.sessions.clear()

    resumed = api.encounter_resume("enc-9001")

    assert resumed["state"]["combatants"] == expected["combatants"]
    assert resumed["state"]["round"] == expected["round"]


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

    # -- the replay loop, which is the other half of recovery ----------------

    def test_an_act_this_build_refuses_names_the_record_it_stopped_at(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported case, and an ``EncounterError`` like the two above.

        Rebuilding the encounter is only the first thing recovery does; it then
        replays every recorded result through the same ``Encounter``. A rules
        change that makes a recorded action illegal lands here rather than in
        ``__init__``, and it used to escape the same way — untranslated, and
        out of the adapter as a 500.
        """
        root = tmp_path / "journal"
        monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
        saved = self.a_recorded_fight(root, seed=157)
        position = result_position(saved, "encounter_act")
        arguments = saved[position]["arguments"]
        assert isinstance(arguments, dict)
        del arguments["target"]

        recorded = self.replayed(root, saved)

        with pytest.raises(
            RequestError,
            match=rf"cannot recover 'enc-9001''s fight: record "
            rf"{saved[position]['index']} \(encounter_act, .+\) will not replay "
            rf"under this build: EncounterError: this action needs a target",
        ):
            api.encounter_resume(recorded)

    def test_a_condition_the_content_no_longer_defines_arrives_as_a_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The escape no ``ValueError`` clause catches.

        ``UnknownCondition`` is a ``KeyError``, so a handler written around the
        ``ValueError`` family — which is every other refusal the service layer
        raises — lets it straight through. ``encounters.condition`` names it
        explicitly for exactly that reason; the replay loop did not, and a pack
        that has since dropped a condition is the ordinary way to arrive here.
        """
        root = tmp_path / "journal"
        monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
        saved = self.a_recorded_fight(root, seed=163)
        position = result_position(saved, "encounter_condition")
        arguments = saved[position]["arguments"]
        assert isinstance(arguments, dict)
        arguments["condition"] = "bewildered"

        recorded = self.replayed(root, saved)

        with pytest.raises(
            RequestError,
            match=rf"cannot recover 'enc-9001''s fight: record "
            rf"{saved[position]['index']} \(encounter_condition, .+\) will not "
            rf"replay under this build: UnknownCondition: no condition named "
            rf"'bewildered'",
        ):
            api.encounter_resume(recorded)

    def test_an_advance_whose_recorded_faces_are_not_a_list_arrives_as_a_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The third replayed operation, and the ``TypeError`` route into it.

        Nothing that fails here is the engine call itself: the failure is in
        reading the recorded arguments back, which is where a journal written
        against a different argument shape breaks first. It is the same
        unrecoverable fight to a caller, so it is the same refusal.
        """
        root = tmp_path / "journal"
        monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
        saved = self.a_recorded_fight(root, seed=167)
        position = result_position(saved, "encounter_advance")
        arguments = saved[position]["arguments"]
        assert isinstance(arguments, dict)
        arguments["natural"] = 19

        recorded = self.replayed(root, saved)

        with pytest.raises(
            RequestError,
            match=rf"cannot recover 'enc-9001''s fight: record "
            rf"{saved[position]['index']} \(encounter_advance, .+\) will not "
            rf"replay under this build: TypeError: 'int' object is not iterable",
        ):
            api.encounter_resume(recorded)

    def test_an_action_kind_this_build_does_not_define_arrives_as_a_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plain ``ValueError``, raised before the encounter is even asked.

        ``specs.action_from_journal`` builds an ``ActionKind`` out of the
        recorded string, and an engine that has renamed or dropped one refuses
        there. That is a build difference rather than a rules difference, and
        the caller needs to be told the same thing either way.
        """
        root = tmp_path / "journal"
        monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
        saved = self.a_recorded_fight(root, seed=173)
        position = result_position(saved, "encounter_act")
        arguments = saved[position]["arguments"]
        assert isinstance(arguments, dict)
        arguments["kind"] = "parley"

        recorded = self.replayed(root, saved)

        with pytest.raises(
            RequestError,
            match=rf"cannot recover 'enc-9001''s fight: record "
            rf"{saved[position]['index']} \(encounter_act, .+\) will not replay "
            rf"under this build: ValueError: 'parley' is not a valid ActionKind",
        ):
            api.encounter_resume(recorded)

    def test_a_record_missing_an_argument_the_replay_reads_arrives_as_a_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bare ``KeyError``, which is the shape a hand-repaired record has."""
        root = tmp_path / "journal"
        monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
        saved = self.a_recorded_fight(root, seed=179)
        position = result_position(saved, "encounter_act")
        arguments = saved[position]["arguments"]
        assert isinstance(arguments, dict)
        del arguments["kind"]

        recorded = self.replayed(root, saved)

        with pytest.raises(
            RequestError,
            match=rf"cannot recover 'enc-9001''s fight: record "
            rf"{saved[position]['index']} \(encounter_act, .+\) will not replay "
            rf"under this build: KeyError: kind",
        ):
            api.encounter_resume(recorded)

    def test_the_refusal_says_the_journal_is_intact_and_names_both_remedies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The half of the sentence that exists to prevent the next failure.

        A caller told only "cannot replay past record 4" reaches for the
        journal and edits it, which is strictly worse than what was reported:
        every record after the edit fails its hash, ``encounter.list`` drops
        the fight to ``corrupt``, and nothing is preserved the way
        ``repair_partial`` preserves a crash tail. So the refusal has to say
        the file is intact, say not to edit it, and name the two things that
        do work — the build that wrote it, and reading the record out of the
        ``journal_path`` the listing already reports.
        """
        root = tmp_path / "journal"
        monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
        saved = self.a_recorded_fight(root, seed=181)
        position = result_position(saved, "encounter_act")
        arguments = saved[position]["arguments"]
        assert isinstance(arguments, dict)
        del arguments["target"]

        recorded = self.replayed(root, saved)

        with pytest.raises(
            RequestError, match="will not replay under this build"
        ) as refused:
            api.encounter_resume(recorded)

        refusal = str(refused.value)
        assert "The journal is intact and hash-valid; do not edit it" in refusal
        assert "Run the build that wrote it" in refusal
        assert "journal_path that encounter.list reports" in refusal

    def test_nothing_is_published_by_the_attempt_that_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal is clean and repeatable, which is what makes it safe.

        Recovery neither installs a partial session nor appends to the journal,
        so a second attempt fails exactly like the first rather than reporting a
        different record, and a caller that fixes its build gets the whole fight
        rather than whatever the first attempt left behind.
        """
        root = tmp_path / "journal"
        monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
        saved = self.a_recorded_fight(root, seed=191)
        position = result_position(saved, "encounter_act")
        arguments = saved[position]["arguments"]
        assert isinstance(arguments, dict)
        del arguments["target"]

        recorded = self.replayed(root, saved)
        before = journal_path(root, recorded).read_bytes()

        with pytest.raises(RequestError, match="will not replay under this build"):
            api.encounter_resume(recorded)
        with pytest.raises(RequestError, match="will not replay under this build"):
            api.encounter_resume(recorded)

        assert recorded not in api.STATE.sessions
        assert journal_path(root, recorded).read_bytes() == before

    def test_the_refusal_reaches_an_adventure_that_would_carry_the_fight_forward(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The case that forbids ever falling back to a partial session.

        ``adventures._carried_specs`` reaches ``session_for`` on the default
        carry path and writes what it finds into the *next* chapter's creation
        journal. A recovery that stopped at the last replayable record and
        served what it had would put a fight that never happened on disk, under
        a fresh hash chain, indistinguishable from one that did — which is the
        durable lie ``adventures`` already refuses to compose a replay out of.
        So the chapter boundary is refused too, and it is refused with the same
        sentence rather than a second one.
        """
        root = tmp_path / "journal"
        monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
        adventure_id = str(api.adventure_create("The Broken Build")["id"])
        first = api.adventure_encounter(
            adventure_id, combatants=[dict(REPLAY_HERO), dict(REPLAY_GOBLIN)], seed=197
        )
        encounter_id = str(first["encounter_id"])
        advance_encounter_to(encounter_id, "Thora")
        api.encounter_act(encounter_id, "attack", target="Goblin", attack="Longsword")

        # Amended in place, because it is the adventure's own member: the
        # chapter that has to be carried out of is the one on the document.
        saved = deepcopy(records(journal_path(root, encounter_id)))
        position = result_position(saved, "encounter_act")
        arguments = saved[position]["arguments"]
        assert isinstance(arguments, dict)
        del arguments["target"]
        journal_path(root, encounter_id).unlink()
        rechained(encounter_id, saved)
        api.STATE.sessions.clear()

        with pytest.raises(
            RequestError,
            match=rf"cannot recover {encounter_id!r}'s fight: record "
            rf"{saved[position]['index']} \(encounter_act, .+\) will not replay "
            rf"under this build: EncounterError: this action needs a target",
        ):
            api.adventure_encounter(adventure_id, seed=199)

    def test_the_refusal_names_the_build_that_wrote_it_and_the_one_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without this the refusal cannot be acted on, only read.

        "This build will not replay it" raises the question it does not answer:
        *which* build, and is this a second engine on the shared encounters
        root, a checkout the reload flag swapped underneath a live fight, or a
        record that was already wrong. ``engine_version`` was written and
        compared nowhere, and the source digest — the one thing that tells two
        checkouts of one release apart, which is exactly the ``FIVEE_SIM_RELOAD``
        case — was journaled nowhere at all.
        """
        root = tmp_path / "journal"
        monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
        monkeypatch.setenv(SOURCE_ID_ENV, "a" * 64)
        saved = self.a_recorded_fight(root, seed=193)
        position = result_position(saved, "encounter_act")
        arguments = saved[position]["arguments"]
        assert isinstance(arguments, dict)
        del arguments["target"]
        recorded = self.replayed(root, saved)
        monkeypatch.setenv(SOURCE_ID_ENV, "b" * 64)

        with pytest.raises(
            RequestError, match="will not replay under this build"
        ) as refused:
            api.encounter_resume(recorded)

        refusal = str(refused.value)
        assert f"recorded: engine {__version__}, source {'a' * 12}" in refusal
        assert f"running: engine {__version__}, source {'b' * 12}" in refusal

    def test_a_launch_watching_no_source_says_so_rather_than_inventing_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ordinary launch exports no digest, and that is not an error.

        ``FIVEE_SIM_RELOAD`` is opt-in, so most journals carry no source at all
        and most refusals are raised by a process that has none either. Both
        halves have to name the absence rather than print an empty field, or
        the diagnostic reads as though the two builds matched.
        """
        root = tmp_path / "journal"
        monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
        monkeypatch.delenv(SOURCE_ID_ENV, raising=False)
        saved = self.a_recorded_fight(root, seed=211)
        position = result_position(saved, "encounter_act")
        arguments = saved[position]["arguments"]
        assert isinstance(arguments, dict)
        del arguments["target"]
        recorded = self.replayed(root, saved)

        with pytest.raises(
            RequestError, match="will not replay under this build"
        ) as refused:
            api.encounter_resume(recorded)

        refusal = str(refused.value)
        assert f"recorded: engine {__version__}, source unrecorded" in refusal
        assert f"running: engine {__version__}, source unset" in refusal

    def test_a_journal_written_before_the_source_was_recorded_still_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The key is read with ``.get``, so an older journal is not a new failure.

        Every journal on disk today predates the field. Recovery must read it
        as absent rather than as a mismatch — the identity is a diagnostic and
        nothing anywhere refuses on it, because a fight that outlives a release
        is the ordinary case and refusing would break cross-version recovery.
        """
        root = tmp_path / "journal"
        monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
        monkeypatch.setenv(SOURCE_ID_ENV, "c" * 64)
        saved = self.a_recorded_fight(root, seed=223)
        assert saved[0].pop("source_id") == "c" * 64
        position = result_position(saved, "encounter_act")
        arguments = saved[position]["arguments"]
        assert isinstance(arguments, dict)
        del arguments["target"]
        recorded = self.replayed(root, saved)

        with pytest.raises(
            RequestError, match="will not replay under this build"
        ) as refused:
            api.encounter_resume(recorded)

        assert "source unrecorded" in str(refused.value)

    # -- fixtures -----------------------------------------------------------

    def a_recorded_fight(self, root: Path, seed: int) -> list[dict[str, Any]]:
        """One fight's journal, holding a result of each replayed operation.

        The replay loop has three engine calls in it and a refusal from any of
        them escaped identically, so a fixture that recorded only an attack
        would leave two thirds of the loop unpinned.

        The closing advance is not the one ``advance_encounter_to`` may have
        taken: at some seeds Thora already holds the first turn and that helper
        records nothing at all, which left this fixture's third operation a
        property of the seed rather than of the fixture.
        """
        encounter_id = mapless_fight(seed=seed)
        advance_encounter_to(encounter_id, "Thora")
        api.encounter_act(encounter_id, "attack", target="Goblin", attack="Longsword")
        api.encounter_condition(encounter_id, "Goblin", "poisoned")
        api.encounter_advance(encounter_id)
        return deepcopy(records(journal_path(root, encounter_id)))

    def replayed(self, root: Path, saved: list[dict[str, Any]]) -> str:
        """These records as a journal of their own, re-chained around the edit.

        Re-chained rather than rewritten in place, because a hand-edited record
        fails its own hash and would be refused by ``read`` long before the
        replay loop saw it — a different refusal, with a different owner. What
        arrives here is a journal that is internally perfect and simply cannot
        be replayed by this build.
        """
        recorded = "enc-9001"
        for entry in saved:
            if entry["kind"] == "creation":
                entry["encounter_id"] = recorded
        rechained(recorded, saved)
        assert journal_path(root, recorded).exists()
        return recorded


def result_position(saved: list[dict[str, Any]], operation: str) -> int:
    """Where in the journal the one recorded ``result`` for ``operation`` sits."""
    for position, entry in enumerate(saved):
        if entry["kind"] == "result" and entry["operation"] == operation:
            return position
    raise AssertionError(f"no recorded {operation!r} result to amend")


def rechained(encounter_id: str, saved: list[dict[str, Any]]) -> None:
    """Append these records under ``encounter_id``, letting ``append`` re-hash."""
    for entry in saved:
        entry.pop("previous_sha256", None)
        entry.pop("sha256", None)
        encounter_journal.append(encounter_id, entry)


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
        encounter_id, "move", to_position=[20, 0], actor="Kettle", view="full"
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


# --- what a journal is allowed to weigh ------------------------------------
#: A fight big enough that a per-combatant cost shows up in the total: six
#: combatants is where a single ``state`` snapshot stops being a rounding error.
SKIRMISH_SEED = 20260806


def _skirmisher(index: int, team: str) -> dict[str, Any]:
    return {
        "name": f"{team}-{index}",
        "team": team,
        "ac": 14,
        "max_hp": 40,
        "position": [5 if team == "party" else 10, 5 + index * 5],
        "attacks": [
            {
                "name": "Blade",
                "attack_bonus": 5,
                "damage": "1d8+3",
                "damage_type": "slashing",
                "kind": "melee",
            }
        ],
    }


#: The roster size both byte ceilings below are calibrated against. Declared
#: rather than described, because ``CREATION_RECORD_CEILING`` tracks it almost
#: exactly — the record is mostly ``combatants`` — so a fixture that quietly
#: grew a seventh would push a real regression under a ceiling that was sized
#: for six and no test would say why. The ceiling test reads it back off the
#: journal and holds it here.
SKIRMISH_COMBATANTS = 6


def _skirmish(acts: int = 20) -> str:
    """Six combatants trading ``acts`` blows, each act followed by an advance.

    Fixed rather than random so the byte ceiling below measures the format and
    not the seed: the same roster, the same seed and the same number of turns
    write the same records every run.
    """
    roster = [_skirmisher(index, "party") for index in range(3)]
    roster += [_skirmisher(index, "monsters") for index in range(3)]
    encounter_id = str(
        api.encounter_create(roster, seed=SKIRMISH_SEED)["encounter_id"]
    )
    for _ in range(acts):
        state = api.encounter_state(encounter_id)
        assert not state["over"], "the fixture must not end before it is written"
        turn = str(state["turn"])
        enemy = "monsters" if turn.startswith("party") else "party"
        target = next(
            str(one["name"])
            for one in state["combatants"]
            if str(one["name"]).startswith(enemy) and one["conscious"]
        )
        api.encounter_act(encounter_id, "attack", target=target, attack="Blade")
        api.encounter_advance(encounter_id)
    return encounter_id


#: What the fixture above is allowed to write, in bytes.
#:
#: Measured at 55,663 on the change that stopped result records carrying the
#: state each action produced; the ceiling is that with about 15% of headroom
#: for a field or two, and deliberately far below the 256,072 bytes the same
#: fixture wrote before. The number is here to fail when derived state creeps
#: back: a ``state`` block is roughly 700 bytes per combatant per record, so
#: one reinstated snapshot per result puts this fixture four times over.
#:
#: What is left is not evenly spread, and the split is worth knowing before
#: anyone tries to move this number: 22,222 bytes are the single creation
#: record — almost all of it the captured content snapshot — and the remaining
#: 33,360 are the eighty attempt and result records together.
#:
#: The fixture measures the two operations recovery replays and no others, so
#: the number is unchanged by a journal keeping the results of the four it does
#: not. That cost is real but small and flat: about 135 to 155 bytes per
#: ``roll``, ``check``, ``save`` or ``encounter_note``, against the 700 bytes
#: *per combatant* a state block costs — it does not grow with the roster,
#: which is exactly why it was affordable and a stored state was not.
#:
#: Recalibrate it downward on a deliberate saving and upward only with a
#: reason; a ceiling quietly raised to fit a regression measures nothing.
SKIRMISH_JOURNAL_CEILING = 64_000


def test_a_result_record_records_that_the_state_moved_rather_than_the_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A journal replays actions; it does not need what each action produced.

    ``recover_session`` recomputes every state it needs by replaying the
    recorded actions through the same stepper that first ran them, so a
    ``state`` in a result record is a second copy of something already
    derivable — and the expensive one, at roughly 700 bytes per combatant per
    record. What stays is ``state_sha256``, which says the state moved and lets
    a reader check a recovered fight against the one that was recorded without
    storing it twice.

    Every act and advance in the fixture, and so every record here, is one
    recovery replays — which is the condition, not the operation. What a
    journal keeps of the four it does *not* replay is
    ``test_every_journalled_operation_obeys_the_rule_it_is_classified_by``.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))

    encounter_id = _skirmish()

    saved = records(journal_path(root, encounter_id))
    results = [entry for entry in saved if entry["kind"] == "result"]
    assert len(results) == 40
    for entry in results:
        assert "state" not in entry, f"record {entry['index']} carries a state"
        assert "result" not in entry, f"record {entry['index']} carries a result"
        assert len(str(entry["state_sha256"])) == 64


def test_a_journal_stays_under_the_size_a_fixed_fight_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling that stops derived state creeping back in a shape nobody named.

    The test above names the two keys that were removed. This one measures the
    whole file, so a *third* copy of the state arriving under some other key
    fails here even though every named assertion still passes.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))

    encounter_id = _skirmish()

    written = journal_path(root, encounter_id).stat().st_size
    assert written < SKIRMISH_JOURNAL_CEILING, (
        f"the fixed 6-combatant 20-act fight wrote {written} bytes, over the "
        f"{SKIRMISH_JOURNAL_CEILING} this format is allowed; see "
        f"SKIRMISH_JOURNAL_CEILING for what the number means"
    )


def test_the_arguments_recorded_are_the_ones_the_caller_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key the caller never named is not a fact about the call.

    ``encounters.act`` builds a twenty-key dict with every parameter present,
    so an unadorned attack used to record seventeen nulls. Every reader of
    these dicts uses ``.get``, for which an absent key and a null are the same
    value — so the nulls were bytes and nothing else.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=241)
    advance_encounter_to(encounter_id, "Thora")

    api.encounter_act(encounter_id, "attack", target="Goblin", attack="Longsword")

    acted = [
        entry
        for entry in records(journal_path(root, encounter_id))
        if entry.get("operation") == "encounter_act"
    ]
    assert len(acted) == 2, "one attempt and one result, both carrying arguments"
    for entry in acted:
        arguments = entry["arguments"]
        assert isinstance(arguments, dict)
        assert arguments == {
            "kind": "attack",
            "target": "Goblin",
            "attack": "Longsword",
            # Supplied by ``act`` itself rather than by the caller, and neither
            # is null: a false flag and an empty sequence are values.
            "as_bonus_action": False,
            "natural": [],
        }


# --- the format this build reads -------------------------------------------
def _stripped_of_its_version(root: Path, encounter_id: str) -> str:
    """This fight's journal again, with the creation record's version removed.

    Re-chained rather than edited in place, for ``replayed``'s reason: a
    hand-edited record fails its own hash and would be refused as corrupt long
    before the format check saw it. What arrives is a journal that is
    internally perfect and simply predates the format this build reads.
    """
    saved = deepcopy(records(journal_path(root, encounter_id)))
    recorded = "enc-9100"
    for entry in saved:
        if entry["kind"] == "creation":
            entry["encounter_id"] = recorded
            entry.pop("journal_version", None)
    rechained(recorded, saved)
    return recorded


def test_a_journal_from_before_this_format_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean break, said in full rather than as a missing key.

    There is no reader for the older format and no migration operation, so the
    refusal has to carry everything a caller can act on: which encounter, that
    the file is *fine* and must not be edited, and which build wrote it. Told
    only "no journal_version", a caller reaches for the file — and a hand-edited
    record breaks every hash after it, dropping the fight to ``corrupt`` with
    nothing preserved.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=251)
    recorded = _stripped_of_its_version(root, encounter_id)
    api.STATE.sessions.clear()

    with pytest.raises(
        RequestError,
        match=(
            rf"cannot recover 'enc-9100''s fight: its journal is written in an "
            rf"unversioned format, and this build reads journal_version "
            rf"{sessions_service.JOURNAL_VERSION} only\. "
            r"The journal is intact and hash-valid; do not edit it .*"
            r"There is no reader for the older format and no migration\. "
            r"Run the build that wrote it \(recorded: engine .*running: engine .*\), "
            r"or read the record with the journal_path that encounter\.list reports\."
        ),
    ):
        api.encounter_resume(recorded)


def test_a_journal_this_build_cannot_read_is_still_listed_rather_than_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only recovery refuses. The file is hash-valid, so ``list`` says so.

    ``encounter.list`` reports ``corrupt`` on a broken chain and nothing else,
    and an unreadable *format* is not a broken chain — a listing that hid the
    fight would leave a caller with no way to find the journal the refusal
    above tells them to read.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=257)
    recorded = _stripped_of_its_version(root, encounter_id)

    listed = {
        str(entry["encounter_id"]): entry
        for entry in api.encounter_list(status="all")["encounters"]
    }

    assert recorded in listed
    assert listed[recorded]["status"] == "active"
    # And the field the refusal tells the caller to use, which a ``corrupt``
    # entry would still carry but an omitted one would not.
    assert listed[recorded]["journal_path"].endswith(f"{recorded}/journal.jsonl")


def test_a_journal_this_build_writes_declares_the_format_it_is_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))

    encounter_id = mapless_fight(seed=263)

    created = records(journal_path(root, encounter_id))[0]
    assert "journal_version" in created, "a build that stopped stamping records at all"
    assert created["journal_version"] == sessions_service.JOURNAL_VERSION
    # Not a vacuity guard, and it used to claim to be one: ``records`` returns
    # dicts and this subscripts them, so a build that stopped stamping raises
    # ``KeyError`` on the line above rather than comparing two ``None``s. What
    # this does check is that the constant is a version and not a sentinel — the
    # comparison alone would hold if ``JOURNAL_VERSION`` became a string or a
    # ``None`` and the writer matched it.
    assert isinstance(sessions_service.JOURNAL_VERSION, int)


# --- what a caller who asked for idempotency keeps --------------------------
def test_a_request_id_keeps_its_result_across_a_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """You pay for what you ask for, and a retry gets the first answer back.

    A caller that passed ``request_id`` bought idempotency, so that call's
    result is kept whole in the journal — it is the only copy, and a retry
    after a restart has nothing else to answer from. Every other call keeps
    only the hash.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=269)

    first = api.encounter_advance(encounter_id, request_id="turn-1", view="full")
    api.encounter_act(encounter_id, "dodge")
    api.STATE.sessions.clear()
    api.encounter_resume(encounter_id)

    assert api.encounter_advance(encounter_id, request_id="turn-1") == first
    kept = {
        str(entry["operation"]): entry
        for entry in records(journal_path(root, encounter_id))
        if entry["kind"] == "result"
    }
    # ``view`` and ``state_sha256`` are how an answer was rendered for one
    # caller, not what happened, so the journal keeps neither and a retry
    # renders them again from the result it did keep.
    presentation = {"view", "state_sha256"}
    assert kept["encounter_advance"]["result"] == {
        key: value for key, value in first.items() if key not in presentation
    }
    assert "result" not in kept["encounter_act"], (
        "a call that asked for nothing keeps nothing but the hash"
    )


def test_a_recovered_fight_is_the_fight_it_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim the dropped state used to be evidence for, made directly.

    Nothing reads a recorded state back, so the only thing that could make
    dropping it wrong is a replay that does not reproduce it. This is that
    check, over a fight long enough for a divergence to show.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = _skirmish(acts=8)
    live = api.encounter_state(encounter_id)
    live_attempts = deepcopy(api.STATE.sessions[encounter_id].attempts)
    recorded = [
        str(entry["state_sha256"])
        for entry in records(journal_path(root, encounter_id))
        if entry["kind"] == "result"
    ]
    api.STATE.sessions.clear()

    recovered = api.encounter_resume(encounter_id)

    assert recovered["state"] == live
    # And the hashes the journal kept instead of the states agree with the
    # replay that produced them, which is what makes them worth keeping.
    assert [
        str(entry["state_sha256"])
        for entry in api.STATE.sessions[encounter_id].attempts
    ] == recorded
    # The audit trail comes back whole, not merely equivalent. It has to: a
    # replay bundle carries ``Session.attempts`` verbatim, so a recovered
    # session that rebuilt them to a second shape would export a different
    # artifact from the live fight it replaced.
    assert api.STATE.sessions[encounter_id].attempts == live_attempts


# --- what a journal keeps, and why -----------------------------------------
def test_a_roll_nothing_replays_keeps_what_it_rolled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The journal is the only record a primitive will ever have.

    ``recover_session`` re-derives an act, a ruling and an advance by replaying
    them, so what those produced is stored nowhere. A ``roll`` is resolved once
    and never replayed, and this one names no seed — so the seed the engine
    chose lives in the result and in no argument dict. Drop the result and the
    face that was rolled is recorded nowhere at all, which is not a saving but
    a deletion.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=277)

    rolled = api.roll("1d20", encounter_id=encounter_id)

    kept = next(
        entry
        for entry in records(journal_path(root, encounter_id))
        if entry["kind"] == "result" and entry["operation"] == "roll"
    )
    arguments = kept["arguments"]
    assert isinstance(arguments, dict)
    # The arguments are what makes this load-bearing: the caller named no seed,
    # so ``supplied_arguments`` dropped it and the result is the only place the
    # resolved one exists.
    assert "seed" not in arguments
    assert kept["result"] == rolled
    result = kept["result"]
    assert isinstance(result, dict)
    assert result["seed"] == rolled["seed"]
    assert result["rolls"] == rolled["rolls"]
    # And it survives the round trip, because a recovered session reads its
    # audit trail back off these records.
    api.STATE.sessions.clear()
    api.encounter_resume(encounter_id)
    recovered = next(
        entry
        for entry in api.STATE.sessions[encounter_id].attempts
        if entry["operation"] == "roll"
    )
    assert recovered["result"] == rolled


def test_a_note_nothing_replays_keeps_what_it_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rule, on the operation that is not a die.

    ``encounter_note`` is the fourth operation recovery does not replay, and
    its result carries a timestamp the engine read from the clock — as
    underivable as a rolled face and recorded in exactly the same one place.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=281)

    written = api.encounter_note(encounter_id, "the door groans open")

    kept = next(
        entry
        for entry in records(journal_path(root, encounter_id))
        if entry["kind"] == "result" and entry["operation"] == "encounter_note"
    )
    assert kept["result"] == written


def test_an_operation_recovery_replays_keeps_only_the_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the rule, and the half the saving comes from.

    An act is re-derived from its arguments, so its result is a second copy of
    something already computable — and the expensive one. What stays is the
    hash, which says the state moved without storing it twice.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=283)
    advance_encounter_to(encounter_id, "Thora")

    api.encounter_act(encounter_id, "dodge")

    kept = next(
        entry
        for entry in records(journal_path(root, encounter_id))
        if entry["kind"] == "result" and entry["operation"] == "encounter_act"
    )
    assert "result" not in kept
    assert "state" not in kept
    assert len(str(kept["state_sha256"])) == 64


def test_the_replayed_set_is_the_one_recovery_actually_replays() -> None:
    """One declaration, read by the writer and by the reader.

    ``attempt_finished`` decides whether to keep a result by asking whether
    recovery will reproduce it, and ``recover_session`` decides what to replay.
    A hand-maintained second copy of that list would drift the first time an
    operation joined or left it — so both read the same table, and this is the
    assertion that the table is the one doing the work rather than a label
    beside it.
    """
    assert sessions_service.REPLAYED_OPERATIONS == frozenset(
        sessions_service.REPLAY_BY_OPERATION
    )
    assert sessions_service.REPLAYED_OPERATIONS == {
        "encounter_act",
        "encounter_condition",
        "encounter_advance",
    }


# --- when the replay does not land where the journal says -------------------
def _with_a_poisoned_state_hash(root: Path, encounter_id: str, recorded: str) -> str:
    """This fight's journal again, with the last result record's hash replaced.

    Re-chained rather than edited in place: a hand-edited record fails its own
    sha256 and would be refused as corrupt long before recovery replayed
    anything. What arrives is a journal that is internally perfect and simply
    disagrees with what replaying it produces — which is what a kernel edit
    under a live fight looks like from here.
    """
    saved = deepcopy(records(journal_path(root, encounter_id)))
    for entry in saved:
        if entry["kind"] == "creation":
            entry["encounter_id"] = recorded
    last = next(
        entry
        for entry in reversed(saved)
        if entry["kind"] == "result" and entry["status"] == "success"
    )
    last["state_sha256"] = "0" * 64
    rechained(recorded, saved)
    return recorded


def test_a_replay_that_lands_somewhere_else_says_so_without_refusing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detector for the sharp edge of a reload, on the warning channel.

    A recovered fight is re-derived under whatever rules this build now has, so
    a kernel edit can leave it disagreeing with what the journal recorded. That
    is the feature working and also its sharp edge, and ``state_sha256`` is the
    only thing that can see it.

    It warns rather than refuses on purpose. A fight outliving a release is
    ordinary, and refusing would break cross-version recovery for the sake of a
    diagnostic — the same reasoning the creation record's ``engine_version``
    already carries.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=293)
    advance_encounter_to(encounter_id, "Thora")
    api.encounter_act(encounter_id, "dodge")
    poisoned = _with_a_poisoned_state_hash(root, encounter_id, "enc-9200")
    api.STATE.sessions.clear()

    recovered = api.encounter_resume(poisoned)

    # Recovered, not refused: the fight is here and usable.
    assert recovered["state"]["round"] == 1
    drift = recovered["recovery_warning"]["state_drift"]
    assert drift.startswith(
        "'enc-9200' was recovered, but it is not the fight its journal recorded: "
        "after record "
    )
    assert "encounter_act" in drift
    assert "the journal has state_sha256 000000000000 and replaying it here produced " in drift
    assert drift.endswith(
        "The fight is usable and the journal is intact; the rules that replayed it "
        f"are not the rules that recorded it (recorded: engine {__version__}, "
        f"source unrecorded; running: engine {__version__}, source unset)."
    )


def test_an_ordinary_recovery_warns_about_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warning that fires on a healthy recovery is worse than no warning.

    Every resume of a fight that outlived its process goes through this, so a
    false positive here would teach a caller to ignore the channel that carries
    the true one.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = _skirmish(acts=4)
    api.roll("1d20", encounter_id=encounter_id)
    api.encounter_note(encounter_id, "a quiet moment")
    api.STATE.sessions.clear()

    recovered = api.encounter_resume(encounter_id)

    assert "recovery_warning" not in recovered


def test_a_fight_that_replayed_nothing_at_all_warns_about_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No result record carries a hash, so there is nothing to disagree with.

    A fight created and never acted in has only a creation record. Quiet is the
    right answer: a missing comparison is not a failed one.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=307)
    api.STATE.sessions.clear()

    assert "recovery_warning" not in api.encounter_resume(encounter_id)


def test_an_interrupted_tail_is_not_mistaken_for_a_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An attempt with no result is compared against nothing.

    The replay stops at the last record that has one, and so does the hash it
    is held against — so a process that died mid-operation recovers as quietly
    as one that stopped cleanly.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=311)
    advance_encounter_to(encounter_id, "Thora")
    api.encounter_act(encounter_id, "dodge")
    encounter_journal.append(
        encounter_id,
        {
            "kind": "attempt",
            "timestamp": sessions_service.utc_now(),
            "index": 99,
            "operation": "encounter_act",
            "request_id": None,
            "arguments": {"kind": "dodge"},
        },
    )
    api.STATE.sessions.clear()

    recovered = api.encounter_resume(encounter_id)

    assert "recovery_warning" not in recovered
    assert recovered["state"]["round"] == 1


def test_a_crash_tail_and_a_divergence_are_both_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One channel, two keys, and neither overwrites the other.

    Both can be true of the same journal — a process that died mid-write can
    also have been running different rules — and the channel is a single dict.
    So each takes its own key: a caller reading ``problem`` still sees the tail
    it always saw, and a divergence arrives beside it rather than instead of
    it.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=313)
    advance_encounter_to(encounter_id, "Thora")
    api.encounter_act(encounter_id, "dodge")
    poisoned = _with_a_poisoned_state_hash(root, encounter_id, "enc-9300")
    with journal_path(root, poisoned).open("ab") as handle:
        handle.write(b'{"partial"')
    api.STATE.sessions.clear()

    warning = api.encounter_resume(poisoned)["recovery_warning"]

    assert warning["problem"] == "partial final record was removed from the journal"
    assert warning["preserved_tail"].endswith(".corrupt-tail")
    assert "it is not the fight its journal recorded" in warning["state_drift"]


def journalled_operations() -> set[str]:
    """Every ``operation`` a journal can record, off the source rather than a fixture.

    Read statically, on the same reasoning as ``test_player_brief``'s
    ``emitted_data_keys``: a fixture that calls seven operations proves the
    fixture called seven operations, not that seven is all there are. An eighth
    operation added to ``service/`` and never added here would pass that
    fixture-literal check green while its classification went unchecked — the
    silent-data-loss shape the journal format exists to avoid.

    A journal record's ``operation`` originates at exactly two call shapes:
    ``audited_primitive(..., operation="...", ...)`` and
    ``attempt_finished(..., operation="...", ...)``, one per module the two
    kinds are called from. ``audited_primitive`` itself forwards its own
    ``operation`` parameter into ``attempt_finished`` — a ``Name``, not a
    string literal — and that forwarded value is deliberately not collected:
    it names no new operation, only the one its caller already declared.
    """
    service_dir = Path(sessions_service.__file__).parent
    operations: set[str] = set()
    sites = 0
    for path in sorted(service_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            name = (
                called.id if isinstance(called, ast.Name)
                else called.attr if isinstance(called, ast.Attribute)
                else None
            )
            if name not in {"audited_primitive", "attempt_finished"}:
                continue
            for keyword in node.keywords:
                if keyword.arg != "operation":
                    continue
                if isinstance(keyword.value, ast.Constant) and isinstance(
                    keyword.value.value, str
                ):
                    operations.add(keyword.value.value)
                    sites += 1
                else:
                    assert isinstance(keyword.value, ast.Name), (
                        f"operation= at {path.name}:{node.lineno} is neither a "
                        f"string literal nor a forwarded name; widen the reader"
                    )
    assert sites >= 9, (
        f"only {sites} operation= literal sites were found in service/; the "
        f"derivation has stopped reading the source rather than the source "
        f"having stopped declaring operations"
    )
    return operations


def test_every_journalled_operation_obeys_the_rule_it_is_classified_by(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All seven operations in one fight, each checked against the constant.

    The cases above name three operations between them. This one exercises the
    whole set — derived from ``journalled_operations()`` rather than written
    out, so an operation added to ``service/`` and never exercised here fails
    this test until somebody adds it to the fixture and thereby decides its
    classification — and checks what each record should carry against
    ``REPLAYED_OPERATIONS`` itself, so an operation added to or removed from
    the replay table is checked here without anybody remembering to add a
    case.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=317)
    advance_encounter_to(encounter_id, "Thora")

    api.roll("1d20", encounter_id=encounter_id)
    api.check(3, 12, encounter_id=encounter_id)
    api.save(2, 11, encounter_id=encounter_id, request_id="the-save")
    api.encounter_note(encounter_id, "a line somebody spoke")
    api.encounter_condition(encounter_id, "Goblin", "prone")
    api.encounter_act(encounter_id, "dodge")
    api.encounter_advance(encounter_id, request_id="the-turn")

    saved = [
        entry for entry in records(journal_path(root, encounter_id))
        if entry["kind"] == "result"
    ]
    assert {str(entry["operation"]) for entry in saved} == journalled_operations(), (
        "the fixture must exercise every operation service/ can journal"
    )
    for entry in saved:
        operation = str(entry["operation"])
        kept = "result" in entry
        expected = (
            operation not in sessions_service.REPLAYED_OPERATIONS
            or entry["request_id"] is not None
        )
        assert kept is expected, (
            f"{operation} {'kept' if kept else 'dropped'} its result, and the rule "
            f"says it should have been {'kept' if expected else 'dropped'}"
        )
        # Whatever it kept, it says the state moved.
        assert len(str(entry["state_sha256"])) == 64


# --- what the creation record names rather than carries ---------------------
#: What one creation record is allowed to weigh, in bytes, for the six-combatant
#: roster :func:`_skirmish` builds.
#:
#: Measured at **22,223 bytes** while the record carried its content snapshot
#: inline, of which 14,589 — 66% — was that one payload, byte-identical in every
#: journal on the machine. It names a blob instead, and the same record measures
#: **7,708**. The ceiling is that with about 15% of headroom.
#:
#: It is a *creation record* ceiling rather than a share of
#: :data:`SKIRMISH_JOURNAL_CEILING` above for one reason: there the creation
#: record is one line among eighty-one, so a payload creeping back into it would
#: be absorbed into a total the acts dominate. Here it fails at full size.
#:
#: What is left is worth knowing before anyone tries to move this number,
#: because it is no longer the content: 7,196 of the 7,708 are ``combatants``,
#: the normalized creation input recovery replays the fight from. That is an
#: input rather than a derivation, so it stays — and it means this ceiling now
#: tracks roster size almost exactly, which is why the fixture is fixed at
#: :data:`SKIRMISH_COMBATANTS` and why the test asserts that before the bytes.
CREATION_RECORD_CEILING = 8_900


def _creation_bytes(root: Path, encounter_id: str) -> int:
    """How many bytes of the journal file this fight's creation record is."""
    first, _, _ = journal_path(root, encounter_id).read_bytes().partition(b"\n")
    return len(first) + 1


def test_the_creation_record_names_its_content_rather_than_carrying_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The captured content moves to a blob, and the record keeps the name.

    Nothing downstream changes shape: ``recover_session`` resolves the reference
    and repopulates ``Session.content_snapshot`` with exactly the payload that
    used to ride here, so everything reading the *session* — a replay bundle
    above all — still sees the content by value.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))

    encounter_id = mapless_fight(seed=271)

    created = records(journal_path(root, encounter_id))[0]
    assert "content" not in created, "the payload is a blob's job now"
    reference = created["content_ref"]
    assert isinstance(reference, str)
    assert blobs.get(reference) == api.STATE.sessions[encounter_id].content_snapshot


def test_one_content_blob_serves_every_fight_that_captured_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The saving is sharing, not compression, so it is checked as sharing.

    Two fights under the same content compute the same name, so the second
    writes nothing. A store that merely moved the payload out of the journal
    into a file per fight would pass every other case in this section and save
    nothing at all — the measured finding this change answers is that the
    snapshot was byte-identical in all twenty-two journals on the machine.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))

    first = mapless_fight(seed=273)
    second = mapless_fight(seed=277)

    references = {
        str(records(journal_path(root, encounter_id))[0]["content_ref"])
        for encounter_id in (first, second)
    }
    assert len(references) == 1, "two fights, two names, and so no sharing"
    assert [path.name for path in sorted(blobs.blobs_root().iterdir())] == [
        f"{references.pop()}.json"
    ]


def test_the_creation_record_names_its_map_rather_than_carrying_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other by-value capture, and the same treatment.

    ``map_kind`` and ``map_source`` stay in the record and are deliberately not
    part of the blob: they are what ``adventures._creation_record`` reads to
    decide whether a chapter's ground can be carried into the next one, and
    neither is the document.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))

    encounter_id = str(
        api.encounter_create(
            [dict(one) for one in INTERLUDE_PARTY],
            seed=281,
            mode="exploration",
            map={
                "name": "mill floor",
                "width": 40,
                "height": 1,
                "default_terrain": "normal",
            },
        )["encounter_id"]
    )

    created = records(journal_path(root, encounter_id))[0]
    assert "map" not in created, "the document is a blob's job now"
    assert created["map_kind"] == "inline"
    reference = created["map_ref"]
    assert isinstance(reference, str)
    assert blobs.get(reference)["name"] == "mill floor"


def test_a_fight_on_no_map_names_no_map_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent ground is a null reference, not a blob holding ``null``.

    Worth its own case because the two are indistinguishable to every reader
    downstream and only one of them writes a file: a mapless fight that still
    published a blob would put one empty payload in the shared store and then
    name it from every mapless journal ever written.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))

    encounter_id = mapless_fight(seed=283)

    created = records(journal_path(root, encounter_id))[0]
    assert created["map_ref"] is None
    assert created["map_kind"] == "none"
    stored = [path.name for path in blobs.blobs_root().iterdir()]
    assert stored == [f"{created['content_ref']}.json"], "the content blob and nothing else"


def test_a_creation_record_stays_under_the_size_one_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measurement this change was made for, pinned rather than described.

    The roster is asserted before the bytes are, because the ceiling is a
    function of it: what is left in a creation record is almost entirely
    ``combatants``, so a fixture grown to seven would carry a real regression in
    under a number sized for six. The premise was stated in
    ``CREATION_RECORD_CEILING``'s own comment and observed nowhere.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))

    encounter_id = _skirmish(acts=0)

    recorded = records(journal_path(root, encounter_id))[0]["combatants"]
    assert isinstance(recorded, list)
    assert len(recorded) == SKIRMISH_COMBATANTS, (
        f"this ceiling is calibrated against {SKIRMISH_COMBATANTS} combatants and the "
        f"record holds {len(recorded)}; recalibrate the number, do not widen it"
    )

    written = _creation_bytes(root, encounter_id)
    assert written < CREATION_RECORD_CEILING, (
        f"the fixed six-combatant creation record wrote {written} bytes, over the "
        f"{CREATION_RECORD_CEILING} this format is allowed; see "
        f"CREATION_RECORD_CEILING for what the number means and what is left in it"
    )


def test_a_fight_recovers_with_its_content_resolved_from_the_blob_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The round trip under the clean break: create, act, restart, resume.

    The content the fight finishes under is the content it started under, and
    the journal no longer holds a copy of it — so a recovery that could not
    reach the blob store would rebuild the fight under whatever the process
    happened to have loaded, which is the drift this whole arrangement exists
    to make impossible.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=287)
    advance_encounter_to(encounter_id, "Thora")
    api.encounter_act(encounter_id, "attack", target="Goblin", attack="Longsword")
    before = api.encounter_state(encounter_id)
    snapshot = deepcopy(api.STATE.sessions[encounter_id].content_snapshot)
    api.STATE.sessions.clear()

    recovered = api.encounter_resume(encounter_id)

    assert recovered["state"] == before
    assert api.STATE.sessions[encounter_id].content_snapshot == snapshot


def test_a_recovered_fight_still_exports_its_map_and_content_by_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle leaves the machine, so a reference in one would be a broken file.

    The two are different kinds of artifact and this is where that bites. A
    journal names a blob because the blob is right there beside it; a replay
    bundle is handed to somebody else, where nothing resolves a bare digest. So
    a fight recovered *from* references must still export payloads — which it
    does because recovery repopulates the session, and the writers read the
    session rather than the journal. This is the case that would catch a writer
    reaching past it.

    Held against the bundle the *live* session wrote, not against a shape. The
    assertion used to be that ``content["packs"]`` was truthy, and a writer
    mutated to ship a different non-empty registry — a recovery that resolved
    the wrong blob, or fell back to whatever the process had loaded — satisfied
    it exactly. What the claim actually is, is that the two agree, so that is
    what is compared: same payload, by value, on both sides of the restart.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = str(
        api.encounter_create(
            [dict(one) for one in INTERLUDE_PARTY],
            seed=291,
            mode="exploration",
            map={
                "name": "mill floor",
                "width": 40,
                "height": 1,
                "default_terrain": "normal",
            },
        )["encounter_id"]
    )
    live = api.replay_export(encounter_id)["bundle"]
    assert live["content"]["packs"], "the fixture must load content for this to say anything"

    api.STATE.sessions.clear()
    api.encounter_resume(encounter_id)

    recovered = api.replay_export(encounter_id)["bundle"]

    assert recovered["content"] == live["content"]
    assert recovered["map"] == live["map"]
    assert recovered["map"]["name"] == "mill floor"


def test_a_fight_whose_content_blob_is_gone_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named payload can be missing where an inline one could not be.

    That is what the trade costs, and it is paid here as a sentence rather than
    as a stack trace: the journals and the blobs are sibling roots that move
    independently, so a journal carried somewhere its blobs were not says which
    encounter and says what is missing.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=293)
    reference = str(records(journal_path(root, encounter_id))[0]["content_ref"])
    (blobs.blobs_root() / f"{reference}.json").unlink()
    api.STATE.sessions.clear()

    with pytest.raises(
        RequestError,
        match=f"cannot recover {encounter_id!r}'s content: no blob {reference!r}",
    ):
        api.encounter_resume(encounter_id)


def test_a_fight_whose_map_blob_is_gone_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The content refusal's twin, and not a copy of it.

    ``recover_session`` resolves two references and translates each in its own
    ``except``, with its own noun in the sentence. Testing one of them proves
    the *pattern* exists and says nothing about whether the other was written,
    reached, or worded — a map branch that raised past its translation would
    have shipped a bare ``BlobError`` out of a fight the caller asked to
    resume, and the content case would still have been green.

    The map is also the branch a fight can skip: ``map_ref`` is absent when
    there is no map, so this is the only case that both takes it and fails it.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = str(
        api.encounter_create(
            [dict(one) for one in INTERLUDE_PARTY],
            seed=295,
            mode="exploration",
            map={"name": "mill floor", "width": 40, "height": 1, "default_terrain": "normal"},
        )["encounter_id"]
    )
    created = records(journal_path(root, encounter_id))[0]
    reference = str(created["map_ref"])
    assert reference != created["content_ref"], "the two must be separate blobs to tell apart"
    (blobs.blobs_root() / f"{reference}.json").unlink()
    api.STATE.sessions.clear()

    with pytest.raises(
        RequestError,
        match=f"cannot recover {encounter_id!r}'s map: no blob {reference!r}",
    ):
        api.encounter_resume(encounter_id)


def _restamped(root: Path, encounter_id: str, version: int) -> str:
    """This fight's journal again, stamped with a format version it is not in.

    Re-chained rather than edited in place, for :func:`_stripped_of_its_version`'s
    reason: a hand-edited record fails its own hash and would be refused as
    corrupt long before the format check saw it.
    """
    saved = deepcopy(records(journal_path(root, encounter_id)))
    recorded = "enc-9101"
    for entry in saved:
        if entry["kind"] == "creation":
            entry["encounter_id"] = recorded
            entry["journal_version"] = version
    rechained(recorded, saved)
    return recorded


def test_the_format_before_this_one_is_refused_by_name_like_every_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clean break exercised at the boundary it was written for.

    ``_stripped_of_its_version`` above covers a journal from before the field
    existed. This is the sharper case and the reason the version moved: a
    version-2 record carries its content and its map as payloads under keys this
    reader no longer looks for. Without the check it would not fail at the
    version — it would fail hunting a ``content_ref`` that was never written,
    well past the point where the message could say anything a caller can act
    on.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    previous = sessions_service.JOURNAL_VERSION - 1
    encounter_id = mapless_fight(seed=297)
    recorded = _restamped(root, encounter_id, previous)
    api.STATE.sessions.clear()

    with pytest.raises(
        RequestError,
        match=(
            rf"cannot recover 'enc-9101''s fight: its journal is written in "
            rf"journal_version {previous}, and this build reads journal_version "
            rf"{sessions_service.JOURNAL_VERSION} only\. "
            rf"The journal is intact and hash-valid; do not edit it .*"
            rf"There is no reader for the older format and no migration\. "
        ),
    ):
        api.encounter_resume(recorded)


# --- Cheap reads -------------------------------------------------------------
#
# What a journal says about itself without being replayed. ``read`` parses and
# hash-verifies every line to answer anything at all, which is the right price
# for recovery and the wrong one for a listing: ``encounter.list`` wants two
# timestamps and a count, and ``creation_request`` wants one field off line 1.


def _journal_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    return root


def test_a_summary_names_the_first_and_last_records_and_counts_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _journal_root(tmp_path, monkeypatch)
    encounter_id = mapless_fight(seed=151)
    api.encounter_advance(encounter_id)
    written = records(journal_path(root, encounter_id))
    assert len(written) > 2, "a head and a tail say nothing about a two-line file"

    summary = encounter_journal.head_and_tail(encounter_id)

    assert summary is not None
    assert summary.first == written[0]
    assert summary.last == written[-1]
    assert summary.records == len(written)


def test_a_summary_of_a_claimed_but_unwritten_journal_is_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty file ``claim`` leaves behind has no first record to name."""
    _journal_root(tmp_path, monkeypatch)
    assert encounter_journal.claim("enc-claimed") is True

    assert encounter_journal.head_and_tail("enc-claimed") is None


def test_a_summary_of_an_unknown_encounter_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _journal_root(tmp_path, monkeypatch)

    with pytest.raises(
        encounter_journal.JournalError, match="unknown encounter 'enc-nobody'"
    ):
        encounter_journal.head_and_tail("enc-nobody")


def test_a_summary_answers_a_journal_whose_middle_read_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole of what makes it cheap, and the whole of what it gives up.

    A summary reads two lines and counts newlines for the rest, so a record
    between them that is neither parseable nor correctly chained costs it
    nothing — and buys it nothing either. That trade is only sound because
    ``read`` still stands in front of every path that acts on what a journal
    says, which is the half this asserts second.
    """
    root = _journal_root(tmp_path, monkeypatch)
    encounter_id = mapless_fight(seed=157)
    api.encounter_advance(encounter_id)
    path = journal_path(root, encounter_id)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) > 2
    lines[1] = "this line is not a record at all"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(
        encounter_journal.JournalError, match="line 2 is not valid JSON"
    ):
        encounter_journal.read(encounter_id)

    summary = encounter_journal.head_and_tail(encounter_id)

    assert summary is not None
    assert summary.records == len(lines)
    assert summary.first["kind"] == "creation"


def test_a_summary_stops_at_the_last_complete_record_and_repairs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A look is not a repair. ``read`` preserves a crash tail and rewrites the
    file; a summary may do neither, because a listing must not mutate the thing
    it is listing.
    """
    root = _journal_root(tmp_path, monkeypatch)
    encounter_id = mapless_fight(seed=163)
    api.encounter_advance(encounter_id)
    path = journal_path(root, encounter_id)
    written = records(path)
    with path.open("ab") as handle:
        handle.write(b'{"partial"')
    before = path.read_bytes()

    summary = encounter_journal.head_and_tail(encounter_id)

    assert summary is not None
    assert summary.last == written[-1]
    assert summary.records == len(written)
    assert path.read_bytes() == before
    assert not path.with_suffix(".corrupt-tail").exists()


def test_a_summary_refuses_a_first_record_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two lines it does read are the two it cannot shrug off."""
    root = _journal_root(tmp_path, monkeypatch)
    encounter_id = mapless_fight(seed=167)
    path = journal_path(root, encounter_id)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0] = "{"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(
        encounter_journal.JournalError, match="line 1 is not valid JSON"
    ):
        encounter_journal.head_and_tail(encounter_id)


def test_a_summary_refuses_a_record_that_is_not_an_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _journal_root(tmp_path, monkeypatch)
    encounter_id = mapless_fight(seed=173)
    path = journal_path(root, encounter_id)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[-1] = "[1, 2, 3]"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(
        encounter_journal.JournalError,
        match=rf"line {len(lines)} must be an object",
    ):
        encounter_journal.head_and_tail(encounter_id)


def test_a_one_record_journal_is_its_own_head_and_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary a two-pointer read gets wrong: nothing before the tail."""
    root = _journal_root(tmp_path, monkeypatch)
    encounter_id = mapless_fight(seed=179)
    written = records(journal_path(root, encounter_id))
    assert len(written) == 1, "creation alone, before anybody acts"

    summary = encounter_journal.head_and_tail(encounter_id)

    assert summary is not None
    assert summary.first == summary.last == written[0]
    assert summary.records == 1


def test_listing_encounters_summarises_rather_than_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cost this phase is about: a listing answers from two lines a file.

    Before this, ``encounter.list`` parsed and hash-verified *every* journal on
    the disk to report an id and two timestamps.
    """
    root = _journal_root(tmp_path, monkeypatch)
    active = mapless_fight(seed=181)
    api.encounter_advance(active)
    over = mapless_fight(seed=191)
    api.encounter_finalize(over)
    counts = {
        active: len(records(journal_path(root, active))),
        over: len(records(journal_path(root, over))),
    }

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("encounter.list must not replay a journal to list it")

    monkeypatch.setattr(encounter_journal, "read", refuse)
    listed = api.encounter_list(status="all")

    entries = {entry["encounter_id"]: entry for entry in listed["encounters"]}
    assert entries.keys() == {active, over}
    assert entries[active]["status"] == "active"
    assert entries[over]["status"] == "finalized"
    assert entries[active]["records"] == counts[active]
    assert entries[over]["records"] == counts[over]


def test_matching_a_creation_request_replays_only_the_journal_it_matched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``request_id`` lives on line 1, so finding it never needed line 40."""
    _journal_root(tmp_path, monkeypatch)
    # Sorted first, so the scan has to look at it and decline before it matches.
    decoy = mapless_fight(seed=193)
    api.encounter_advance(decoy)
    target = api.encounter_create(
        [dict(REPLAY_HERO), dict(REPLAY_GOBLIN)], seed=197, request_id="one-fight"
    )["encounter_id"]
    api.STATE.sessions.clear()

    replayed: list[str] = []
    verbatim = encounter_journal.read

    def counted(
        encounter_id: str, **kwargs: Any
    ) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
        replayed.append(encounter_id)
        return verbatim(encounter_id, **kwargs)

    monkeypatch.setattr(encounter_journal, "read", counted)
    again = api.encounter_create(
        [dict(REPLAY_HERO), dict(REPLAY_GOBLIN)], seed=999, request_id="one-fight"
    )

    assert again["encounter_id"] == target
    assert again["seed"] == 197
    assert replayed == [target], (
        f"only the matched journal may be replayed; {decoy!r} was summarised "
        f"and declined, and this read {replayed}"
    )


def test_a_finalized_fight_refuses_a_write_before_it_journals_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What makes ``finalized`` the last thing a journal can say.

    The refusal used to arrive *after* ``attempt_started`` had appended, so a
    finished fight collected an ``attempt`` and a ``result`` record for every
    call that bounced off it — and a listing could not then read a fight's
    status off its last line, which is what this phase needs it to do.

    Nothing is lost by moving it. A refusal here rolled no dice and changed no
    state, so the record was of the caller's mistake rather than of the fight.
    A refusal the *rules* make is still audited in full — see
    ``test_a_refused_action_is_part_of_the_audit_record``.
    """
    root = _journal_root(tmp_path, monkeypatch)
    encounter_id = mapless_fight(seed=199)
    api.encounter_finalize(encounter_id)
    path = journal_path(root, encounter_id)
    closed = records(path)
    assert closed[-1]["kind"] == "finalized"

    refusal = rf"encounter {encounter_id!r} is finalized"
    with pytest.raises(RequestError, match=refusal):
        api.encounter_advance(encounter_id)
    with pytest.raises(RequestError, match=refusal):
        api.encounter_act(encounter_id, "attack", target="Goblin")
    with pytest.raises(RequestError, match=refusal):
        api.roll("1d20", encounter_id=encounter_id)

    assert records(path) == closed


# --- Layout and lifecycle ----------------------------------------------------
#
# One directory per encounter. A fight's journal, the lock guarding it, its
# crash tail and its frozen replay are one thing on disk rather than four
# files sharing a root with everybody else's — and the empty journal a claim
# leaves behind now has somewhere to be reaped from.


def test_a_fights_artifacts_are_siblings_in_a_directory_named_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _journal_root(tmp_path, monkeypatch)
    encounter_id = mapless_fight(seed=211)
    advance_encounter_to(encounter_id, "Thora")
    api.encounter_act(encounter_id, "dodge")
    api.encounter_finalize(encounter_id)

    directory = root / encounter_id
    assert directory.is_dir()
    assert {path.name for path in directory.iterdir()} == {
        "journal.jsonl",
        "journal.jsonl.lock",
        "replay.json",
    }
    # And nothing of this fight is left loose beside everybody else's.
    assert [path.name for path in root.iterdir()] == [encounter_id]


def test_a_crash_tail_is_preserved_inside_the_fights_own_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _journal_root(tmp_path, monkeypatch)
    encounter_id = mapless_fight(seed=223)
    path = journal_path(root, encounter_id)
    with path.open("ab") as handle:
        handle.write(b'{"partial"')

    records_read, warning = encounter_journal.read(encounter_id, repair_partial=True)

    assert warning is not None
    tail = Path(warning["preserved_tail"])
    assert tail.parent == root / encounter_id
    assert tail.read_bytes() == b'{"partial"'
    assert len(records_read) == 1


def test_a_journal_left_flat_in_the_root_is_not_a_fight_this_build_knows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clean break, at the layout rather than the format.

    ``journal_version`` already refuses an older record shape by name; a file
    written *where* an older build put it is not found at all, which is the
    honest answer — there is no id whose directory it is.
    """
    root = _journal_root(tmp_path, monkeypatch)
    root.mkdir(parents=True, exist_ok=True)
    (root / "enc-1.jsonl").write_text('{"kind": "creation"}\n', encoding="utf-8")

    assert api.encounter_list(status="all")["encounters"] == []
    with pytest.raises(
        encounter_journal.JournalError, match="unknown encounter 'enc-1'"
    ):
        encounter_journal.read("enc-1")


def test_a_second_caller_is_refused_the_id_the_first_one_claimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``O_EXCL``'s losing side, which is the whole reason allocation claims.

    Every other call site in this suite asserts ``is True``, and a taken id is
    the only case that says anything: ``claim`` collapses the test and the
    taking into one syscall precisely so two engines cannot both be told a name
    is free. Delete the ``FileExistsError`` arm and every fight-level test here
    still passes, because ``new_encounter_id`` only ever walks forward — the
    refusal is what it walks *on*.
    """
    _journal_root(tmp_path, monkeypatch)

    assert encounter_journal.claim("enc-9001") is True
    assert encounter_journal.claim("enc-9001") is False, (
        "a claimed id must be refused to the next caller; handing it out twice "
        "is two fights appending one journal"
    )


def test_a_claim_the_filesystem_refuses_is_a_named_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``claim``'s own ``OSError`` arm, which nothing had ever reached."""
    root = _journal_root(tmp_path, monkeypatch)
    root.mkdir(parents=True, exist_ok=True)
    # A fight's directory cannot be created under a regular file, so the
    # ``mkdir`` in ``claim`` fails with something that is not FileExistsError.
    (root / "enc-9001").write_text("not a directory", encoding="utf-8")

    with pytest.raises(encounter_journal.JournalError, match="cannot claim .*enc-9001"):
        encounter_journal.claim("enc-9001")


def test_an_append_whose_lock_cannot_be_taken_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``file_lock`` is the one primitive here that raises outside the family.

    ``durable.py`` opens the guard with ``O_NOFOLLOW`` on purpose — "a lock that
    cannot be taken safely fails the write rather than skipping it" — and that
    refusal is an ``OSError``. Nothing in ``service/`` may hand a caller one:
    this module already turns seven other ``OSError`` sites into
    :class:`JournalError`, and the two ``file_lock`` call sites were the ones
    that got missed, so a booby-trapped lock left the write path answering a
    bare 500 instead of naming the file.
    """
    root = _journal_root(tmp_path, monkeypatch)
    encounter_id = mapless_fight(seed=241)
    guard = durable.lock_path(journal_path(root, encounter_id))
    guard.unlink()
    guard.symlink_to(tmp_path / "elsewhere")

    with pytest.raises(encounter_journal.JournalError, match="cannot lock .*journal.jsonl"):
        encounter_journal.append(encounter_id, {"kind": "note"})


def test_a_lock_that_will_not_let_go_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape one line below the one above, and the reason a flag was wrong.

    ``file_lock``'s own ``finally`` releases and then closes, and
    ``TestLockLifecycle`` pins that a failing release propagates an ``OSError``
    rather than being swallowed — correct there, because that failure is real
    and the descriptor still has to close. It arrives here *after* the body has
    finished, so a wrapper that asks only "did we acquire?" attributes it to the
    body, declines to translate, and leaks the very thing it was added to catch.

    Acquiring and releasing get different sentences rather than one, because an
    operator can do something about a guard that will not open and nothing at
    all about one that will not let go: the write already happened.
    """
    _journal_root(tmp_path, monkeypatch)
    encounter_id = mapless_fight(seed=251)

    def refuse(descriptor: int) -> None:
        raise OSError("release failed")

    monkeypatch.setattr(durable, "_release", refuse)

    with pytest.raises(
        encounter_journal.JournalError, match="cannot release the lock on .*journal.jsonl"
    ):
        encounter_journal.append(encounter_id, {"kind": "note"})


def test_a_prune_whose_lock_cannot_be_taken_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same hole on the destructive path, where it matters most.

    ``journal.jsonl.lock`` is in :data:`_RECLAIMABLE_NAMES`, so a symlink wearing
    that name passes the "directory holds only what a claim leaves" filter and
    reaches ``file_lock`` — which means the operator asking an engine to reclaim
    stranded ids could be answered with an unhandled error rather than a
    sentence naming the directory that would not go.
    """
    root = _journal_root(tmp_path, monkeypatch)
    assert encounter_journal.claim("enc-9001") is True
    durable.lock_path(journal_path(root, "enc-9001")).symlink_to(tmp_path / "elsewhere")

    # The dry run still reads and writes nothing at all, lock included.
    assert encounter_journal.prune(apply=False) == ["enc-9001"]

    with pytest.raises(encounter_journal.JournalError, match="cannot lock .*journal.jsonl"):
        encounter_journal.prune(apply=True)


def test_a_prune_refusal_reaches_the_caller_as_a_request_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``encounters.prune`` was the one journal caller translating nothing.

    ``JournalError`` is a bare ``ValueError`` rather than a ``RequestError``, and
    every other caller — ``sessions.journal_append``, ``sessions.recover_session``,
    ``encounters.list_encounters`` — catches it and re-raises. This one passed it
    straight through, so the refusal the part-way-prune case exists to produce
    reached the adapter's catch-all instead of its ``RequestError`` arm.
    """
    root = _journal_root(tmp_path, monkeypatch)
    assert encounter_journal.claim("enc-9001") is True
    durable.lock_path(journal_path(root, "enc-9001")).symlink_to(tmp_path / "elsewhere")

    with pytest.raises(RequestError, match="cannot lock .*journal.jsonl"):
        api.encounter_prune(apply=True)


def test_pruning_reports_the_ids_it_would_reap_and_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run is the default because the alternative is a deletion by typo."""
    root = _journal_root(tmp_path, monkeypatch)
    kept = mapless_fight(seed=227)
    assert encounter_journal.claim("enc-9001") is True

    reported = api.encounter_prune()

    assert reported["applied"] is False
    assert reported["encounters"] == ["enc-9001"]
    assert (root / "enc-9001").is_dir()
    assert (root / kept).is_dir()


def test_pruning_removes_a_claimed_id_nobody_ever_wrote_a_fight_into(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``claim`` takes an id with an empty file and nothing has ever reaped one.

    The lock goes with it, because a lock is mutual exclusion over an inode and
    the inode it guarded is going: leaving one behind would be leaving a file
    that excludes nobody.
    """
    root = _journal_root(tmp_path, monkeypatch)
    kept = mapless_fight(seed=229)
    assert encounter_journal.claim("enc-9001") is True
    durable.lock_path(journal_path(root, "enc-9001")).touch()

    reported = api.encounter_prune(apply=True)

    assert reported["applied"] is True
    assert reported["encounters"] == ["enc-9001"]
    assert not (root / "enc-9001").exists()
    assert journal_path(root, kept).is_file()


def test_pruning_leaves_a_fight_that_has_a_creation_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One record is a fight somebody started. Only *nothing* is reapable."""
    root = _journal_root(tmp_path, monkeypatch)
    untouched = mapless_fight(seed=233)
    finished = mapless_fight(seed=239)
    api.encounter_finalize(finished)

    reported = api.encounter_prune(apply=True)

    assert reported["encounters"] == []
    assert journal_path(root, untouched).is_file()
    assert journal_path(root, finished).is_file()


def test_pruning_leaves_a_directory_holding_anything_it_did_not_expect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reaping is defined by what is *there*, never by what the name suggests.

    An empty journal beside a file this build does not recognise is not a
    reclaimed id — it is somebody else's business in a directory that happens to
    be named like one, and deleting the directory would take it with them.
    """
    root = _journal_root(tmp_path, monkeypatch)
    assert encounter_journal.claim("enc-9001") is True
    (root / "enc-9001" / "notes.txt").write_text("mine", encoding="utf-8")

    reported = api.encounter_prune(apply=True)

    assert reported["encounters"] == []
    assert (root / "enc-9001" / "notes.txt").is_file()


def test_pruning_will_not_follow_a_symlink_wearing_a_fights_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk decides what to delete, so the walk may not follow a link.

    ``is_dir()`` and ``iterdir()`` both follow, so a symlink named ``enc-7``
    used to be graded on *the victim's* contents: a directory holding an empty
    ``journal.jsonl`` and its lock satisfies :data:`_RECLAIMABLE_NAMES`, and
    both files were unlinked before ``rmdir`` finally hit ``ENOTDIR`` on the
    link itself. Two files deleted outside the encounters root, and the refusal
    named neither — ``reaped`` is appended after the directory goes, so the
    report said nothing had happened.

    ``durable.file_lock`` already opens its guard with ``O_NOFOLLOW`` for
    exactly this reason and says so. The operation that *deletes* has more to
    lose by following a link than the one that locks, not less.

    Skipped rather than refused, like a non-directory and a name outside the
    grammar: a link here is not this build's artifact, and prune's whole rule is
    that it removes only what a claim leaves.
    """
    root = _journal_root(tmp_path, monkeypatch)
    root.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "somebody-elses-directory"
    victim.mkdir()
    (victim / encounter_journal.JOURNAL_FILENAME).write_text("", encoding="utf-8")
    (victim / f"{encounter_journal.JOURNAL_FILENAME}.lock").write_text("", encoding="utf-8")
    (root / "enc-7").symlink_to(victim, target_is_directory=True)

    assert encounter_journal.prune(apply=False) == []
    assert encounter_journal.prune(apply=True) == []

    assert sorted(path.name for path in victim.iterdir()) == [
        encounter_journal.JOURNAL_FILENAME,
        f"{encounter_journal.JOURNAL_FILENAME}.lock",
    ], "prune followed a symlink and deleted inside somebody else's directory"
    assert (root / "enc-7").is_symlink(), "the link itself is not this build's to remove"


def test_pruning_an_empty_root_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing on disk yet is the state every first run is in."""
    _journal_root(tmp_path, monkeypatch)

    assert api.encounter_prune(apply=True) == {"applied": True, "encounters": []}


def test_a_fresh_engine_starts_past_the_ids_already_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seeding is the whole point of ``_seed_from_disk``, so it is what is
    asserted — not the id that comes out.

    ``claim`` refuses a name that is taken, so a engine seeded at zero walks
    ``enc-1``, ``enc-2``, … until it clears the directory and *still* returns
    the right id. Every observable outcome is identical; only the count of
    attempts differs, which is exactly the cost the function exists to avoid
    and exactly why a test of the returned id would have stayed green through
    the layout move that broke it.
    """
    root = _journal_root(tmp_path, monkeypatch)
    for taken in ("enc-1", "enc-2", "enc-7"):
        assert encounter_journal.claim(taken) is True
    assert sorted(path.parent.name for path in encounter_journal.list_journals()) == [
        "enc-1",
        "enc-2",
        "enc-7",
    ], "the fixture must put ids on disk for this to say anything"

    attempted: list[str] = []
    real_claim = encounter_journal.claim

    def counted(encounter_id: str) -> bool:
        attempted.append(encounter_id)
        return real_claim(encounter_id)

    # `sessions` binds this module under an alias rather than copying the
    # function out of it, so patching the module here is patching the one the
    # allocator calls.
    monkeypatch.setattr(encounter_journal, "claim", counted)
    api.STATE.next_id = 0
    api.STATE.sessions.clear()

    allocated = sessions_service.new_encounter_id(api.STATE)

    assert allocated == "enc-8"
    assert attempted == ["enc-8"], (
        f"a seeded engine claims once; this one walked {attempted} past ids the "
        f"directory already held, in {root}"
    )


def test_pruning_reports_what_it_already_reclaimed_when_a_later_one_will_not_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure part-way through must not delete the record of the work done.

    ``prune`` removes a directory outside the lock that guarded its journal, and
    the docstring is explicit that a creation between its ``claim`` and its
    first append is not excluded — so a directory can refuse to go. Raising past
    the ids already removed tells the operator nothing was pruned when several
    were, and the ids are gone either way: the report is the only record.
    """
    root = _journal_root(tmp_path, monkeypatch)
    for claimed in ("enc-9001", "enc-9002"):
        assert encounter_journal.claim(claimed) is True

    real_remove = encounter_journal._remove

    def obstruct(path: Path) -> None:
        # Stand in for the race the docstring names: something arrives in the
        # second directory after its journal was unlinked under the lock.
        if path.name == "enc-9002":
            (path / "journal.jsonl").write_text("", encoding="utf-8")
        real_remove(path)

    monkeypatch.setattr(encounter_journal, "_remove", obstruct)

    with pytest.raises(
        encounter_journal.JournalError, match="cannot prune .*enc-9002"
    ) as raised:
        encounter_journal.prune(apply=True)

    assert getattr(raised.value, "reaped", None) == ["enc-9001"], (
        "the ids already reclaimed have to survive the refusal; they are gone "
        "from disk and this is the only place they are named"
    )
    assert "enc-9001" in str(raised.value), (
        "an attribute does not cross the adapter — `web/http_server.py` renders a "
        "ValueError into problem+json from its message and reads nothing else — so "
        "an operator calling this over HTTP sees the ids only if the sentence "
        "carries them"
    )
    assert not (root / "enc-9001").exists()
