"""Adventures: an ordered run of encounters, with the party carried between them.

Two claims live here and they are different in kind.

**What ``carry_forward`` produces** is a *creation spec*, not a report. So every
case about it asserts through the derived property a rules reader cares about —
``dying``, ``conscious`` — rather than the raw flag, and the key-set cases
derive their expectation from ``specs.DESCRIBED_SPEC_KEYS`` and from a real
state payload rather than from a list written here. A literal set would pin each
side against a copy of itself, which is exactly how ``facing`` and the four
carry-over keys reached one declaration and not the other.

**What the adventure document does** is durable and shared, so its cases are
about the file: it is written by ``durable.guarded_write``, a caller holding a
stale version is refused rather than merged, and a retried link under one
``request_id`` links once. The two-process race lives in
``test_durable_writes.py`` with the other concurrency cases.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fivee_sim.service import adventures, specs
from fivee_sim.service.errors import NotFoundError, RequestError, StaleWriteError

from . import api
from .conftest import AMBUSHER, LOOKOUT, MILL, SCOUT

#: Two hand-written combatants standing next to each other, each swinging at a
#: bonus that beats the other's AC on all but a natural 1. The fight only has to
#: move somebody's hit points off their maximum, and a looked-up monster would
#: make how long that takes a property of the stat block.
BRAWLER: dict[str, Any] = {
    "name": "Thora",
    "team": "party",
    "ac": 10,
    "max_hp": 30,
    "position": [0, 0],
    "abilities": {"strength": 18, "dexterity": 12, "constitution": 14},
    "size": "large",
    "resistances": ["cold"],
    "attacks": [
        {
            "name": "Longsword",
            "attack_bonus": 20,
            "damage": "2d6+3",
            "damage_type": "slashing",
            "kind": "melee",
        }
    ],
}
RUFFIAN: dict[str, Any] = {
    "name": "Bram",
    "team": "monsters",
    "ac": 10,
    "max_hp": 30,
    "position": [5, 0],
    "attacks": [
        {
            "name": "Club",
            "attack_bonus": 20,
            "damage": "2d6+3",
            "damage_type": "bludgeoning",
            "kind": "melee",
        }
    ],
}

#: Keys a spec accepts *and* the state payload reports, that are deliberately
#: not carried. Written out so shrinking ``CARRIED_STATE_KEYS`` is a decision
#: somebody has to record here rather than a silent loss of state: the derived
#: case below subtracts the carried set from the real overlap and holds the
#: remainder against exactly this.
NOT_CARRIED: frozenset[str] = frozenset({
    # Facts about the creature that a fight cannot change. Carrying them would
    # be a no-op; the capture already has them, and has them right.
    "name", "team", "ac", "max_hp", "death_rule", "terrain_cost_overrides",
    "bonus_actions", "redirect_attack", "spells",
    # Reset rather than carried: whoever walks into the next fight is there when
    # it starts, however late they joined the last one.
    "arrival_round",
    # One name, two shapes. The state payload *names* a combatant's attacks; a
    # spec *describes* them, and overlaying one onto the other would hand
    # `attack_from_spec` a list of strings.
    "attacks",
})


def combatant(state: dict[str, Any], name: str) -> dict[str, Any]:
    return next(entry for entry in state["combatants"] if entry["name"] == name)


def shapes(encounter_id: str, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The two payloads ``carry_forward`` joins, taken from a real fight.

    Built by the engine rather than by hand: the whole point of the function is
    that the *captured creation input* and the *live state* have different key
    sets, so a fixture written here would be a third shape agreeing with neither.
    """
    session = api.STATE.sessions[encounter_id]
    normalized = next(
        entry for entry in session.normalized_combatants if entry["name"] == name
    )
    return normalized, combatant(api.encounter_state(encounter_id), name)


def land_a_hit(encounter_id: str, attacker: str, victim: str, limit: int = 12) -> int:
    """Fight until ``victim`` is off their maximum, and answer what they are on."""
    for _ in range(limit):
        entry = combatant(api.encounter_state(encounter_id), victim)
        if int(entry["hp"]) < int(entry["max_hp"]):
            return int(entry["hp"])
        if api.encounter_state(encounter_id)["turn"] == attacker:
            api.encounter_act(encounter_id, "attack", target=victim)
        api.encounter_advance(encounter_id)
    raise AssertionError(f"{attacker} never hurt {victim} in {limit} turns")


