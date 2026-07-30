"""Analytics tests.

The load-bearing one is ``test_one_iteration_matches_a_single_hand_driven_run``.
If a batch of one ever stops matching a single encounter at the same seed, the
analytics have drifted away from the rules live play uses, and every number they
produce is suspect.
"""

from __future__ import annotations

from collections.abc import Sequence
from random import Random

import pytest

from fivee_sim.analytics.montecarlo import (
    auto_action,
    run_encounter,
    simulate_dpr,
    simulate_rounds,
    summarise,
)
from fivee_sim.data import make_monster, spellbook
from fivee_sim.kernel.actions import AttackKind
from fivee_sim.kernel.dice import Dice
from fivee_sim.kernel.grid import as_point, distance_feet
from fivee_sim.kernel.rules import Ability, DamageType
from fivee_sim.model.battlemap import BattleMap
from fivee_sim.model.creature import AttackOption, Creature
from fivee_sim.model.encounter import ActionKind, Encounter

from .test_encounter import advance_to, fighter, shaped_spellbook, shaper

SEED = 20260730


def duel() -> Sequence[Creature]:
    return [
        fighter("Thora", position=0),
        make_monster("Goblin Warrior", label="Goblin", position=15),
    ]


def melee_brawl() -> Sequence[Creature]:
    return [
        fighter("Thora", position=0),
        make_monster("Goblin Warrior", label="Goblin A", position=10),
        make_monster("Goblin Warrior", label="Goblin B", position=15),
    ]


class TestReproducibility:
    def test_the_same_seed_produces_an_identical_transcript(self) -> None:
        def transcript(seed: int) -> list[tuple[str, str, str, str]]:
            rng = Random(seed)
            encounter = Encounter(list(duel()), rng, spellbook=spellbook())
            run_encounter(encounter, rng, max_rounds=20)
            return [
                (event.kind, event.actor, event.target, event.detail)
                for event in encounter.log
            ]

        assert transcript(SEED) == transcript(SEED)

    def test_different_seeds_diverge(self) -> None:
        def outcome(seed: int) -> str:
            rng = Random(seed)
            encounter = Encounter(list(duel()), rng, spellbook=spellbook())
            run_encounter(encounter, rng, max_rounds=20)
            return "|".join(event.detail for event in encounter.log)

        assert outcome(SEED) != outcome(SEED + 1)


class TestStatefulAndBatchAgree:
    def test_one_iteration_matches_a_single_hand_driven_run(self) -> None:
        rng = Random(SEED)
        encounter = Encounter(list(duel()), rng, spellbook=spellbook())
        manual = run_encounter(encounter, rng, max_rounds=20)

        batch = simulate_rounds(
            duel, iterations=1, seed=SEED, max_rounds=20, spellbook=spellbook()
        )

        expected_winner = manual.winner if manual.winner is not None else "none"
        assert batch["wins"] == {expected_winner: 1}
        assert batch["rounds"]["mean"] == float(manual.rounds)
        assert batch["rounds"]["min"] == batch["rounds"]["max"] == float(manual.rounds)


class TestSimulateRounds:
    def test_win_rates_cover_every_iteration(self) -> None:
        result = simulate_rounds(
            melee_brawl, iterations=60, seed=SEED, max_rounds=20, spellbook=spellbook()
        )
        assert sum(result["wins"].values()) == 60
        assert pytest.approx(sum(result["win_rate"].values()), abs=1e-6) == 1.0

    def test_a_fight_reaches_a_conclusion_rather_than_timing_out(self) -> None:
        result = simulate_rounds(duel, iterations=40, seed=SEED, max_rounds=20)
        assert result["timed_out"] == 0
        assert "none" not in result["wins"]

    def test_two_goblins_beat_one_fighter_more_often_than_one_does(self) -> None:
        alone = simulate_rounds(duel, iterations=80, seed=SEED, max_rounds=20)
        outnumbered = simulate_rounds(melee_brawl, iterations=80, seed=SEED, max_rounds=20)
        assert outnumbered["win_rate"].get("monsters", 0.0) > alone["win_rate"].get(
            "monsters", 0.0
        )

    def test_iterations_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            simulate_rounds(duel, iterations=0, seed=SEED)


