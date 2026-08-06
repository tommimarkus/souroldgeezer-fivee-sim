"""Batch simulation: the same stepper, run many times and counted.

Both entry points here are loops over :mod:`fivee_sim.analytics.montecarlo`,
which is itself a loop over the encounter stepper the stateful tools drive —
never a parallel implementation of the rules. What this module owns is the
translation either side of that: combatant specs in, summary statistics out.

The registry is captured once per call and reused for every iteration.
Resolving content per iteration would let a reconfiguration land mid-batch and
make the result unreproducible from its seed, which is the one property these
numbers rest on.
"""

from __future__ import annotations

from typing import Any

from ..analytics.montecarlo import simulate_dpr as montecarlo_dpr
from ..analytics.montecarlo import simulate_rounds as montecarlo_rounds
from ..model.creature import Creature
from ..model.encounter import EncounterError
from . import sessions, specs
from .errors import RequestError
from .sessions import EngineState

__all__ = ["simulate_dpr", "simulate_rounds"]


def simulate_rounds(
    state: EngineState,
    combatants: list[dict[str, Any]],
    iterations: int = 500,
    seed: int = 0,
    max_rounds: int = 20,
    movement_rule: str = "5-5-5",
    map_spec: dict[str, Any] | None = None,
    map_id: str | None = None,
) -> dict[str, Any]:
    given = list(combatants)
    # The registry is captured once, here. Resolving content per iteration would let
    # a reconfiguration land mid-batch and make the result unreproducible from its
    # seed, which is the one property these numbers rest on.
    registry = sessions.active_registry(state)
    resolved = sessions.resolve_battle_map(state, map_spec, map_id)

    def factory() -> list[Creature]:
        return specs.combatants_from_specs(given, registry)

    try:
        result = montecarlo_rounds(
            factory,
            iterations=iterations,
            seed=seed,
            max_rounds=max_rounds,
            spellbook=dict(registry.spells),
            items=dict(registry.items),
            condition_effects=registry.condition_effects,
            movement_rule=specs.parse_movement_rule(movement_rule),
            map_document=resolved.document if resolved is not None else None,
            terrain_effects=registry.terrain_effects,
        )
    except (ValueError, EncounterError) as error:
        raise RequestError(str(error)) from error
    if resolved is not None and resolved.source is not None:
        result["map_source"] = resolved.source
    return result


def simulate_dpr(
    state: EngineState,
    build: dict[str, Any],
    target_ac: int,
    rounds: int = 3,
    iterations: int = 1000,
    seed: int = 0,
    distance: int = 5,
) -> dict[str, Any]:
    spec = dict(build)
    registry = sessions.active_registry(state)

    def attacker() -> Creature:
        creature = specs.creature_from_spec(spec, registry)
        creature.team = "attacker"
        return creature

    try:
        return montecarlo_dpr(
            attacker,
            target_ac=target_ac,
            rounds=rounds,
            iterations=iterations,
            seed=seed,
            distance=distance,
            spellbook=dict(registry.spells),
            items=dict(registry.items),
            condition_effects=registry.condition_effects,
        )
    except (ValueError, EncounterError) as error:
        raise RequestError(str(error)) from error