class TestCarryForward:
    """The join: captured creation input, overlaid with how the fight left them."""

    def test_a_carried_combatant_is_a_complete_spec_and_nothing_more(self) -> None:
        # Derived from the declaration a spec is read against, in both
        # directions. A key the join drops is state the next fight never hears
        # about; a key it invents is one `creature_from_spec` refuses outright.
        encounter_id = str(api.encounter_create([BRAWLER, RUFFIAN], seed=41)["encounter_id"])
        normalized, live = shapes(encounter_id, "Thora")

        carried = adventures.carry_forward(normalized, live)

        assert set(carried) == specs.DESCRIBED_SPEC_KEYS

    def test_every_carried_key_is_one_a_combatant_spec_accepts(self) -> None:
        # The other half of the same contract, asserted against the declaration
        # rather than against an example: a carried key outside this set would
        # be refused by `reject_unknown_keys` the moment some fight happened to
        # produce it.
        assert adventures.CARRIED_STATE_KEYS <= specs.DESCRIBED_SPEC_KEYS

    def test_every_carried_key_is_one_the_state_payload_really_reports(self) -> None:
        # The failure this closes is silent: `carry_forward` overlays only the
        # keys the state actually has, so a misspelled name here would simply
        # never overlay anything and every other case would stay green. Read off
        # a fight on a map, with a facing, because `level` and `facing` are the
        # two keys the payload omits when there is nothing to say.
        hero = dict(BRAWLER) | {"facing": "north"}
        encounter_id = str(
            api.encounter_create(
                [hero, dict(RUFFIAN)],
                seed=42,
                map={"width": 6, "height": 6, "default_terrain": "normal"},
            )["encounter_id"]
        )
        _normalized, live = shapes(encounter_id, "Thora")

        assert adventures.CARRIED_STATE_KEYS <= set(live), sorted(
            adventures.CARRIED_STATE_KEYS - set(live)
        )

    def test_no_state_a_spec_could_carry_is_dropped_without_saying_so(self) -> None:
        # The guard the two subset cases above cannot give. Both of them stay
        # green when a key is *removed* from the carried set — a smaller set is
        # still a subset of everything — so neither notices state quietly
        # ceasing to cross between fights. This one subtracts the carried set
        # from the real overlap of "what a spec accepts" and "what the state
        # reports", and holds the remainder against the exclusions written at
        # the top of this file with their reasons.
        hero = dict(BRAWLER) | {"facing": "north"}
        encounter_id = str(
            api.encounter_create(
                [hero, dict(RUFFIAN)],
                seed=52,
                map={"width": 6, "height": 6, "default_terrain": "normal"},
            )["encounter_id"]
        )
        _normalized, live = shapes(encounter_id, "Thora")

        overlap = specs.DESCRIBED_SPEC_KEYS & set(live)

        assert overlap, "the two shapes share no keys at all; this case is vacuous"
        assert overlap - adventures.CARRIED_STATE_KEYS == NOT_CARRIED

    def test_a_stabilised_combatant_walks_into_the_next_fight_stable(self) -> None:
        # Asserted through `dying`, which is derived as `not dead and hp == 0
        # and not stable` — the raw flag is only interesting because of what it
        # decides. The control below is the same fight with `stable` dropped.
        down = dict(BRAWLER) | {
            "hp": 0, "stable": True, "death_saves": {"successes": 2, "failures": 1}
        }
        first = str(api.encounter_create([down, RUFFIAN], seed=43)["encounter_id"])
        normalized, live = shapes(first, "Thora")

        carried = adventures.carry_forward(normalized, live)
        second = str(
            api.encounter_create([carried, dict(RUFFIAN)], seed=44)["encounter_id"]
        )
        arrived = combatant(api.encounter_state(second), "Thora")

        assert arrived["hp"] == 0
        assert arrived["stable"] is True
        assert arrived["dying"] is False
        assert arrived["death_saves"] == {"successes": 2, "failures": 1}

    def test_the_same_combatant_without_the_carried_flag_is_dying(self) -> None:
        # The control for the case above: it is the identical spec with one
        # carried field removed, and it must fail the assertion that one passes.
        # Without it "stable survives" would also hold for a join that carried
        # nothing at all, since the fight above never moved anybody.
        down = dict(BRAWLER) | {"hp": 0, "stable": True}
        first = str(api.encounter_create([down, RUFFIAN], seed=45)["encounter_id"])
        normalized, live = shapes(first, "Thora")

        careless = dict(adventures.carry_forward(normalized, live))
        careless["stable"] = False
        second = str(
            api.encounter_create([careless, dict(RUFFIAN)], seed=46)["encounter_id"]
        )
        arrived = combatant(api.encounter_state(second), "Thora")

        assert arrived["stable"] is False
        assert arrived["dying"] is True, (
            "losing `stable` must flip `dying`, or the case above proves nothing"
        )

    def test_a_fight_that_takes_somebodys_stabilisation_away_carries_that(self) -> None:
        # The case the two above cannot make, and the one that proves the
        # overlay does any work at all: both of them start the fight with
        # `stable` already set, so the captured creation input and the ending
        # state agree and a join that ignored the state entirely would pass.
        # Here the *fight* moves it — a hit on a creature at 0 hit points is a
        # critical, which clears `stable` and books two failures — so the
        # capture says stabilised and only the state knows better.
        #
        # Three combatants because two would not do: a fight ends the moment one
        # side has nobody conscious, so Wren is what keeps it running long
        # enough for Bram to reach the floor.
        down = dict(BRAWLER) | {"hp": 0, "stable": True}
        medic = dict(BRAWLER) | {"name": "Wren", "position": [0, 5]}
        first = str(
            api.encounter_create([down, medic, RUFFIAN], seed=43)["encounter_id"]
        )
        for _ in range(8):
            if combatant(api.encounter_state(first), "Thora")["stable"] is False:
                break
            if api.encounter_state(first)["turn"] == "Bram":
                api.encounter_act(first, "attack", target="Thora")
            api.encounter_advance(first)
        else:  # pragma: no cover - a fixture that stopped working, not a branch
            raise AssertionError("nobody ever knocked Thora off her stabilisation")

        normalized, live = shapes(first, "Thora")
        carried = adventures.carry_forward(normalized, live)
        second = str(
            api.encounter_create(
                [carried, dict(medic), dict(RUFFIAN)], seed=44
            )["encounter_id"]
        )
        arrived = combatant(api.encounter_state(second), "Thora")

        assert normalized["stable"] is True, "the capture must still say stabilised"
        assert live["stable"] is False and live["death_saves"]["failures"] > 0
        assert carried["stable"] is False
        assert arrived["stable"] is False
        assert arrived["dying"] is True
        assert arrived["death_saves"] == live["death_saves"]

    def test_a_killed_combatant_carries_its_death_rather_than_getting_up(self) -> None:
        dead = dict(BRAWLER) | {"hp": 0, "dead": True}
        first = str(api.encounter_create([dead, RUFFIAN], seed=47)["encounter_id"])
        normalized, live = shapes(first, "Thora")

        carried = adventures.carry_forward(normalized, live)
        second = str(
            api.encounter_create([carried, dict(RUFFIAN)], seed=48)["encounter_id"]
        )
        arrived = combatant(api.encounter_state(second), "Thora")

        assert carried["dead"] is True
        assert arrived["dead"] is True
        # `conscious` is derived as `not dead and hp > 0`, and `dying` as `not
        # dead and ...` — so a lost `dead` would show up as a creature who is
        # bleeding out rather than one who is gone.
        assert arrived["conscious"] is False
        assert arrived["dying"] is False

    def test_a_carried_combatant_arrives_in_round_one_however_late_it_was(self) -> None:
        latecomer = dict(BRAWLER) | {"arrival_round": 4}
        encounter_id = str(
            api.encounter_create([latecomer, RUFFIAN], seed=49)["encounter_id"]
        )
        normalized, live = shapes(encounter_id, "Thora")

        carried = adventures.carry_forward(normalized, live)

        assert normalized["arrival_round"] == 4 and live["arrival_round"] == 4
        assert carried["arrival_round"] == 1

    def test_initiative_and_concentration_do_not_cross_into_the_next_fight(self) -> None:
        encounter_id = str(api.encounter_create([BRAWLER, RUFFIAN], seed=50)["encounter_id"])
        normalized, live = shapes(encounter_id, "Thora")

        carried = adventures.carry_forward(normalized, live)

        # Not vacuous: the state payload really does report both.
        assert {"initiative", "concentrating_on"} <= set(live)
        assert not {"initiative", "concentrating_on"} & set(carried)

    def test_the_carried_spec_keeps_what_the_state_payload_never_reported(self) -> None:
        # Why the join starts from the captured creation input rather than
        # projecting from state: `_creature_state` emits `attacks` as bare names
        # and emits no abilities, save bonuses, resistances or size at all, so a
        # state-only projection rebuilds a creature with no attacks and default
        # stats — and nothing refuses it, because none of those keys is required.
        encounter_id = str(api.encounter_create([BRAWLER, RUFFIAN], seed=51)["encounter_id"])
        normalized, live = shapes(encounter_id, "Thora")

        carried = adventures.carry_forward(normalized, live)

        assert live["attacks"] == ["Longsword"], "the state payload names attacks only"
        assert not {"abilities", "save_bonuses", "resistances", "size"} & set(live)
        assert carried["attacks"] == normalized["attacks"]
        assert carried["attacks"][0]["damage"] == "2d6+3"
        assert carried["abilities"]["strength"] == 18
        assert carried["resistances"] == ["cold"]
        assert carried["size"] == "large"


