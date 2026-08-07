"""Overwriting a combatant's state when the simulation got it wrong.

``Encounter.correct`` is the table overruling the fight: an engine defect, a
rule this engine does not model, or an input the game master only later learns
was wrong. It is not a rules mechanism and it decides nothing about what a
creature may do — it writes what the table says was true, and says so in the log
with a reason attached.

**The classification is derived, never written out here.**
:data:`~fivee_sim.model.encounter.CORRECTABLE_KEYS` and
:data:`~fivee_sim.model.encounter.UNCORRECTABLE_KEYS` are held against a real
combatant payload, exactly as ``test_state_split`` and ``test_player_brief``
hold their own pairs: a literal expected set here would pin the model against a
copy of itself and both halves would be edited in the same commit.

The pair is a **third** classification of the same payload, and it is orthogonal
to the two that exist. The brief's asks *may this seat see it*; the sheet/live
split asks *can the fight move it*; this one asks *can the table overwrite it*.
A key belongs to one bucket of each, for three different reasons, and no pair
may be read as another — which is why nothing below imports either of the other
two pairs' sets.
"""

from __future__ import annotations

from collections.abc import Mapping
from random import Random
from typing import Any

import pytest

from fivee_sim.content import make_monster, spellbook
from fivee_sim.kernel.conditions import Condition
from fivee_sim.kernel.rules import Ability
from fivee_sim.map_types import MapDocument, MapGrid, MapLevel
from fivee_sim.model import encounter as model
from fivee_sim.model.encounter import (
    Action,
    ActionKind,
    Encounter,
    EncounterError,
)
from fivee_sim.service import adventures

from .conftest import FixedRandom, advance_to, caster, fighter, fixture_provenance

FIXTURE = "synthetic test fixture, not SRD content"

REASON = "the fireball never landed"


def two_storeys() -> MapDocument:
    """Ground for a fight that reports ``level`` and ``elevation``.

    Two storeys rather than one, because ``level`` is a correctable key and a
    correction to a storey the map does not have would prove nothing.
    """
    return MapDocument(
        name="cellar",
        grid=MapGrid(width=4, height=4),
        legend={".": "normal"},
        provenance=fixture_provenance(FIXTURE),
        levels={
            index: MapLevel(
                index=index,
                name=f"level-{index}",
                tiles=("." * 4,) * 4,
                features=(),
            )
            for index in (0, 1)
        },
    )


def fight(*, mapped: bool = True) -> Encounter:
    return Encounter(
        [
            fighter("Thora", position=(0, 0)),
            fighter("Grelk", team="monsters", position=(15, 15)),
        ],
        Random(7),
        map_document=two_storeys() if mapped else None,
    )


def entry(encounter: Encounter, name: str) -> dict[str, Any]:
    return next(
        one for one in encounter.state()["combatants"] if one["name"] == name
    )


def emitted_creature_keys() -> set[str]:
    """Every key a combatant entry can carry, off a fight that reaches them all."""
    return {key for key in entry(fight(), "Thora")}


def corrections(encounter: Encounter) -> list[Mapping[str, Any]]:
    return [event.as_dict() for event in encounter.log if event.kind == "correction"]


#: One plausible correction per correctable key, held against
#: :data:`CORRECTABLE_KEYS` below rather than trusted: a key added to the model
#: with no sample here fails that case rather than going unexercised.
SAMPLES: dict[str, Any] = {
    "hp": 7,
    "max_hp": 44,
    "temp_hp": 5,
    "ac": 11,
    "conditions": ["poisoned"],
    "condition_levels": {"exhaustion": 3},
    "death_saves": {"successes": 1, "failures": 2},
    "stable": True,
    "dead": True,
    "surrendered": True,
    "spell_slots": {1: 3},
    "items": {"Emberflask": 2},
    "position": [10, 10],
    "level": 1,
    "facing": "north",
    "present": False,
    "initiative": 99,
}


