"""MCP stdio server: a thin adapter over the engine.

Every tool validates its input, calls the kernel or the encounter model, and
serialises the result. No rules logic belongs in this file — if a behaviour needs
deciding, it is decided in ``kernel`` or ``model`` where the tests can reach it.

Two conventions worth knowing:

* Every tool that consumes randomness accepts an optional ``seed`` and **always
  reports the seed it used**. Omitting one does not make a result irreproducible;
  it makes the engine choose a seed and tell you, so any roll can be replayed.
* Encounter state lives in this process, keyed by an id. ``encounter_state`` is
  the authoritative view — narration should follow it rather than memory.

Anything written to stdout other than protocol traffic corrupts the stream, so
diagnostics go to stderr.
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass
from random import Random
from typing import Any

from mcp.server.mcpserver import MCPServer

from .. import __version__
from ..analytics.montecarlo import simulate_dpr as _simulate_dpr
from ..analytics.montecarlo import simulate_rounds as _simulate_rounds
from ..content import (
    BUILTIN_ENV,
    CONTENT_ENV,
    BuiltinMode,
    ContentError,
    ContentRegistry,
    builtin_mode,
    builtin_registry,
    environment_paths,
    load_packs,
)
from ..content import validate as _validate_content
from ..data import DataError, make_creature
from ..kernel.actions import AttackKind
from ..kernel.dice import Advantage, Dice, roll_d20, roll_dice
from ..kernel.rules import Ability, DamageType, make_d20_test
from ..model.creature import AttackOption, Creature
from ..model.encounter import Action, ActionKind, Encounter, EncounterError

INSTRUCTIONS = """\
A 5E-compatible combat engine. The engine owns the fight: hit points, initiative
order, conditions, and dice are computed here, so read encounter_state as
authoritative and narrate from it rather than tracking state yourself.

Content is configurable. The bundled SRD 5.2 slice loads by default, and a campaign
may add its own creatures, spells, conditions, and items as content packs — or run on
its own material alone. Call content_status to see what is actually loaded before
telling anyone what is available.

