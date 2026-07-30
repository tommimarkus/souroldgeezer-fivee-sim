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
from .kernel.grid import TERRAIN, CoverGrade, DiagonalRule, TerrainEffect
from .kernel.rules import DamageType
from .kernel.spells import Spell, SpellShape
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
        "Battlefield geometry beyond a flat grid",
        "The grid itself is real now — see the Battlefield section for the terrain "
        "kinds, cover grades, line of sight, area shapes, the diagonal-cost knob, "
        "and doors. What remains absent is the third dimension and body mechanics: "
        "elevation and 3-D space, flying, creature size and squeezing (every "
        "combatant occupies one square whatever its printed size), facing, "
        "flanking, forced movement (nothing pushes, drags, or knocks a creature "
        "through space), and climbing or swimming as movement modes.",
    ),
    (
        "Timed durations beyond attack riders",
        "Concentration is tracked, and ending it lifts the condition the spell "
        "imposed. An attack's on-hit condition rider can carry its own clock — "
        "expiring at the start of the attacker's next turn or the end of the "
        "target's next turn, and the expiry fires even if the attacker has died. "
        "Beyond those two anchors, elapsed time is not modelled: the 'up to 1 "
        "minute' cap on a concentration spell never expires it, a spell's repeat "
        "saving throw at the end of the target's turn is not rolled, and a "
        "condition applied by an item or set directly on a stat block lasts until "
        "something removes it.",
    ),
    (
        "Reactions other than opportunity attacks",
        "Readied actions, Shield and similar reaction spells, Parry, and legendary or "
        "lair actions. Each combatant has one reaction per round and only ever spends "
        "it on an opportunity attack.",
    ),
    (
        "A fight that carries on over the dying",
        "An encounter ends as soon as one side has nobody conscious left, so a side "
        "reduced to dying creatures counts as beaten. Their death saves stop with the "
        "fight: a downed creature can never roll the natural 20 that would put it back "
        "on its feet, and a mutual knockout is reported as a draw rather than decided "
        "by whichever side recovers first. Damage to a creature at 0 hit points is "
        "fully modelled — an attack, an area spell, and an item all reach one — but "
        "this is about when the fight stops being simulated. Measured on the bundled "
        "stat blocks, counting the dying as still in the fight would lengthen a "
        "reported fight by 58% to 131%, and 30% to 46% of every round reported would "
        "be one in which nobody acts at all — more still once a caster is involved. "
        "Nothing in the auto-play policy that drives a batch finishes a downed "
        "creature off or takes the Help action to stabilise one, so those rounds are "
        "an empty room rather than a fight.",
    ),
    (
        "Monster instant death",
        "SRD 5.2 has a monster die the instant it drops to 0 hit points, where a "
        "character instead falls unconscious and makes death saving throws. Every "
        "combatant here is treated as a character, so any creature that drops begins "
        "the dying state.",
    ),
    (
        "Conditions imposed on a creature that is already down",
        "A spell or item that imposes a condition applies it only to a conscious "
        "target. Damage from the same effect still lands on a dying creature, and "
        "still costs it a death saving throw failure; the condition does not follow.",
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


def _rider_summary(attack: dict[str, Any]) -> str:
    """The attack's riders, rendered after its damage — empty when it has none."""
    text = ""
    if attack.get("bonus_damage"):
        text += f" plus {attack['bonus_damage']} {attack['bonus_damage_type']}"
    if attack.get("advantage_bonus_damage"):
        text += (
            f" plus {attack['advantage_bonus_damage']} if the attack roll "
            f"had advantage"
        )
    if attack.get("on_hit_condition"):
        text += f", on hit: {attack['on_hit_condition']}"
        if attack.get("on_hit_save_ability"):
            text += (
                f" (DC {attack['on_hit_save_dc']} "
                f"{attack['on_hit_save_ability']} save)"
            )
        expiry = attack.get("on_hit_expiry", "none")
        if expiry == "start_of_attacker_next_turn":
            text += " until the start of the attacker's next turn"
        elif expiry == "end_of_target_next_turn":
            text += " until the end of the target's next turn"
    return text


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
            f"{attack['damage']} {attack['damage_type']}{_rider_summary(attack)}"
        )
    return "; ".join(parts) or "none"


def _trait_summary(record: dict[str, Any]) -> str:
    """The stat block's modelled trait flags, as a line after the attacks."""
    traits: list[str] = []
    if record.get("pack_tactics"):
        traits.append(
            "Pack Tactics — Advantage while a capable ally is within 5 ft of the target"
        )
    if record.get("undead_fortitude"):
        traits.append(
            "Undead Fortitude — on a drop to 0 HP, a Constitution save (DC 5 + damage "
            "taken) leaves 1 HP instead, unless the damage was Radiant, a Critical "
            "Hit, or enough to kill outright"
        )
    if not traits:
        return ""
    return "<br>Traits: " + "; ".join(traits)


def _notes(record: dict[str, Any]) -> str:
    return "<br>".join(_md_escape(note) for note in record.get("unmodelled", [])) or "—"


def _area_summary(spell: Spell) -> str:
    match spell.effective_shape:
        case SpellShape.SPHERE:
            return f"{spell.radius} ft sphere"
        case SpellShape.CONE:
            return f"{spell.length} ft cone"
        case SpellShape.LINE:
            return f"{spell.length} ft line"
        case SpellShape.CUBE:
            return f"{spell.size} ft cube"
        case _:
            return "single target"


