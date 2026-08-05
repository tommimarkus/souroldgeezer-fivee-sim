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
from typing import Any

import pytest

from fivee_sim.service import adventures, specs
from fivee_sim.service.errors import NotFoundError, RequestError, StaleWriteError

from . import api

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