class TestSimulateDpr:
    def test_damage_accumulates_over_the_requested_rounds(self) -> None:
        result = simulate_dpr(
            lambda: fighter("Thora"), target_ac=15, rounds=3, iterations=200, seed=SEED
        )
        assert result["damage"]["mean"] > 0
        # Both figures are reported rounded to three decimals, so they agree to
        # within rounding rather than exactly.
        assert result["damage_per_round"] == pytest.approx(
            result["damage"]["mean"] / 3, abs=1e-3
        )

    def test_a_higher_armour_class_takes_less_damage(self) -> None:
        soft = simulate_dpr(
            lambda: fighter("Thora"), target_ac=10, rounds=3, iterations=300, seed=SEED
        )
        armoured = simulate_dpr(
            lambda: fighter("Thora"), target_ac=20, rounds=3, iterations=300, seed=SEED
        )
        assert armoured["damage"]["mean"] < soft["damage"]["mean"]

    def test_extra_attack_raises_expected_damage(self) -> None:
        once = simulate_dpr(
            lambda: fighter("Thora"), target_ac=14, rounds=2, iterations=300, seed=SEED
        )
        twice = simulate_dpr(
            lambda: fighter("Thora", attacks_per_action=2),
            target_ac=14,
            rounds=2,
            iterations=300,
            seed=SEED,
        )
        assert twice["damage"]["mean"] > once["damage"]["mean"] * 1.5


class TestSummarise:
    def test_empty_input_is_all_zeroes(self) -> None:
        stats = summarise([])
        assert stats.samples == 0
        assert stats.mean == 0.0

    def test_percentiles_come_from_the_sorted_sample(self) -> None:
        stats = summarise([float(value) for value in range(1, 11)])
        assert stats.samples == 10
        assert stats.minimum == 1.0
        assert stats.maximum == 10.0
        assert stats.median == 5.5
        # Nearest-rank: index round(0.9 * 9) == 8, so the ninth of ten values.
        assert stats.p90 == 9.0


def blaster(
    name: str = "Ilva", *, position: int | tuple[int, int] = 0, team: str = "party"
) -> Creature:
    """A caster carrying a weapon — the case the old policy measured wrongly."""
    return Creature(
        name=name,
        team=team,
        ac=12,
        max_hp=32,
        speed=30,
        abilities={Ability.DEXTERITY: 14, Ability.CONSTITUTION: 14},
        attacks=(
            AttackOption(
                name="Dagger",
                attack_bonus=6,
                damage=Dice.parse("1d4+4"),
                damage_type=DamageType.PIERCING,
                kind=AttackKind.MELEE,
                provenance="synthetic test fixture, not SRD content",
            ),
        ),
        spells=("Fireball",),
        spell_slots={3: 3},
        spell_save_dc=15,
        spell_attack_bonus=7,
        position=position,
        provenance="synthetic test fixture, not SRD content",
    )


def clustered_goblins() -> Sequence[Creature]:
    """Four goblins packed inside one Fireball, and a wizard well clear of it."""
    return [
        blaster(position=0),
        *(
            make_monster("Goblin Warrior", label=f"Goblin {letter}", position=100 + step)
            for step, letter in enumerate("ABCD")
        ),
    ]


class TestPolicyChoosesByExpectedDamage:
    def test_a_caster_holding_a_weapon_still_casts(self) -> None:
        # The policy used to take attacks[0] whenever it was in range, so a caster
        # with any weapon at all never reached the spell branch. simulate_dpr — the
        # tool for "is this build better?" — understated such a build sixfold.
        armed = simulate_dpr(
            lambda: blaster(), target_ac=15, rounds=3, iterations=300, seed=SEED,
            spellbook=spellbook(),
        )
        unarmed = simulate_dpr(
            lambda: Creature(
                name="Ilva",
                team="party",
                ac=12,
                max_hp=32,
                speed=30,
                spells=("Fireball",),
                spell_slots={3: 3},
                spell_save_dc=15,
                position=0,
                provenance="synthetic test fixture, not SRD content",
            ),
            target_ac=15,
            rounds=3,
            iterations=300,
            seed=SEED,
            spellbook=spellbook(),
        )
        assert "cast:Fireball" in armed["actions"]
        # The dagger is strictly worse than the spell, so carrying one must not
        # change what the build is measured at.
        assert armed["damage"]["mean"] == pytest.approx(unarmed["damage"]["mean"])

    def test_a_weapon_wins_when_it_is_actually_the_better_option(self) -> None:
        # The converse, so the test above is not just "always prefer the spell": a
        # build whose spell slots are gone falls back to the weapon.
        def spent() -> Creature:
            creature = blaster()
            creature.spell_slots = {3: 0}
            return creature

        result = simulate_dpr(
            lambda: spent(), target_ac=15, rounds=3, iterations=200, seed=SEED,
            spellbook=spellbook(),
        )
        assert result["actions"] == {"attack:Dagger": 600}
        assert result["damage"]["mean"] > 0