class TestTheAdventureDocument:
    """A guarded document, not a journal: one small write per encounter."""

    def test_a_new_adventure_is_an_empty_run_written_where_encounters_live(self) -> None:
        created = api.adventure_create("The Sunless Citadel")

        assert created["format"] == "fivee-sim-adventure"
        assert created["format_version"] == 1
        assert created["id"] == "adv-1"
        assert created["name"] == "The Sunless Citadel"
        assert created["status"] == "active"
        assert created["members"] == []
        assert created["version"]

        path = adventures.adventure_path("adv-1")
        assert json.loads(path.read_text(encoding="utf-8"))["id"] == "adv-1"

    def test_the_ids_do_not_collide_with_the_journals_beside_them(self) -> None:
        # `adventures` and `encounter_journal` share a directory on purpose, and
        # this is the property that makes that safe: `list_journals` globs
        # `enc-*.jsonl` and an adventure is `adv-<n>.json`, so neither listing
        # can ever report the other's files.
        adventure = api.adventure_create("Shared Roots")
        api.adventure_encounter(
            str(adventure["id"]), combatants=[BRAWLER, RUFFIAN], seed=52
        )

        listed = {entry["encounter_id"] for entry in api.encounter_list("all")["encounters"]}
        assert listed == {"enc-1"}
        assert {entry["adventure_id"] for entry in api.adventure_list("all")["adventures"]} == {
            "adv-1"
        }

    def test_reading_one_back_answers_the_document_and_its_version(self) -> None:
        created = api.adventure_create("Barrow of the Forgotten King")

        read = api.adventure_state("adv-1")

        assert read["name"] == "Barrow of the Forgotten King"
        assert read["version"] == created["version"]

    def test_an_unknown_adventure_names_what_is_actually_there(self) -> None:
        api.adventure_create("The Sunless Citadel")

        with pytest.raises(NotFoundError, match="no adventure 'adv-9'; adventures here: adv-1"):
            api.adventure_state("adv-9")

    def test_an_id_outside_the_grammar_is_an_unknown_adventure_not_a_path(self) -> None:
        with pytest.raises(NotFoundError, match=r"no adventure '\.\./\.\./etc/passwd'"):
            api.adventure_state("../../etc/passwd")

    def test_a_blank_name_is_refused_rather_than_written(self) -> None:
        with pytest.raises(RequestError, match="adventure name must not be blank"):
            api.adventure_create("   ")

    def test_the_listing_filters_by_status_the_way_encounters_do(self) -> None:
        api.adventure_create("One")
        api.adventure_create("Two")
        api.adventure_finalize("adv-1")

        assert [entry["adventure_id"] for entry in api.adventure_list()["adventures"]] == [
            "adv-2"
        ]
        assert [
            entry["adventure_id"] for entry in api.adventure_list("finalized")["adventures"]
        ] == ["adv-1"]
        assert len(api.adventure_list("all")["adventures"]) == 2

    def test_an_unknown_status_filter_names_the_three_that_work(self) -> None:
        with pytest.raises(RequestError, match="status must be active, finalized, or all"):
            api.adventure_list("halfway")

    def test_finalizing_closes_the_run_and_saying_so_twice_is_the_same_answer(self) -> None:
        api.adventure_create("Tomb of Horrors")

        first = api.adventure_finalize("adv-1")
        second = api.adventure_finalize("adv-1")

        assert first["status"] == "finalized"
        assert second["status"] == "finalized"
        assert first["version"] == second["version"], "a second finalize must not rewrite it"

    def test_a_finalized_adventure_takes_no_further_encounters(self) -> None:
        api.adventure_create("Tomb of Horrors")
        api.adventure_finalize("adv-1")

        with pytest.raises(RequestError, match="adventure 'adv-1' is finalized"):
            api.adventure_encounter("adv-1", combatants=[BRAWLER, RUFFIAN], seed=53)

    def test_a_writer_holding_a_version_someone_else_replaced_is_refused(self) -> None:
        created = api.adventure_create("Against the Giants")
        stale = str(created["version"])
        api.adventure_encounter("adv-1", combatants=[BRAWLER, RUFFIAN], seed=54)

        with pytest.raises(StaleWriteError, match="the adventure 'adv-1' has advanced"):
            api.adventure_encounter(
                "adv-1",
                carry=[],
                combatants=[dict(BRAWLER), dict(RUFFIAN)],
                seed=55,
                expected_version=stale,
            )
        assert len(api.adventure_state("adv-1")["members"]) == 1
        # Refused before the fight was started, not after. A link that created
        # its encounter and only then found itself stale would leave a whole
        # journal on disk belonging to no run at all.
        assert len(api.encounter_list("all")["encounters"]) == 1