class TestTheClassificationIsTotal:
    """Every key lands in exactly one bucket, or the suite says who has to decide.

    Two frozen sets rather than one allowlist, for the reason the other two
    pairs use two: an allowlist alone answers "may this be written?" and not
    "has anybody looked at this?" A new field would default to uncorrectable and
    simply be refused, which reads exactly like a field somebody considered and
    protected.
    """

    def test_every_creature_field_the_model_emits_is_classified_exactly_once(
        self,
    ) -> None:
        emitted = emitted_creature_keys()

        unclassified = sorted(
            emitted - model.CORRECTABLE_KEYS - model.UNCORRECTABLE_KEYS
        )
        assert not unclassified, (
            "Encounter._creature_state emits these and nobody has decided "
            "whether a game master may overwrite them. Put each in exactly one "
            "of CORRECTABLE_KEYS (the fight can move it, so the table can "
            "correct it) or UNCORRECTABLE_KEYS (with the reason, in the "
            "comment): " + ", ".join(unclassified)
        )
        both = sorted(model.CORRECTABLE_KEYS & model.UNCORRECTABLE_KEYS)
        assert not both, f"both buckets claim {', '.join(both)}; a field has one answer"

    def test_neither_bucket_names_a_key_the_model_never_emits(self) -> None:
        emitted = emitted_creature_keys()

        stale = sorted(
            (model.CORRECTABLE_KEYS | model.UNCORRECTABLE_KEYS) - emitted
        )
        assert not stale, (
            "CORRECTABLE_KEYS or UNCORRECTABLE_KEYS classifies these and "
            "Encounter._creature_state emits none of them: " + ", ".join(stale)
        )

    def test_the_fixture_reaches_the_fields_the_model_only_sometimes_emits(
        self,
    ) -> None:
        """The vacuity guard: ``level`` and ``elevation`` need a battle map."""
        emitted = emitted_creature_keys()

        assert {"facing", "level", "elevation"} <= emitted
        assert len(emitted) > 25, (
            f"only {len(emitted)} creature keys were sampled; the derivation "
            f"has stopped reading a real payload"
        )

    def test_everything_a_fight_carries_between_chapters_can_be_corrected(
        self,
    ) -> None:
        """Anything a fight can change, a game master can correct.

        ``CARRIED_STATE_KEYS`` is the fight's own answer to "what did this
        combatant end up different about", written down for the next chapter. A
        key on that list the table cannot overwrite would be state the engine
        moves and nobody can put back.
        """
        assert adventures.CARRIED_STATE_KEYS <= model.CORRECTABLE_KEYS


class TestEveryCorrectableKeyReallyMoves:
    """The declaration held against behaviour, not against a second list.

    A key declared correctable with no writer behind it is the failure this
    closes: it would be accepted, journalled, replayed — and change nothing.
    """

    def test_a_sample_exists_for_every_correctable_key(self) -> None:
        assert set(SAMPLES) == model.CORRECTABLE_KEYS

    @pytest.mark.parametrize("field", sorted(SAMPLES))
    def test_correcting_it_moves_the_payload(self, field: str) -> None:
        encounter = fight()
        before = entry(encounter, "Thora")[field]

        encounter.correct("Thora", {field: SAMPLES[field]}, reason=REASON)

        after = entry(encounter, "Thora")[field]
        assert after != before, f"correcting {field} left the payload unchanged"

    @pytest.mark.parametrize("field", sorted(SAMPLES))
    def test_correcting_it_emits_one_event_naming_it(self, field: str) -> None:
        encounter = fight()

        encounter.correct("Thora", {field: SAMPLES[field]}, reason=REASON)

        fields = [event["data"]["field"] for event in corrections(encounter)]
        assert fields == [field]


