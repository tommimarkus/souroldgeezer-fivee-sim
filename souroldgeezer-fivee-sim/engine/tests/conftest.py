"""Shared test infrastructure: global-state isolation and the fixtures every module reaches for.

Two jobs, and they are separate.

**Isolation.** ``mcp_server.server`` keeps its sessions, maps, content registry and
id counters in module-level globals. A test that creates an encounter or loads a
map mutates process state that outlives it, so the suite's result could depend on
collection order. :func:`_isolate_server_state` saves all five around every test
and puts them back, which makes each test start from the same globals it would see
if it ran alone.

**Shared helpers.** These used to live in ``test_kernel`` and ``test_encounter``,
and five other modules imported them across test-module boundaries — a test file
importing another test file is a dependency edge nothing declares and nothing
checks. They live here instead. pytest injects *fixtures* from a conftest
automatically but not plain functions and classes, so importers still say
``from .conftest import fighter`` explicitly; that import is the declaration.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from random import Random
from typing import Any

import pytest

from fivee_sim.kernel.actions import AttackKind
from fivee_sim.kernel.dice import Dice
from fivee_sim.kernel.rules import Ability, DamageType
from fivee_sim.kernel.spells import Spell, SpellShape
from fivee_sim.mcp_server import server as api
from fivee_sim.model.creature import AttackOption, Creature
from fivee_sim.model.encounter import Encounter

FIXTURE = "synthetic test fixture, not SRD content"


@pytest.fixture(autouse=True)
def _isolate_server_state() -> Iterator[None]:
    """Save and restore every module-level global in the MCP server around each test.

    ``_CONTENT`` is loaded lazily, so restoring it also *resets* it: the value put
    back is the ``None`` the module started with, and the next test that asks for
    content loads it fresh. That is what the two per-class fixtures in
    ``test_content`` used to arrange by hand for ``_CONTENT`` and ``_SESSIONS``;
    doing it here covers ``_MAPS`` and both id counters as well, which they missed.
    """
    sessions = dict(api._SESSIONS)
    maps = dict(api._MAPS)
    content = api._CONTENT
    next_id = api._NEXT_ID
    next_map_id = api._NEXT_MAP_ID
    try:
        yield
    finally:
        api._SESSIONS.clear()
        api._SESSIONS.update(sessions)
        api._MAPS.clear()
        api._MAPS.update(maps)
        api._CONTENT = content
        api._NEXT_ID = next_id
        api._NEXT_MAP_ID = next_map_id


class FixedRandom(Random):
    """A generator that forces a chosen d20 face, for pinning edge cases.

    The value is clamped to each die's own maximum, so ``FixedRandom(20)`` yields a
    natural 20 on a d20 *and* a 6 on every d6 of the damage that follows. Without
    the clamp a d6 would come back as 20 and damage assertions would be nonsense.
    """

    def __init__(self, natural: int) -> None:
        super().__init__(0)
        self._natural = natural

    def randint(self, a: int, b: int) -> int:
        return min(self._natural, b)


class ScriptedRandom(Random):
    """A generator that plays a written sequence of faces, then falls back to the max.

    :class:`FixedRandom` cannot express a sequence where the rolls must differ — an
    attack that *hits* and then a Constitution save that *fails* need a high face
    and a low one out of the same call. Each face is clamped into the die's own
    range, so a script written for d20s stays sane when a damage die is drawn.
    """

    def __init__(self, script: Sequence[int]) -> None:
        super().__init__(0)
        self._script = list(script)

    def randint(self, a: int, b: int) -> int:
        face = self._script.pop(0) if self._script else b
        return max(a, min(face, b))


def advance_to(encounter: Encounter, name: str, rng: Random, limit: int = 24) -> None:
    """Advance the initiative order until ``name`` holds the turn.

    Raises rather than falling through, so a test whose subject never gets a turn
    fails at the cause instead of going on to assert against the wrong creature.
    """
    for _ in range(limit):
        if encounter.current_name == name:
            return
        encounter.advance(rng)
    raise AssertionError(f"{name} never got a turn")


def fighter(
    name: str = "Thora",
    *,
    position: int | tuple[int, int] = 0,
    hp: int | None = None,
    max_hp: int = 30,
    team: str = "party",
    attacks_per_action: int = 1,
) -> Creature:
    return Creature(
        name=name,
        team=team,
        ac=16,
        max_hp=max_hp,
        hp=max_hp if hp is None else hp,
        speed=30,
        abilities={
            Ability.STRENGTH: 16,
            Ability.DEXTERITY: 14,
            Ability.CONSTITUTION: 14,
            Ability.INTELLIGENCE: 10,
            Ability.WISDOM: 12,
            Ability.CHARISMA: 8,
        },
        attacks=(
            AttackOption(
                name="Longsword",
                attack_bonus=5,
                damage=Dice(1, 8, 3),
                damage_type=DamageType.SLASHING,
                kind=AttackKind.MELEE,
                provenance=FIXTURE,
            ),
        ),
        attacks_per_action=attacks_per_action,
        position=position,
        provenance=FIXTURE,
    )


def caster(
    name: str = "Wren", *, position: int | tuple[int, int] = 0, team: str = "party"
) -> Creature:
    return Creature(
        name=name,
        team=team,
        ac=13,
        max_hp=24,
        speed=30,
        abilities={
            Ability.CONSTITUTION: 14,
            Ability.DEXTERITY: 12,
            Ability.INTELLIGENCE: 16,
        },
        spells=("Fireball", "Hold Person"),
        spell_slots={2: 1, 3: 1},
        spell_save_dc=15,
        spell_attack_bonus=6,
        position=position,
        provenance=FIXTURE,
    )


def shaped_spellbook() -> dict[str, Spell]:
    """One spell of each grid shape, small enough to reason about by hand."""
    common: dict[str, Any] = {
        "level": 1,
        "save_ability": Ability.DEXTERITY,
        "damage": Dice(3, 6, 0),
        "damage_type": DamageType.FIRE,
        "provenance": FIXTURE,
    }
    return {
        "Flame Fan": Spell(name="Flame Fan", shape=SpellShape.CONE, length=15,
                           **common),
        "Spark Line": Spell(name="Spark Line", shape=SpellShape.LINE, length=30,
                            **common),
        "Stone Cube": Spell(name="Stone Cube", shape=SpellShape.CUBE, size=10,
                            range_feet=60, **common),
    }


def shaper(position: int | tuple[int, int] = 0) -> Creature:
    return Creature(
        name="Vesna",
        team="party",
        ac=12,
        max_hp=20,
        spells=("Flame Fan", "Spark Line", "Stone Cube"),
        spell_slots={1: 5},
        spell_save_dc=13,
        position=position,
        provenance=FIXTURE,
    )
