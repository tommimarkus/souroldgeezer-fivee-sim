"""Loading the bundled SRD 5.2 data slice.

The JSON files are a transcription, not a derivation: each record carries its own
provenance, and each lists the printed traits this engine does not implement so
the gaps are visible. Nothing outside SRD 5.2 belongs here — see CLAUDE.md.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

from ..kernel.actions import AttackKind
from ..kernel.conditions import Condition
from ..kernel.dice import Dice
from ..kernel.rules import Ability, DamageType
from ..kernel.spells import Spell, SpellShape
from ..model.creature import AttackOption, Creature

_SRD = "fivee_sim.data.srd"


class DataError(ValueError):
    """A bundled data file is malformed."""


def _read(filename: str) -> dict[str, Any]:
    text = resources.files(_SRD).joinpath(filename).read_text(encoding="utf-8")
    payload: dict[str, Any] = json.loads(text)
    return payload


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
    )


@lru_cache(maxsize=1)
def monster_records() -> dict[str, dict[str, Any]]:
    payload = _read("monsters.json")
    return {str(record["name"]): record for record in payload["monsters"]}


@lru_cache(maxsize=1)
def spellbook() -> dict[str, Spell]:
    """Every bundled spell, keyed by name."""
    payload = _read("spells.json")
    book: dict[str, Spell] = {}
    for record in payload["spells"]:
        damage = record.get("damage")
        upcast = record.get("upcast_damage")
        save = record.get("save_ability")
        condition = record.get("condition")
        damage_type = record.get("damage_type")
        spell = Spell(
            name=str(record["name"]),
            level=int(record["level"]),
            school=str(record.get("school", "")),
            requires_attack_roll=bool(record.get("requires_attack_roll", False)),
            save_ability=Ability(save) if save else None,
            damage=Dice.parse(str(damage)) if damage else None,
            damage_type=DamageType(damage_type) if damage_type else None,
            half_on_save=bool(record.get("half_on_save", True)),
            upcast_damage=Dice.parse(str(upcast)) if upcast else None,
            shape=SpellShape(record.get("shape", "single")),
            radius=int(record.get("radius", 0)),
            range_feet=int(record.get("range_feet", 0)),
            max_targets=int(record.get("max_targets", 1)),
            condition=Condition(condition) if condition else None,
            concentration=bool(record.get("concentration", False)),
        )
        book[spell.name] = spell
    return book


def monster_names() -> list[str]:
    return sorted(monster_records())


def make_monster(
    name: str,
    *,
    label: str | None = None,
    team: str | None = None,
    position: int = 0,
) -> Creature:
    """Build a fresh creature from a bundled stat block.

    ``label`` renames the instance, which matters because combatant names identify
    them: two goblins in one fight need distinct labels.
    """
    records = monster_records()
    record = records.get(name)
    if record is None:
        raise DataError(f"no bundled stat block named {name!r}; have: {', '.join(records)}")
    return Creature(
        name=label or str(record["name"]),
        team=team or str(record.get("team", "monsters")),
        ac=int(record["ac"]),
        max_hp=int(record["max_hp"]),
        speed=int(record.get("speed", 30)),
        abilities={Ability(key): int(value) for key, value in record["abilities"].items()},
        save_bonuses={
            Ability(key): int(value)
            for key, value in record.get("save_bonuses", {}).items()
        },
        attacks=tuple(_attack(entry) for entry in record.get("attacks", [])),
        attacks_per_action=int(record.get("attacks_per_action", 1)),
        immunities=frozenset(DamageType(entry) for entry in record.get("immunities", [])),
        resistances=frozenset(DamageType(entry) for entry in record.get("resistances", [])),
        vulnerabilities=frozenset(
            DamageType(entry) for entry in record.get("vulnerabilities", [])
        ),
        position=position,
        provenance=str(record.get("provenance", "SRD 5.2")),
    )