class TestTheRefusals:
    def test_an_unknown_combatant_is_refused_by_name(self) -> None:
        encounter = fight()

        with pytest.raises(
            EncounterError,
            match="no combatant named 'Bob' in this encounter; there is: Grelk, Thora",
        ):
            encounter.correct("Bob", {"hp": 1}, reason=REASON)

    @pytest.mark.parametrize("field", sorted(model.UNCORRECTABLE_KEYS))
    def test_an_uncorrectable_field_is_refused_by_name(self, field: str) -> None:
        encounter = fight()

        with pytest.raises(EncounterError, match=f"{field!r} cannot be corrected"):
            encounter.correct("Thora", {field: 1}, reason=REASON)

    def test_a_field_the_payload_has_never_had_is_refused_the_same_way(self) -> None:
        # One sentence for "protected" and for "not a field at all": a caller
        # who mistyped a key is told the same thing as one who named a derived
        # one, and both are told what they may write.
        encounter = fight()

        with pytest.raises(
            EncounterError, match="'hitpoints' cannot be corrected; correctable"
        ):
            encounter.correct("Thora", {"hitpoints": 1}, reason=REASON)

    def test_the_refusal_lists_what_may_be_corrected(self) -> None:
        encounter = fight()

        with pytest.raises(EncounterError, match="cannot be corrected") as refused:
            encounter.correct("Thora", {"speeds": {}}, reason=REASON)

        for field in model.CORRECTABLE_KEYS:
            assert field in str(refused.value)

    def test_a_refused_correction_writes_nothing(self) -> None:
        # Refusals are checked before anything is written, so a caller who
        # named one bad field among four does not get the other three applied.
        encounter = fight()
        before = entry(encounter, "Thora")

        with pytest.raises(EncounterError, match="cannot be corrected"):
            encounter.correct(
                "Thora", {"hp": 3, "conscious": True}, reason=REASON
            )

        assert entry(encounter, "Thora") == before
        assert corrections(encounter) == []


class TestHitPointsRunTheirOwnBookkeeping:
    """A raw write to ``hp`` is the defect this closes.

    Dropping a creature to 0 knocks it out, drops it prone, ends its
    concentration and starts its death saves from nothing; raising it off 0 puts
    it back on its feet. Both live on :class:`Creature` and both are the same
    helpers ``take_damage`` and ``heal`` run, so a correction cannot drift from
    the fight's own answer.
    """

    def test_dropping_to_zero_knocks_the_creature_out(self) -> None:
        encounter = fight()
        thora = encounter.creatures["Thora"]
        thora.concentrating_on = "Bless"
        thora.death_save_successes = 2

        encounter.correct("Thora", {"hp": 0}, reason=REASON)

        assert Condition.UNCONSCIOUS in thora.conditions
        assert Condition.PRONE in thora.conditions
        assert thora.concentrating_on is None
        assert thora.death_save_successes == 0
        assert not thora.stable

    def test_raising_a_downed_creature_puts_it_back_on_its_feet(self) -> None:
        encounter = fight()
        thora = encounter.creatures["Thora"]
        encounter.correct("Thora", {"hp": 0}, reason=REASON)

        encounter.correct("Thora", {"hp": 6}, reason="she was never hit")

        assert Condition.UNCONSCIOUS not in thora.conditions
        assert thora.death_save_failures == 0
        assert not thora.stable

    def test_a_correction_that_does_not_cross_zero_leaves_the_bookkeeping_alone(
        self,
    ) -> None:
        encounter = fight()
        thora = encounter.creatures["Thora"]
        thora.concentrating_on = "Bless"

        encounter.correct("Thora", {"hp": 4}, reason=REASON)

        assert thora.concentrating_on == "Bless"
        assert Condition.UNCONSCIOUS not in thora.conditions

    def test_the_table_may_state_hit_points_above_the_maximum(self) -> None:
        # The model writes what it is given: an ``hp`` a stat block cannot
        # support is a caller's business, and refusing it here would turn a soft
        # drift warning into a whole-fight recovery refusal the day a kernel
        # edit moves ``max_hp``.
        encounter = fight()

        encounter.correct("Thora", {"hp": 900}, reason=REASON)

        assert encounter.creatures["Thora"].hp == 900

    def test_hit_points_are_written_before_every_other_stated_field(self) -> None:
        # The order is what lets a game master say "she is down, with two
        # failures" in one call: the drop resets the counters, and the stated
        # ``death_saves`` then overrides the reset rather than being erased by
        # it.
        encounter = fight()

        encounter.correct(
            "Thora",
            {"hp": 0, "death_saves": {"successes": 0, "failures": 2}},
            reason=REASON,
        )

        assert encounter.creatures["Thora"].death_save_failures == 2