Bundled rules content comes from SRD 5.2 under CC-BY-4.0; see the plugin's NOTICE.
"""

server: MCPServer = MCPServer(
    name="souroldgeezer-fivee-sim",
    version=__version__,
    instructions=INSTRUCTIONS,
)


@dataclass(slots=True)
class _Session:
    encounter: Encounter
    rng: Random
    seed: int
    #: Which content generation this fight was built against. An encounter keeps
    #: resolving under the content it started with, so this is how a later
    #: reconfiguration becomes visible rather than mysterious.
    content_generation: int = 0


@dataclass(slots=True)
class _Content:
    """The active registry, replaced wholesale rather than mutated."""

    registry: ContentRegistry
    generation: int = 1
    configured: tuple[str, ...] = ()
    #: Set when content named by the environment could not be loaded at start-up.
    startup_error: str = ""


_SESSIONS: dict[str, _Session] = {}
_NEXT_ID = 0
_CONTENT: _Content | None = None


class ToolError(ValueError):
    """Bad tool input, reported to the caller rather than crashing the server."""


def _content() -> _Content:
    """The active content, loaded from the environment on first use.

    A pack the environment names but that will not load must not take the server
    down with it: the process would fail its handshake and the user would get no
    tools at all, with the reason invisible. Instead the built-in slice loads, the
    failure goes to stderr, and ``content_status`` reports it — so the session works
    and the problem is still discoverable.
    """
    global _CONTENT
    if _CONTENT is None:
        try:
            _CONTENT = _Content(registry=load_packs(builtin=builtin_mode()))
        except ContentError as error:
            print(f"fivee-sim: falling back to bundled content: {error}", file=sys.stderr)
            _CONTENT = _Content(registry=builtin_registry(), startup_error=str(error))
    return _CONTENT


def _registry() -> ContentRegistry:
    return _content().registry


def _mode(value: str | None, *, default: BuiltinMode) -> BuiltinMode:
    if value is None:
        return default
    try:
        return BuiltinMode(value.strip().casefold())
    except ValueError as error:
        allowed = ", ".join(item.value for item in BuiltinMode)
        raise ToolError(f"builtin must be one of: {allowed}") from error


def _new_encounter_id() -> str:
    global _NEXT_ID
    _NEXT_ID += 1
    return f"enc-{_NEXT_ID}"


def _resolve_seed(seed: int | None) -> int:
    """Use the given seed, or pick one and report it so the result stays replayable."""
    if seed is not None:
        return seed
    return random.SystemRandom().randrange(2**31)


def _advantage(value: str | None) -> Advantage:
    try:
        return Advantage(value or "none")
    except ValueError as error:
        allowed = ", ".join(state.value for state in Advantage)
        raise ToolError(f"advantage must be one of: {allowed}") from error


def _session(encounter_id: str) -> _Session:
    session = _SESSIONS.get(encounter_id)
    if session is None:
        known = ", ".join(sorted(_SESSIONS)) or "none"
        raise ToolError(f"unknown encounter {encounter_id!r}; active: {known}")
    return session


def _attack_from_spec(spec: dict[str, Any]) -> AttackOption:
    try:
        return AttackOption(
            name=str(spec["name"]),
            attack_bonus=int(spec["attack_bonus"]),
            damage=Dice.parse(str(spec["damage"])),
            damage_type=DamageType(spec["damage_type"]),
            kind=AttackKind(spec.get("kind", "melee")),
            reach=int(spec.get("reach", 5)),
            normal_range=int(spec.get("normal_range", 0)),
            long_range=int(spec.get("long_range", 0)),
            provenance=str(spec.get("provenance", "caller-supplied")),
        )
    except KeyError as error:
        raise ToolError(f"attack spec is missing {error.args[0]!r}") from error


def _creature_from_spec(spec: dict[str, Any], registry: ContentRegistry) -> Creature:
    """Build a combatant from a loaded stat block or an explicit description.

    ``monster`` and ``creature`` are accepted interchangeably; the stat block is
    looked up in ``registry``, so which names resolve depends on what is loaded.
    """
    named = spec.get("creature", spec.get("monster"))
    if named is not None:
        try:
            return make_creature(
                str(named),
                registry=registry,
                label=spec.get("label"),
                team=spec.get("team"),
                position=int(spec.get("position", 0)),
            )
        except DataError as error:
            raise ToolError(str(error)) from error
    try:
        return Creature(
            name=str(spec["name"]),
            team=str(spec["team"]),
            ac=int(spec["ac"]),
            max_hp=int(spec["max_hp"]),
            hp=int(spec.get("hp", -1)),
            speed=int(spec.get("speed", 30)),
            abilities={
                Ability(key): int(value)
                for key, value in spec.get("abilities", {}).items()
            },
            save_bonuses={
                Ability(key): int(value)
                for key, value in spec.get("save_bonuses", {}).items()
            },
            attacks=tuple(_attack_from_spec(entry) for entry in spec.get("attacks", [])),
            attacks_per_action=int(spec.get("attacks_per_action", 1)),
            spells=tuple(str(name) for name in spec.get("spells", [])),
            spell_slots={int(k): int(v) for k, v in spec.get("spell_slots", {}).items()},
            spell_save_dc=int(spec.get("spell_save_dc", 10)),
            spell_attack_bonus=int(spec.get("spell_attack_bonus", 0)),
            resistances=frozenset(
                DamageType(entry) for entry in spec.get("resistances", [])
            ),
            immunities=frozenset(DamageType(entry) for entry in spec.get("immunities", [])),
            vulnerabilities=frozenset(
                DamageType(entry) for entry in spec.get("vulnerabilities", [])
            ),
            items={str(k): int(v) for k, v in spec.get("items", {}).items()},
            conditions={str(entry) for entry in spec.get("conditions", [])},
            condition_effects=registry.condition_effects,
            position=int(spec.get("position", 0)),
            provenance=str(spec.get("provenance", "caller-supplied")),
        )
    except KeyError as error:
        raise ToolError(
            f"combatant spec is missing {error.args[0]!r}; give either "
            f"{{'creature': '<loaded name>'}} or name/team/ac/max_hp"
        ) from error


def _combatants(specs: list[dict[str, Any]], registry: ContentRegistry) -> list[Creature]:
    if len(specs) < 2:
        raise ToolError("an encounter needs at least two combatants")
    return [_creature_from_spec(spec, registry) for spec in specs]


def _new_encounter(
    combatants: list[Creature], rng: Random, registry: ContentRegistry
) -> Encounter:
    """Build an encounter bound to ``registry``'s tables, captured by value."""
    return Encounter(
        combatants,
        rng,
        spellbook=registry.spells,
        items=registry.items,
        condition_effects=registry.condition_effects,
    )