class TestPolicyPlacesAreaSpells:
    def test_a_fireball_is_placed_to_catch_the_whole_cluster(self) -> None:
        # The decision itself is what matters here, so it is read straight off the
        # policy rather than inferred from the event log. The old policy always
        # passed a single name and never a point of origin, so a 20 ft blast landed
        # on exactly one goblin however tightly they were packed.
        rng = Random(SEED)
        combatants = list(clustered_goblins())
        encounter = Encounter(combatants, rng, spellbook=spellbook())
        advance_to(encounter, "Ilva", rng)
        action = auto_action(encounter)
        assert action is not None
        assert action.kind is ActionKind.CAST
        assert action.spell == "Fireball"
        assert action.center is not None
        # The caught set is the assertion, not the centre coordinate: any placement
        # that blankets the cluster and spares the caster is a correct answer.
        centre = as_point(action.center)
        caught = [
            creature
            for creature in combatants
            if distance_feet(as_point(creature.position), centre) <= 20
        ]
        assert sorted(c.name for c in caught) == [
            "Goblin A", "Goblin B", "Goblin C", "Goblin D",
        ]

    def test_the_caster_is_never_caught_in_its_own_blast(self) -> None:
        # A wizard standing among the goblins must not centre a Fireball on itself.
        rng = Random(SEED)
        wizard = blaster(position=102)
        goblins = [
            make_monster("Goblin Warrior", label=f"Goblin {letter}", position=100 + step)
            for step, letter in enumerate("ABCD")
        ]
        encounter = Encounter([wizard, *goblins], rng, spellbook=spellbook())
        run_encounter(encounter, rng, max_rounds=20)
        hit_self = [
            event
            for event in encounter.log
            if event.kind == "spell_effect" and event.target == wizard.name
        ]
        assert hit_self == []

    def test_placement_beats_naming_a_single_target(self) -> None:
        # The measurable consequence of the fix: against a cluster, the same party
        # at the same seed wins far more often than it did casting single-target.
        result = simulate_rounds(
            clustered_goblins, iterations=200, seed=SEED, max_rounds=20,
            spellbook=spellbook(),
        )
        assert result["win_rate"].get("party", 0.0) > 0.5


FIXTURE = "synthetic test fixture, not SRD content"


def walled_arena() -> BattleMap:
    """A 6x3 arena with a wall stub: melee must walk around, sight is partial."""
    return BattleMap(
        name="arena", width=6, height=3,
        terrain={(2, 0): "wall", (2, 1): "wall"},
        provenance=FIXTURE,
    )


def mapped_duel() -> Sequence[Creature]:
    return [
        fighter("Thora", position=(0, 0)),
        make_monster("Goblin Warrior", label="Goblin", position=(25, 0)),
    ]


class TestMappedAnalytics:
    def test_a_mapped_iteration_matches_a_single_hand_driven_run(self) -> None:
        # The mapped sibling of the load-bearing parity test: one batch iteration
        # on a map must equal a hand-driven encounter on the identical map.
        rng = Random(SEED)
        encounter = Encounter(
            list(mapped_duel()), rng, spellbook=spellbook(),
            battle_map=walled_arena(),
        )
        manual = run_encounter(encounter, rng, max_rounds=20)

        batch = simulate_rounds(
            mapped_duel, iterations=1, seed=SEED, max_rounds=20,
            spellbook=spellbook(), battle_map=walled_arena(),
        )

        expected_winner = manual.winner if manual.winner is not None else "none"
        assert batch["wins"] == {expected_winner: 1}
        assert batch["rounds"]["mean"] == float(manual.rounds)

    def test_the_same_seed_produces_an_identical_mapped_transcript(self) -> None:
        def transcript(seed: int) -> list[tuple[str, str, str, str]]:
            rng = Random(seed)
            encounter = Encounter(
                list(mapped_duel()), rng, spellbook=spellbook(),
                battle_map=walled_arena(),
            )
            run_encounter(encounter, rng, max_rounds=20)
            return [
                (event.kind, event.actor, event.target, event.detail)
                for event in encounter.log
            ]

        assert transcript(SEED) == transcript(SEED)

    def test_the_policy_closes_around_the_wall_and_the_fight_concludes(self) -> None:
        # A dominant-axis stepper would march into the wall, be refused, and
        # stall to the round cap; a routed closer reaches the goblin and ends it.
        result = simulate_rounds(
            mapped_duel, iterations=5, seed=SEED, max_rounds=20,
            spellbook=spellbook(), battle_map=walled_arena(),
        )
        assert result["timed_out"] == 0
        assert sum(result["wins"].values()) == 5