class TestLinkingEncounters:
    """The link call is where an adventure is more than a name."""

    def test_the_first_encounter_is_created_and_recorded_in_order(self) -> None:
        api.adventure_create("Keep on the Borderlands")

        linked = api.adventure_encounter(
            "adv-1", combatants=[BRAWLER, RUFFIAN], seed=56
        )

        assert linked["index"] == 0
        assert linked["carried"] == []
        assert linked["encounter"]["seed"] == 56
        members = api.adventure_state("adv-1")["members"]
        assert [entry["encounter_id"] for entry in members] == [linked["encounter_id"]]
        assert [entry["index"] for entry in members] == [0]

    def test_the_second_encounter_starts_at_the_first_ones_ending_hit_points(self) -> None:
        # The headline claim, end to end and through the engine's own state
        # rather than through the join in isolation: fight until somebody is
        # hurt, link the next encounter, and read what they walked in on.
        api.adventure_create("The Sunless Citadel")
        first = api.adventure_encounter("adv-1", combatants=[BRAWLER, RUFFIAN], seed=57)
        first_id = str(first["encounter_id"])
        ending_hp = land_a_hit(first_id, attacker="Bram", victim="Thora")

        second = api.adventure_encounter(
            "adv-1",
            carry=["Thora"],
            combatants=[dict(RUFFIAN) | {"name": "Skeleton", "position": [10, 0]}],
            seed=58,
        )
        arrived = combatant(api.encounter_state(str(second["encounter_id"])), "Thora")

        assert ending_hp < 30, "the first fight never hurt anybody, so nothing is proved"
        assert arrived["hp"] == ending_hp
        assert arrived["max_hp"] == 30
        assert second["index"] == 1 and second["carried"] == ["Thora"]

    def test_carrying_nobody_by_name_leaves_the_last_fights_cast_behind(self) -> None:
        api.adventure_create("The Sunless Citadel")
        api.adventure_encounter("adv-1", combatants=[BRAWLER, RUFFIAN], seed=59)

        second = api.adventure_encounter(
            "adv-1",
            carry=["Thora"],
            combatants=[dict(RUFFIAN) | {"name": "Skeleton", "position": [10, 0]}],
            seed=60,
        )
        names = {
            entry["name"]
            for entry in api.encounter_state(str(second["encounter_id"]))["combatants"]
        }

        assert names == {"Thora", "Skeleton"}

    def test_carrying_nothing_by_default_brings_the_whole_previous_cast(self) -> None:
        api.adventure_create("The Sunless Citadel")
        api.adventure_encounter("adv-1", combatants=[BRAWLER, RUFFIAN], seed=61)

        second = api.adventure_encounter("adv-1", seed=62)
        names = {
            entry["name"]
            for entry in api.encounter_state(str(second["encounter_id"]))["combatants"]
        }

        assert second["carried"] == ["Thora", "Bram"]
        assert names == {"Thora", "Bram"}

    def test_a_recovery_delta_is_applied_before_the_carry_over(self) -> None:
        api.adventure_create("The Sunless Citadel")
        first = api.adventure_encounter("adv-1", combatants=[BRAWLER, RUFFIAN], seed=63)
        ending_hp = land_a_hit(str(first["encounter_id"]), attacker="Bram", victim="Thora")

        second = api.adventure_encounter(
            "adv-1",
            carry=["Thora"],
            recovery={"Thora": {"hp": 30, "position": [20, 20]}},
            combatants=[dict(RUFFIAN) | {"name": "Skeleton", "position": [10, 0]}],
            seed=64,
        )
        arrived = combatant(api.encounter_state(str(second["encounter_id"])), "Thora")

        assert ending_hp < 30
        assert arrived["hp"] == 30
        assert arrived["position"] == [20, 20]

    def test_a_recovery_hp_exceeding_max_hp_is_refused(self) -> None:
        # SRD 5.2.1 Rules Glossary: "You can't have more Hit Points than your Hit
        # Point maximum." The recovery door must validate this too.
        api.adventure_create("The Sunless Citadel")
        api.adventure_encounter("adv-1", combatants=[BRAWLER, RUFFIAN], seed=65)

        with pytest.raises(
            RequestError, match="combatant Thora: hp 500 cannot exceed max_hp"
        ):
            api.adventure_encounter(
                "adv-1",
                carry=["Thora"],
                recovery={"Thora": {"hp": 500}},
                seed=66,
                combatants=[dict(RUFFIAN) | {"name": "Skeleton", "position": [10, 0]}],
            )

    def test_a_recovery_key_that_is_not_carried_state_is_refused(self) -> None:
        api.adventure_create("The Sunless Citadel")
        api.adventure_encounter("adv-1", combatants=[BRAWLER, RUFFIAN], seed=65)

        with pytest.raises(RequestError, match="unknown recovery key 'max_hp' for 'Thora'"):
            api.adventure_encounter(
                "adv-1", carry=["Thora"], recovery={"Thora": {"max_hp": 99}}, seed=66,
                combatants=[dict(RUFFIAN) | {"name": "Skeleton", "position": [10, 0]}],
            )

    def test_a_recovery_for_somebody_who_is_not_coming_is_refused(self) -> None:
        api.adventure_create("The Sunless Citadel")
        api.adventure_encounter("adv-1", combatants=[BRAWLER, RUFFIAN], seed=67)

        with pytest.raises(RequestError, match="cannot recover 'Bram': it is not being carried"):
            api.adventure_encounter(
                "adv-1", carry=["Thora"], recovery={"Bram": {"hp": 30}}, seed=68,
                combatants=[dict(RUFFIAN) | {"name": "Skeleton", "position": [10, 0]}],
            )

    def test_carrying_somebody_the_last_fight_never_had_is_refused(self) -> None:
        api.adventure_create("The Sunless Citadel")
        api.adventure_encounter("adv-1", combatants=[BRAWLER, RUFFIAN], seed=69)

        with pytest.raises(RequestError, match="cannot carry 'Wren'"):
            api.adventure_encounter("adv-1", carry=["Wren"], seed=70)

    def test_carrying_from_an_adventure_with_no_encounters_yet_is_refused(self) -> None:
        api.adventure_create("The Sunless Citadel")

        with pytest.raises(RequestError, match="has no encounter to carry from yet"):
            api.adventure_encounter("adv-1", carry=["Thora"], seed=71)

    def test_a_retried_link_under_one_request_id_links_once(self) -> None:
        api.adventure_create("The Sunless Citadel")

        first = api.adventure_encounter(
            "adv-1", combatants=[BRAWLER, RUFFIAN], seed=72, request_id="link-1"
        )
        again = api.adventure_encounter(
            "adv-1", combatants=[BRAWLER, RUFFIAN], seed=72, request_id="link-1"
        )

        assert again["encounter_id"] == first["encounter_id"]
        assert again["index"] == first["index"] == 0
        assert again["encounter"]["seed"] == first["encounter"]["seed"]
        assert len(api.adventure_state("adv-1")["members"]) == 1
        assert len(api.encounter_list("all")["encounters"]) == 1

    def test_a_retried_creation_under_one_request_id_makes_one_adventure(self) -> None:
        first = api.adventure_create("The Sunless Citadel", request_id="new-1")
        again = api.adventure_create("The Sunless Citadel", request_id="new-1")

        assert again["id"] == first["id"]
        assert len(api.adventure_list("all")["adventures"]) == 1

    def test_a_key_a_creation_already_spent_cannot_be_reused_on_a_link(self) -> None:
        # The other direction, and the one that has to refuse rather than
        # sidestep: the key is recorded in *this* document, so linking under it
        # would overwrite the record the creation left and quietly make a later
        # retried creation start a second run.
        api.adventure_create("The Sunless Citadel", request_id="shared-key")

        with pytest.raises(
            RequestError, match="request id 'shared-key' was already used for"
        ):
            api.adventure_encounter(
                "adv-1", combatants=[BRAWLER, RUFFIAN], seed=76, request_id="shared-key"
            )
        assert api.adventure_state("adv-1")["members"] == []

    def test_a_key_a_link_recorded_does_not_answer_for_a_creation(self) -> None:
        # A request id is the caller's string and nothing stops one being reused
        # across operations. Matching the key alone would make this second call
        # answer with the adventure the *link* recorded, so the caller would be
        # handed a run they did not ask to start and never learn that the one
        # they did ask for was never created.
        api.adventure_create("The Sunless Citadel")
        api.adventure_encounter(
            "adv-1", combatants=[BRAWLER, RUFFIAN], seed=75, request_id="shared-key"
        )

        started = api.adventure_create("Barrow of the Forgotten King",
                                       request_id="shared-key")

        assert started["id"] == "adv-2"
        assert started["name"] == "Barrow of the Forgotten King"
        assert len(api.adventure_list("all")["adventures"]) == 2

    def test_two_combatants_of_the_same_name_are_refused_by_the_fight_itself(self) -> None:
        api.adventure_create("The Sunless Citadel")
        api.adventure_encounter("adv-1", combatants=[BRAWLER, RUFFIAN], seed=73)

        with pytest.raises(RequestError, match="combatant names must be unique"):
            api.adventure_encounter("adv-1", combatants=[dict(BRAWLER)], seed=74)