# --- primitives ------------------------------------------------------------
@server.tool()
def roll(expression: str, advantage: str = "none", seed: int | None = None) -> dict[str, Any]:
    """Roll a dice expression such as "2d6+3" or "d20", optionally with advantage.

    Advantage and disadvantage apply only to a single d20; they are ignored for
    other expressions because the rules attach them to d20 tests.
    """
    used = _resolve_seed(seed)
    rng = Random(used)
    dice = Dice.parse(expression)
    state = _advantage(advantage)
    if dice.count == 1 and dice.faces == 20 and state is not Advantage.NONE:
        d20 = roll_d20(rng, state)
        return {
            "expression": str(dice),
            "seed": used,
            "advantage": state.value,
            "natural": d20.natural,
            "rolls": list(d20.rolls),
            "total": d20.natural + dice.modifier,
            "detail": d20.describe(),
        }
    result = roll_dice(dice, rng)
    return {
        "expression": str(dice),
        "seed": used,
        "advantage": Advantage.NONE.value,
        "rolls": list(result.rolls),
        "total": result.total,
        "detail": result.describe(),
    }


@server.tool()
def check(
    modifier: int,
    dc: int,
    advantage: str = "none",
    seed: int | None = None,
) -> dict[str, Any]:
    """Make an ability check against a DC."""
    used = _resolve_seed(seed)
    test = make_d20_test(Random(used), modifier=modifier, dc=dc, advantage=_advantage(advantage))
    return {
        "seed": used,
        "natural": test.roll.natural,
        "total": test.total,
        "dc": dc,
        "success": test.success,
        "detail": test.describe(),
    }


@server.tool()
def save(
    modifier: int,
    dc: int,
    advantage: str = "none",
    auto_fail: bool = False,
    seed: int | None = None,
) -> dict[str, Any]:
    """Make a saving throw. ``auto_fail`` covers conditions that forfeit the save."""
    used = _resolve_seed(seed)
    test = make_d20_test(
        Random(used),
        modifier=modifier,
        dc=dc,
        advantage=_advantage(advantage),
        auto_fail=auto_fail,
    )
    return {
        "seed": used,
        "natural": test.roll.natural,
        "total": test.total,
        "dc": dc,
        "success": test.success,
        "auto_failed": test.auto_failed,
        "detail": test.describe(),
    }


def _condition_entry(registry: ContentRegistry, name: str) -> dict[str, Any]:
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
        "provenance": str(record.get("provenance", "SRD 5.2")),
        "unmodelled": list(record.get("unmodelled", [])),
    }


def _spell_entry(registry: ContentRegistry, name: str) -> dict[str, Any]:
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
        "radius": spell.radius,
        "range_feet": spell.range_feet,
        "condition": spell.condition,
        "concentration": spell.concentration,
        "source": registry.source_of("spells", name),
        "provenance": spell.provenance,
        "unmodelled": list(record.get("unmodelled", [])),
    }


def _item_entry(registry: ContentRegistry, name: str) -> dict[str, Any]:
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
    }


def _creature_entry(registry: ContentRegistry, name: str) -> dict[str, Any]:
    record = registry.creatures[name]
    entry: dict[str, Any] = {"kind": "creature", **record}
    entry["source"] = registry.source_of("creatures", name)
    # ``unmodelled`` is present even when empty. The skill tells Claude to check it
    # before promising a trait will fire, and that instruction has to stay true for a
    # campaign's own creature rather than hitting a missing key.
    entry.setdefault("unmodelled", [])
    entry.setdefault("provenance", entry["source"])
    return entry


@server.tool()
def lookup_rule(topic: str = "") -> dict[str, Any]:
    """Look up a loaded condition, spell, creature, or item. Omit ``topic`` to list all.

    Searches whatever content is loaded, bundled or not, and every entry names the
    pack it came from in ``source``. A miss means the subject is not loaded — check
    content_status before concluding it does not exist.
    """
    registry = _registry()
    if not topic:
        listing: dict[str, Any] = dict(registry.names())
        listing["builtin"] = registry.builtin.value
        listing["packs"] = [pack.label for pack in registry.packs]
        listing["provenance"] = sorted({pack.provenance for pack in registry.packs})
        return listing
    key = topic.strip().casefold()

    finders = (
        ("conditions", registry.condition_effects, _condition_entry),
        ("spells", registry.spells, _spell_entry),
        ("creatures", registry.creatures, _creature_entry),
        ("items", registry.items, _item_entry),
    )
    for _section, table, build in finders:
        for name in table:
            if name.casefold() == key:
                return build(registry, name)

    raise ToolError(
        f"nothing loaded for {topic!r}. Call lookup_rule with no topic to list what "
        f"is available, or content_status to see which packs are loaded."
    )