class TestTheMaximumHasAFloorAndPullsHitPointsDown:
    def test_a_maximum_below_one_is_floored(self) -> None:
        # ``health_band`` reports a maximum of 0 or less as permanently
        # "unharmed" while ``take_damage``'s overflow check kills on the first
        # point past zero, so a fight holding one is a fight lying twice.
        encounter = fight()

        encounter.correct("Thora", {"max_hp": 0}, reason=REASON)

        assert encounter.creatures["Thora"].max_hp == 1

    def test_lowering_the_maximum_below_current_hit_points_clamps_them(self) -> None:
        encounter = fight()

        encounter.correct("Thora", {"max_hp": 12}, reason=REASON)

        assert encounter.creatures["Thora"].hp == 12

    def test_the_clamp_is_a_correction_of_its_own_in_the_log(self) -> None:
        # It changed a field the caller did not name, so the log says so rather
        # than leaving a reader to derive it from the maximum.
        encounter = fight()

        encounter.correct("Thora", {"max_hp": 12}, reason=REASON)

        fields = [event["data"]["field"] for event in corrections(encounter)]
        assert sorted(fields) == ["hp", "max_hp"]

    def test_a_stated_maximum_does_not_clamp_a_stated_hit_point_total(self) -> None:
        encounter = fight()

        encounter.correct("Thora", {"hp": 5, "max_hp": 12}, reason=REASON)

        assert encounter.creatures["Thora"].hp == 5
        fields = [event["data"]["field"] for event in corrections(encounter)]
        assert sorted(fields) == ["hp", "max_hp"]


class TestConditionsGoThroughTheLedger:
    """Never a dict assignment, which is what makes this more than tidiness.

    ``service/specs.py`` replaces ``creature.conditions`` wholesale, which is
    safe at construction because the effect ledger is empty and wrong mid-fight:
    it orphans every ongoing effect naming a wiped condition, and the effect
    later ends by lifting a condition the creature no longer holds.
    """

    def held(self) -> tuple[Encounter, Any]:
        wren = caster(position=0)
        victim = fighter("Bandit0", team="foes", position=10)
        victim.abilities[Ability.WISDOM] = 6
        rng = Random(11)
        encounter = Encounter([wren, victim], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Hold Person", target="Bandit0"),
            FixedRandom(1),
        )
        assert Condition.PARALYZED in victim.conditions
        assert encounter.state()["ongoing_effects"] != []
        return encounter, victim

    def test_correcting_a_condition_away_ends_what_was_sustaining_it(self) -> None:
        encounter, victim = self.held()

        encounter.correct("Bandit0", {"conditions": []}, reason=REASON)

        assert Condition.PARALYZED not in victim.conditions
        assert encounter.state()["ongoing_effects"] == []

    def test_a_condition_the_correction_keeps_keeps_its_effect(self) -> None:
        encounter, victim = self.held()

        encounter.correct(
            "Bandit0", {"conditions": [Condition.PARALYZED]}, reason=REASON
        )

        assert Condition.PARALYZED in victim.conditions
        assert encounter.state()["ongoing_effects"] != []

    def test_a_stated_level_reaches_the_creature(self) -> None:
        encounter = fight()

        encounter.correct(
            "Thora",
            {"conditions": ["exhaustion"], "condition_levels": {"exhaustion": 4}},
            reason=REASON,
        )

        assert encounter.creatures["Thora"].conditions["exhaustion"] == 4

    def test_a_level_alone_re_levels_a_condition_already_held(self) -> None:
        # Not an accumulation: ``add_condition`` adds levels to what is there,
        # so a correction that went straight through it would make "her
        # exhaustion is 3" mean 5.
        encounter = fight()
        encounter.correct(
            "Thora",
            {"conditions": ["exhaustion"], "condition_levels": {"exhaustion": 2}},
            reason=REASON,
        )

        encounter.correct(
            "Thora", {"condition_levels": {"exhaustion": 3}}, reason=REASON
        )

        assert encounter.creatures["Thora"].conditions["exhaustion"] == 3

    def test_lifting_a_condition_is_announced(self) -> None:
        encounter = fight()
        encounter.correct("Thora", {"conditions": ["poisoned"]}, reason=REASON)

        encounter.correct("Thora", {"conditions": []}, reason=REASON)

        kinds = [event.kind for event in encounter.log if event.target == "Thora"]
        assert "effect_end" in kinds