class TestInterludeChapters:
    """A run is fights *and* interludes, and the boundary keeps the ground.

    ``mode`` on the link is the whole of "start a chapter with no fight in it",
    and ``carry_map`` is the whole of "on the same floor as the last one". The
    party's squares already crossed the boundary — ``position`` has been in
    :data:`~fivee_sim.service.adventures.CARRIED_STATE_KEYS` since the first
    link — but the *map* did not, so an ambush at the mill meant restating the
    map id every chapter and a mistyped one silently put the fight somewhere
    else.
    """

    def link_the_mill(self, seed: int = 80) -> str:
        """An adventure whose first chapter is an interlude on a saved map."""
        api.map_save("mill", MILL, "*")
        api.adventure_create("The Drowned Mill")
        linked = api.adventure_encounter(
            "adv-1",
            combatants=[dict(SCOUT), dict(LOOKOUT)],
            seed=seed,
            map_id="mill",
            mode="exploration",
        )
        return str(linked["encounter_id"])

    def test_the_party_starts_the_fight_on_the_squares_the_interlude_left_them(
        self,
    ) -> None:
        # The claim the phase exists to make, end to end and against the
        # interlude's *live* ending state rather than a second copy of the
        # numbers: a run that carried nobody would leave `ending` and `arrived`
        # agreeing about an empty set of names, so the case asserts the walk
        # happened and that every name it produced arrived.
        interlude = self.link_the_mill()
        api.encounter_act(interlude, "move", to_position=[25, 25], actor="Kettle")
        api.encounter_act(interlude, "move", to_position=[25, 15], actor="Bo")
        ending = {
            entry["name"]: entry["position"]
            for entry in api.encounter_state(interlude)["combatants"]
        }
        api.encounter_finalize(interlude)

        ambush = api.adventure_encounter(
            "adv-1",
            combatants=[dict(AMBUSHER)],
            seed=81,
            carry_map=True,
            mode="combat",
        )
        state = api.encounter_state(str(ambush["encounter_id"]))
        arrived = {entry["name"]: entry["position"] for entry in state["combatants"]}

        assert ending == {"Kettle": [25, 25], "Bo": [25, 15]}
        assert ending["Kettle"] != SCOUT["position"], (
            "nobody moved in the interlude, so the carry-over proves nothing"
        )
        assert {name: arrived[name] for name in ending} == ending
        assert arrived["Stalker"] == AMBUSHER["position"]
        # The ground came with them, and it is the same file rather than a
        # second copy of it.
        assert state["map_source"]["map_id"] == "mill"
        assert state["mode"] == "combat"
        assert state["turn"] is not None, "the linked chapter is a fight again"

    def test_the_run_records_which_kind_of_chapter_each_member_is(self) -> None:
        interlude = self.link_the_mill(seed=82)
        api.encounter_finalize(interlude)
        api.adventure_encounter(
            "adv-1", combatants=[dict(AMBUSHER)], seed=83, carry_map=True
        )

        members = api.adventure_state("adv-1")["members"]

        assert [member["mode"] for member in members] == ["exploration", "combat"]

    def test_the_listing_says_the_shape_of_a_run_without_opening_a_chapter(
        self,
    ) -> None:
        # The point of putting it on the listing at all: an adventure's shape —
        # walk, fight, walk — is legible from one call that reads no journal and
        # no replay artifact.
        interlude = self.link_the_mill(seed=84)
        api.encounter_finalize(interlude)
        api.adventure_encounter(
            "adv-1", combatants=[dict(AMBUSHER)], seed=85, carry_map=True
        )

        entry = api.adventure_list("all")["adventures"][0]

        assert entry["encounters"] == 2
        assert entry["modes"] == ["exploration", "combat"]

    def test_a_link_that_says_nothing_about_the_mode_still_starts_a_fight(self) -> None:
        # Omission keeps meaning exactly what it meant, which is what lets every
        # caller written before interludes existed stay correct.
        api.adventure_create("Keep on the Borderlands")
        linked = api.adventure_encounter("adv-1", combatants=[BRAWLER, RUFFIAN], seed=86)

        assert api.encounter_state(str(linked["encounter_id"]))["mode"] == "combat"
        assert api.adventure_state("adv-1")["members"][0]["mode"] == "combat"

    def test_a_mode_nobody_declared_is_refused_by_the_one_declaration(self) -> None:
        api.adventure_create("The Drowned Mill")

        with pytest.raises(RequestError, match="mode must be one of: combat, exploration"):
            api.adventure_encounter(
                "adv-1", combatants=[dict(SCOUT)], seed=87, mode="wandering"
            )
        assert api.adventure_state("adv-1")["members"] == []


    def test_a_member_that_is_not_a_record_at_all_is_a_corrupt_document(self) -> None:
        # The listing now reads a field off every member rather than counting
        # them, so a document whose members are not records has to be refused
        # by name instead of raising out of the middle of a listing.
        api.adventure_create("The Sunless Citadel")
        path = adventures.adventure_path("adv-1")
        document = json.loads(path.read_text(encoding="utf-8"))
        document["members"] = ["enc-1"]
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

        with pytest.raises(RequestError, match="member 0 is not a record"):
            api.adventure_state("adv-1")

        listed = api.adventure_list("all")["adventures"]
        assert [entry["status"] for entry in listed] == ["corrupt"]
        assert "member 0 is not a record" in listed[0]["problem"]


