"""Analytics tests.

The load-bearing one is ``test_one_iteration_matches_a_single_hand_driven_run``.
If a batch of one ever stops matching a single encounter at the same seed, the
analytics have drifted away from the rules live play uses, and every number they
produce is suspect.
"""

from __future__ import annotations

from collections.abc import Sequence
from random import Random
from typing import Any

import pytest

from fivee_sim.analytics.expectation import attack_damage_expectation
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
from fivee_sim.kernel.rules import Ability, DamageType
from fivee_sim.model.creature import AttackOption, Creature
from fivee_sim.model.encounter import ActionKind, Encounter

from .test_encounter import advance_to, fighter

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


def sword_and_spell(name: str = "Thora") -> Creature:
    """A three-attack build that also knows a spell.

    The weapon is deliberately worth more per swing than the spell (about 14.7
    against AC 15, against the bolt's 9.8), so the policy attacks first. That
    ordering is what exposes a turn budget the attacker never had: an Attack
    action it has already spent must not leave a second action in hand.
    """
    return Creature(
        name=name,
        team="party",
        ac=16,
        max_hp=60,
        speed=30,
        abilities={Ability.STRENGTH: 16, Ability.DEXTERITY: 14},
        attacks=(
            AttackOption(
                name="Greatsword",
                attack_bonus=9,
                damage=Dice(2, 8, 10),
                damage_type=DamageType.SLASHING,
                kind=AttackKind.MELEE,
                provenance="synthetic test fixture, not SRD content",
            ),
        ),
        attacks_per_action=3,
        spells=("Guiding Bolt",),
        spell_slots={1: 4},
        spell_save_dc=15,
        spell_attack_bonus=7,
        position=0,
        provenance="synthetic test fixture, not SRD content",
    )


class TestSimulateDprSpendsTheAttackersOwnTurnBudget:
    """Round 1 must run on the attacker's budget, not the initiative winner's.

    ``Encounter.__init__`` begins a turn for whoever won Initiative, and
    ``simulate_dpr`` then rewrites the order to put the attacker first. While the
    turn state was left as ``__init__`` built it, round 1 ran on the *dummy's*
    budget — ``movement_left=0``, ``attacks_left=1`` — in the 153/400 of seeds
    where the dummy won the roll, which cost swings, forfeited the round for a
    build starting out of reach, and left a spent action looking unspent.

    SRD 5.2 is explicit on both halves: "On your turn, you can move a distance up
    to your Speed and take one action" (Combat, "Your Turn"), and Extra Attack is
    "You can attack twice instead of once whenever you take the Attack action on
    your turn" — more swings inside one action, never a second action.
    """

    ROUNDS = 3
    TARGET_AC = 14

    @pytest.mark.parametrize("attacks_per_action", [1, 2, 3])
    def test_every_round_lands_the_full_legal_swing_count(
        self, attacks_per_action: int
    ) -> None:
        # Zero variance by construction: the dummy has 10,000 hit points so it
        # never drops, neither creature moves, and nothing else is on offer. The
        # count is therefore exactly the legal one or the budget was wrong.
        iterations = 200
        result = simulate_dpr(
            lambda: fighter("Thora", attacks_per_action=attacks_per_action),
            target_ac=self.TARGET_AC,
            rounds=self.ROUNDS,
            iterations=iterations,
            seed=SEED,
        )
        legal = attacks_per_action * self.ROUNDS * iterations
        swings = result["actions"].get("attack:Longsword", 0)
        # An Attack action grants attacks_per_action swings and no more.
        assert swings <= legal
        # And every one of them is taken.
        assert result["actions"] == {"attack:Longsword": legal}

    @pytest.mark.parametrize("attacks_per_action", [1, 2, 3])
    def test_damage_per_round_matches_the_closed_form(
        self, attacks_per_action: int
    ) -> None:
        # The oracle is the engine's own exact arithmetic, read off the same
        # fixture the run uses so the two cannot drift apart.
        option = fighter("Thora").attacks[0]
        expected = attacks_per_action * attack_damage_expectation(
            attack_bonus=option.attack_bonus,
            target_ac=self.TARGET_AC,
            damage=option.damage,
        )
        result = simulate_dpr(
            lambda: fighter("Thora", attacks_per_action=attacks_per_action),
            target_ac=self.TARGET_AC,
            rounds=self.ROUNDS,
            iterations=6_000,
            seed=SEED,
        )
        # 3% sits above four standard errors at this sample size and well below
        # the 6-8% the stale budget cost, so it discriminates rather than merely
        # passing.
        assert result["damage_per_round"] == pytest.approx(expected, rel=0.03)

    def test_a_build_starting_out_of_reach_still_closes_in_round_one(self) -> None:
        # The other half of the budget. A stale movement_left of 0 made
        # _closing_move return None, so a melee build measured at range simply
        # forfeited its first round.
        iterations = 200
        result = simulate_dpr(
            lambda: fighter("Thora"),
            target_ac=self.TARGET_AC,
            rounds=self.ROUNDS,
            iterations=iterations,
            seed=SEED,
            distance=20,
        )
        assert result["actions"]["attack:Longsword"] == self.ROUNDS * iterations

    def test_a_spent_attack_action_does_not_also_buy_a_spell(self) -> None:
        # The rules defect rather than the measurement one. _do_attack marks the
        # action used only when attacks_left falls to attacks_per_action - 1, so
        # a stale attacks_left of 1 on a three-attack build decremented to 0
        # without ever setting it — and the policy was then offered a Magic
        # action on top of the Attack action already taken.
        iterations = 200
        result = simulate_dpr(
            sword_and_spell,
            target_ac=15,
            rounds=self.ROUNDS,
            iterations=iterations,
            seed=SEED,
            spellbook=spellbook(),
        )
        assert result["actions"] == {"attack:Greatsword": 3 * self.ROUNDS * iterations}


class TestSimulateDprStaysReproducible:
    def test_the_same_seed_reproduces_the_same_result(self) -> None:
        def run(seed: int) -> dict[str, object]:
            return simulate_dpr(
                lambda: fighter("Thora", attacks_per_action=3),
                target_ac=14,
                rounds=3,
                iterations=120,
                seed=seed,
            )

        assert run(SEED) == run(SEED)
        assert run(SEED) != run(SEED + 1)

    def test_iteration_i_still_uses_seed_plus_i(self) -> None:
        def run(*, iterations: int, seed: int) -> dict[str, Any]:
            return simulate_dpr(
                lambda: fighter("Thora", attacks_per_action=3),
                target_ac=14,
                rounds=3,
                iterations=iterations,
                seed=seed,
            )

        batch = run(iterations=6, seed=SEED)
        singles = [
            run(iterations=1, seed=SEED + index)["damage"]["mean"] for index in range(6)
        ]
        assert batch["damage"]["min"] == min(singles)
        assert batch["damage"]["max"] == max(singles)
        # ``Stats.as_dict`` reports the mean rounded to three decimals, so this
        # agrees to within that rounding rather than exactly.
        assert batch["damage"]["mean"] == pytest.approx(sum(singles) / 6, abs=1e-3)


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


def blaster(name: str = "Ilva", *, position: int = 0, team: str = "party") -> Creature:
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
        caught = [
            creature
            for creature in combatants
            if abs(creature.position - action.center) <= 20
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