# --- stateful encounters ---------------------------------------------------
@server.tool()
def encounter_create(
    combatants: list[dict[str, Any]],
    seed: int | None = None,
) -> dict[str, Any]:
    """Start an encounter and roll initiative.

    Each combatant is either ``{"monster": "Goblin Warrior", "label": "Goblin A",
    "team": "monsters", "position": 15}`` for a bundled stat block, or an explicit
    description with at least name, team, ac, and max_hp. Names must be unique —
    they identify combatants in every later call. Positions are feet on one axis.
    """
    used = _resolve_seed(seed)
    rng = Random(used)
    content = _content()
    try:
        encounter = _new_encounter(
            _combatants(combatants, content.registry), rng, content.registry
        )
    except EncounterError as error:
        raise ToolError(str(error)) from error
    encounter_id = _new_encounter_id()
    _SESSIONS[encounter_id] = _Session(
        encounter=encounter, rng=rng, seed=used,
        content_generation=content.generation,
    )
    result: dict[str, Any] = {
        "encounter_id": encounter_id,
        "seed": used,
        "content_generation": content.generation,
        "state": encounter.state(),
        "log": [event.as_dict() for event in encounter.log],
    }
    if content.startup_error:
        result["content_warning"] = (
            "configured content failed to load; this fight uses the bundled slice "
            "only. See content_status."
        )
    return result


@server.tool()
def encounter_state(encounter_id: str) -> dict[str, Any]:
    """The authoritative state of an encounter. Narrate from this, not from memory."""
    return _session(encounter_id).encounter.state()


@server.tool()
def encounter_act(
    encounter_id: str,
    kind: str,
    target: str | None = None,
    attack: str | None = None,
    item: str | None = None,
    spell: str | None = None,
    slot_level: int | None = None,
    to_position: int | None = None,
    targets: list[str] | None = None,
    center: int | None = None,
) -> dict[str, Any]:
    """Take an action for the creature whose turn it is.

    ``kind`` is attack, cast, use_item, move, dash, disengage, or dodge. Attacks need
    ``target``; casting needs ``spell`` plus either ``target``/``targets`` or a
    ``center`` for an area; using an item needs ``item``, and ``target`` unless the
    item is self-directed; moving needs ``to_position``. Illegal actions are refused
    with the reason rather than silently adjusted.
    """
    session = _session(encounter_id)
    try:
        action_kind = ActionKind(kind)
    except ValueError as error:
        allowed = ", ".join(item.value for item in ActionKind)
        raise ToolError(f"kind must be one of: {allowed}") from error
    action = Action(
        kind=action_kind,
        target=target,
        attack=attack,
        item=item,
        spell=spell,
        slot_level=slot_level,
        to_position=to_position,
        targets=tuple(targets or ()),
        center=center,
    )
    try:
        events = session.encounter.act(action, session.rng)
    except EncounterError as error:
        raise ToolError(str(error)) from error
    return {
        "events": [event.as_dict() for event in events],
        "state": session.encounter.state(),
    }


@server.tool()
def encounter_advance(encounter_id: str) -> dict[str, Any]:
    """End the current turn and begin the next, rolling any death saves that are due."""
    session = _session(encounter_id)
    events = session.encounter.advance(session.rng)
    return {
        "events": [event.as_dict() for event in events],
        "state": session.encounter.state(),
    }


# --- content ---------------------------------------------------------------
def _status() -> dict[str, Any]:
    content = _content()
    stale = [
        {"encounter_id": name, "content_generation": session.content_generation}
        for name, session in sorted(_SESSIONS.items())
        if session.content_generation != content.generation
    ]
    status: dict[str, Any] = {
        "generation": content.generation,
        "configured_paths": list(content.configured),
        "environment": {
            CONTENT_ENV: environment_paths() or None,
            BUILTIN_ENV: content.registry.builtin.value,
        },
        **content.registry.summary(),
    }
    if stale:
        # A fight keeps the content it started with, so this is not a fault — it is
        # the divergence made visible, which is the only way narration stays honest.
        status["encounters_on_older_content"] = stale
    if content.startup_error:
        status["startup_error"] = content.startup_error
    return status


@server.tool()
def content_status() -> dict[str, Any]:
    """What content is loaded, from where, and under which mode.

    Use this before telling anyone what the engine supports: with packs loaded, or
    with the bundled slice excluded, the answer is whatever this reports and not what
    ships by default. It also names any encounter still running on older content.
    """
    return _status()


