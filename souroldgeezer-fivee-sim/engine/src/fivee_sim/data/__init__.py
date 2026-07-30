"""Building creatures from content records.

The bundled SRD 5.2 slice is no longer parsed here — it is two content packs,
loaded by :mod:`fivee_sim.content` like anyone else's. What remains is the step
that turns a validated record into a live
:class:`~fivee_sim.model.creature.Creature`, plus thin accessors over the
built-in-only registry for callers that genuinely only want what ships.

Those accessors are cached because the bundled files cannot change within a
session. Anything reflecting *loaded* content must take a registry instead: a
cached view of content the user can reconfigure is a stale answer waiting to
happen.

Records reaching :func:`make_creature` have already been validated by ``content``,
so construction here does not re-check them; a malformed pack fails at load, with
a diagnostic naming the field, rather than half-way into a fight.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from ..content import ContentRegistry, builtin_registry
from ..kernel.actions import AttackKind
from ..kernel.conditions import ConditionTable
from ..kernel.dice import Dice
from ..kernel.items import ItemEffect
from ..kernel.rules import Ability, DamageType
from ..kernel.spells import Spell
from ..model.creature import AttackOption, Creature


class DataError(ValueError):
    """A creature was asked for that the active content does not define."""


@lru_cache(maxsize=1)
def builtin() -> ContentRegistry:
    """The bundled SRD slice alone. Safe to cache: it ships inside the plugin."""
    return builtin_registry()


def _attack(record: dict[str, Any]) -> AttackOption:
    return AttackOption(
        name=str(record["name"]),
        attack_bonus=int(record["attack_bonus"]),
        damage=Dice.parse(str(record["damage"])),
        damage_type=DamageType(record["damage_type"]),
        kind=AttackKind(record.get("kind", "melee")),
        reach=int(record.get("reach", 5)),
        normal_range=int(record.get("normal_range", 0)),
        long_range=int(record.get("long_range", 0)),
        provenance=str(record.get("provenance", "SRD 5.2")),
    )


def make_creature(
    name: str,
    *,
    registry: ContentRegistry | None = None,
    label: str | None = None,
    team: str | None = None,
    position: int = 0,
) -> Creature:
    """Build a fresh creature from a content record.

    ``label`` renames the instance, which matters because combatant names identify
    them: two goblins in one fight need distinct labels.
    """
    active = builtin() if registry is None else registry
    record = active.creatures.get(name)
    if record is None:
        available = ", ".join(sorted(active.creatures)) or "none"
        raise DataError(
            f"no stat block named {name!r} in the loaded content "
            f"(built-in content is {active.builtin.value}d); available: {available}"
        )
    return Creature(
        name=label or str(record["name"]),
        team=team or str(record.get("team", "monsters")),
        ac=int(record["ac"]),
        max_hp=int(record["max_hp"]),
        speed=int(record.get("speed", 30)),
        abilities={
            Ability(key): int(value) for key, value in record.get("abilities", {}).items()
        },
        save_bonuses={
            Ability(key): int(value)
            for key, value in record.get("save_bonuses", {}).items()
        },
        attacks=tuple(_attack(entry) for entry in record.get("attacks", [])),
        attacks_per_action=int(record.get("attacks_per_action", 1)),
        spells=tuple(str(entry) for entry in record.get("spells", [])),
        spell_slots={int(k): int(v) for k, v in record.get("spell_slots", {}).items()},
        spell_save_dc=int(record.get("spell_save_dc", 10)),
        spell_attack_bonus=int(record.get("spell_attack_bonus", 0)),
        items={str(k): int(v) for k, v in record.get("items", {}).items()},
        immunities=frozenset(DamageType(entry) for entry in record.get("immunities", [])),
        resistances=frozenset(DamageType(entry) for entry in record.get("resistances", [])),
        vulnerabilities=frozenset(
            DamageType(entry) for entry in record.get("vulnerabilities", [])
        ),
        conditions={str(entry) for entry in record.get("conditions", [])},
        condition_effects=active.condition_effects,
        position=position,
        provenance=str(record.get("provenance", active.source_of("creatures", name))),
    )


# --- built-in-only accessors ----------------------------------------------
def monster_records() -> dict[str, dict[str, Any]]:
    """Bundled stat blocks, keyed by name."""
    return dict(builtin().creatures)


def spell_records() -> dict[str, dict[str, Any]]:
    """Raw bundled spell records, which carry fields the engine does not model."""
    return dict(builtin().spell_records)


def spellbook() -> dict[str, Spell]:
    """Every bundled spell, keyed by name."""
    return dict(builtin().spells)


def item_effects() -> dict[str, ItemEffect]:
    """Every bundled usable item, keyed by name."""
    return dict(builtin().items)


def condition_table() -> ConditionTable:
    """The bundled condition table."""
    return builtin().condition_effects


def monster_names() -> list[str]:
    return sorted(builtin().creatures)


def make_monster(
    name: str,
    *,
    label: str | None = None,
    team: str | None = None,
    position: int = 0,
) -> Creature:
    """Build a creature from a *bundled* stat block. See :func:`make_creature`."""
    return make_creature(name, registry=builtin(), label=label, team=team, position=position)