class TestCarryingTheGround:
    """``carry_map`` reuses a *saved* map, resolved from the frozen journal."""

    def link_the_mill(self, seed: int = 88) -> str:
        api.map_save("mill", MILL, "*")
        api.adventure_create("The Drowned Mill")
        linked = api.adventure_encounter(
            "adv-1",
            combatants=[dict(SCOUT), dict(LOOKOUT)],
            seed=seed,
            map_id="mill",
            mode="exploration",
        )
        return str(linked["encounter_id"])

    def test_the_map_is_resolved_from_the_journal_and_not_a_live_session(self) -> None:
        # The same reason composition reads frozen artifacts: the previous
        # chapter's session is dropped here, so a resolver that reached for one
        # would either recover the whole fight to read one id off it or fail
        # outright. The record on disk is what a chapter *was* started on.
        interlude = self.link_the_mill()
        api.STATE.sessions.pop(interlude)

        linked = api.adventure_encounter(
            "adv-1", carry=[], combatants=[dict(AMBUSHER), dict(SCOUT)], seed=89,
            carry_map=True,
        )
        state = api.encounter_state(str(linked["encounter_id"]))

        assert interlude not in api.STATE.sessions
        assert state["map_source"]["map_id"] == "mill"

    def test_omitting_it_leaves_the_next_chapter_with_no_map_at_all(self) -> None:
        # The control for every case above: carrying is explicit because
        # omitting it means theatre of the mind, and that has to keep meaning
        # what it always meant.
        self.link_the_mill(seed=90)

        linked = api.adventure_encounter(
            "adv-1", combatants=[dict(AMBUSHER)], seed=91
        )

        assert api.encounter_state(str(linked["encounter_id"]))["map_source"] is None

    def test_carrying_a_map_and_naming_one_is_refused(self) -> None:
        self.link_the_mill(seed=92)

        with pytest.raises(
            RequestError, match="carry_map cannot be given with 'map_id'"
        ):
            api.adventure_encounter(
                "adv-1", combatants=[dict(AMBUSHER)], seed=93,
                carry_map=True, map_id="mill",
            )

    def test_carrying_a_map_and_sending_one_inline_is_refused(self) -> None:
        self.link_the_mill(seed=94)

        with pytest.raises(RequestError, match="carry_map cannot be given with 'map'"):
            api.adventure_encounter(
                "adv-1", combatants=[dict(AMBUSHER)], seed=95,
                carry_map=True, map=dict(MILL),
            )

    def test_carrying_a_map_before_there_is_a_chapter_to_carry_from_is_refused(
        self,
    ) -> None:
        api.adventure_create("The Drowned Mill")

        with pytest.raises(
            RequestError,
            match="adventure 'adv-1' has no encounter to carry a map from yet",
        ):
            api.adventure_encounter(
                "adv-1", combatants=[dict(SCOUT), dict(LOOKOUT)], seed=96,
                carry_map=True,
            )
        assert api.adventure_state("adv-1")["members"] == []

    def test_carrying_from_a_chapter_that_was_never_on_a_map_is_refused(self) -> None:
        api.adventure_create("The Sunless Citadel")
        api.adventure_encounter("adv-1", combatants=[BRAWLER, RUFFIAN], seed=97)

        with pytest.raises(
            RequestError,
            match="cannot carry the map of encounter 'enc-1': it was not on a map",
        ):
            api.adventure_encounter("adv-1", seed=98, carry_map=True)

    def test_carrying_a_map_that_was_sent_inline_is_a_different_refusal(self) -> None:
        # The distinction the three map keys force. An inline map *is* a map —
        # ``map_kind`` says ``inline`` — but it was never saved, so there is no
        # id to reuse and the remedy is to send the document again rather than
        # to put the chapter on a map at all. One message for both would send
        # the caller to the wrong fix.
        api.adventure_create("The Drowned Mill")
        api.adventure_encounter(
            "adv-1", combatants=[dict(SCOUT), dict(LOOKOUT)], seed=99,
            map=dict(MILL), mode="exploration",
        )

        with pytest.raises(
            RequestError,
            match=(
                "cannot carry the map of encounter 'enc-1': it was given its "
                "map inline, so it has no id to carry"
            ),
        ):
            api.adventure_encounter(
                "adv-1", combatants=[dict(AMBUSHER)], seed=100, carry_map=True
            )

    def test_a_refused_carry_map_starts_no_encounter(self) -> None:
        # Refused before anything durable happens, the way a stale version is:
        # a link that created its fight and only then found it had no map to
        # carry would leave a whole journal belonging to no run at all.
        api.adventure_create("The Sunless Citadel")
        api.adventure_encounter("adv-1", combatants=[BRAWLER, RUFFIAN], seed=101)

        with pytest.raises(RequestError, match="was not on a map"):
            api.adventure_encounter("adv-1", seed=102, carry_map=True)

        assert len(api.adventure_state("adv-1")["members"]) == 1
        assert len(api.encounter_list("all")["encounters"]) == 1

