"""Content packs: the engine's rules data, whether we shipped it or you wrote it.

A pack is one JSON file holding executable records, catalog identities, and
printed tables. The bundled SRD slice under ``data/srd/`` is not a special case —
its packs are parsed by the code below like any other. That is what
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
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .catalog import (
    CatalogCell,
    CatalogColumn,
    CatalogRecord,
    CatalogRow,
    CatalogTable,
    CatalogValueType,
    ContentRef,
    FactStatus,
    freeze_json,
)
from .kernel.actions import AttackKind, RiderExpiry
from .kernel.conditions import EFFECT_FLAGS, EFFECTS, Condition, ConditionEffect, ConditionTable
from .kernel.grid import TERRAIN, TERRAIN_FLAGS, Point, TerrainEffect
from .kernel.items import ActionCost, ItemEffect, ItemError
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
CATALOG_CHAPTERS = {
    1: "legal-information",
    2: "contents",
    3: "index-of-stat-blocks",
    4: "playing-the-game",
    5: "character-creation",
    6: "classes",
    7: "character-origins",
    8: "feats",
    9: "equipment",
    10: "spells",
    11: "rules-glossary",
    12: "gameplay-toolbox",
    13: "magic-items",
    14: "monsters",
    15: "monsters-a-z",
    16: "animals",
}
CATALOG_FILES = tuple(
    f"catalog-{chapter:02d}-{slug}.json"
    for chapter, slug in CATALOG_CHAPTERS.items()
)
BUILTIN_FILES = CATALOG_FILES

#: A pack larger than this fails cleanly instead of stalling session start. Packs
#: are hand-authored rules data; the whole SRD would not approach this.
MAX_PACK_BYTES = 4 * 1024 * 1024

#: Conditions the stepper applies on its own behalf when a creature drops to 0 hit
#: points. They survive ``exclude`` mode because dropping is engine machinery, not
#: content — a pack may still override them, and ``content_status`` reports when
#: they were retained rather than loaded.
STRUCTURAL_CONDITIONS = (Condition.UNCONSCIOUS, Condition.PRONE)

SECTIONS = ("creatures", "spells", "conditions", "terrain", "items")
CATALOG_SECTIONS = ("catalog", "catalog_tables")
MERGE_SECTIONS = (*SECTIONS, *CATALOG_SECTIONS)

_PACK_KEYS = frozenset(
    {"pack", "version", "provenance", "attribution", "note", *MERGE_SECTIONS}
)
_COMMON_RECORD_KEYS = frozenset(
    {"name", "provenance", "unmodelled", "unmodelled_facts", "overrides"}
)
_CREATURE_KEYS = _COMMON_RECORD_KEYS | {
    # "hit_dice" is accepted and validated below but no rule consumes it: the
    # engine rolls no hit points and models no rest, so there is nothing for a
    # transcribed die expression to feed. It stays in the allowlist because the
    # value is a faithful part of the SRD stat block and re-deriving it later
    # would be wasted work, not because anything reads it.
    "team", "ac", "max_hp", "hit_dice", "speed", "climb_speed", "swim_speed",
    "fly_speed", "terrain_cost_overrides", "darkvision", "blindsight", "death_rule",
    "size", "abilities", "save_bonuses",
    "attacks", "attacks_per_action", "bonus_actions", "surrender_when_last",
    "redirect_attack",
    "spells", "spell_slots", "spell_save_dc", "spellcasting_ability",
    "spell_attack_bonus", "items", "conditions", "immunities", "resistances",
    "vulnerabilities", "pack_tactics", "undead_fortitude",
}
_ATTACK_KEYS = frozenset({
    "name", "attack_bonus", "damage", "damage_type", "kind", "reach", "normal_range",
    "long_range", "bonus_damage", "bonus_damage_type", "advantage_bonus_damage",
    "advantage_bonus_with_adjacent_ally",
    "on_hit_condition", "on_hit_save_ability", "on_hit_save_dc", "on_hit_expiry",
    "on_hit_max_size", "on_hit_attach", "attached_damage", "attached_damage_type",
    "detach_after_damage", "ammunition", "loading", "thrown", "provenance",
})
_SPELL_KEYS = _COMMON_RECORD_KEYS | {
    "level", "school", "requires_attack_roll", "attack_kind", "save_ability", "damage",
    "damage_type", "heal",
    "half_on_save", "upcast_damage", "upcast_heal", "add_spellcasting_modifier",
    "shape", "radius", "length",
    "size", "width",
    "range_feet", "max_targets", "condition", "concentration", "action_cost",
}
_CONDITION_KEYS = _COMMON_RECORD_KEYS | {"effects", "description"}
_TERRAIN_KEYS = _COMMON_RECORD_KEYS | {"effects", "description"}
_ITEM_KEYS = _COMMON_RECORD_KEYS | {"use", "description"}
_USE_KEYS = frozenset({
    "heal", "damage", "damage_type", "save_ability", "save_dc", "half_on_save",
    "condition", "action_cost",
})
_CATALOG_KEYS = frozenset({
    "id", "kind", "name", "source_ids", "chapter_id", "parent_id", "pages",
    "fact_status", "facts", "aliases", "content_ref", "unmodelled_facts",
    "provenance", "overrides",
})
_CATALOG_TABLE_KEYS = frozenset({
    "id", "name", "section_id", "page", "fact_status", "columns", "rows",
    "source_row_count", "omissions", "provenance", "overrides",
})
_CATALOG_COLUMN_KEYS = frozenset({"id", "name", "type"})
_CATALOG_ROW_KEYS = frozenset({"cells"})
_CATALOG_CELL_KEYS = frozenset({"value", "numeric_value", "omission_code"})
_OMISSION_KEYS = frozenset({"code", "feature", "source_ids", "row", "column"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
MAX_CATALOG_RECORD_BYTES = 48 * 1024
MAX_ATOMIC_FACT_CHARS = 512


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
    catalog: Mapping[str, CatalogRecord] = field(
        default_factory=lambda: MappingProxyType({})
    )
    catalog_tables: Mapping[str, CatalogTable] = field(
        default_factory=lambda: MappingProxyType({})
    )
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
        progress = {status.value: 0 for status in FactStatus}
        table_progress = {status.value: 0 for status in FactStatus}
        kinds: dict[str, int] = {}
        for record in self.catalog.values():
            progress[record.fact_status.value] += 1
            kinds[record.kind] = kinds.get(record.kind, 0) + 1
        for table in self.catalog_tables.values():
            table_progress[table.fact_status.value] += 1
        return {
            "builtin": self.builtin.value,
            "counts": {
                section: len(self.records_for(section)) for section in SECTIONS
            },
            "catalog": {
                "records": len(self.catalog),
                "tables": len(self.catalog_tables),
                "kinds": dict(sorted(kinds.items())),
                "progress": {"sections": progress, "tables": table_progress},
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
    catalog: dict[str, CatalogRecord]
    catalog_records: dict[str, dict[str, Any]]
    catalog_tables: dict[str, CatalogTable]
    catalog_table_records: dict[str, dict[str, Any]]

    def section(self, name: str) -> Mapping[str, Any]:
        return {
            "creatures": self.creatures,
            "spells": self.spell_records,
            "conditions": self.condition_records,
            "terrain": self.terrain_records,
            "items": self.item_records,
            "catalog": self.catalog_records,
            "catalog_tables": self.catalog_table_records,
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


def _identified_records(
    payload: Mapping[str, Any],
    section: str,
    diagnostics: list[Diagnostic],
    source: str,
) -> list[tuple[str, Mapping[str, Any]]]:
    """Pull an ID-keyed catalog section from a pack with duplicate diagnostics."""
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
                    source=source,
                    section=section,
                    record=f"#{index}",
                    problem=f"must be an object, got {type(entry).__name__}",
                )
            )
            continue
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not identifier.strip():
            diagnostics.append(
                Diagnostic(
                    source=source,
                    section=section,
                    record=f"#{index}",
                    field="id",
                    problem="required, and must be non-empty text",
                )
            )
            continue
        if identifier in seen:
            diagnostics.append(
                Diagnostic(
                    source=source,
                    section=section,
                    record=identifier,
                    problem="defined twice in the same pack",
                )
            )
            continue
        seen.add(identifier)
        out.append((identifier, entry))
    return out


def _validate_json_facts(reader: _Reader, field_name: str, value: Any) -> bool:
    """Validate bounded JSON facts recursively; prose-shaped field names are refused."""
    ok = True
    prose_keys = {"body", "description", "flavor", "rules", "text"}

    def walk(child: Any, path: str) -> None:
        nonlocal ok
        if child is None or isinstance(child, bool | int | float):
            return
        if isinstance(child, str):
            if len(child) > MAX_ATOMIC_FACT_CHARS:
                reader.fail(field_name, f"{path} exceeds {MAX_ATOMIC_FACT_CHARS} characters")
                ok = False
            return
        if isinstance(child, list):
            for index, item in enumerate(child):
                walk(item, f"{path}[{index}]")
            return
        if isinstance(child, dict):
            for key, item in child.items():
                if not isinstance(key, str) or not key:
                    reader.fail(field_name, f"{path} keys must be non-empty text")
                    ok = False
                    continue
                if key.casefold() in prose_keys:
                    reader.fail(
                        field_name,
                        f"{path}.{key} is a prose field; catalog facts are structured only",
                    )
                    ok = False
                walk(item, f"{path}.{key}")
            return
        reader.fail(field_name, f"{path} is not a JSON scalar, list, or object")
        ok = False

    walk(value, field_name)
    return ok


def _structured_omissions(
    reader: _Reader, key: str = "unmodelled_facts"
) -> tuple[Mapping[str, Any], ...]:
    """Validate machine-readable omission codes used by execution and catalog rows."""
    out: list[Mapping[str, Any]] = []
    for index, value in enumerate(reader.sequence(key)):
        if not isinstance(value, dict):
            reader.fail(key, f"entry #{index} must be an object")
            continue
        unknown = sorted(set(value) - _OMISSION_KEYS)
        if unknown:
            reader.fail(key, f"entry #{index} has unknown keys: {', '.join(unknown)}")
            continue
        code = value.get("code")
        if not isinstance(code, str) or not _IDENTIFIER.fullmatch(code):
            reader.fail(key, f"entry #{index}.code must be a stable identifier")
            continue
        feature = value.get("feature")
        if feature is not None and (
            not isinstance(feature, str) or not feature or len(feature) > MAX_ATOMIC_FACT_CHARS
        ):
            reader.fail(
                key,
                f"entry #{index}.feature must be non-empty text of at most "
                f"{MAX_ATOMIC_FACT_CHARS} characters",
            )
            continue
        source_ids = value.get("source_ids", [])
        if not isinstance(source_ids, list) or not all(
            isinstance(item, str) and item for item in source_ids
        ):
            reader.fail(key, f"entry #{index}.source_ids must be a list of identifiers")
            continue
        row = value.get("row")
        if row is not None and (isinstance(row, bool) or not isinstance(row, int) or row < 0):
            reader.fail(key, f"entry #{index}.row must be a non-negative whole number")
            continue
        column = value.get("column")
        if column is not None and (not isinstance(column, str) or not column):
            reader.fail(key, f"entry #{index}.column must be non-empty text")
            continue
        out.append(freeze_json(dict(value)))
    return tuple(out)


def _common_fields(reader: _Reader) -> str:
    """The fields every record carries, checked identically in every section.

    Shared rather than repeated because it was repeated and one section quietly did
    not: creatures went without a ``provenance`` check, which is the one rule the
    licence boundary actually turns on.
    """
    provenance = reader.string("provenance", required=True)
    reader.string_list("unmodelled")
    _structured_omissions(reader)
    reader.boolean("overrides")
    return provenance


def _parse_catalog_record(
    identifier: str,
    record: Mapping[str, Any],
    diagnostics: list[Diagnostic],
    source: str,
) -> CatalogRecord | None:
    reader = _Reader(record, diagnostics, source=source, section="catalog", name=identifier)
    reader.unknown_keys(_CATALOG_KEYS)
    if not _IDENTIFIER.fullmatch(identifier):
        reader.fail("id", "must be a stable identifier using letters, numbers, '.', ':', '_', '-'")
    kind = reader.string("kind", required=True)
    name = reader.string("name", required=True)
    provenance = reader.string("provenance", required=True)
    source_ids = reader.string_list("source_ids")
    if not source_ids:
        reader.fail("source_ids", "must contain at least one source identifier")
    chapter_id = reader.string("chapter_id")
    parent_id = reader.string("parent_id")
    aliases = tuple(reader.string_list("aliases"))
    pages_raw = reader.sequence("pages")
    pages: list[int] = []
    for page in pages_raw:
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            reader.fail("pages", f"entries must be positive page numbers, got {page!r}")
        else:
            pages.append(page)
    if not pages:
        reader.fail("pages", "must contain at least one printed page")
    fact_status = reader.enum("fact_status", FactStatus)
    facts = reader.mapping("facts")
    _validate_json_facts(reader, "facts", facts)
    omissions = _structured_omissions(reader)
    reader.boolean("overrides")

    content_ref: ContentRef | None = None
    if record.get("content_ref") is not None:
        raw_ref = reader.mapping("content_ref")
        unknown = sorted(set(raw_ref) - {"section", "name"})
        if unknown:
            reader.fail("content_ref", f"unknown keys: {', '.join(unknown)}")
        section = raw_ref.get("section")
        content_name = raw_ref.get("name")
        if section not in SECTIONS:
            reader.fail("content_ref", f"section must be one of: {', '.join(SECTIONS)}")
        elif not isinstance(content_name, str) or not content_name:
            reader.fail("content_ref", "name must be non-empty text")
        else:
            content_ref = ContentRef(section=str(section), name=content_name)

    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_CATALOG_RECORD_BYTES:
        reader.fail(
            "id",
            f"record is {len(encoded)} bytes, over the {MAX_CATALOG_RECORD_BYTES} byte limit",
        )
    if not reader.ok or fact_status is None:
        return None
    return CatalogRecord(
        id=identifier,
        kind=kind,
        name=name,
        source_ids=tuple(source_ids),
        chapter_id=chapter_id,
        parent_id=parent_id,
        pages=tuple(pages),
        fact_status=fact_status,
        facts=freeze_json(dict(facts)),
        aliases=aliases,
        content_ref=content_ref,
        unmodelled_facts=omissions,
        provenance=provenance,
    )


def _parse_catalog_table(
    identifier: str,
    record: Mapping[str, Any],
    diagnostics: list[Diagnostic],
    source: str,
) -> CatalogTable | None:
    reader = _Reader(
        record, diagnostics, source=source, section="catalog_tables", name=identifier
    )
    reader.unknown_keys(_CATALOG_TABLE_KEYS)
    if not _IDENTIFIER.fullmatch(identifier):
        reader.fail("id", "must be a stable identifier")
    name = reader.string("name", required=True)
    section_id = reader.string("section_id", required=True)
    page = reader.integer("page", required=True, minimum=1)
    fact_status = reader.enum("fact_status", FactStatus)
    provenance = reader.string("provenance", required=True)
    reader.boolean("overrides")

    columns: list[CatalogColumn] = []
    column_ids: set[str] = set()
    for index, raw_column in enumerate(reader.sequence("columns")):
        if not isinstance(raw_column, dict):
            reader.fail("columns", f"column #{index} must be an object")
            continue
        unknown = sorted(set(raw_column) - _CATALOG_COLUMN_KEYS)
        if unknown:
            reader.fail("columns", f"column #{index} has unknown keys: {', '.join(unknown)}")
            continue
        column_id = raw_column.get("id")
        column_name = raw_column.get("name")
        raw_type = raw_column.get("type")
        if not isinstance(column_id, str) or not _IDENTIFIER.fullmatch(column_id):
            reader.fail("columns", f"column #{index}.id must be a stable identifier")
            continue
        if column_id in column_ids:
            reader.fail("columns", f"column id {column_id!r} is defined twice")
            continue
        if not isinstance(column_name, str) or not column_name:
            reader.fail("columns", f"column #{index}.name must be non-empty text")
            continue
        try:
            column_type = CatalogValueType(str(raw_type))
        except (TypeError, ValueError):
            reader.fail(
                "columns",
                f"column #{index}.type must be one of: "
                f"{', '.join(member.value for member in CatalogValueType)}",
            )
            continue
        column_ids.add(column_id)
        columns.append(CatalogColumn(id=column_id, name=column_name, type=column_type))
    if not columns:
        reader.fail("columns", "must contain at least one typed column")

    rows: list[CatalogRow] = []
    for row_index, raw_row in enumerate(reader.sequence("rows")):
        if not isinstance(raw_row, dict):
            reader.fail("rows", f"row #{row_index} must be an object")
            continue
        unknown = sorted(set(raw_row) - _CATALOG_ROW_KEYS)
        if unknown:
            reader.fail("rows", f"row #{row_index} has unknown keys: {', '.join(unknown)}")
            continue
        raw_cells = raw_row.get("cells")
        if not isinstance(raw_cells, list):
            reader.fail("rows", f"row #{row_index}.cells must be a list")
            continue
        if len(raw_cells) != len(columns):
            reader.fail(
                "rows",
                f"row #{row_index} has {len(raw_cells)} cells for {len(columns)} columns",
            )
            continue
        cells: list[CatalogCell] = []
        for cell_index, raw_cell in enumerate(raw_cells):
            if not isinstance(raw_cell, dict):
                reader.fail("rows", f"row #{row_index} cell #{cell_index} must be an object")
                continue
            unknown = sorted(set(raw_cell) - _CATALOG_CELL_KEYS)
            if unknown:
                reader.fail(
                    "rows",
                    f"row #{row_index} cell #{cell_index} has unknown keys: "
                    f"{', '.join(unknown)}",
                )
                continue
            value = raw_cell.get("value")
            if not (
                value is None
                or isinstance(value, str | int | float | bool)
            ):
                reader.fail("rows", f"row #{row_index} cell #{cell_index} is not scalar")
                continue
            if isinstance(value, str) and len(value) > MAX_ATOMIC_FACT_CHARS:
                reader.fail(
                    "rows",
                    f"row #{row_index} cell #{cell_index} exceeds "
                    f"{MAX_ATOMIC_FACT_CHARS} characters",
                )
                continue
            numeric = raw_cell.get("numeric_value")
            if numeric is not None and (
                isinstance(numeric, bool) or not isinstance(numeric, int | float)
            ):
                reader.fail(
                    "rows", f"row #{row_index} cell #{cell_index}.numeric_value must be numeric"
                )
                continue
            omission_code = raw_cell.get("omission_code", "")
            if not isinstance(omission_code, str) or (
                omission_code and not _IDENTIFIER.fullmatch(omission_code)
            ):
                reader.fail(
                    "rows",
                    f"row #{row_index} cell #{cell_index}.omission_code must be an identifier",
                )
                continue
            column_type = columns[cell_index].type
            matches_type = {
                CatalogValueType.STRING: isinstance(value, str),
                CatalogValueType.INTEGER: isinstance(value, int)
                and not isinstance(value, bool),
                CatalogValueType.NUMBER: isinstance(value, int | float)
                and not isinstance(value, bool),
                CatalogValueType.BOOLEAN: isinstance(value, bool),
            }[column_type]
            if value is None and not omission_code:
                reader.fail(
                    "rows",
                    f"row #{row_index} cell #{cell_index} may be null only with an "
                    "omission_code",
                )
                continue
            if value is not None and not matches_type:
                reader.fail(
                    "rows",
                    f"row #{row_index} cell #{cell_index} does not match "
                    f"{column_type.value} column {columns[cell_index].id!r}",
                )
                continue
            cells.append(
                CatalogCell(
                    value=value,
                    numeric_value=numeric,
                    omission_code=omission_code,
                )
            )
        if len(cells) == len(columns):
            rows.append(CatalogRow(cells=tuple(cells)))

    source_row_count = reader.integer("source_row_count", required=True, minimum=0)
    if source_row_count < len(rows):
        reader.fail("source_row_count", "cannot be smaller than the committed row count")
    omissions = _structured_omissions(reader, "omissions")
    if fact_status is FactStatus.COMPLETE and source_row_count != len(rows):
        reader.fail(
            "rows",
            "a complete table keeps every source row; omit prose-only cells with codes",
        )
    if not reader.ok or fact_status is None:
        return None
    return CatalogTable(
        id=identifier,
        name=name,
        section_id=section_id,
        page=page,
        fact_status=fact_status,
        columns=tuple(columns),
        rows=tuple(rows),
        source_row_count=source_row_count,
        omissions=omissions,
        provenance=provenance,
    )


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
    bonus_actions = reader.string_list("bonus_actions")
    allowed_bonus_actions = {"dash", "disengage"}
    for value in bonus_actions:
        if value not in allowed_bonus_actions:
            reader.fail(
                "bonus_actions",
                f"{value!r} is not supported. Valid values: "
                f"{', '.join(sorted(allowed_bonus_actions))}",
            )
    reader.boolean("surrender_when_last")
    reader.boolean("redirect_attack")
    reader.boolean("pack_tactics")
    reader.boolean("undead_fortitude")
    reader.integer("spell_save_dc", default=10, minimum=1)
    reader.integer("spell_attack_bonus")
    reader.enum("spellcasting_ability", Ability)
    reader.string("team")
    # Validated as a faithful transcription and then discarded: no field on
    # Creature carries it, and Creature.from_record never reads it. See the
    # allowlist comment above for why that is a deliberate, standing decision
    # rather than an oversight.
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
        sub.boolean("advantage_bonus_with_adjacent_ally")
        if attack.get("advantage_bonus_with_adjacent_ally") and (
            attack.get("advantage_bonus_damage") is None
        ):
            sub.fail(
                "advantage_bonus_with_adjacent_ally",
                "needs advantage_bonus_damage — there are no bonus dice to apply",
            )
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
        sub.string("ammunition")
        sub.boolean("loading")
        sub.boolean("thrown")
        attack_kind = attack.get("kind", "melee")
        if attack.get("ammunition") is not None and attack_kind != "ranged":
            sub.fail(
                "ammunition",
                "needs kind ranged — a melee attack spends nothing to swing",
            )
        if attack.get("loading") and attack_kind != "ranged":
            sub.fail(
                "loading",
                "needs kind ranged — a melee attack has no reload rhythm to gate",
            )
        # The two ``AttackOption.__post_init__`` guards on ``thrown``, reported
        # here first as a diagnostic the pack author can act on. Both are the
        # SRD's "Melee or Ranged Attack Roll" line: ``thrown`` says what the
        # swing does inside ``reach``, so it needs a ranged option to qualify
        # and somewhere to be thrown to.
        if attack.get("thrown") and attack_kind != "ranged":
            sub.fail(
                "thrown",
                "needs kind ranged — it says what happens inside reach, and a "
                "melee attack is already there",
            )
        if attack.get("thrown") and not (
            attack.get("normal_range") or attack.get("long_range")
        ):
            sub.fail(
                "thrown",
                "needs a normal_range or long_range — there is nowhere to throw it",
            )
        ammunition = attack.get("ammunition")
        if isinstance(ammunition, str) and not ammunition.strip():
            sub.fail("ammunition", "must not be blank")
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
        heal=reader.dice("heal"),
        half_on_save=reader.boolean("half_on_save", default=False),
        upcast_damage=reader.dice("upcast_damage"),
        upcast_heal=reader.dice("upcast_heal"),
        add_spellcasting_modifier=reader.boolean("add_spellcasting_modifier"),
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
        action_cost=reader.enum("action_cost", ActionCost) or ActionCost.ACTION,
    )
    if spell.damage is not None and spell.damage_type is None:
        reader.fail("damage_type", "a spell that deals damage must name a damage type")
    if spell.requires_attack_roll and spell.save_ability is not None:
        reader.fail(
            "save_ability",
            "a spell cannot both require an attack roll and offer a saving throw",
        )
    # ``range_feet`` defaults to 0, and 0 already means "no range check at all"
    # (``Encounter._require_in_range``). So an author who omits the field — the
    # honest transcription of "Range: Touch", which names no number — is
    # indistinguishable from one deliberately declaring unlimited reach, and Cure
    # Wounds and Regenerate are both Touch. Exempt for an area spell, whose range
    # is measured at its point of origin or poured out of the caster rather than
    # named on the cast.
    #
    # A warning rather than a refusal, and the bound is a compatibility promise
    # rather than taste: a pack this repo has never seen may already omit the
    # field, and ``test_existing_packs_remain_compatible_and_new_sections_are_optional``
    # exists to say a campaign's own content keeps loading. Scanning the bundled
    # catalog and the test corpus cannot speak for that population. Same trade as
    # the unshaped-radius warning below — the legacy reading survives, and the
    # record is told to say what it means.
    if record.get("range_feet") is None and not spell.is_area:
        reader.warn(
            "range_feet",
            "does not say what range it has, so it resolves with no range check "
            "at all and can be cast at any distance. Declare it — a number of "
            "feet, 5 for Touch, or 0 for Self — so the record says what the "
            "spell does",
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
        underwater=bool(values.get("underwater", defaults.underwater)),
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
    action_cost = sub.enum("action_cost", ActionCost) or ActionCost.ACTION
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
            action_cost=action_cost,
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
        items={}, item_records={}, catalog={}, catalog_records={},
        catalog_tables={}, catalog_table_records={},
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

    for identifier, record in _identified_records(payload, "catalog", diagnostics, label):
        catalog_record = _parse_catalog_record(identifier, record, diagnostics, label)
        if catalog_record is not None:
            parsed.catalog[identifier] = catalog_record
            parsed.catalog_records[identifier] = dict(record)
    for identifier, record in _identified_records(
        payload, "catalog_tables", diagnostics, label
    ):
        catalog_table = _parse_catalog_table(identifier, record, diagnostics, label)
        if catalog_table is not None:
            parsed.catalog_tables[identifier] = catalog_table
            parsed.catalog_table_records[identifier] = dict(record)

    counts = {section: len(parsed.section(section)) for section in MERGE_SECTIONS}
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
        "pack": "srd-5.2.1-conditions",
        "version": "1.0",
        "provenance": "SRD 5.2.1",
        "note": (
            "Rendered from the engine's own condition table so it validates and "
            "merges like any other pack."
        ),
        "conditions": [
            {
                "name": str(name),
                "provenance": "SRD 5.2.1",
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
            "Engine policy; movement, cover, and vision semantics follow SRD 5.2.1"
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
    catalog: Mapping[str, CatalogRecord] | None = None,
) -> None:
    """Check references that only the merged set can answer.

    A spell naming ``condition: "vale-cursed"`` is valid exactly when some pack in
    the merged set defines it, which no per-file validator can know. A catalog
    row's ``content_ref`` is the same kind of claim pointing the other way.
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
        # An items entry the creature's own attacks name as ``ammunition`` is not
        # an item at all — ``ItemEffect.__post_init__`` refuses a use that does
        # nothing, and ammunition has no ``use`` block to give it one. So it can
        # never be "defined" and the items cross-reference below must not ask for
        # that; it would be reporting a fact the engine does not act on.
        ammunition_names = {
            str(attack["ammunition"])
            for attack in (record.get("attacks", []) or [])
            if isinstance(attack, dict) and attack.get("ammunition") is not None
        }

        # Warnings, not errors: the encounter refuses these at use time with a clear
        # reason rather than crashing, so a pack meant to be combined with another is
        # still loadable. It is worth saying now, though — the alternative is finding
        # out mid-fight.
        for referenced, table, field_name in (
            (record.get("spells", []) or [], spells, "spells"),
            (record.get("items", {}) or {}, items, "items"),
        ):
            for entry in referenced:
                text = str(entry)
                if text in table or text in ammunition_names:
                    continue
                diagnostics.append(
                    Diagnostic(
                        source=sources.get(("creatures", name), "unknown"),
                        section="creatures", record=name, field=field_name,
                        problem=(
                            f"refers to {text!r}, which no loaded pack defines; "
                            f"the engine will refuse it when the creature tries to use it"
                        ),
                        severity=Severity.WARNING,
                    )
                )

        # The other direction: an attack naming ammunition the creature's own
        # ``items`` never stocks is an authoring mistake worth catching now — the
        # first shot will refuse it, since the stepper spends a piece per shot and
        # refuses an empty quiver. It cannot be folded into the loop above because
        # it is never an ``items`` entry at all; it is read straight off ``attacks``.
        held_items = record.get("items", {}) or {}
        for index, attack in enumerate(record.get("attacks", []) or []):
            if not isinstance(attack, dict):
                continue
            ammo = attack.get("ammunition")
            if ammo is None or str(ammo) in held_items:
                continue
            diagnostics.append(
                Diagnostic(
                    source=sources.get(("creatures", name), "unknown"),
                    section="creatures", record=name,
                    field=f"attacks[{index}].ammunition",
                    problem=(
                        f"names {str(ammo)!r} as ammunition, but this creature does "
                        f"not carry any {str(ammo)!r} in items; the first shot will refuse"
                    ),
                    severity=Severity.WARNING,
                )
            )

    # A catalog row's ``content_ref`` is what makes ``catalog.get`` answer "the
    # engine can run this", so a dangling one ships a broken promise: the payload
    # still carries the ref while ``sources.executable`` comes back null, pointing a
    # caller at an executable record that is not there. An error rather than a
    # warning, unlike the creature references above — those describe a fight that
    # will refuse cleanly at use time, whereas this one misreports identity, which
    # is the catalog's whole job.
    #
    # The ``section`` is taken literally as the place to look. Nothing ties a row's
    # ``kind`` to the section it points at, and whether it should is a live question
    # — it is what made Goodberry (a spell whose healing happens later, by whoever
    # eats a berry) a judgement call rather than an error.
    by_section: Mapping[str, Mapping[str, Any]] = {
        "spells": spells, "items": items, "creatures": creatures,
        "conditions": registry_conditions,
    }
    for row in (catalog or {}).values():
        ref = row.content_ref
        if ref is None:
            continue
        section_table = by_section.get(ref.section)
        if section_table is not None and ref.name in section_table:
            continue
        diagnostics.append(
            Diagnostic(
                source=sources.get(("catalog", row.id), "unknown"),
                section="catalog", record=row.id, field="content_ref",
                problem=(
                    f"points at {ref.name!r} in {ref.section!r}, which no loaded "
                    f"pack defines; the row would claim an executable record that "
                    f"is not there"
                ),
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
    catalog: dict[str, CatalogRecord] = {}
    catalog_tables: dict[str, CatalogTable] = {}
    sources: dict[tuple[str, str], str] = {}

    ownership = {
        section: _merge_section(section, levels, diagnostics) for section in MERGE_SECTIONS
    }
    for _level, packs in levels:
        for pack in packs:
            for section in MERGE_SECTIONS:
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
                    elif section == "items":
                        items[name] = pack.items[name]
                        item_records[name] = pack.item_records[name]
                    elif section == "catalog":
                        catalog[name] = pack.catalog[name]
                    else:
                        catalog_tables[name] = pack.catalog_tables[name]

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
            "provenance": "SRD 5.2.1",
            "unmodelled": [],
            "unmodelled_facts": [],
        }
        retained.append(name)

    _cross_reference(
        condition_effects, spells, items, creatures, sources, diagnostics, catalog
    )

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
        catalog=MappingProxyType(catalog),
        catalog_tables=MappingProxyType(catalog_tables),
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
    arrival_round: int = 1,
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
        names = sorted(active.creatures)
        shown = names[:10]
        available = ", ".join(shown) or "none"
        raise DataError(
            f"no stat block named {name!r} in the loaded content "
            f"(built-in content is {active.builtin.value}d); available: {available} "
            f"(showing {len(shown)} of {len(names)})"
        )
    return Creature.from_record(
        record,
        condition_effects=active.condition_effects,
        source=active.source_of("creatures", name),
        label=label,
        team=team,
        position=position,
        level=level,
        arrival_round=arrival_round,
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
