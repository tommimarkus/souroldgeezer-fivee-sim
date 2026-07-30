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
from dataclasses import dataclass
from random import Random
from typing import Any

from mcp.server.mcpserver import MCPServer

from .. import __version__
from ..analytics.montecarlo import simulate_dpr as _simulate_dpr
from ..analytics.montecarlo import simulate_rounds as _simulate_rounds
from ..data import DataError, make_monster, monster_names, monster_records, spellbook
from ..kernel.actions import AttackKind
from ..kernel.conditions import EFFECTS, Condition
from ..kernel.dice import Advantage, Dice, roll_d20, roll_dice
from ..kernel.rules import Ability, DamageType, make_d20_test
from ..model.creature import AttackOption, Creature
from ..model.encounter import Action, ActionKind, Encounter, EncounterError

INSTRUCTIONS = """\
A 5E-compatible combat engine. The engine owns the fight: hit points, initiative
order, conditions, and dice are computed here, so read encounter_state as
authoritative and narrate from it rather than tracking state yourself.

Rules content comes from SRD 5.2 under CC-BY-4.0; see the plugin's NOTICE.
"""

server: MCPServer = MCPServer(
    name="fivee-sim",
    version=__version__,
    instructions=INSTRUCTIONS,
)


@dataclass(slots=True)
class _Session:
    encounter: Encounter
    rng: Random
    seed: int


_SESSIONS: dict[str, _Session] = {}
_NEXT_ID = 0


class ToolError(ValueError):
    """Bad tool input, reported to the caller rather than crashing the server."""


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


def _creature_from_spec(spec: dict[str, Any]) -> Creature:
    """Build a combatant from a bundled stat block or an explicit description."""
    if "monster" in spec:
        try:
            return make_monster(
                str(spec["monster"]),
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
            position=int(spec.get("position", 0)),
            provenance=str(spec.get("provenance", "caller-supplied")),
        )
    except KeyError as error:
        raise ToolError(
            f"combatant spec is missing {error.args[0]!r}; give either "
            f"{{'monster': '<bundled name>'}} or name/team/ac/max_hp"
        ) from error


def _combatants(specs: list[dict[str, Any]]) -> list[Creature]:
    if len(specs) < 2:
        raise ToolError("an encounter needs at least two combatants")
    return [_creature_from_spec(spec) for spec in specs]


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


@server.tool()
def lookup_rule(topic: str = "") -> dict[str, Any]:
    """Look up a bundled condition, spell, or stat block. Omit ``topic`` to list everything.

    Only SRD 5.2 content is bundled, so a miss usually means the subject is outside
    the SRD rather than misspelled.
    """
    if not topic:
        return {
            "conditions": sorted(condition.value for condition in Condition),
            "spells": sorted(spellbook()),
            "monsters": monster_names(),
            "provenance": "SRD 5.2",
        }
    key = topic.strip().casefold()

    for condition in Condition:
        if condition.value == key:
            effect = EFFECTS[condition]
            return {
                "kind": "condition",
                "name": condition.value,
                "effects": {
                    field: getattr(effect, field)
                    for field in effect.__dataclass_fields__
                    if getattr(effect, field)
                } or {"note": "no combat-roll consequences"},
                "provenance": "SRD 5.2",
            }

    for name, spell in spellbook().items():
        if name.casefold() == key:
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
                "condition": spell.condition.value if spell.condition else None,
                "concentration": spell.concentration,
                "provenance": spell.provenance,
            }

    for name, record in monster_records().items():
        if name.casefold() == key:
            return {"kind": "monster", **record}

    raise ToolError(
        f"nothing bundled for {topic!r}. Call lookup_rule with no topic to list "
        f"what is available; only SRD 5.2 content ships with this engine."
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
    try:
        encounter = Encounter(_combatants(combatants), rng, spellbook=spellbook())
    except EncounterError as error:
        raise ToolError(str(error)) from error
    encounter_id = _new_encounter_id()
    _SESSIONS[encounter_id] = _Session(encounter=encounter, rng=rng, seed=used)
    return {
        "encounter_id": encounter_id,
        "seed": used,
        "state": encounter.state(),
        "log": [event.as_dict() for event in encounter.log],
    }


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
    spell: str | None = None,
    slot_level: int | None = None,
    to_position: int | None = None,
    targets: list[str] | None = None,
    center: int | None = None,
) -> dict[str, Any]:
    """Take an action for the creature whose turn it is.

    ``kind`` is attack, cast, move, dash, disengage, or dodge. Attacks need
    ``target``; casting needs ``spell`` plus either ``target``/``targets`` or a
    ``center`` for an area; moving needs ``to_position``. Illegal actions are
    refused with the reason rather than silently adjusted.
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

    def factory() -> list[Creature]:
        return _combatants(specs)

    try:
        return _simulate_rounds(
            factory,
            iterations=iterations,
            seed=seed,
            max_rounds=max_rounds,
            spellbook=spellbook(),
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

    def attacker() -> Creature:
        creature = _creature_from_spec(spec)
        creature.team = "attacker"
        return creature

    try:
        return _simulate_dpr(
            attacker,
            target_ac=target_ac,
            rounds=rounds,
            iterations=iterations,
            seed=seed,
            spellbook=spellbook(),
        )
    except (ValueError, EncounterError) as error:
        raise ToolError(str(error)) from error


def main() -> None:
    """Entry point for the stdio server."""
    server.run("stdio")


if __name__ == "__main__":
    main()