class TestTempHpCarryForward:
    """``temp_hp`` joined ``CARRIED_STATE_KEYS`` in the same wave as
    ``condition_levels``, and only the latter got a test for it.

    SRD 5.2.1 p.18 is why it carries at all: Temporary Hit Points last "until
    they're depleted or you finish a Long Rest", and this engine models no
    rest — so a chapter boundary is not something that ends them, and dropping
    the buffer there would end something the printed rule says survives.

    The second case is the one worth having. ``carry_forward`` overlays *by
    presence*, so a field the fight spent to zero must arrive at zero rather
    than at the value the creation capture still remembers.
    """

    def test_an_unspent_buffer_survives_the_chapter_boundary(self) -> None:
        warded = dict(BRAWLER) | {"temp_hp": 5}
        first = str(api.encounter_create([warded, RUFFIAN], seed=91)["encounter_id"])
        assert combatant(api.encounter_state(first), "Thora")["temp_hp"] == 5

        normalized, live = shapes(first, "Thora")
        carried = adventures.carry_forward(normalized, live)
        second = str(
            api.encounter_create([carried, dict(RUFFIAN)], seed=92)["encounter_id"]
        )

        assert combatant(api.encounter_state(second), "Thora")["temp_hp"] == 5

    def test_a_buffer_spent_before_the_boundary_arrives_spent(self) -> None:
        # Bram's Club is +20 against AC 10 and deals 2d6+3, so one landed hit
        # always exceeds a 5-point buffer — the spend is deterministic without
        # scripting the dice.
        warded = dict(BRAWLER) | {"temp_hp": 5}
        first = str(api.encounter_create([warded, RUFFIAN], seed=93)["encounter_id"])
        for _ in range(8):
            if combatant(api.encounter_state(first), "Thora")["temp_hp"] == 0:
                break
            if api.encounter_state(first)["turn"] == "Bram":
                api.encounter_act(first, "attack", target="Thora")
            api.encounter_advance(first)
        else:  # pragma: no cover - a fixture that stopped working, not a branch
            raise AssertionError("nobody ever spent Thora's temporary hit points")

        normalized, live = shapes(first, "Thora")
        carried = adventures.carry_forward(normalized, live)
        second = str(
            api.encounter_create([carried, dict(RUFFIAN)], seed=94)["encounter_id"]
        )

        # Not 5: the creation capture still says 5, and only the overlay makes
        # the arrival honest about what the fight did to it.
        assert combatant(api.encounter_state(second), "Thora")["temp_hp"] == 0