def _grade_name(grade: CoverGrade) -> str:
    return grade.name.replace("_", "-").lower()


def _terrain_summary(effect: TerrainEffect) -> str:
    parts: list[str] = []
    if effect.move_cost_multiplier != 1:
        parts.append(f"movement x{effect.move_cost_multiplier}")
    if not effect.passable:
        parts.append("impassable")
    if effect.opaque:
        parts.append("blocks sight")
    if effect.cover:
        parts.append(f"grants {_grade_name(CoverGrade(effect.cover))} cover")
    return ", ".join(parts) or "ordinary ground"


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
    add(f"| Terrain kinds | {len(TERRAIN)} built in — packs may add more |")
    add("| Classes, species, backgrounds, feats | 0 — not modelled |")
    add("")
    add(
        "The creature and spell lists are a deliberately narrow starting slice, not an "
        "attempt at the whole SRD."
    )
    add("")

    add("## Creatures")
    add("")
    add("| Name | AC | HP | Speed | Attacks and traits | Printed features not implemented |")
    add("| --- | --- | --- | --- | --- | --- |")
    for name in sorted(monsters):
        record = monsters[name]
        add(
            f"| {name} | {record['ac']} | {record['max_hp']} "
            f"({record.get('hit_dice', '—')}) | {record.get('speed', 30)} ft | "
            f"{_md_escape(_attack_summary(record.get('attacks', [])))}"
            f"{_trait_summary(record)} | "
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
        area = _area_summary(spell)
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
    add("`interact` is the free object interaction: once per turn, without spending "
        "the action, it opens or closes a map feature the actor stands on or next "
        "to.")
    add("")
    add("Also resolved: death saving throws, stabilising, instant death when damage "
        "past 0 hit points equals maximum hit points, damage resistance, vulnerability "
        "and immunity, and concentration checks when a concentrating creature is "
        "damaged. A condition a concentration spell imposed is lifted when that "
        "concentration ends — by a failed check, by the caster being incapacitated or "
        "killed, or by the caster beginning another concentration spell — unless "
        "another effect is still imposing it.")
    add("")
    add("An attack may carry riders, straight from its stat block: bonus damage of "
        "a second type on every hit, defended against its own type; extra dice added "
        "only when the attack roll actually resolved with Advantage; and an on-hit "
        "condition, automatic or applied on a failed save. Rider dice double on a "
        "critical hit like any damage dice. An on-hit condition may expire on its "
        "own — at the start of the attacker's next turn or the end of the target's "
        "next turn — and the expiry fires when that turn slot passes, even if the "
        "attacker has died; it never strips a condition something else is still "
        "imposing.")
    add("")
    add("Two printed creature traits are modelled as stat-block flags. Pack Tactics "
        "grants a creature's attack rolls — weapon and spell alike, opportunity "
        "attacks included — Advantage while another member of its team is within 5 "
        "feet of the target, conscious, and free of incapacitating conditions; it "
        "counts as one Advantage source and cancels against Disadvantage like any "
        "other. Undead Fortitude turns damage that would drop the creature to 0 hit "
        "points into a Constitution saving throw at DC 5 plus the damage taken, and "
        "a success leaves it standing at 1 hit point — bypassed when any of the "
        "damage was Radiant, the hit was a critical, or the overflow was enough to "
        "kill outright.")
    add("")
    add("A creature at 0 hit points is a legal target, not an untouchable one: an "
        "attack, an area effect it stands inside, and a usable item all reach it. Each "
        "costs it one death saving throw failure, two if the damage came from a "
        "critical hit — and an attack from within 5 feet of an Unconscious creature is "
        "always a critical hit. Only a dead creature is refused as a target.")
    add("")

    add("## Damage types")
    add("")
    add(", ".join(damage.value for damage in DamageType) + ".")
    add("")

    add("## Battlefield")
    add("")
    add(
        "Positions are `[x, y]` points in feet on a plane of 5-foot squares. A fight "
        "may run mapless — an open, featureless plane — or on a battle map, supplied "
        "inline to `encounter_create` and `simulate_rounds`, which adds terrain "
        "movement costs, walls, line of sight, cover, pathfinding, and doors. Doors "
        "are named map features flipped by the `interact` action; closed they are "
        "impassable and block sight."
    )
    add("")
    add(
        "**Area shapes:** "
        + ", ".join(
            shape.value for shape in SpellShape if shape is not SpellShape.SINGLE
        )
        + ". **Cover grades:** "
        + ", ".join(_grade_name(grade) for grade in CoverGrade)
        + " — graded by corner-counted sight lines; half and three-quarters raise "
        "the target's AC against attacks and its Dexterity saving throws against "
        "areas, while total cover refuses the attack and excludes the target "
        "from an area outright. **Diagonal rules:** "
        + ", ".join(f"`{rule.value}`" for rule in DiagonalRule)
        + " — a per-encounter knob governing movement and areas alike; the default "
        "prices every diagonal at 5 ft."
    )
    add("")
    add("Built-in terrain kinds — content packs may define more:")
    add("")
    add("| Kind | Effects |")
    add("| --- | --- |")
    for name, effect in sorted(TERRAIN.items()):
        add(f"| {name} | {_terrain_summary(effect)} |")
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
    add("")
    add(
        "The `map_*` tools — `map_generate`, `map_load`, `map_save`, `map_render`, "
        "`map_edit`, `map_query` — manage battle maps as seeded, editable documents "
        "in the running session; what maps exist there, and at which generation, is "
        "their answer rather than this document's."
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
