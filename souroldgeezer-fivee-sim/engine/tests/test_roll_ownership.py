"""One owner for randomness: the engine draws every die it resolves."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields
from inspect import signature

import pytest

from fivee_sim.kernel.actions import resolve_attack
from fivee_sim.kernel.dice import roll_d20
from fivee_sim.kernel.rules import make_d20_test, resolve_attack_roll
from fivee_sim.kernel.spells import resolve_spell
from fivee_sim.model.encounter import Action, Encounter
from fivee_sim.service import encounters, primitives
from fivee_sim.web import routes

from . import api


@pytest.mark.parametrize(
    "resolver",
    [roll_d20, make_d20_test, resolve_attack_roll, resolve_attack, resolve_spell],
)
def test_kernel_roll_resolvers_do_not_accept_a_caller_supplied_face(
    resolver: Callable[..., object],
) -> None:
    assert "supplied" not in signature(resolver).parameters


def test_action_and_turn_advance_do_not_carry_a_caller_supplied_face() -> None:
    assert "natural" not in {field.name for field in fields(Action)}
    assert "natural" not in signature(Encounter.advance).parameters


@pytest.mark.parametrize(
    "operation",
    [
        primitives.roll,
        primitives.check,
        primitives.save,
        encounters.execute_act,
        encounters.act,
        encounters.execute_advance,
        encounters.advance,
    ],
)
def test_service_roll_operations_do_not_accept_a_caller_supplied_face(
    operation: Callable[..., object],
) -> None:
    assert "natural" not in signature(operation).parameters


@pytest.mark.parametrize(
    "operation",
    ["dice.roll", "dice.check", "dice.save", "encounter.act", "encounter.advance"],
)
def test_published_roll_operations_do_not_advertise_a_natural_input(
    operation: str,
) -> None:
    route = next(route for route in routes.ROUTES if route.operation == operation)
    assert route.body_schema is not None
    assert "natural" not in route.body_schema["properties"]


def test_engine_generated_d20_output_stays_natural_and_seeded() -> None:
    rolled = api.roll("1d20", seed=73)
    rolled_again = api.roll("1d20", seed=73)
    first = api.check(modifier=3, dc=12, advantage="advantage", seed=73)
    again = api.check(modifier=3, dc=12, advantage="advantage", seed=73)

    assert rolled == rolled_again
    assert rolled["natural"] == rolled["rolls"][0]
    assert first == again
    assert 1 <= first["natural"] <= 20
    assert first["total"] == first["natural"] + 3