class TestConditionLevelsCarryForward:
    """The last leg of the T10b acceptance check: a pack-declared cumulative
    condition's level survives an adventure chapter boundary.

    ``condition_levels`` is emitted unconditionally from ``Encounter.state()``
    specifically so this overlay is correct — see the ruling on
    ``adventures.CARRIED_STATE_KEYS``.
    """

    PACK = str(Path(__file__).parent / "packs" / "01-ashfall-reach.json")

    def test_a_level_reached_by_three_impositions_survives_the_chapter_boundary(
        self,
    ) -> None:
        api.content_configure([self.PACK], add=True)
        api.adventure_create("The Sunless Citadel")
        first = api.adventure_encounter("adv-1", combatants=[BRAWLER, RUFFIAN], seed=76)
        first_id = str(first["encounter_id"])
        for index in range(3):
            api.encounter_condition(
                first_id, "Thora", "ashfall-ember-marked", request_id=f"mark-{index}"
            )
        assert combatant(api.encounter_state(first_id), "Thora")["condition_levels"] == {
            "ashfall-ember-marked": 3
        }

        second = api.adventure_encounter(
            "adv-1",
            carry=["Thora"],
            combatants=[dict(RUFFIAN) | {"name": "Skeleton", "position": [10, 0]}],
            seed=77,
        )
        arrived = combatant(api.encounter_state(str(second["encounter_id"])), "Thora")

        assert arrived["condition_levels"] == {"ashfall-ember-marked": 3}
        assert "ashfall-ember-marked" in arrived["conditions"]

    def test_shedding_the_condition_before_the_boundary_carries_no_level(self) -> None:
        # The defect unconditional emission exists to prevent: a combatant who
        # lost the condition mid-fight must not arrive at the next chapter
        # still carrying the level a stale, non-empty capture would leave.
        api.content_configure([self.PACK], add=True)
        api.adventure_create("The Sunless Citadel")
        first = api.adventure_encounter("adv-1", combatants=[BRAWLER, RUFFIAN], seed=78)
        first_id = str(first["encounter_id"])
        api.encounter_condition(
            first_id, "Thora", "ashfall-ember-marked", request_id="mark-0"
        )
        api.encounter_condition(
            first_id, "Thora", "ashfall-ember-marked",
            applied=False, request_id="lift-0",
        )

        second = api.adventure_encounter(
            "adv-1",
            carry=["Thora"],
            combatants=[dict(RUFFIAN) | {"name": "Skeleton", "position": [10, 0]}],
            seed=79,
        )
        arrived = combatant(api.encounter_state(str(second["encounter_id"])), "Thora")

        assert arrived["condition_levels"] == {}
        assert "ashfall-ember-marked" not in arrived["conditions"]