class TestPolicyPlacesShapes:
    def test_a_cone_is_aimed_at_the_wedge_that_catches_the_cluster(self) -> None:
        rng = Random(SEED)
        combatants = [
            shaper(position=(0, 0)),
            make_monster("Goblin Warrior", label="East A", position=(10, 0)),
            make_monster("Goblin Warrior", label="East B", position=(10, 5)),
        ]
        encounter = Encounter(combatants, rng, spellbook=shaped_spellbook())
        advance_to(encounter, "Vesna", rng)
        action = auto_action(encounter)
        assert action is not None
        assert action.kind is ActionKind.CAST
        assert action.spell == "Flame Fan"
        assert action.direction is not None
        caught = encounter.area_targets(
            encounter.spellbook["Flame Fan"], "Vesna", direction=action.direction
        )
        assert sorted(creature.name for creature in caught) == ["East A", "East B"]

    def test_a_line_is_aimed_down_the_rank_of_enemies(self) -> None:
        rng = Random(SEED)
        combatants = [
            shaper(position=(0, 0)),
            make_monster("Goblin Warrior", label="Near", position=(15, 0)),
            make_monster("Goblin Warrior", label="Far", position=(30, 0)),
        ]
        encounter = Encounter(combatants, rng, spellbook=shaped_spellbook())
        # Keep only the line in hand so the choice under test is the aim.
        encounter.creatures["Vesna"].spells = ("Spark Line",)
        advance_to(encounter, "Vesna", rng)
        action = auto_action(encounter)
        assert action is not None
        assert action.spell == "Spark Line"
        assert action.toward is not None
        caught = encounter.area_targets(
            encounter.spellbook["Spark Line"], "Vesna", toward=action.toward
        )
        assert sorted(creature.name for creature in caught) == ["Far", "Near"]

    def test_a_cube_corner_is_chosen_to_cover_both_targets(self) -> None:
        rng = Random(SEED)
        combatants = [
            shaper(position=(0, 0)),
            make_monster("Goblin Warrior", label="A", position=(30, 0)),
            make_monster("Goblin Warrior", label="B", position=(35, 5)),
        ]
        encounter = Encounter(combatants, rng, spellbook=shaped_spellbook())
        encounter.creatures["Vesna"].spells = ("Stone Cube",)
        advance_to(encounter, "Vesna", rng)
        action = auto_action(encounter)
        assert action is not None
        assert action.spell == "Stone Cube"
        assert action.center is not None
        caught = encounter.area_targets(
            encounter.spellbook["Stone Cube"], "Vesna", center=action.center
        )
        assert sorted(creature.name for creature in caught) == ["A", "B"]

    def test_a_sphere_on_a_map_is_never_centred_where_the_caster_cannot_see(
        self,
    ) -> None:
        # Goblins far enough behind a full-height wall that every origin within
        # the blast's radius of them is on the hidden side: no visible origin
        # catches them, so the policy must not propose the cast at all. Everyone
        # stands in interior rows — a corner on the map boundary could legally
        # graze along the edge, which is the sight policy, not the subject here.
        rng = Random(SEED)
        sealed = BattleMap(
            name="sealed", width=10, height=5,
            terrain={(3, row): "wall" for row in range(5)},
            provenance=FIXTURE,
        )
        combatants = [
            blaster(position=(0, 10)),
            make_monster("Goblin Warrior", label="Hidden A", position=(40, 10)),
            make_monster("Goblin Warrior", label="Hidden B", position=(40, 15)),
        ]
        encounter = Encounter(
            combatants, rng, spellbook=spellbook(), battle_map=sealed
        )
        advance_to(encounter, "Ilva", rng)
        action = auto_action(encounter)
        assert action is None or action.kind is not ActionKind.CAST


class TestRoundsReported:
    def test_a_timed_out_fight_reports_the_cap_not_one_past_it(self) -> None:
        # advance() ticks the round over before the loop guard fails, so this used
        # to report 21 rounds for a 20-round cap.
        def stalemate() -> Sequence[Creature]:
            # Two creatures that cannot reach each other and have no ranged option.
            return [
                fighter("Thora", position=0),
                fighter("Ulf", team="foes", position=10_000),
            ]

        result = simulate_rounds(stalemate, iterations=3, seed=SEED, max_rounds=5)
        assert result["timed_out"] == 3
        assert result["rounds"]["max"] == 5.0
