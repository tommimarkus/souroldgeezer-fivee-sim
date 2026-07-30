"""Generates the human-readable coverage report.

The report is derived from the data and the enums rather than maintained by hand,
because a hand-written coverage list is a promise that quietly stops being true.
A test compares the committed ``docs/COVERAGE.md`` against this renderer, so
adding a monster without regenerating the report fails the suite.

Regenerate with::

    uv run python -m fivee_sim.coverage

The "not supported" section is prose, not derivation: absence cannot be read off
the data, and it is the part a reader most needs.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .data import item_effects, monster_records, spell_records, spellbook
from .kernel.conditions import Condition, effect_of
from .kernel.rules import DamageType
from .model.encounter import ActionKind

#: Defined by SRD 5.2 and deliberately not implemented.
UNIMPLEMENTED_CONDITIONS = ("Exhaustion",)

NOT_SUPPORTED = (
    (
        "Character building",
        "Classes, subclasses, species and lineages, backgrounds, feats, ability-score "
        "generation, levelling, and multiclassing. Combatants are described directly "
        "by their statistics — armour class, hit points, attacks, save bonuses — the "
        "way a stat block presents them. There is no notion of a character sheet that "
        "derives those numbers.",
    ),
    (
        "Equipment beyond simple usable items",
        "Simple usable items *are* modelled: potions, flasks, and doses of poison — one "
        "use that heals, deals damage, or applies a condition, held in a quantity that "
        "is also its charge count. None ship in the bundled slice, so potions reach a "
        "session through a content pack. Nothing beyond that use is modelled. Weapons "
        "and armour as objects that derive attack bonuses and armour class, scrolls, "
        "attunement, encumbrance, ammunition, and charges tracked separately from "
        "quantity are all absent. An attack carries its own bonus and damage "
        "expression; nothing models the object producing it.",
    ),
    (
        "Spell resources beyond slots",
        "Spell lists per class, preparation rules, ritual casting, cantrip scaling by "
        "level, components, and material costs. A combatant simply holds a set of "
        "spell names and a count of slots per level.",
    ),
    (
        "Anything outside a fight",
        "Exploration, travel, downtime, resting and recovery, skills and proficiencies "
        "as a system, social interaction, and the adventuring day. Resources do not "
        "regenerate; an encounter begins and ends.",
    ),
    (
        "Battlefield geometry",
        "Positions are feet along a single axis. Reach, ranged bands, and spell radii "
        "work; facing, flanking, cover, difficult terrain, elevation, and movement "
        "around obstacles do not exist.",
    ),
    (
        "Reactions other than opportunity attacks",
        "Readied actions, Shield and similar reaction spells, Parry, and legendary or "
        "lair actions. Each combatant has one reaction per round and only ever spends "
        "it on an opportunity attack.",
    ),
)


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|")


def _condition_summary(condition: Condition) -> str:
    effect = effect_of(condition)
    active = [name for name in effect.__dataclass_fields__ if getattr(effect, name)]
    if not active:
        return "tracked; no combat-roll consequences"
    return ", ".join(name.replace("_", " ") for name in active)


def _attack_summary(attacks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for attack in attacks:
        reach = (
            f"reach {attack.get('reach', 5)} ft"
            if attack.get("kind", "melee") == "melee"
            else f"range {attack.get('normal_range')}/{attack.get('long_range')} ft"
        )
        parts.append(
            f"{attack['name']} +{attack['attack_bonus']}, {reach}, "
            f"{attack['damage']} {attack['damage_type']}"
        )
    return "; ".join(parts) or "none"


def _notes(record: dict[str, Any]) -> str:
    return "<br>".join(_md_escape(note) for note in record.get("unmodelled", [])) or "—"


def render_markdown() -> str:
    monsters = monster_records()
    spells = spellbook()
    raw_spells = spell_records()
    items = item_effects()
    conditions = list(Condition)

    lines: list[str] = []
    add = lines.append

    add("# Coverage")
    add("")
    add("What this engine actually implements, and what it does not.")
    add("")
    add(
        "**Generated** from the bundled data and the engine's own enums by "
        "`uv run python -m fivee_sim.coverage` — do not edit by hand. A test fails if "
        "this file drifts from the data."
    )
    add("")
    add(
        "Rules content is SRD 5.2 under CC-BY-4.0; see [NOTICE](../NOTICE). SRD 5.2 "
        "covers only part of the 2024 ruleset, so content absent from the SRD is not "
        "available to this project at all."
    )
    add("")
    add(
        "**This describes the bundled slice.** A campaign can add its own creatures, "
        "spells, conditions, and usable items as content packs, or exclude the bundled "
        "content entirely and run on its own material — see "
        "[CONTENT-PACKS.md](CONTENT-PACKS.md). What a given session actually has "
        "loaded is reported by the `content_status` tool, which is the authority when "
        "packs are in play; this document is the authority for what ships."
    )
    add("")
    add("## At a glance")
    add("")
    add("| Category | Supported |")
    add("| --- | --- |")
    add(f"| Creatures (stat blocks) | {len(monsters)} |")
    add(f"| Spells | {len(spells)} |")
    add(f"| Conditions | {len(conditions)} |")
    add(f"| Damage types | {len(list(DamageType))} |")
    add(f"| Actions | {len(list(ActionKind))} |")
    add(f"| Usable items | {len(items)} bundled — the category is modelled, packs supply it |")
    add("| Classes, species, backgrounds, feats | 0 — not modelled |")
    add("")
    add(
        "The creature and spell lists are a deliberately narrow starting slice, not an "
        "attempt at the whole SRD."
    )
    add("")

    add("## Creatures")
    add("")
    add("| Name | AC | HP | Speed | Attacks | Printed features not implemented |")
    add("| --- | --- | --- | --- | --- | --- |")
    for name in sorted(monsters):
        record = monsters[name]
        add(
            f"| {name} | {record['ac']} | {record['max_hp']} "
            f"({record.get('hit_dice', '—')}) | {record.get('speed', 30)} ft | "
            f"{_md_escape(_attack_summary(record.get('attacks', [])))} | "
            f"{_notes(record)} |"
        )
    add("")

    add("## Spells")
    add("")
    add("| Name | Level | Resolution | Damage | Upcast | Area | Concentration | Not implemented |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for name in sorted(spells):
        spell = spells[name]
        if spell.requires_attack_roll:
            resolution = "spell attack roll"
        elif spell.save_ability is not None:
            resolution = f"{spell.save_ability.value} save"
            # Only meaningful for a spell that deals damage; a save-or-suffer spell
            # with no damage would otherwise read as "half on save" of nothing.
            if spell.damage is not None:
                resolution += ", half on save" if spell.half_on_save else ", nothing on save"
        else:
            resolution = "automatic"
        area = f"{spell.radius} ft radius" if spell.radius else "single target"
        add(
            f"| {name} | {spell.level} | {resolution} | "
            f"{spell.damage if spell.damage else '—'} | "
            f"{f'+{spell.upcast_damage}/level' if spell.upcast_damage else '—'} | "
            f"{area} | {'yes' if spell.concentration else 'no'} | "
            f"{_notes(raw_spells.get(name, {}))} |"
        )
    add("")

    add("## Conditions")
    add("")
    add("| Condition | Mechanical effect |")
    add("| --- | --- |")
    for condition in conditions:
        add(f"| {condition.value} | {_condition_summary(condition)} |")
    add("")
    add(
        "**Not implemented:** "
        + ", ".join(UNIMPLEMENTED_CONDITIONS)
        + ". SRD 5.2 defines it; this engine does not track it."
    )
    add("")

    add("## Actions")
    add("")
    add(
        "Each combatant may take one action per turn, plus movement: "
        + ", ".join(f"`{kind.value}`" for kind in ActionKind)
        + ". Extra Attack is supported as a count of attacks per action. Opportunity "
        "attacks are taken automatically when a creature leaves reach without "
        "disengaging."
    )
    add("")
    add("Also resolved: death saving throws, stabilising, instant death when damage "
        "past 0 hit points equals maximum hit points, damage resistance, vulnerability "
        "and immunity, and concentration checks when a concentrating creature is "
        "damaged.")
    add("")

    add("## Damage types")
    add("")
    add(", ".join(damage.value for damage in DamageType) + ".")
    add("")

    add("## Not supported")
    add("")
    add(
        "Stated explicitly because absence is invisible in the data above, and because "
        "a caller who assumes one of these exists will get a wrong answer rather than "
        "an error."
    )
    add("")
    for heading, detail in NOT_SUPPORTED:
        add(f"**{heading}.** {detail}")
        add("")

    add("## Checking at runtime")
    add("")
    add(
        "`lookup_rule` with no topic lists every loaded condition, spell, creature, and "
        "item. With a topic it returns that entry, including the pack it came from and "
        "its `unmodelled` field. A miss means the subject is not loaded — it is refused "
        "rather than invented."
    )
    add("")
    add(
        "`content_status` reports which packs are loaded, whether the bundled slice is "
        "included, and any encounter still running on content from before the last "
        "change. `content_validate` checks a pack without loading it."
    )
    add("")
    add(
        "`encounter_log` pages the full event and action history of a fight in "
        "progress, stamped with rounds and turns — the record to recap or replay "
        "from, where `encounter_state` is only the view of now."
    )
    return "\n".join(lines) + "\n"


def default_output_path() -> Path:
    """``<plugin root>/docs/COVERAGE.md``, resolved from this file's location."""
    return Path(__file__).resolve().parents[3] / "docs" / "COVERAGE.md"


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    target = Path(arguments[0]) if arguments else default_output_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(), encoding="utf-8")
    print(f"wrote {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
