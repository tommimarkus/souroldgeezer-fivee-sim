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
    run_encounter,
    simulate_dpr,
    simulate_rounds,
    summarise,
)
from fivee_sim.data import make_monster, spellbook
from fivee_sim.model.creature import Creature
from fivee_sim.model.encounter import Encounter

from .test_encounter import fighter

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