class TestTheCorrectionEvent:
    def test_the_reason_rides_as_the_events_detail(self) -> None:
        # And as nothing else: ``detail`` is already withheld from every seat by
        # omission, so a second copy in ``data`` would have identical reach and
        # be one more key to keep in step.
        encounter = fight()

        encounter.correct("Thora", {"ac": 11}, reason=REASON)

        [event] = corrections(encounter)
        assert event["detail"] == REASON
        assert "reason" not in event["data"]

    def test_it_carries_what_the_field_was_and_what_it_became(self) -> None:
        encounter = fight()

        encounter.correct("Thora", {"ac": 11}, reason=REASON)

        [event] = corrections(encounter)
        assert event["data"]["before"] == 16
        assert event["data"]["after"] == 11

    def test_after_is_what_the_payload_really_says_afterwards(self) -> None:
        # Not an echo of the request: the floor, the clamp and a condition's own
        # rules can all leave the creature somewhere other than where the caller
        # pointed, and an event that repeated the request would say otherwise.
        encounter = fight()

        encounter.correct("Thora", {"max_hp": -3}, reason=REASON)

        stated = next(
            event for event in corrections(encounter)
            if event["data"]["field"] == "max_hp"
        )
        assert stated["data"]["after"] == 1

    def test_one_event_per_field_rather_than_one_per_call(self) -> None:
        encounter = fight()

        encounter.correct("Thora", {"ac": 11, "temp_hp": 4}, reason=REASON)

        fields = sorted(event["data"]["field"] for event in corrections(encounter))
        assert fields == ["ac", "temp_hp"]

    def test_no_creature_is_named_as_the_actor(self) -> None:
        # The table did this, not a combatant, and an actor would put a
        # correction on somebody's turn record.
        encounter = fight()

        encounter.correct("Thora", {"ac": 11}, reason=REASON)

        [event] = corrections(encounter)
        assert event["actor"] == ""
        assert event["target"] == "Thora"

    def test_an_empty_correction_writes_nothing_and_says_nothing(self) -> None:
        encounter = fight()
        before = entry(encounter, "Thora")

        encounter.correct("Thora", {}, reason=REASON)

        assert entry(encounter, "Thora") == before
        assert corrections(encounter) == []


class TestInitiative:
    def test_a_corrected_initiative_re_sorts_the_order(self) -> None:
        encounter = fight()
        assert encounter.order == ["Thora", "Grelk"]

        encounter.correct("Grelk", {"initiative": 99}, reason="he rolled a 19")

        assert encounter.order == ["Grelk", "Thora"]

    def test_the_new_leader_gets_its_own_turn_budget(self) -> None:
        # ``__init__`` opened the turn for whoever was on top, so the budget on
        # the table belongs to them. Promoting somebody else without re-deriving
        # it hands the newcomer another creature's movement and attacks.
        slow = fighter("Grelk", team="monsters", position=(15, 15))
        slow.speed = 15
        encounter = Encounter(
            [fighter("Thora", position=(0, 0)), slow], Random(7)
        )
        assert encounter.current_name == "Thora"

        encounter.correct("Grelk", {"initiative": 99}, reason="he rolled a 19")

        assert encounter.current_name == "Grelk"
        assert encounter.state()["turn_state"]["movement_left"] == 15

    def test_an_interlude_refuses_it_by_name(self) -> None:
        encounter = Encounter(
            [fighter("Thora", position=(0, 0))],
            Random(7),
            mode=model.EncounterMode.EXPLORATION,
        )

        with pytest.raises(
            EncounterError, match="an interlude has no initiative to correct"
        ):
            encounter.correct("Thora", {"initiative": 9}, reason=REASON)

    def test_it_is_refused_once_the_order_has_been_walked(self) -> None:
        # Re-sorting after a turn has passed is unsound in both directions: a
        # combatant moved above the pointer never acts this round, and one moved
        # below it acts twice. Verified against the stepper — at ``turn_index``
        # 0 in round 2 the creature on top has already had its turn begun and
        # may already have spent it.
        encounter = fight()
        encounter.advance(Random(3))

        with pytest.raises(
            EncounterError,
            match="initiative can only be corrected before the first turn is taken",
        ):
            encounter.correct("Thora", {"initiative": 9}, reason=REASON)

    def test_everything_else_is_still_correctable_once_the_fight_is_running(
        self,
    ) -> None:
        encounter = fight()
        encounter.advance(Random(3))

        encounter.correct("Thora", {"hp": 3}, reason=REASON)

        assert encounter.creatures["Thora"].hp == 3