@server.tool()
def content_validate(
    paths: list[str] | None = None,
    builtin: str | None = None,
) -> dict[str, Any]:
    """Report problems with content packs without loading them. The authoring aid.

    Give ``paths`` to check specific files or directories, or omit it to re-check what
    is currently configured. Every diagnostic names the pack, section, record, and
    field, and separates errors from warnings.
    """
    content = _content()
    candidates = list(paths) if paths is not None else list(content.configured)
    diagnostics = _validate_content(
        candidates, builtin=_mode(builtin, default=content.registry.builtin)
    )
    errors = [d for d in diagnostics if d.severity.value == "error"]
    warnings = [d for d in diagnostics if d.severity.value == "warning"]
    return {
        "checked": candidates,
        "builtin": _mode(builtin, default=content.registry.builtin).value,
        "ok": not errors,
        "errors": [d.as_dict() for d in errors],
        "warnings": [d.as_dict() for d in warnings],
        "summary": (
            "no problems found" if not diagnostics
            else f"{len(errors)} error(s), {len(warnings)} warning(s)"
        ),
    }


@server.tool()
def content_configure(
    paths: list[str] | None = None,
    builtin: str | None = None,
    add: bool = False,
) -> dict[str, Any]:
    """Load content packs and/or switch whether the bundled SRD slice is included.

    ``paths`` names files or directories of ``*.json`` packs and replaces the current
    set unless ``add`` is true. ``builtin`` is "include" or "exclude"; omit either
    argument to leave it as it is.

    Nothing changes unless the new content loads cleanly — a failed call reports every
    diagnostic and leaves the working content in place. Encounters already in progress
    keep resolving under the content they started with; only new ones use the result.
    """
    content = _content()
    if paths is None and builtin is None:
        raise ToolError("give 'paths', 'builtin', or both — there is nothing to change")
    mode = _mode(builtin, default=content.registry.builtin)
    if paths is None:
        configured = list(content.configured)
    elif add:
        configured = [*content.configured, *paths]
    else:
        configured = list(paths)

    try:
        registry = load_packs(configured, builtin=mode)
    except ContentError as error:
        raise ToolError(
            f"content not changed. {error}"
        ) from error

    global _CONTENT
    _CONTENT = _Content(
        registry=registry,
        generation=content.generation + 1,
        configured=tuple(configured),
        startup_error="",
    )
    return {"changed": True, **_status()}


# --- analytics -------------------------------------------------------------
@server.tool()
def simulate_rounds(
    combatants: list[dict[str, Any]],
    iterations: int = 500,
    seed: int = 0,
    max_rounds: int = 20,
) -> dict[str, Any]:
    """Auto-play the same encounter many times and report win rates and length.

    Combatant specs match ``encounter_create``. Iteration ``i`` uses ``seed + i``,
    so one iteration reproduces a single hand-played encounter at that seed.
    """
    specs = list(combatants)
    # The registry is captured once, here. Resolving content per iteration would let
    # a reconfiguration land mid-batch and make the result unreproducible from its
    # seed, which is the one property these numbers rest on.
    registry = _registry()

    def factory() -> list[Creature]:
        return _combatants(specs, registry)

    try:
        return _simulate_rounds(
            factory,
            iterations=iterations,
            seed=seed,
            max_rounds=max_rounds,
            spellbook=dict(registry.spells),
            items=dict(registry.items),
            condition_effects=registry.condition_effects,
        )
    except (ValueError, EncounterError) as error:
        raise ToolError(str(error)) from error


@server.tool()
def simulate_dpr(
    build: dict[str, Any],
    target_ac: int,
    rounds: int = 3,
    iterations: int = 1000,
    seed: int = 0,
) -> dict[str, Any]:
    """Measure the damage a build lands over several rounds against a given AC.

    The target is a passive dummy with enough hit points to absorb the whole run,
    driven through the real encounter stepper — so advantage, criticals, and
    resistances apply exactly as they would in play.
    """
    spec = dict(build)
    registry = _registry()

    def attacker() -> Creature:
        creature = _creature_from_spec(spec, registry)
        creature.team = "attacker"
        return creature

    try:
        return _simulate_dpr(
            attacker,
            target_ac=target_ac,
            rounds=rounds,
            iterations=iterations,
            seed=seed,
            spellbook=dict(registry.spells),
            items=dict(registry.items),
            condition_effects=registry.condition_effects,
        )
    except (ValueError, EncounterError) as error:
        raise ToolError(str(error)) from error


def main() -> None:
    """Entry point for the stdio server."""
    server.run("stdio")


if __name__ == "__main__":
    main()
