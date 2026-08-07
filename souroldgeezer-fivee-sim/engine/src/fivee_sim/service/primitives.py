"""Dice and lookups: the operations that need a seed but not a fight.

A primitive is stateless by nature — a d20 is a d20 — but *auditable* on
request. Give one an ``encounter_id`` and it is journalled against that fight
like any action, with ``request_id`` making a retry idempotent; give it none
and it simply rolls. :func:`audited_primitive` is that fork, written once so
every primitive answers it the same way.

The rest is lookup: what a loaded condition, spell, creature, item or terrain
kind actually says, rendered from whatever registry the session is running on
rather than from anything bundled.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from random import Random
from typing import Any

from ..analytics.scenario import response_window, travel_timing
from ..content import ContentRegistry
from ..kernel.dice import Advantage, Dice, DiceError, roll_d20, roll_dice
from ..kernel.grid import TerrainEffect
from ..kernel.rules import Ability, make_d20_test
from . import sessions, specs
from .errors import NotFoundError, RequestError
from .sessions import EngineState

__all__ = [
    "audited_primitive",
    "check",
    "condition_entry",
    "creature_entry",
    "item_entry",
    "lookup_rule",
    "roll",
    "save",
    "scenario_timing",
    "spell_entry",
    "terrain_entry",
]


def audited_primitive(
    state: EngineState,
    *,
    encounter_id: str | None,
    request_id: str | None,
    operation: str,
    arguments: Mapping[str, Any],
    execute: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    if encounter_id is None:
        if request_id is not None:
            raise RequestError("request_id requires encounter_id")
        # Translated here rather than left to escape: an unaudited primitive
        # takes the same bad expression and the same unusable reported face as
        # an audited one, and the branch below already turns those into a
        # refusal. Without this the two paths answer the same mistake with a
        # 400 and a 500.
        try:
            return execute()
        except DiceError as error:
            raise RequestError(str(error)) from error
    session = sessions.session_for(state, encounter_id)
    cached = sessions.cached_request(session, request_id)
    if cached is not None:
        return cached
    # Ahead of the journal, beside the cache hit above and for the same reason:
    # this refusal rolls nothing and changes nothing, so a record of it is a
    # record of the caller's mistake rather than of the fight. Keeping it here
    # is also what makes ``finalized`` the last thing a journal can say, which
    # is what lets ``list_encounters`` read a fight's status off one line.
    if session.finalized:
        raise RequestError(f"encounter {encounter_id!r} is finalized")
    index, started_at = sessions.attempt_started(
        state, encounter_id, session, operation, arguments, request_id
    )
    try:
        result = execute()
        result["encounter_id"] = encounter_id
    except ValueError as error:
        sessions.attempt_finished(
            state,
            encounter_id,
            session,
            index=index,
            started_at=started_at,
            operation=operation,
            arguments=arguments,
            request_id=request_id,
            status="refused",
            error=str(error),
        )
        raise RequestError(str(error)) from error
    sessions.attempt_finished(
        state,
        encounter_id,
        session,
        index=index,
        started_at=started_at,
        operation=operation,
        arguments=arguments,
        request_id=request_id,
        status="success",
        result=result,
    )
    return result


def scenario_timing(
    distance_feet: int,
    speed_feet: int,
    dash: bool = False,
    start_delay_rounds: int = 0,
    response_after_rounds: int | None = None,
) -> dict[str, Any]:
    try:
        if response_after_rounds is None:
            return {
                "traveller": travel_timing(
                    distance_feet=distance_feet,
                    speed_feet=speed_feet,
                    dash=dash,
                    start_delay_rounds=start_delay_rounds,
                ).as_dict()
            }
        return response_window(
            distance_feet=distance_feet,
            speed_feet=speed_feet,
            dash=dash,
            start_delay_rounds=start_delay_rounds,
            response_after_rounds=response_after_rounds,
        )
    except ValueError as error:
        raise RequestError(str(error)) from error


def roll(
    state: EngineState,
    expression: str,
    advantage: str = "none",
    seed: int | None = None,
    encounter_id: str | None = None,
    request_id: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    def execute() -> dict[str, Any]:
        used = specs.checked_seed(seed)
        rng = Random(used)
        dice = Dice.parse(expression)
        chosen = specs.parse_advantage(advantage)
        # A single d20 goes through the d20 resolver even without Advantage so
        # the generated natural remains an explicit output of this operation.
        if dice.count == 1 and dice.faces == 20:
            d20 = roll_d20(rng, chosen)
            result: dict[str, Any] = {
                "expression": str(dice),
                "seed": used,
                "advantage": chosen.value,
                "natural": d20.natural,
                "rolls": list(d20.rolls),
                "total": d20.natural + dice.modifier,
                "detail": d20.describe(),
            }
        else:
            rolled = roll_dice(dice, rng)
            result = {
                "expression": str(dice),
                "seed": used,
                "advantage": Advantage.NONE.value,
                "rolls": list(rolled.rolls),
                "total": rolled.total,
                "detail": rolled.describe(),
            }
        if label is not None:
            result["label"] = label
        return result

    return audited_primitive(
        state,
        encounter_id=encounter_id,
        request_id=request_id,
        operation="roll",
        arguments={
            "expression": expression, "advantage": advantage, "seed": seed,
            "label": label,
        },
        execute=execute,
    )


def check(
    state: EngineState,
    modifier: int,
    dc: int,
    advantage: str = "none",
    seed: int | None = None,
    encounter_id: str | None = None,
    request_id: str | None = None,
    ability: str | None = None,
    skill: str | None = None,
) -> dict[str, Any]:
    def execute() -> dict[str, Any]:
        if ability is not None:
            Ability(ability)
        if skill is not None and not skill.strip():
            raise RequestError("skill must not be blank")
        used = specs.checked_seed(seed)
        test = make_d20_test(
            Random(used), modifier=modifier, dc=dc,
            advantage=specs.parse_advantage(advantage),
        )
        result: dict[str, Any] = {
            "seed": used,
            "natural": test.roll.natural,
            "total": test.total,
            "dc": dc,
            "success": test.success,
            "detail": test.describe(),
        }
        if ability is not None:
            result["ability"] = ability
        if skill is not None:
            result["skill"] = skill
        return result

    return audited_primitive(
        state,
        encounter_id=encounter_id,
        request_id=request_id,
        operation="check",
        arguments={
            "modifier": modifier, "dc": dc, "advantage": advantage, "seed": seed,
            "ability": ability, "skill": skill,
        },
        execute=execute,
    )


def save(
    state: EngineState,
    modifier: int,
    dc: int,
    advantage: str = "none",
    auto_fail: bool = False,
    seed: int | None = None,
    encounter_id: str | None = None,
    request_id: str | None = None,
    ability: str | None = None,
) -> dict[str, Any]:
    def execute() -> dict[str, Any]:
        if ability is not None:
            Ability(ability)
        used = specs.checked_seed(seed)
        test = make_d20_test(
            Random(used),
            modifier=modifier,
            dc=dc,
            advantage=specs.parse_advantage(advantage),
            auto_fail=auto_fail,
        )
        result: dict[str, Any] = {
            "seed": used,
            "natural": test.roll.natural,
            "total": test.total,
            "dc": dc,
            "success": test.success,
            "auto_failed": test.auto_failed,
            "detail": test.describe(),
        }
        if ability is not None:
            result["ability"] = ability
        return result

    return audited_primitive(
        state,
        encounter_id=encounter_id,
        request_id=request_id,
        operation="save",
        arguments={
            "modifier": modifier, "dc": dc, "advantage": advantage,
            "auto_fail": auto_fail, "seed": seed, "ability": ability,
        },
        execute=execute,
    )


def condition_entry(registry: ContentRegistry, name: str) -> dict[str, Any]:
    effect = registry.condition_effects[name]
    record = registry.condition_records.get(name, {})
    return {
        "kind": "condition",
        "name": name,
        "effects": {
            flag: getattr(effect, flag)
            for flag in effect.__dataclass_fields__
            if getattr(effect, flag)
        } or {"note": "no combat-roll consequences"},
        "description": str(record.get("description", "")),
        "source": registry.source_of("conditions", name),
        "provenance": str(record.get("provenance", "SRD 5.2.1")),
        "unmodelled": list(record.get("unmodelled", [])),
        "unmodelled_facts": list(record.get("unmodelled_facts", [])),
    }


def spell_entry(registry: ContentRegistry, name: str) -> dict[str, Any]:
    spell = registry.spells[name]
    record = registry.spell_records.get(name, {})
    return {
        "kind": "spell",
        "name": spell.name,
        "level": spell.level,
        "school": spell.school,
        "save": spell.save_ability.value if spell.save_ability else None,
        "attack_roll": spell.requires_attack_roll,
        "damage": str(spell.damage) if spell.damage else None,
        "damage_type": spell.damage_type.value if spell.damage_type else None,
        "half_on_save": spell.half_on_save,
        "upcast_damage": str(spell.upcast_damage) if spell.upcast_damage else None,
        "shape": spell.effective_shape.value,
        "radius": spell.radius,
        "length": spell.length,
        "size": spell.size,
        "range_feet": spell.range_feet,
        "condition": spell.condition,
        "concentration": spell.concentration,
        "source": registry.source_of("spells", name),
        "provenance": spell.provenance,
        "unmodelled": list(record.get("unmodelled", [])),
        "unmodelled_facts": list(record.get("unmodelled_facts", [])),
    }


def item_entry(registry: ContentRegistry, name: str) -> dict[str, Any]:
    effect = registry.items[name]
    record = registry.item_records.get(name, {})
    return {
        "kind": "item",
        "name": name,
        "use": {
            "heal": str(effect.heal) if effect.heal else None,
            "damage": str(effect.damage) if effect.damage else None,
            "damage_type": effect.damage_type.value if effect.damage_type else None,
            "save_ability": effect.save_ability.value if effect.save_ability else None,
            "save_dc": effect.save_dc or None,
            "half_on_save": effect.half_on_save,
            "condition": effect.condition,
        },
        "description": effect.description,
        "source": registry.source_of("items", name),
        "provenance": effect.provenance,
        "unmodelled": list(record.get("unmodelled", [])),
        "unmodelled_facts": list(record.get("unmodelled_facts", [])),
    }


def terrain_entry(registry: ContentRegistry, name: str) -> dict[str, Any]:
    effect = registry.terrain_effects[name]
    record = registry.terrain_records.get(name, {})
    defaults = TerrainEffect()
    return {
        "kind": "terrain",
        "name": name,
        "effects": {
            flag: getattr(effect, flag)
            for flag in effect.__dataclass_fields__
            if getattr(effect, flag) != getattr(defaults, flag)
        } or {"note": "ordinary ground; no movement or sight consequences"},
        "description": str(record.get("description", "")),
        "source": registry.source_of("terrain", name),
        "provenance": str(record.get("provenance", "engine policy")),
        "unmodelled": list(record.get("unmodelled", [])),
        "unmodelled_facts": list(record.get("unmodelled_facts", [])),
    }


def creature_entry(registry: ContentRegistry, name: str) -> dict[str, Any]:
    record = registry.creatures[name]
    entry: dict[str, Any] = {"kind": "creature", **record}
    entry["source"] = registry.source_of("creatures", name)
    # ``unmodelled`` is present even when empty. The skill tells the assistant to check it
    # before promising a trait will fire, and that instruction has to stay true for a
    # campaign's own creature rather than hitting a missing key.
    entry.setdefault("unmodelled", [])
    entry.setdefault("unmodelled_facts", [])
    entry.setdefault("provenance", entry["source"])
    return entry


def lookup_rule(state: EngineState, topic: str = "") -> dict[str, Any]:
    registry = sessions.active_registry(state)
    if not topic:
        summary = registry.summary()
        return {
            "builtin": registry.builtin.value,
            "counts": summary["counts"],
            "catalog": summary["catalog"],
            "packs": [pack.label for pack in registry.packs],
            "provenance": sorted({pack.provenance for pack in registry.packs}),
            "guidance": {
                "search_tool": "catalog_search",
                "exact_lookup": "Call lookup_rule with an exact loaded content name.",
            },
        }
    key = topic.strip().casefold()

    finders = (
        ("conditions", registry.condition_effects, condition_entry),
        ("spells", registry.spells, spell_entry),
        ("creatures", registry.creatures, creature_entry),
        ("items", registry.items, item_entry),
        ("terrain", registry.terrain_effects, terrain_entry),
    )
    for _section, table, build in finders:
        for name in table:
            if name.casefold() == key:
                return build(registry, name)

    raise NotFoundError(
        f"nothing loaded for {topic!r}. Search the catalog to find catalog or custom "
        f"content, or read the content status to see which packs are loaded."
    )