class TestTheFightCanBeUnEnded:
    def test_reviving_the_last_enemy_puts_the_fight_back_on(self) -> None:
        # "It ended and it should not have" is the correction case, and ``over``
        # is derived, so nothing extra is needed to make it work — and no guard
        # may be added that would refuse it.
        encounter = fight()
        encounter.correct("Grelk", {"hp": 0, "dead": True}, reason="a bad ruling")
        assert encounter.over

        encounter.correct("Grelk", {"hp": 5, "dead": False}, reason=REASON)

        assert not encounter.over


class TestTakeDamageAndHealAreUnchanged:
    """The refactor's regression net, stated rather than implied.

    ``Creature`` is a ``@dataclass``, so mutmut generates no mutants for any of
    its methods: these tests carry the whole weight of the extraction.
    """

    def test_damage_that_drops_a_creature_still_knocks_it_out(self) -> None:
        creature = fighter(hp=4)
        creature.concentrating_on = "Bless"
        creature.death_save_successes = 2

        creature.take_damage(4)

        assert creature.hp == 0
        assert Condition.UNCONSCIOUS in creature.conditions
        assert Condition.PRONE in creature.conditions
        assert creature.concentrating_on is None
        assert creature.death_save_successes == 0
        assert not creature.stable

    def test_damage_taken_while_already_down_costs_a_failure_and_resets_nothing(
        self,
    ) -> None:
        creature = fighter(hp=1)
        creature.take_damage(1)
        creature.death_save_successes = 2
        creature.stable = True

        creature.take_damage(1)

        assert creature.death_save_failures == 1
        assert creature.death_save_successes == 2
        assert not creature.stable

    def test_healing_off_zero_still_stands_the_creature_up(self) -> None:
        creature = fighter(hp=1)
        creature.take_damage(1)
        creature.death_save_failures = 2

        creature.heal(3)

        assert creature.hp == 3
        assert Condition.UNCONSCIOUS not in creature.conditions
        assert Condition.PRONE in creature.conditions
        assert creature.death_save_failures == 0

    def test_healing_still_clamps_and_correcting_does_not(self) -> None:
        creature = fighter(hp=1, max_hp=10)

        creature.heal(50)
        assert creature.hp == 10

        creature.set_hp(50)
        assert creature.hp == 50

    def test_a_temporary_buffer_is_still_spent_before_hit_points(self) -> None:
        creature = fighter(hp=10)
        creature.grant_temp_hp(4)

        creature.take_damage(6)

        assert creature.temp_hp == 0
        assert creature.hp == 8

    def test_massive_damage_still_kills_outright(self) -> None:
        creature = fighter(hp=5, max_hp=30)

        creature.take_damage(40)

        assert creature.dead


class TestTheOrderIsSortedInOnePlace:
    def test_a_correction_sorts_by_the_same_rule_the_fight_opened_with(self) -> None:
        # The tie-break is the Dexterity modifier and then the name, and a
        # second copy of that rule would drift the first time either changes.
        quick = fighter("Alda", position=(0, 0))
        quick.abilities[Ability.DEXTERITY] = 20
        slow = fighter("Bram", position=(5, 5))
        slow.abilities[Ability.DEXTERITY] = 8
        encounter = Encounter([quick, slow, make_monster("Wolf")], Random(7))
        encounter.initiative["Alda"] = 10
        encounter.initiative["Bram"] = 10

        encounter.correct("Wolf", {"initiative": 1}, reason=REASON)

        assert encounter.order == ["Alda", "Bram", "Wolf"]
