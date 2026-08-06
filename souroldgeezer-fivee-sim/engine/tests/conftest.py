"""Shared test infrastructure: global-state isolation and the fixtures every module reaches for.

Two jobs, and they are separate.

**Isolation.** ``tests.api`` keeps its sessions, maps, content registry and
id counters in one process-wide ``EngineState``. A test that creates an encounter or
loads a map mutates process state that outlives it, so the suite's result could depend
on collection order. :func:`_isolate_server_state` saves all five fields around every
test and puts them back, which makes each test start from the same state it would see
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
from fivee_sim.model.creature import AttackOption, Creature
from fivee_sim.model.encounter import Encounter

from . import api

FIXTURE = "synthetic test fixture, not SRD content"

REPLAY_HERO: dict[str, Any] = {
    "name": "Thora",
    "team": "party",
    "ac": 16,
    "max_hp": 30,
    "position": [5, 5],
    "attacks": [
        {
            "name": "Longsword",
            "attack_bonus": 5,
            "damage": "1d8+3",
            "damage_type": "slashing",
            "kind": "melee",
        }
    ],
}
REPLAY_GOBLIN: dict[str, Any] = {
    "monster": "Goblin Warrior",
    "label": "Goblin",
    "team": "monsters",
    "position": [15, 15],
}

#: The interlude fixtures, here rather than in one of the two test modules that
#: want them, for this file's own reason: ``test_adventure_replay`` composes the
#: run ``test_adventures`` links, and a test module importing another test
#: module is a dependency edge nothing declares.
#:
#: Ground for an interlude: open floor, wide enough that everybody who walks
#: across it in one chapter is still somewhere distinct in the next. A *saved*
#: map, because carrying one is carrying an id.
MILL: dict[str, Any] = {
    "format": "fivee-sim-map",
    "format_version": 1,
    "name": "The Drowned Mill",
    "grid": {"width": 12, "height": 12, "cell_feet": 5},
    "legend": {".": "normal"},
    "tiles": ["." * 12 for _ in range(12)],
    "features": [],
    "provenance": {
        "generator": "hand",
        "seed": 0,
        "params": {},
        "edited": False,
        "source": FIXTURE,
    },
}
#: An interlude's cast: two of the party and nobody to fight. Neither carries an
#: attack, which is the point — this chapter is a walk across a floor.
SCOUT: dict[str, Any] = {
    "name": "Kettle", "team": "party", "ac": 12, "max_hp": 20, "position": [5, 5],
}
LOOKOUT: dict[str, Any] = {
    "name": "Bo", "team": "party", "ac": 12, "max_hp": 20, "position": [5, 15],
}
#: Who is waiting when the interlude ends, standing well clear of both squares
#: the party walks to.
AMBUSHER: dict[str, Any] = {
    "name": "Stalker",
    "team": "monsters",
    "ac": 10,
    "max_hp": 30,
    "position": [55, 55],
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


@pytest.fixture(autouse=True)
def _isolate_server_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> Iterator[None]:
    """Save and restore the shared engine state, and root every file it writes.

    ``content`` is loaded lazily, so restoring it also *resets* it: the value put
    back is the ``None`` the state started with, and the next test that asks for
    content loads it fresh. That is what the two per-class fixtures in
    ``test_content`` used to arrange by hand for the content and the sessions;
    doing it here covers the id counter as well, which they missed.

    The four directory variables matter more than they used to. A map is a
    *file* now rather than an entry in a process dictionary, and a scene always
    was one, so a test that saves either writes to whatever ``maps_root()`` or
    ``scenes_root()`` resolves — the current directory's ``.fivee-sim/`` when
    nothing says otherwise, which is the repository. Pointing all four at
    ``tmp_path`` keeps the suite's writes inside the test and keeps one test's
    files invisible to the next.
    """
    sessions = dict(api.STATE.sessions)
    content = api.STATE.content
    next_id = api.STATE.next_id
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(tmp_path / "encounters"))
    monkeypatch.setenv("FIVEE_SIM_MAPS", str(tmp_path / "maps"))
    monkeypatch.setenv("FIVEE_SIM_REPLAYS", str(tmp_path / "replays"))
    monkeypatch.setenv("FIVEE_SIM_SCENES", str(tmp_path / "scenes"))
    try:
        yield
    finally:
        api.STATE.sessions.clear()
        api.STATE.sessions.update(sessions)
        api.STATE.content = content
        api.STATE.next_id = next_id


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


def advance_encounter_to(encounter_id: str, name: str, limit: int = 24) -> None:
    """Advance an in-process encounter until ``name`` holds the turn."""
    for _ in range(limit):
        if api.encounter_state(encounter_id)["turn"] == name:
            return
        api.encounter_advance(encounter_id)
    raise AssertionError(f"{name} never got a turn")


def mapless_fight(seed: int = 41) -> str:
    """Create the shared two-combatant replay fixture through the engine."""
    created = api.encounter_create(
        [dict(REPLAY_HERO), dict(REPLAY_GOBLIN)], seed=seed
    )
    return str(created["encounter_id"])


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
        "Warm Aura": Spell(name="Warm Aura", shape=SpellShape.EMANATION, radius=10,
                           **common),
        "Frost Pillar": Spell(name="Frost Pillar", shape=SpellShape.CYLINDER,
                              radius=10, height=40, range_feet=60, **common),
    }


def shaper(position: int | tuple[int, int] = 0) -> Creature:
    return Creature(
        name="Vesna",
        team="party",
        ac=12,
        max_hp=20,
        spells=("Flame Fan", "Spark Line", "Stone Cube", "Warm Aura", "Frost Pillar"),
        spell_slots={1: 5},
        spell_save_dc=13,
        position=position,
        provenance=FIXTURE,
    )
