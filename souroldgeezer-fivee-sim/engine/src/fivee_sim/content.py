"""Content packs: the engine's rules data, whether we shipped it or you wrote it.

A pack is one JSON file holding creatures, spells, conditions, terrain, and
items. The bundled SRD slice under ``data/srd/`` is not a special case — it is
two packs the engine happens to carry, parsed by the code below like any other. That is what
makes a campaign's own file "compatible" rather than a second dialect: there is
only one format, and we eat it too.

Three properties this module exists to guarantee:

**Validation is strict.** An unknown section or an unknown record key is an error,
never ignored. A pack that misspells ``attack_bonus`` as ``attack_bonuses`` would
otherwise load a creature that fights wrongly and looks entirely fine.

**A name collision is reported, never resolved silently.** Two packs defining
``Goblin Warrior`` fail and name both files. A pack that *means* to replace
something says ``"overrides": true`` on that record, so intent is declared rather
than inferred from whichever file happened to load first.

**Nothing is ambient.** :func:`load_packs` returns a :class:`ContentRegistry` and
holds no module state. Callers pass the tables into the encounter, which captures
them, so reloading content cannot reach into a fight already in progress.

There is exactly one cached thing here, and the line it sits on matters.
:func:`builtin` caches the *bundled* registry, which is safe because the files it
reads ship inside the plugin and cannot change within a session; the
built-in-only accessors near the bottom of this module are views over it. Anything
reflecting **loaded** content must take a registry argument instead, because the
user can reconfigure that at any time and a cached view of it is a stale answer
waiting to happen. Add an accessor on the wrong side of that line and
``content_configure`` stops meaning anything.

Diagnostics are structured because the consumer is a campaign author debugging
their own JSON, and "invalid pack" tells them nothing they can act on.

:func:`make_creature` closes the loop from a name to a combatant. It lives here
rather than in ``model`` because the lookup is a question about a registry, which
is this module's concept; the construction it delegates to lives in
:meth:`fivee_sim.model.creature.Creature.from_record`, because that is the layer
that owns creatures. The two halves change for different reasons — the record
format here, the creature's fields there — and neither imports upward.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from .kernel.actions import AttackKind, RiderExpiry
from .kernel.conditions import EFFECT_FLAGS, EFFECTS, Condition, ConditionEffect, ConditionTable
from .kernel.grid import TERRAIN, TERRAIN_FLAGS, Point, TerrainEffect
from .kernel.items import ItemEffect, ItemError
from .kernel.rules import Ability, DamageType, Size
from .kernel.spells import Spell, SpellShape
from .model.creature import Creature, DeathRule

# Re-exported under their historical home: the diagnostic machinery grew up in
# this module and every import site — packs, tests, the server — still reads it
# from here. The definitions moved to ``validation`` so map documents can share
# the idiom without importing the pack machinery.
from .validation import ContentError as ContentError
from .validation import Diagnostic as Diagnostic
from .validation import Reader as _Reader
from .validation import Severity as Severity

#: Environment variable holding an ``os.pathsep``-separated list of files or
#: directories to load.
CONTENT_ENV = "FIVEE_SIM_CONTENT"
#: Environment variable selecting whether the bundled SRD slice is loaded.
BUILTIN_ENV = "FIVEE_SIM_BUILTIN"
#: Host-neutral project root. Plugin skills set this when the host does not export
#: a native project-directory variable.
PROJECT_ENV = "FIVEE_SIM_PROJECT_DIR"
#: Claude Code's native project root, retained as a compatibility fallback.
CLAUDE_PROJECT_ENV = "CLAUDE_PROJECT_DIR"
PROJECT_SUBDIR = Path(".fivee-sim") / "content"

_BUILTIN_PACKAGE = "fivee_sim.data.srd"
BUILTIN_FILES = ("monsters.json", "spells.json")

#: A pack larger than this fails cleanly instead of stalling session start. Packs
#: are hand-authored rules data; the whole SRD would not approach this.
MAX_PACK_BYTES = 4 * 1024 * 1024

#: Conditions the stepper applies on its own behalf when a creature drops to 0 hit
#: points. They survive ``exclude`` mode because dropping is engine machinery, not
#: content — a pack may still override them, and ``content_status`` reports when
#: they were retained rather than loaded.
STRUCTURAL_CONDITIONS = (Condition.UNCONSCIOUS, Condition.PRONE)

SECTIONS = ("creatures", "spells", "conditions", "terrain", "items")

_PACK_KEYS = frozenset({"pack", "version", "provenance", "attribution", "note", *SECTIONS})
_COMMON_RECORD_KEYS = frozenset({"name", "provenance", "unmodelled", "overrides"})
_CREATURE_KEYS = _COMMON_RECORD_KEYS | {
    "team", "ac", "max_hp", "hit_dice", "speed", "climb_speed", "swim_speed",
    "fly_speed", "terrain_cost_overrides", "darkvision", "blindsight", "death_rule",
    "size", "abilities", "save_bonuses",
    "attacks", "attacks_per_action", "spells", "spell_slots", "spell_save_dc",
    "spell_attack_bonus", "items", "conditions", "immunities", "resistances",
    "vulnerabilities", "pack_tactics", "undead_fortitude",
}
_ATTACK_KEYS = frozenset({
    "name", "attack_bonus", "damage", "damage_type", "kind", "reach", "normal_range",
    "long_range", "bonus_damage", "bonus_damage_type", "advantage_bonus_damage",
    "on_hit_condition", "on_hit_save_ability", "on_hit_save_dc", "on_hit_expiry",
    "on_hit_max_size", "on_hit_attach", "attached_damage", "attached_damage_type",
    "detach_after_damage", "provenance",
})
_SPELL_KEYS = _COMMON_RECORD_KEYS | {
    "level", "school", "requires_attack_roll", "attack_kind", "save_ability", "damage",
    "damage_type",
    "half_on_save", "upcast_damage", "shape", "radius", "length", "size", "width",
    "range_feet", "max_targets", "condition", "concentration",
}
_CONDITION_KEYS = _COMMON_RECORD_KEYS | {"effects", "description"}
_TERRAIN_KEYS = _COMMON_RECORD_KEYS | {"effects", "description"}
_ITEM_KEYS = _COMMON_RECORD_KEYS | {"use", "description"}
_USE_KEYS = frozenset({
    "heal", "damage", "damage_type", "save_ability", "save_dc", "half_on_save",
    "condition",
})


class BuiltinMode(StrEnum):
    INCLUDE = "include"
    EXCLUDE = "exclude"


@dataclass(frozen=True, slots=True)
class PackInfo:
    """What a loaded pack was and where it came from."""

    label: str
    level: str
    pack: str
    version: str
    provenance: str
    path: str = ""
    counts: Mapping[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "level": self.level,
            "pack": self.pack,
            "version": self.version,
            "provenance": self.provenance,
            "path": self.path,
            "counts": dict(self.counts),
        }


@dataclass(frozen=True, slots=True)
class ContentRegistry:
    """Everything the engine knows about, merged and immutable.

    Immutability is the point: an encounter captures the tables it needs, and a
    later reload builds a *new* registry rather than mutating this one, so a fight
    in progress cannot have content changed underneath it.
    """

    builtin: BuiltinMode = BuiltinMode.INCLUDE
    creatures: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    spells: Mapping[str, Spell] = field(default_factory=dict)
    spell_records: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    condition_effects: Mapping[str, ConditionEffect] = field(default_factory=dict)
    condition_records: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    terrain_effects: Mapping[str, TerrainEffect] = field(default_factory=dict)
    terrain_records: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    items: Mapping[str, ItemEffect] = field(default_factory=dict)
    item_records: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    packs: tuple[PackInfo, ...] = ()
    sources: Mapping[tuple[str, str], str] = field(default_factory=dict)
    warnings: tuple[Diagnostic, ...] = ()
    #: Structural conditions kept despite not being defined by any loaded pack.
    retained_conditions: tuple[str, ...] = ()

    def source_of(self, section: str, name: str) -> str:
        """Which pack a name came from, or ``"engine"`` for a retained structural row."""
        return self.sources.get((section, name), "engine")

    def records_for(self, section: str) -> Mapping[str, dict[str, Any]]:
        return {
            "creatures": self.creatures,
            "spells": self.spell_records,
            "conditions": self.condition_records,
            "terrain": self.terrain_records,
            "items": self.item_records,
        }[section]

    def names(self) -> dict[str, list[str]]:
        return {section: sorted(self.records_for(section)) for section in SECTIONS}

    def summary(self) -> dict[str, Any]:
        return {
            "builtin": self.builtin.value,
            "counts": {
                section: len(self.records_for(section)) for section in SECTIONS
            },
            "packs": [pack.as_dict() for pack in self.packs],
            "retained_conditions": list(self.retained_conditions),
            "warnings": [warning.as_dict() for warning in self.warnings],
        }


# --- parsing one pack ------------------------------------------------------
@dataclass(slots=True)
class _ParsedPack:
    info: PackInfo
    creatures: dict[str, dict[str, Any]]
    spells: dict[str, Spell]
    spell_records: dict[str, dict[str, Any]]
    condition_effects: dict[str, ConditionEffect]
    condition_records: dict[str, dict[str, Any]]
    terrain_effects: dict[str, TerrainEffect]
    terrain_records: dict[str, dict[str, Any]]
    items: dict[str, ItemEffect]
    item_records: dict[str, dict[str, Any]]

    def section(self, name: str) -> Mapping[str, Any]:
        return {
            "creatures": self.creatures,
            "spells": self.spell_records,
            "conditions": self.condition_records,
            "terrain": self.terrain_records,
            "items": self.item_records,
        }[name]


def _named_records(
    payload: Mapping[str, Any],
    section: str,
    diagnostics: list[Diagnostic],
    source: str,
) -> list[tuple[str, Mapping[str, Any]]]:
    """Pull ``section`` out of a pack, reporting anything that is not a named record."""
    entries = payload.get(section)
    if entries is None:
        return []
    if not isinstance(entries, list):
        diagnostics.append(
            Diagnostic(source=source, section=section, problem="must be a list of records")
        )
        return []
    out: list[tuple[str, Mapping[str, Any]]] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            diagnostics.append(
                Diagnostic(
                    source=source, section=section, record=f"#{index}",
                    problem=f"must be an object, got {type(entry).__name__}",
                )
            )
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            diagnostics.append(
                Diagnostic(
                    source=source, section=section, record=f"#{index}",
                    field="name", problem="required, and must be non-empty text",
                )
            )
            continue
        if name in seen:
            diagnostics.append(
                Diagnostic(
                    source=source, section=section, record=name,
                    problem="defined twice in the same pack",
                )
            )
            continue
        seen.add(name)
        out.append((name, entry))
    return out


def _common_fields(reader: _Reader) -> str:
    """The fields every record carries, checked identically in every section.

    Shared rather than repeated because it was repeated and one section quietly did
    not: creatures went without a ``provenance`` check, which is the one rule the
    licence boundary actually turns on.
    """
    provenance = reader.string("provenance", required=True)
    reader.string_list("unmodelled")
    reader.boolean("overrides")
    return provenance


def _parse_creature(
    name: str, record: Mapping[str, Any], diagnostics: list[Diagnostic], source: str
) -> dict[str, Any] | None:
    reader = _Reader(record, diagnostics, source=source, section="creatures", name=name)
    reader.unknown_keys(_CREATURE_KEYS)
    _common_fields(reader)
    reader.integer("ac", required=True, minimum=0)
    reader.integer("max_hp", required=True, minimum=1)
    reader.integer("speed", default=30, minimum=0)
    reader.integer("climb_speed", default=0, minimum=0)
    reader.integer("swim_speed", default=0, minimum=0)
    reader.integer("fly_speed", default=0, minimum=0)
    reader.string_list("terrain_cost_overrides")
    reader.integer("darkvision", default=0, minimum=0)
    reader.integer("blindsight", default=0, minimum=0)
    reader.enum("death_rule", DeathRule)
    reader.enum("size", Size)
    reader.integer("attacks_per_action", default=1, minimum=1)
    reader.boolean("pack_tactics")
    reader.boolean("undead_fortitude")
    reader.integer("spell_save_dc", default=10, minimum=1)
    reader.integer("spell_attack_bonus")
    reader.string("team")
    reader.string("hit_dice")
    reader.enum_keyed_ints("abilities", Ability)
    reader.enum_keyed_ints("save_bonuses", Ability)
    for key in ("immunities", "resistances", "vulnerabilities"):
        reader.enum_list(key, DamageType)
    reader.string_list("spells")
    reader.string_list("conditions")
    for item_name, count in reader.mapping("items").items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            reader.fail("items", f"{item_name} quantity must be a whole number of 0 or more")
    for level, count in reader.mapping("spell_slots").items():
        if not (isinstance(level, str) and level.isdigit()):
            reader.fail("spell_slots", f"slot level {level!r} must be a number")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            reader.fail("spell_slots", f"slot {level} count must be a whole number")

    for index, attack in enumerate(reader.sequence("attacks")):
        label = f"{name} attack #{index}"
        if not isinstance(attack, dict):
            reader.fail("attacks", f"attack #{index} must be an object")
            continue
        sub = _Reader(attack, diagnostics, source=source, section="creatures", name=label)
        sub.unknown_keys(_ATTACK_KEYS)
        sub.string("name", required=True)
        sub.integer("attack_bonus", required=True)
        if attack.get("damage") is None:
            sub.fail("damage", "required")
        else:
            sub.dice("damage")
        if attack.get("damage_type") is None:
            sub.fail("damage_type", "required")
        else:
            sub.enum("damage_type", DamageType)
        sub.enum("kind", AttackKind)
        sub.integer("reach", default=5, minimum=0)
        sub.integer("normal_range", minimum=0)
        sub.integer("long_range", minimum=0)
        # The riders. Pairings are enforced here, in the layer whose job is a
        # diagnostic the author can act on; whether an on_hit_condition resolves
        # is a question only the merged set can answer, so that one check lives
        # in _cross_reference with the spells' and items' condition checks.
        sub.dice("bonus_damage")
        sub.enum("bonus_damage_type", DamageType)
        if attack.get("bonus_damage") is not None and (
            attack.get("bonus_damage_type") is None
        ):
            sub.fail(
                "bonus_damage_type",
                "required when bonus_damage is present: the extra damage is "
                "defended against its own type",
            )
        if attack.get("bonus_damage") is None and (
            attack.get("bonus_damage_type") is not None
        ):
            sub.fail("bonus_damage", "bonus_damage_type names a type for no damage")
        sub.dice("advantage_bonus_damage")
        sub.string("on_hit_condition")
        sub.enum("on_hit_save_ability", Ability)
        sub.integer("on_hit_save_dc", minimum=1)
        sub.enum("on_hit_expiry", RiderExpiry)
        sub.enum("on_hit_max_size", Size)
        sub.boolean("on_hit_attach")
        sub.dice("attached_damage")
        sub.enum("attached_damage_type", DamageType)
        sub.integer("detach_after_damage", minimum=0)
        if attack.get("on_hit_attach"):
            if attack.get("attached_damage") is None:
                sub.fail("attached_damage", "required when on_hit_attach is true")
            if attack.get("attached_damage_type") is None:
                sub.fail("attached_damage_type", "required when on_hit_attach is true")
        elif any(
            attack.get(key) is not None
            for key in ("attached_damage", "attached_damage_type", "detach_after_damage")
        ):
            sub.fail(
                "on_hit_attach",
                "must be true when attachment damage or a detach threshold is declared",
            )
        if attack.get("on_hit_condition") is None:
            for dependent in (
                "on_hit_save_ability", "on_hit_save_dc", "on_hit_expiry", "on_hit_max_size",
            ):
                if attack.get(dependent) is not None:
                    sub.fail(
                        dependent,
                        "needs on_hit_condition: there is no condition to ride the hit",
                    )
        else:
            if attack.get("on_hit_save_ability") is not None and (
                attack.get("on_hit_save_dc") is None
            ):
                sub.fail(
                    "on_hit_save_dc", "required when on_hit_save_ability is present"
                )
            if attack.get("on_hit_save_dc") is not None and (
                attack.get("on_hit_save_ability") is None
            ):
                sub.fail(
                    "on_hit_save_ability", "required when on_hit_save_dc is present"
                )
        if not sub.ok:
            reader.ok = False

    return dict(record) if reader.ok else None


def _parse_spell(
    name: str, record: Mapping[str, Any], diagnostics: list[Diagnostic], source: str
) -> tuple[Spell, dict[str, Any]] | None:
    reader = _Reader(record, diagnostics, source=source, section="spells", name=name)
    reader.unknown_keys(_SPELL_KEYS)
    provenance = _common_fields(reader)
    level = reader.integer("level", required=True, minimum=0)
    shape = reader.enum("shape", SpellShape)
    spell = Spell(
        name=name,
        level=level,
        school=reader.string("school"),
        requires_attack_roll=reader.boolean("requires_attack_roll"),
        attack_kind=reader.enum("attack_kind", AttackKind) or AttackKind.RANGED,
        save_ability=reader.enum("save_ability", Ability),
        damage=reader.dice("damage"),
        damage_type=reader.enum("damage_type", DamageType),
        half_on_save=reader.boolean("half_on_save", default=False),
        upcast_damage=reader.dice("upcast_damage"),
        shape=shape or SpellShape.SINGLE,
        radius=reader.integer("radius", minimum=0),
        length=reader.integer("length", minimum=0),
        size=reader.integer("size", minimum=0),
        width=reader.integer("width", default=5, minimum=5),
        range_feet=reader.integer("range_feet", minimum=0),
        max_targets=reader.integer("max_targets", default=1, minimum=1),
        condition=reader.string("condition") or None,
        concentration=reader.boolean("concentration"),
        provenance=provenance,
    )
    if spell.damage is not None and spell.damage_type is None:
        reader.fail("damage_type", "a spell that deals damage must name a damage type")
    if spell.requires_attack_roll and spell.save_ability is not None:
        reader.fail(
            "save_ability",
            "a spell cannot both require an attack roll and offer a saving throw",
        )
    _check_area_declaration(reader, record, spell, shape)
    return (spell, dict(record)) if reader.ok else None


def _check_area_declaration(
    reader: _Reader, record: Mapping[str, Any], spell: Spell, shape: SpellShape | None
) -> None:
    """Refuse an area declaration whose shape and measurement disagree.

    ``shape`` names the template and one measurement field gives its extent —
    ``radius`` for a sphere, ``length`` for a cone or line, ``size`` for a cube.
    Resolution branches on the shape, so a shape without its measurement has no
    extent at all, and a measurement without its shape is a declaration split
    against itself. One legacy reading survives: a ``radius`` with no ``shape``
    resolves as a sphere, because packs predating the other templates wrote
    exactly that.

    Checked at parse time rather than in :func:`_cross_reference`, because this is a
    property of one record and the raw ``record`` is what distinguishes an omitted
    ``shape`` from an explicit ``"single"``. A ``shape`` that failed to parse is left
    out of it: the enum check has already reported that, and guessing at the intent
    behind an unknown word would only add a second, wronger message.
    """
    declared = record.get("shape")
    if declared is not None and shape is None:
        return
    # Each shape needs its measurement, or the area has no extent at all.
    if spell.shape is SpellShape.SPHERE and spell.radius <= 0:
        reader.fail("radius", "a sphere needs a radius in feet")
    if spell.shape in (SpellShape.CONE, SpellShape.LINE) and spell.length <= 0:
        reader.fail("length", f"a {spell.shape.value} needs a length in feet")
    if spell.shape is SpellShape.CUBE and spell.size <= 0:
        reader.fail("size", "a cube needs a size in feet")
    if spell.shape is SpellShape.SINGLE and spell.radius:
        if declared is not None:
            reader.fail(
                "shape",
                f"declares shape 'single' but carries a {spell.radius} ft radius. The "
                f"radius is what decides who is caught, so this would affect an area: "
                f"name an area shape, or drop the radius",
            )
        else:
            reader.warn(
                "shape",
                f"has a {spell.radius} ft radius but does not say what shape it is. It "
                f"resolves as a sphere; declare \"shape\": \"sphere\" so the record "
                f"says what the spell does",
            )


def _parse_condition(
    name: str, record: Mapping[str, Any], diagnostics: list[Diagnostic], source: str
) -> tuple[ConditionEffect, dict[str, Any]] | None:
    reader = _Reader(record, diagnostics, source=source, section="conditions", name=name)
    reader.unknown_keys(_CONDITION_KEYS)
    _common_fields(reader)
    reader.string("description")
    flags: dict[str, bool] = {}
    for flag, value in reader.mapping("effects").items():
        if flag not in EFFECT_FLAGS:
            reader.fail(
                "effects",
                f"{flag!r} is not an effect this engine can apply. A pack may name new "
                f"conditions but not new kinds of effect. Valid flags: "
                f"{', '.join(EFFECT_FLAGS)}",
            )
            continue
        if not isinstance(value, bool):
            reader.fail("effects", f"{flag} must be true or false, got {value!r}")
            continue
        flags[flag] = value
    if not reader.ok:
        return None
    return ConditionEffect(**flags), dict(record)


def _parse_terrain(
    name: str, record: Mapping[str, Any], diagnostics: list[Diagnostic], source: str
) -> tuple[TerrainEffect, dict[str, Any]] | None:
    reader = _Reader(record, diagnostics, source=source, section="terrain", name=name)
    reader.unknown_keys(_TERRAIN_KEYS)
    _common_fields(reader)
    reader.string("description")
    values: dict[str, int | bool] = {}
    for flag, value in reader.mapping("effects").items():
        if flag not in TERRAIN_FLAGS:
            reader.fail(
                "effects",
                f"{flag!r} is not an effect this engine can apply. A pack may name new "
                f"terrain kinds but not new kinds of effect. Valid flags: "
                f"{', '.join(TERRAIN_FLAGS)}",
            )
            continue
        if flag in ("passable", "opaque", "underwater"):
            if not isinstance(value, bool):
                reader.fail("effects", f"{flag} must be true or false, got {value!r}")
                continue
        elif flag == "move_cost_multiplier":
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                reader.fail(
                    "effects",
                    f"{flag} must be a whole number of 1 or more, got {value!r}",
                )
                continue
        else:  # cover
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3:
                reader.fail(
                    "effects",
                    f"{flag} must be a whole number from 0 (none) to 3 (total), "
                    f"got {value!r}",
                )
                continue
        values[flag] = value
    if not reader.ok:
        return None
    # Built field by field rather than by ``**values``: the fields are of mixed
    # types, and spelling them out keeps the construction checkable.
    defaults = TerrainEffect()
    effect = TerrainEffect(
        move_cost_multiplier=int(values.get("move_cost_multiplier",
                                            defaults.move_cost_multiplier)),
        passable=bool(values.get("passable", defaults.passable)),
        opaque=bool(values.get("opaque", defaults.opaque)),
        cover=int(values.get("cover", defaults.cover)),
    )
    return effect, dict(record)


def _parse_item(
    name: str, record: Mapping[str, Any], diagnostics: list[Diagnostic], source: str
) -> tuple[ItemEffect, dict[str, Any]] | None:
    reader = _Reader(record, diagnostics, source=source, section="items", name=name)
    reader.unknown_keys(_ITEM_KEYS)
    provenance = _common_fields(reader)
    description = reader.string("description")
    if record.get("use") is None:
        reader.fail("use", "required; an item is defined by what using it does")
        return None
    use = reader.mapping("use")
    sub = _Reader(use, diagnostics, source=source, section="items", name=name)
    sub.unknown_keys(_USE_KEYS)
    heal = sub.dice("heal")
    damage = sub.dice("damage")
    damage_type = sub.enum("damage_type", DamageType)
    save_ability = sub.enum("save_ability", Ability)
    save_dc = sub.integer("save_dc", minimum=1) if save_ability is not None else 0
    half_on_save = sub.boolean("half_on_save", default=False)
    condition = sub.string("condition") or None
    if not (reader.ok and sub.ok):
        return None
    try:
        effect = ItemEffect(
            heal=heal,
            damage=damage,
            damage_type=damage_type,
            save_ability=save_ability,
            save_dc=save_dc,
            half_on_save=half_on_save,
            condition=condition,
            description=description,
            provenance=provenance,
        )
    except ItemError as error:
        reader.fail("use", str(error))
        return None
    return effect, dict(record)


def _parse_pack(
    payload: Mapping[str, Any],
    diagnostics: list[Diagnostic],
    *,
    label: str,
    level: str,
    path: str,
) -> _ParsedPack | None:
    if not isinstance(payload, dict):
        diagnostics.append(
            Diagnostic(source=label, problem="a pack must be a JSON object")
        )
        return None
    for key in sorted(set(payload) - _PACK_KEYS):
        diagnostics.append(
            Diagnostic(
                source=label, field=key,
                problem=(
                    f"unknown top-level key. Valid keys: {', '.join(sorted(_PACK_KEYS))}"
                ),
            )
        )
    provenance = payload.get("provenance")
    if not isinstance(provenance, str) or not provenance.strip():
        diagnostics.append(
            Diagnostic(
                source=label, field="provenance",
                problem=(
                    "required. Every pack must say where its content came from, so a "
                    "session mixing SRD and original material can always report which "
                    "is which"
                ),
            )
        )
        provenance = ""

    parsed = _ParsedPack(
        info=PackInfo(
            label=label, level=level, path=path,
            pack=str(payload.get("pack", label)),
            version=str(payload.get("version", "")),
            provenance=provenance,
            counts={},
        ),
        creatures={}, spells={}, spell_records={}, condition_effects={},
        condition_records={}, terrain_effects={}, terrain_records={},
        items={}, item_records={},
    )

    for name, record in _named_records(payload, "creatures", diagnostics, label):
        creature = _parse_creature(name, record, diagnostics, label)
        if creature is not None:
            parsed.creatures[name] = creature
    for name, record in _named_records(payload, "spells", diagnostics, label):
        spell = _parse_spell(name, record, diagnostics, label)
        if spell is not None:
            parsed.spells[name] = spell[0]
            parsed.spell_records[name] = spell[1]
    for name, record in _named_records(payload, "conditions", diagnostics, label):
        condition = _parse_condition(name, record, diagnostics, label)
        if condition is not None:
            parsed.condition_effects[name] = condition[0]
            parsed.condition_records[name] = condition[1]
    for name, record in _named_records(payload, "terrain", diagnostics, label):
        terrain = _parse_terrain(name, record, diagnostics, label)
        if terrain is not None:
            parsed.terrain_effects[name] = terrain[0]
            parsed.terrain_records[name] = terrain[1]
    for name, record in _named_records(payload, "items", diagnostics, label):
        item = _parse_item(name, record, diagnostics, label)
        if item is not None:
            parsed.items[name] = item[0]
            parsed.item_records[name] = item[1]

    counts = {section: len(parsed.section(section)) for section in SECTIONS}
    parsed.info = PackInfo(
        label=parsed.info.label, level=parsed.info.level, path=parsed.info.path,
        pack=parsed.info.pack, version=parsed.info.version,
        provenance=parsed.info.provenance, counts=counts,
    )
    return parsed


# --- finding packs on disk -------------------------------------------------
def contained_json_files(
    root: Path, refused: Callable[[Path, str], None] | None = None
) -> list[Path]:
    """Every ``*.json`` under ``root`` that does not escape it, in walk order.

    The containment rule is the point: a directory the caller configured does
    not authorise whatever a symlink inside it happens to point at, so each
    candidate is resolved and checked back against ``root``.

    ``refused`` receives every candidate the rule turns away and why. It is
    optional because the two callers differ deliberately in what they do with a
    refusal, not in the rule itself — the pack loader names each one so an
    author can fix it, while the map listing in :mod:`fivee_sim.service.maps`
    stays silent because a listing's job is to show what is usable. Sharing the
    rule and parameterising the reporting is what keeps the two from drifting.
    """
    found: list[Path] = []
    for directory, subdirectories, filenames in os.walk(root, followlinks=False):
        subdirectories.sort()
        for filename in sorted(filenames):
            if not filename.lower().endswith(".json"):
                continue
            candidate = Path(directory) / filename
            try:
                resolved = candidate.resolve()
            except OSError as error:
                if refused is not None:
                    refused(candidate, f"cannot be resolved: {error}")
                continue
            if not resolved.is_relative_to(root):
                if refused is not None:
                    refused(
                        candidate,
                        f"resolves to {resolved}, outside the content directory that "
                        f"was configured; it is not read",
                    )
                continue
            found.append(resolved)
    return found


def _discover(entry: str | Path, diagnostics: list[Diagnostic]) -> list[Path]:
    """Every ``*.json`` an entry names, refusing anything that escapes it.

    A named file is a declaration by whoever configured it, so it needs no
    containment check. A *directory* does: the caller declared the directory, not
    whatever a symlink inside it happens to point at.
    """
    try:
        root = Path(entry).expanduser().resolve()
    except OSError as error:
        diagnostics.append(Diagnostic(source=str(entry), problem=f"cannot be resolved: {error}"))
        return []
    if not root.exists():
        diagnostics.append(Diagnostic(source=str(root), problem="does not exist"))
        return []
    if root.is_file():
        if root.suffix.lower() != ".json":
            diagnostics.append(
                Diagnostic(source=str(root), problem="content packs must be .json files")
            )
            return []
        return [root]

    return contained_json_files(
        root,
        lambda candidate, problem: diagnostics.append(
            Diagnostic(source=str(candidate), problem=problem)
        ),
    )


def _read_pack(path: Path, diagnostics: list[Diagnostic]) -> Mapping[str, Any] | None:
    try:
        size = path.stat().st_size
    except OSError as error:
        diagnostics.append(Diagnostic(source=str(path), problem=f"cannot be read: {error}"))
        return None
    if size > MAX_PACK_BYTES:
        diagnostics.append(
            Diagnostic(
                source=str(path),
                problem=f"is {size} bytes, over the {MAX_PACK_BYTES} byte limit for a pack",
            )
        )
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        diagnostics.append(Diagnostic(source=str(path), problem=f"cannot be read: {error}"))
        return None
    except UnicodeDecodeError as error:
        diagnostics.append(Diagnostic(source=str(path), problem=f"is not valid UTF-8: {error}"))
        return None
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as error:
        diagnostics.append(
            Diagnostic(source=str(path), problem=f"is not valid JSON: {error}")
        )
        return None
    if not isinstance(payload, dict):
        diagnostics.append(Diagnostic(source=str(path), problem="a pack must be a JSON object"))
        return None
    return payload


def _builtin_condition_payload() -> dict[str, Any]:
    """The SRD condition table, expressed as a pack.

    Conditions are the one built-in category that is code rather than a JSON file,
    and deliberately so: :data:`~fivee_sim.kernel.conditions.EFFECTS` is the default
    table every kernel function falls back to, and the kernel is not allowed to
    perform I/O. Rendering it as a pack here keeps a single parse path — the
    built-in conditions go through exactly the validation a campaign's do, which
    also means a malformed row could never ship unnoticed.
    """
    return {
        "pack": "srd-5.2-conditions",
        "version": "1.0",
        "provenance": "SRD 5.2",
        "note": (
            "Rendered from the engine's own condition table so it validates and "
            "merges like any other pack."
        ),
        "conditions": [
            {
                "name": str(name),
                "provenance": "SRD 5.2",
                "effects": {
                    flag: True for flag in EFFECT_FLAGS if getattr(effect, flag)
                },
            }
            for name, effect in EFFECTS.items()
        ],
    }


def _builtin_terrain_payload() -> dict[str, Any]:
    """The engine's terrain table, expressed as a pack.

    The same forced arrangement as the conditions: :data:`~fivee_sim.kernel.grid.TERRAIN`
    is the default table the grid functions fall back to, and the kernel is not
    allowed to perform I/O, so the table lives in code and is rendered as a pack
    here to keep the single parse path. Unlike the conditions, the kinds
    themselves are engine policy rather than SRD records — the SRD defines
    difficult terrain, cover, and vision, but no tile vocabulary — and the
    provenance says so.
    """
    defaults = TerrainEffect()
    return {
        "pack": "engine-terrain",
        "version": "1.0",
        "provenance": (
            "Engine policy; movement, cover, and vision semantics follow SRD 5.2"
        ),
        "note": (
            "Rendered from the engine's own terrain table so it validates and "
            "merges like any other pack."
        ),
        "terrain": [
            {
                "name": name,
                "provenance": "engine policy",
                "effects": {
                    flag: getattr(effect, flag)
                    for flag in TERRAIN_FLAGS
                    if getattr(effect, flag) != getattr(defaults, flag)
                },
            }
            for name, effect in TERRAIN.items()
        ],
    }


def _builtin_payloads(diagnostics: list[Diagnostic]) -> list[tuple[str, Mapping[str, Any]]]:
    out: list[tuple[str, Mapping[str, Any]]] = [
        ("bundled:conditions", _builtin_condition_payload()),
        ("bundled:terrain", _builtin_terrain_payload()),
    ]
    for filename in BUILTIN_FILES:
        label = f"bundled:{filename}"
        text = resources.files(_BUILTIN_PACKAGE).joinpath(filename).read_text(encoding="utf-8")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:  # pragma: no cover - a shipping bug
            diagnostics.append(Diagnostic(source=label, problem=f"is not valid JSON: {error}"))
            continue
        out.append((label, payload))
    return out


def environment_paths(env: Mapping[str, str] | None = None) -> list[str]:
    """Paths the environment asks for, in precedence order.

    ``FIVEE_SIM_CONTENT`` wins outright when set. Only when it is unset does the
    project directory apply, so a developer exporting the variable is not silently
    also loading whatever sits in the repo they happen to be in.
    """
    environ = os.environ if env is None else env
    configured = environ.get(CONTENT_ENV, "").strip()
    if configured:
        return [part for part in configured.split(os.pathsep) if part.strip()]
    project = (
        environ.get(PROJECT_ENV, "").strip()
        or environ.get(CLAUDE_PROJECT_ENV, "").strip()
    )
    if project:
        candidate = Path(project) / PROJECT_SUBDIR
        if candidate.is_dir():
            return [str(candidate)]
    return []


def builtin_mode(env: Mapping[str, str] | None = None) -> BuiltinMode:
    environ = os.environ if env is None else env
    raw = environ.get(BUILTIN_ENV, "").strip().casefold()
    if not raw:
        return BuiltinMode.INCLUDE
    try:
        return BuiltinMode(raw)
    except ValueError:
        allowed = ", ".join(mode.value for mode in BuiltinMode)
        raise ContentError(
            [
                Diagnostic(
                    source=BUILTIN_ENV,
                    problem=f"must be one of: {allowed}; got {raw!r}",
                )
            ]
        ) from None


# --- merging ---------------------------------------------------------------
def _merge_section(
    section: str,
    levels: Sequence[tuple[str, Sequence[_ParsedPack]]],
    diagnostics: list[Diagnostic],
) -> dict[str, str]:
    """Decide which pack owns each name in ``section``. Returns name -> pack label.

    Within a level, order is only path-sorted, so two packs both claiming a name are
    ambiguous and refused even when both say ``overrides``. Across levels the later
    level wins, because that ordering is declared rather than incidental.
    """
    owner: dict[str, str] = {}
    owner_level: dict[str, str] = {}
    for level, packs in levels:
        claimed_here: dict[str, str] = {}
        for pack in packs:
            for name, record in pack.section(section).items():
                overrides = bool(record.get("overrides", False))
                previous = owner.get(name)
                if name in claimed_here:
                    diagnostics.append(
                        Diagnostic(
                            source=pack.info.label, section=section, record=name,
                            problem=(
                                f"is also defined by {claimed_here[name]}. Packs at the "
                                f"same level load in path order, so which one wins would "
                                f"be arbitrary — rename one, or load them at different "
                                f"levels"
                            ),
                        )
                    )
                    continue
                if previous is not None and not overrides:
                    diagnostics.append(
                        Diagnostic(
                            source=pack.info.label, section=section, record=name,
                            problem=(
                                f"is already defined by {previous} (loaded as "
                                f"{owner_level[name]}). Set \"overrides\": true on this "
                                f"record to replace it deliberately, or rename it"
                            ),
                        )
                    )
                    continue
                if previous is None and overrides:
                    diagnostics.append(
                        Diagnostic(
                            source=pack.info.label, section=section, record=name,
                            field="overrides",
                            problem=(
                                "declares an override but nothing of that name is loaded; "
                                "it registers as a new entry. Check for a typo, or drop "
                                "the flag"
                            ),
                            severity=Severity.WARNING,
                        )
                    )
                claimed_here[name] = pack.info.label
                owner[name] = pack.info.label
                owner_level[name] = level
    return owner


def _cross_reference(
    registry_conditions: Mapping[str, ConditionEffect],
    spells: Mapping[str, Spell],
    items: Mapping[str, ItemEffect],
    creatures: Mapping[str, dict[str, Any]],
    sources: Mapping[tuple[str, str], str],
    diagnostics: list[Diagnostic],
) -> None:
    """Check references that only the merged set can answer.

    A spell naming ``condition: "vale-cursed"`` is valid exactly when some pack in
    the merged set defines it, which no per-file validator can know.
    """
    known = sorted(registry_conditions)
    available = ", ".join(known) or "none"

    def check(section: str, owner: str, field: str, condition: str | None) -> None:
        if condition is None or condition in registry_conditions:
            return
        diagnostics.append(
            Diagnostic(
                source=sources.get((section, owner), "unknown"),
                section=section, record=owner, field=field,
                problem=(
                    f"applies condition {condition!r}, which no loaded pack defines. "
                    f"Available: {available}"
                ),
            )
        )

    for name, spell in spells.items():
        check("spells", name, "condition", spell.condition)

    for name, effect in items.items():
        check("items", name, "use.condition", effect.condition)
    for name, record in creatures.items():
        for condition in record.get("conditions", []) or []:
            check("creatures", name, "conditions", str(condition))
        for index, attack in enumerate(record.get("attacks", []) or []):
            if not isinstance(attack, dict):
                continue
            rider = attack.get("on_hit_condition")
            if rider is not None:
                check(
                    "creatures", name,
                    f"attacks[{index}].on_hit_condition", str(rider),
                )
        # Warnings, not errors: the encounter refuses these at use time with a clear
        # reason rather than crashing, so a pack meant to be combined with another is
        # still loadable. It is worth saying now, though — the alternative is finding
        # out mid-fight.
        for referenced, table, field_name in (
            (record.get("spells", []) or [], spells, "spells"),
            (record.get("items", {}) or {}, items, "items"),
        ):
            for entry in referenced:
                if str(entry) in table:
                    continue
                diagnostics.append(
                    Diagnostic(
                        source=sources.get(("creatures", name), "unknown"),
                        section="creatures", record=name, field=field_name,
                        problem=(
                            f"refers to {str(entry)!r}, which no loaded pack defines; "
                            f"the engine will refuse it when the creature tries to use it"
                        ),
                        severity=Severity.WARNING,
                    )
                )


def _build(
    configured: Sequence[str | Path] = (),
    *,
    builtin: BuiltinMode = BuiltinMode.INCLUDE,
    env: Mapping[str, str] | None = None,
    include_environment: bool = True,
) -> tuple[ContentRegistry | None, list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    levels: list[tuple[str, list[_ParsedPack]]] = []

    def parse_level(name: str, entries: Iterable[tuple[str, Mapping[str, Any], str]]) -> None:
        packs: list[_ParsedPack] = []
        for label, payload, path in entries:
            parsed = _parse_pack(payload, diagnostics, label=label, level=name, path=path)
            if parsed is not None:
                packs.append(parsed)
        if packs:
            levels.append((name, packs))

    if builtin is BuiltinMode.INCLUDE:
        parse_level(
            "builtin",
            [(label, payload, "") for label, payload in _builtin_payloads(diagnostics)],
        )

    # One file is one pack however many times it is named. Without this, validating a
    # pack that the environment already loads — or configuring a path that is also in
    # FIVEE_SIM_CONTENT — would report every one of its names as colliding with
    # itself, which is a spurious failure with a baffling message.
    seen: set[Path] = set()

    def from_paths(name: str, entries: Sequence[str | Path]) -> None:
        collected: list[tuple[str, Mapping[str, Any], str]] = []
        for entry in entries:
            for path in _discover(entry, diagnostics):
                if path in seen:
                    continue
                seen.add(path)
                payload = _read_pack(path, diagnostics)
                if payload is not None:
                    collected.append((str(path), payload, str(path)))
        parse_level(name, collected)

    if include_environment:
        from_paths("environment", environment_paths(env))
    from_paths("configured", list(configured))

    creatures: dict[str, dict[str, Any]] = {}
    spells: dict[str, Spell] = {}
    spell_records: dict[str, dict[str, Any]] = {}
    condition_effects: dict[str, ConditionEffect] = {}
    condition_records: dict[str, dict[str, Any]] = {}
    terrain_effects: dict[str, TerrainEffect] = {}
    terrain_records: dict[str, dict[str, Any]] = {}
    items: dict[str, ItemEffect] = {}
    item_records: dict[str, dict[str, Any]] = {}
    sources: dict[tuple[str, str], str] = {}

    ownership = {
        section: _merge_section(section, levels, diagnostics) for section in SECTIONS
    }
    for _level, packs in levels:
        for pack in packs:
            for section in SECTIONS:
                owners = ownership[section]
                for name in pack.section(section):
                    if owners.get(name) != pack.info.label:
                        continue
                    sources[(section, name)] = pack.info.label
                    if section == "creatures":
                        creatures[name] = pack.creatures[name]
                    elif section == "spells":
                        spells[name] = pack.spells[name]
                        spell_records[name] = pack.spell_records[name]
                    elif section == "conditions":
                        condition_effects[name] = pack.condition_effects[name]
                        condition_records[name] = pack.condition_records[name]
                    elif section == "terrain":
                        terrain_effects[name] = pack.terrain_effects[name]
                        terrain_records[name] = pack.terrain_records[name]
                    else:
                        items[name] = pack.items[name]
                        item_records[name] = pack.item_records[name]

    retained: list[str] = []
    for condition in STRUCTURAL_CONDITIONS:
        name = str(condition)
        if name in condition_effects:
            continue
        condition_effects[name] = EFFECTS[condition]
        # A record too, not just an effect. ``names()`` and the counts read the
        # records, so without this the catalogue would omit a condition the stepper
        # goes on to apply — leaving ``lookup_rule`` and ``encounter_state``
        # disagreeing, which is the exact failure the engine exists to prevent.
        condition_records[name] = {
            "name": name,
            "provenance": "SRD 5.2",
            "description": (
                "Retained by the engine: the stepper applies this itself when a "
                "creature drops to 0 hit points."
            ),
            "unmodelled": [],
        }
        retained.append(name)

    _cross_reference(condition_effects, spells, items, creatures, sources, diagnostics)

    errors = [d for d in diagnostics if d.severity is Severity.ERROR]
    if errors:
        return None, diagnostics
    registry = ContentRegistry(
        builtin=builtin,
        creatures=creatures,
        spells=spells,
        spell_records=spell_records,
        condition_effects=condition_effects,
        condition_records=condition_records,
        terrain_effects=terrain_effects,
        terrain_records=terrain_records,
        items=items,
        item_records=item_records,
        packs=tuple(pack.info for _level, packs in levels for pack in packs),
        sources=sources,
        warnings=tuple(d for d in diagnostics if d.severity is Severity.WARNING),
        retained_conditions=tuple(retained),
    )
    return registry, diagnostics


def load_packs(
    configured: Sequence[str | Path] = (),
    *,
    builtin: BuiltinMode | str = BuiltinMode.INCLUDE,
    env: Mapping[str, str] | None = None,
    include_environment: bool = True,
) -> ContentRegistry:
    """Load content and return the merged registry, or raise with every diagnostic."""
    mode = BuiltinMode(builtin)
    registry, diagnostics = _build(
        configured, builtin=mode, env=env, include_environment=include_environment
    )
    if registry is None:
        raise ContentError(diagnostics)
    return registry


def validate(
    configured: Sequence[str | Path] = (),
    *,
    builtin: BuiltinMode | str = BuiltinMode.INCLUDE,
    env: Mapping[str, str] | None = None,
    include_environment: bool = True,
) -> list[Diagnostic]:
    """Every problem with the given packs, without loading them. The authoring aid."""
    _registry, diagnostics = _build(
        configured, builtin=BuiltinMode(builtin), env=env,
        include_environment=include_environment,
    )
    return diagnostics


def registry_from_snapshot(snapshot: Mapping[str, Any]) -> ContentRegistry:
    """Rebuild the rules tables embedded in a durable encounter snapshot.

    Combatants are normalized separately by the journal; this snapshot owns
    the spell, condition, terrain, and item records they may use while the
    encounter continues. It goes through the ordinary pack parser so captured
    data is held to the same strict contract as a file loaded today.
    """
    diagnostics: list[Diagnostic] = []
    raw_records = snapshot.get("records")
    if not isinstance(raw_records, Mapping):
        raise ContentError(
            [Diagnostic(source="encounter snapshot", problem="records must be an object")]
        )
    payload: dict[str, Any] = {
        "pack": "captured-encounter-content",
        "version": "1",
        "provenance": "Captured by fivee-sim when the encounter was created",
    }
    sources: dict[tuple[str, str], str] = {}
    for section in ("spells", "conditions", "terrain", "items"):
        section_entries = raw_records.get(section, {})
        if not isinstance(section_entries, Mapping):
            diagnostics.append(
                Diagnostic(
                    source="encounter snapshot",
                    section=section,
                    problem="must be an object",
                )
            )
            continue
        rows = []
        for name, wrapped in section_entries.items():
            if not isinstance(wrapped, Mapping) or not isinstance(
                wrapped.get("record"), Mapping
            ):
                diagnostics.append(
                    Diagnostic(
                        source="encounter snapshot",
                        section=section,
                        record=str(name),
                        problem="must carry a record object",
                    )
                )
                continue
            rows.append(dict(wrapped["record"]))
            sources[(section, str(name))] = str(wrapped.get("source", "snapshot"))
        payload[section] = rows
    parsed = _parse_pack(
        payload,
        diagnostics,
        label="encounter snapshot",
        level="snapshot",
        path="",
    )
    errors = [item for item in diagnostics if item.severity is Severity.ERROR]
    if parsed is None or errors:
        raise ContentError(diagnostics)
    try:
        mode = BuiltinMode(snapshot.get("builtin", BuiltinMode.EXCLUDE))
    except ValueError:
        mode = BuiltinMode.EXCLUDE
    return ContentRegistry(
        builtin=mode,
        spells=parsed.spells,
        spell_records=parsed.spell_records,
        condition_effects=parsed.condition_effects,
        condition_records=parsed.condition_records,
        terrain_effects=parsed.terrain_effects,
        terrain_records=parsed.terrain_records,
        items=parsed.items,
        item_records=parsed.item_records,
        packs=(parsed.info,),
        sources=sources,
        warnings=tuple(item for item in diagnostics if item.severity is Severity.WARNING),
        retained_conditions=tuple(str(item) for item in snapshot.get("retained_conditions", [])),
    )


def builtin_registry() -> ContentRegistry:
    """The bundled SRD slice alone, with nothing from the environment."""
    return load_packs(builtin=BuiltinMode.INCLUDE, include_environment=False)


def registry_from_environment() -> ContentRegistry:
    """The registry a freshly started server should use."""
    return load_packs(builtin=builtin_mode())


# --- from a name to a combatant --------------------------------------------
class DataError(ValueError):
    """A creature was asked for that the active content does not define."""


def make_creature(
    name: str,
    *,
    registry: ContentRegistry | None = None,
    label: str | None = None,
    team: str | None = None,
    position: Point | int = 0,
    level: int = 0,
) -> Creature:
    """Look ``name`` up in the active content and build it.

    Defaults to the bundled slice, so a caller with no opinion about content
    still gets something. The refusal lists what *is* loaded and whether the
    built-ins were excluded, because "no such creature" with a configurable
    catalogue is not an answer anyone can act on.
    """
    active = builtin() if registry is None else registry
    record = active.creatures.get(name)
    if record is None:
        available = ", ".join(sorted(active.creatures)) or "none"
        raise DataError(
            f"no stat block named {name!r} in the loaded content "
            f"(built-in content is {active.builtin.value}d); available: {available}"
        )
    return Creature.from_record(
        record,
        condition_effects=active.condition_effects,
        source=active.source_of("creatures", name),
        label=label,
        team=team,
        position=position,
        level=level,
    )


# --- built-in-only accessors -----------------------------------------------
# Cached, and only ever over the bundled slice. See the module docstring: a
# cached view of *loaded* content would go stale the moment a pack is
# reconfigured, so anything reflecting that takes a registry instead.
@lru_cache(maxsize=1)
def builtin() -> ContentRegistry:
    """The bundled SRD slice alone. Safe to cache: it ships inside the plugin."""
    return builtin_registry()


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
    position: Point | int = 0,
    level: int = 0,
) -> Creature:
    """Build a creature from a *bundled* stat block. See :func:`make_creature`."""
    return make_creature(
        name, registry=builtin(), label=label, team=team, position=position, level=level
    )
