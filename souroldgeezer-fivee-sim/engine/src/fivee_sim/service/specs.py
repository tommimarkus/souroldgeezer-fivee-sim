"""The wire shapes, translated into engine objects. The anti-corruption edge.

Everything here answers one question: *what did the caller mean?* A combatant
spec becomes a :class:`~fivee_sim.model.creature.Creature`, a map spec a
:class:`~fivee_sim.map_document.MapDocument`, a journal record's arguments an
:class:`~fivee_sim.model.encounter.Action`. Nothing here decides a rule, and
nothing here holds state — this module changes when the shape a caller sends
changes, and at no other time, which is why it is its own boundary rather than
part of the tool bodies that call it.

Refusal is the other half of the job. A key nothing reads is rejected rather
than ignored, because a key read with ``.get`` and a default cannot tell
"omitted" from "misspelled": the caller gets a creature that is not the one
they described and nothing says so.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields
from enum import Enum
from types import MappingProxyType
from typing import Any, TypeVar

from ..content import ContentRegistry, DataError, make_creature
from ..kernel.actions import AttackKind, RiderExpiry
from ..kernel.dice import Advantage, Dice
from ..kernel.grid import DiagonalRule, Facing, MovementMode, Point, TerrainTable
from ..kernel.rules import Ability, DamageType, Size
from ..map_document import (
    DOOR_ORIENTATIONS,
    MAX_MAP_BYTES,
    MapDocument,
    MapFeatureRecord,
    MapProvenance,
    as_payload,
)
from ..model.battlemap import TerrainPair
from ..model.creature import AttackOption, Creature, DeathRule
from ..model.encounter import Action, ActionKind
from .common import resolve_seed
from .errors import RequestError

_EnumT = TypeVar("_EnumT", bound=Enum)

__all__ = [
    "ATTACK_SPEC_KEYS",
    "DESCRIBED_SPEC_KEYS",
    "FEATURE_KEYS",
    "LOOKUP_SPEC_KEYS",
    "MAP_KEYS",
    "MAX_MAP_SQUARES",
    "action_from_journal",
    "attack_from_spec",
    "checked_seed",
    "combatants_from_specs",
    "creature_from_spec",
    "document_from_spec",
    "parse_advantage",
    "parse_carried_flag",
    "parse_death_saves",
    "parse_map_dimension",
    "parse_movement_rule",
    "parse_point",
    "parse_square",
    "reject_unknown_keys",
]


def checked_seed(seed: int | None) -> int:
    """The seed to use, refusing one the JSON side of a caller cannot carry."""
    try:
        return resolve_seed(seed)
    except ValueError as error:
        raise RequestError(str(error)) from error


def parse_advantage(value: str | None) -> Advantage:
    try:
        return Advantage(value or "none")
    except ValueError as error:
        allowed = ", ".join(state.value for state in Advantage)
        raise RequestError(f"advantage must be one of: {allowed}") from error


def parse_movement_rule(value: str) -> DiagonalRule:
    try:
        return DiagonalRule(value)
    except ValueError as error:
        allowed = ", ".join(rule.value for rule in DiagonalRule)
        raise RequestError(f"movement_rule must be one of: {allowed}") from error


def parse_point(value: int | list[int], what: str) -> Point | int:
    """Accept a position as feet along the x-axis or as an ``[x, y]`` pair."""
    if isinstance(value, int):
        return value
    if (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(part, int) and not isinstance(part, bool) for part in value)
    ):
        return (value[0], value[1])
    raise RequestError(f"{what} must be feet along the x-axis or an [x, y] pair of feet")


#: Every key an attack spec may carry, derived from the record it builds rather
#: than listed here. A hand-kept copy is what let ``range`` sit on the bundled
#: pregen sheets unread: two lists drift, and the one nobody runs drifts first.
#: ``tests/test_specs.py`` holds this against the keys ``attack_from_spec``
#: actually reads, so a field added to :class:`AttackOption` and forgotten here
#: fails rather than becoming a key that is accepted and dropped.
ATTACK_SPEC_KEYS = frozenset(field.name for field in fields(AttackOption))


def attack_from_spec(spec: dict[str, Any]) -> AttackOption:
    # The combatant around this attack has had its keys checked since the
    # ``fly_speed`` stirge; the attack inside it had not, and carries sixteen
    # optional keys read with ``.get`` and a default. ``range`` for
    # ``normal_range`` cost every shipped pregen its ranged weapon — built with
    # a maximum range of 0 ft, refused at every distance, silently.
    reject_unknown_keys(spec, ATTACK_SPEC_KEYS, noun="attack")
    bonus_type = spec.get("bonus_damage_type")
    save_ability = spec.get("on_hit_save_ability")
    max_size = spec.get("on_hit_max_size")
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
            bonus_damage=(
                Dice.parse(str(spec["bonus_damage"]))
                if spec.get("bonus_damage") is not None else None
            ),
            bonus_damage_type=(
                DamageType(bonus_type) if bonus_type is not None else None
            ),
            advantage_bonus_damage=(
                Dice.parse(str(spec["advantage_bonus_damage"]))
                if spec.get("advantage_bonus_damage") is not None else None
            ),
            advantage_bonus_with_adjacent_ally=bool(
                spec.get("advantage_bonus_with_adjacent_ally", False)
            ),
            on_hit_condition=(
                str(spec["on_hit_condition"])
                if spec.get("on_hit_condition") is not None else None
            ),
            on_hit_save_ability=(
                Ability(save_ability) if save_ability is not None else None
            ),
            on_hit_save_dc=int(spec.get("on_hit_save_dc", 0)),
            on_hit_expiry=RiderExpiry(spec.get("on_hit_expiry", "none")),
            on_hit_max_size=Size(max_size) if max_size is not None else None,
            on_hit_attach=bool(spec.get("on_hit_attach", False)),
            attached_damage=(
                Dice.parse(str(spec["attached_damage"]))
                if spec.get("attached_damage") is not None else None
            ),
            attached_damage_type=(
                DamageType(spec["attached_damage_type"])
                if spec.get("attached_damage_type") is not None else None
            ),
            detach_after_damage=int(spec.get("detach_after_damage", 0)),
            ammunition=(
                str(spec["ammunition"])
                if spec.get("ammunition") is not None else None
            ),
            loading=bool(spec.get("loading", False)),
            thrown=bool(spec.get("thrown", False)),
            provenance=str(spec.get("provenance", "caller-supplied")),
        )
    except KeyError as error:
        raise RequestError(f"attack spec is missing {error.args[0]!r}") from error
    except ValueError as error:
        raise RequestError(f"attack spec is invalid: {error}") from error


#: The two combatant spec shapes, kept apart because the lookup branch returns
#: before the constructor is reached and so reads none of the description keys —
#: folding them into one set would accept ``{"monster": "...", "ac": 22}`` and
#: silently ignore the AC, which is the very failure this guard exists to stop.
LOOKUP_SPEC_KEYS = frozenset({
    "creature", "monster", "label", "team", "position", "level", "arrival_round",
    "facing",
})
DESCRIBED_SPEC_KEYS = frozenset({
    "name", "team", "ac", "max_hp", "hp", "speed", "climb_speed", "swim_speed",
    "fly_speed", "terrain_cost_overrides", "darkvision", "blindsight", "death_rule",
    "size", "abilities", "save_bonuses", "attacks", "attacks_per_action",
    "bonus_actions", "surrender_when_last", "redirect_attack", "pack_tactics",
    "undead_fortitude", "spells",
    "spell_slots", "spell_save_dc", "spell_attack_bonus", "spellcasting_ability",
    "initiative_bonus",
    "resistances", "immunities", "condition_immunities",
    "vulnerabilities", "items", "conditions", "position", "level", "arrival_round",
    "provenance", "facing",
    # Carried-over state: the condition a combatant walked out of the *previous*
    # fight in. Every one of these is reported by ``Encounter.state()`` and was,
    # until adventures spanned more than one encounter, reportable and not
    # settable — so a party that ended a fight with someone stabilised at 0 hit
    # points started the next one with everybody upright.
    #
    # Named and shaped exactly as the state payload emits them, ``death_saves``
    # object included, because a caller who has to reshape what they were handed
    # before handing it back is a caller who will reshape it wrong.
    "death_saves", "stable", "dead", "surrendered",
})

#: The two counters a ``death_saves`` object carries, as ``Encounter.state()``
#: spells them.
_DEATH_SAVE_KEYS = frozenset({"successes", "failures"})


#: The eight names a facing may take, as plain strings — the model keeps facing
#: as a ``str`` for the same reason it keeps a condition as one.
_FACING_NAMES = frozenset(str(member) for member in Facing)


def reject_unknown_keys(
    spec: dict[str, Any], allowed: frozenset[str], *, noun: str = "combatant"
) -> None:
    """Refuse a key nothing reads, the way every other spec already does.

    ``content.py`` refuses an unknown pack key and ``_map_from_spec`` an unknown
    map key, both for one reason: a key read with ``.get`` and a default cannot
    tell "omitted" from "misspelled", so the caller gets a creature that is not the
    one they described and nothing says so. An inline ``fly_speed`` produced a
    stirge that walked 10 feet with no flight and no warning; ``speeed`` would have
    produced the default 30 just as quietly.

    ``noun`` names the spec being checked. It matters because the two shapes
    nest: ``range`` is absent from the combatant keys *and* from the attack
    keys, so one shared message would send a reader hunting the wrong list.
    """
    for key in sorted(set(spec) - allowed):
        raise RequestError(
            f"unknown {noun} key {key!r}. Valid keys: {', '.join(sorted(allowed))}"
        )


def parse_facing(value: Any) -> str | None:
    """One of the eight grid directions, or ``None`` for untracked.

    Refused rather than coerced: a misspelled facing that silently became
    ``None`` would leave a creature untracked while its author believed it was
    looking somewhere, which is the same class of silent drop that
    :func:`reject_unknown_keys` exists to stop.
    """
    if value is None:
        return None
    named = str(value)
    if named not in _FACING_NAMES:
        raise RequestError(
            f"facing must be one of the eight directions, got {named!r}. "
            f"Valid: {', '.join(sorted(_FACING_NAMES))}"
        )
    return named


def parse_natural(value: Any) -> tuple[int, ...]:
    """The d20 faces a caller rolled themselves, normalised to a tuple.

    One face as a bare integer, two as a list — the same shape ``to_position``
    takes, because ``--natural 17`` and ``--natural '[17, 4]'`` is a grammar the
    client already has. ``None`` is *you roll it* and normalises to the empty
    tuple.

    Range and count belong to the roll rather than to parsing: how many faces a
    roll takes depends on advantage, which is not known until the action is
    resolved. So this refuses only what is not a face at all, and
    :func:`~fivee_sim.kernel.dice.roll_d20` refuses the rest.
    """
    if value is None:
        return ()
    faces = value if isinstance(value, list | tuple) else [value]
    parsed: list[int] = []
    for face in faces:
        if isinstance(face, bool) or not isinstance(face, int):
            raise RequestError(
                f"a reported d20 face must be a whole number, got {face!r}"
            )
        parsed.append(face)
    return tuple(parsed)


def parse_death_saves(value: Any) -> tuple[int, int]:
    """The successes and failures a combatant arrives already holding.

    Shaped ``{"successes": N, "failures": N}`` — what ``Encounter.state()``
    reports — so carrying a combatant from one fight to the next means handing
    the reported object straight back rather than unpacking it into two keys.

    Refused rather than coerced, for :func:`parse_facing`'s reason and with more
    at stake: a malformed count taken as ``0`` would put a creature two failures
    from death back at three away from it, and nothing would say so. There is
    deliberately no *upper* bound — a critical hit adds two failures at once, so
    a fight can and does report four of them.
    """
    if value is None:
        return (0, 0)
    if not isinstance(value, Mapping):
        raise RequestError(
            f"death_saves must be an object of successes and failures, got {value!r}"
        )
    for key in sorted(set(value) - _DEATH_SAVE_KEYS):
        raise RequestError(
            f"unknown death_saves key {key!r}. Valid keys: "
            f"{', '.join(sorted(_DEATH_SAVE_KEYS))}"
        )
    counts: list[int] = []
    for key in ("successes", "failures"):
        count = value.get(key, 0)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise RequestError(
                f"death_saves {key} must be a whole number of 0 or more, got {count!r}"
            )
        counts.append(count)
    return (counts[0], counts[1])


def parse_carried_flag(value: Any, key: str) -> bool:
    """One of the carried-over true/false states, refused rather than coerced.

    ``bool(...)`` is what the description branch does with a stat-block trait,
    and it is the wrong tool for these three. ``bool("false")`` is ``True``, so
    the one word a caller might reach for to say *not stable* would be read as
    *stable* — and ``stable`` is the flag that decides whether a creature at 0
    hit points is bleeding out or merely down.
    """
    if not isinstance(value, bool):
        raise RequestError(f"{key} must be true or false, got {value!r}")
    return value


def _closed(kind: type[_EnumT], value: Any, key: str) -> _EnumT:
    """Coerce ``value`` into a closed vocabulary, naming the key when it is not.

    Every one of these used to reach the enum raw, so a caller's typo left as an
    uncaught ``ValueError`` and the adapter could only render it as a 500
    ``internal`` — the engine reporting its own failure for the caller's bad
    request, and naming neither the field nor what would have been accepted.
    """
    try:
        return kind(value)
    except ValueError as error:
        allowed = ", ".join(sorted(member.value for member in kind))
        raise RequestError(
            f"combatant key {key!r} does not accept {value!r}; "
            f"valid values: {allowed}"
        ) from error


def creature_from_spec(spec: dict[str, Any], registry: ContentRegistry) -> Creature:
    """Build a combatant from a loaded stat block or an explicit description.

    ``monster`` and ``creature`` are accepted interchangeably; the stat block is
    looked up in ``registry``, so which names resolve depends on what is loaded.
    """
    named = spec.get("creature", spec.get("monster"))
    if named is not None:
        reject_unknown_keys(spec, LOOKUP_SPEC_KEYS)
    else:
        reject_unknown_keys(spec, DESCRIBED_SPEC_KEYS)
    if named is not None:
        try:
            looked_up = make_creature(
                str(named),
                registry=registry,
                label=spec.get("label"),
                team=spec.get("team"),
                position=parse_point(spec.get("position", 0), "position"),
                level=int(spec.get("level", 0)),
                arrival_round=int(spec.get("arrival_round", 1)),
            )
        except DataError as error:
            raise RequestError(str(error)) from error
        # Set after construction rather than threaded through make_creature:
        # facing is scenario placement like position, not a fact the stat block
        # carries, and content.py builds creatures for callers who have no
        # scenario at all.
        looked_up.facing = parse_facing(spec.get("facing"))
        return looked_up
    bonus_actions = frozenset(str(value) for value in spec.get("bonus_actions", []))
    unsupported_bonus_actions = sorted(bonus_actions - {"dash", "disengage"})
    if unsupported_bonus_actions:
        raise RequestError(
            "bonus_actions must contain only dash or disengage; got: "
            + ", ".join(unsupported_bonus_actions)
        )
    death_save_successes, death_save_failures = parse_death_saves(spec.get("death_saves"))
    try:
        name_str = str(spec["name"])
        max_hp_value = int(spec["max_hp"])
        hp_value = int(spec.get("hp", -1))
        if hp_value > max_hp_value:
            raise RequestError(
                f"combatant {name_str}: hp {hp_value} cannot exceed max_hp {max_hp_value}"
            )
        return Creature(
            name=name_str,
            team=str(spec["team"]),
            ac=int(spec["ac"]),
            max_hp=max_hp_value,
            hp=hp_value,
            speed=int(spec.get("speed", 30)),
            climb_speed=int(spec.get("climb_speed", 0)),
            swim_speed=int(spec.get("swim_speed", 0)),
            fly_speed=int(spec.get("fly_speed", 0)),
            terrain_cost_overrides=frozenset(
                str(value) for value in spec.get("terrain_cost_overrides", [])
            ),
            darkvision=int(spec.get("darkvision", 0)),
            blindsight=int(spec.get("blindsight", 0)),
            facing=parse_facing(spec.get("facing")),
            # Read here rather than only accepted above: a key on the allow-list
            # that no constructor consumes is the same silent drop by another
            # route. Size gates attack riders like the Wolf's Prone.
            size=_closed(Size, spec["size"], "size") if "size" in spec else Size.MEDIUM,
            abilities={
                _closed(Ability, key, "abilities"): int(value)
                for key, value in spec.get("abilities", {}).items()
            },
            save_bonuses={
                _closed(Ability, key, "save_bonuses"): int(value)
                for key, value in spec.get("save_bonuses", {}).items()
            },
            attacks=tuple(attack_from_spec(entry) for entry in spec.get("attacks", [])),
            attacks_per_action=int(spec.get("attacks_per_action", 1)),
            bonus_actions=bonus_actions,
            surrender_when_last=bool(spec.get("surrender_when_last", False)),
            redirect_attack=bool(spec.get("redirect_attack", False)),
            pack_tactics=bool(spec.get("pack_tactics", False)),
            undead_fortitude=bool(spec.get("undead_fortitude", False)),
            spells=tuple(str(name) for name in spec.get("spells", [])),
            spell_slots={int(k): int(v) for k, v in spec.get("spell_slots", {}).items()},
            spell_save_dc=int(spec.get("spell_save_dc", 10)),
            spell_attack_bonus=int(spec.get("spell_attack_bonus", 0)),
            spellcasting_ability=(
                _closed(Ability, spec["spellcasting_ability"], "spellcasting_ability")
                if spec.get("spellcasting_ability") is not None
                else None
            ),
            initiative_bonus=(
                int(spec["initiative_bonus"])
                if spec.get("initiative_bonus") is not None
                else None
            ),
            resistances=frozenset(
                _closed(DamageType, entry, "resistances")
                for entry in spec.get("resistances", [])
            ),
            immunities=frozenset(
                _closed(DamageType, entry, "immunities")
                for entry in spec.get("immunities", [])
            ),
            vulnerabilities=frozenset(
                _closed(DamageType, entry, "vulnerabilities")
                for entry in spec.get("vulnerabilities", [])
            ),
            condition_immunities=frozenset(
                str(entry) for entry in spec.get("condition_immunities", [])
            ),
            items={str(k): int(v) for k, v in spec.get("items", {}).items()},
            conditions={str(entry) for entry in spec.get("conditions", [])},
            condition_effects=registry.condition_effects,
            position=parse_point(spec.get("position", 0), "position"),
            level=int(spec.get("level", 0)),
            arrival_round=int(spec.get("arrival_round", 1)),
            death_rule=_closed(
                DeathRule, spec.get("death_rule", DeathRule.DEATH_SAVES), "death_rule"
            ),
            # Carried over from the previous fight. Defaulted to a creature who
            # has not been in one yet, so every spec written before adventures
            # spanned encounters builds the creature it always did.
            death_save_successes=death_save_successes,
            death_save_failures=death_save_failures,
            stable=parse_carried_flag(spec.get("stable", False), "stable"),
            dead=parse_carried_flag(spec.get("dead", False), "dead"),
            surrendered=parse_carried_flag(spec.get("surrendered", False), "surrendered"),
            provenance=str(spec.get("provenance", "caller-supplied")),
        )
    except KeyError as error:
        raise RequestError(
            f"combatant spec is missing {error.args[0]!r}; give either "
            f"{{'creature': '<loaded name>'}} or name/team/ac/max_hp"
        ) from error


def combatants_from_specs(
    specs: list[dict[str, Any]], registry: ContentRegistry
) -> list[Creature]:
    """Translate every combatant, *then* check there are enough of them.

    Shape before arity, and the order is the whole of it. A caller's first probe
    at an encounter is one combatant, so counting first meant the least useful
    answer went to the most common mistake: a spec with no ``ac`` was told "an
    encounter needs at least two combatants" — the one thing the caller already
    knew — and had to add a second combatant before the engine would admit which
    field was missing. An empty list is the single case with nothing to
    diagnose, and it still gets the count, because there is no spec to complain
    about.
    """
    built = [creature_from_spec(spec, registry) for spec in specs]
    if len(built) < 2:
        raise RequestError("an encounter needs at least two combatants")
    return built


MAP_KEYS = frozenset({
    "name", "width", "height", "default_terrain", "rows", "legend", "terrain",
    "default_elevation", "elevation", "features",
})
#: What an inline feature may say. ``orientation`` and ``linked_to`` reach no
#: rule a fight resolves — a door blocks its square the same way whichever way it
#: hangs — but the journal and a v2 replay bundle both capture an inline spec as
#: a map *document*, and that format refuses a door that does not say how it
#: hangs. Without these keys a caller could not write a recoverable spec at all,
#: and a linked pair was unreachable: ``Encounter._adopt_map`` requires both
#: leaves to share a horizontal or vertical orientation, which no spec could
#: give them.
FEATURE_KEYS = frozenset({
    "name", "square", "kind", "initially_open", "closed_terrain", "open_terrain",
    "orientation", "linked_to",
})
#: An inline map is authored by hand or by a model, not generated; this bound only
#: exists so a malformed spec fails with a size complaint instead of an allocation.
MAX_MAP_SQUARES = 512
#: What the document a spec builds says about where it came from. Unchanged from
#: the payload ``replay.battle_map_payload`` used to synthesise, so a v2 bundle
#: exported before this collapse and one exported after say the same thing about
#: the same fight.
INLINE_PROVENANCE = MapProvenance(
    generator="inline",
    seed=0,
    params=MappingProxyType({}),
    edited=False,
    source="Caller-supplied inline map",
)


def parse_square(value: Any, what: str, width: int, height: int) -> tuple[int, int]:
    if not (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(part, int) and not isinstance(part, bool) for part in value)
    ):
        raise RequestError(f"{what} must be an [x, y] pair of squares")
    x, y = value
    if not (0 <= x < width and 0 <= y < height):
        raise RequestError(
            f"{what} is [{x}, {y}], outside the {width}x{height} map"
        )
    return (x, y)


def parse_map_dimension(spec: dict[str, Any], key: str) -> int:
    value = spec.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RequestError(f"map {key} is required and must be a whole number of squares")
    if not 1 <= value <= MAX_MAP_SQUARES:
        raise RequestError(f"map {key} must be between 1 and {MAX_MAP_SQUARES}, got {value}")
    return value


def document_from_spec(spec: dict[str, Any], terrain_table: TerrainTable) -> MapDocument:
    """Build a :class:`MapDocument` from the inline tool spec, refusing precisely.

    Terrain is authored either as ``rows`` of characters with a ``legend`` — the
    form a person or a model writes by hand — or as a ``terrain`` list of
    ``{"kind", "squares"}`` entries.

    A **document** and not a battle map, because there is one map format and a
    spec is a shorthand for writing one, not a second kind of map. A fight gets
    its grid from :func:`~fivee_sim.map_document.to_grid` here exactly as a saved
    file does, and the encounter journal captures the document rather than a
    payload re-synthesised out of the grid afterwards — which is what used to
    lose every key ``to_grid`` has no slot for.

    Two things that follow from that, and are the whole of why this function is
    longer than a translation:

    *Terrain kinds are resolved here*, against the table the caller's content
    defines, and the refusal names the kind the caller typed. It would otherwise
    fall out of :func:`~fivee_sim.map_document.parse_document` naming the
    *glyph* this function invented for it — a character the author never wrote,
    in a document they never sent.

    *The document is sized here*, for the same reason: a spec that densifies
    past :data:`~fivee_sim.map_document.MAX_MAP_BYTES` would start its fight and
    then fail to come back from its own journal. ``MAX_MAP_SQUARES`` bounds the
    grid and nothing bounds a height layer, so the two caps are not the same cap
    and only the serialised bytes can answer.
    """
    for key in sorted(set(spec) - MAP_KEYS):
        raise RequestError(
            f"unknown map key {key!r}. Valid keys: {', '.join(sorted(MAP_KEYS))}"
        )
    width = parse_map_dimension(spec, "width")
    height = parse_map_dimension(spec, "height")
    map_name = str(spec.get("name", "battle map"))
    if not map_name.strip():
        # The format asks every map for a name, and journal recovery re-reads
        # what this writes. An empty one used to build a fight that could not be
        # resumed rather than a spec that could not be written.
        raise RequestError("map 'name' must be non-empty text")
    default_terrain = str(spec.get("default_terrain", "normal"))
    terrain: dict[tuple[int, int], str] = {}
    #: The glyphs the author chose, kept where the document format allows them.
    #: Only a ``rows`` spec has any; a ``terrain`` list names kinds and never
    #: characters, so its legend is allocated outright.
    author_legend: dict[str, str] | None = None

    rows = spec.get("rows")
    entries = spec.get("terrain")
    if rows is not None and entries is not None:
        raise RequestError("give 'rows' with a 'legend', or a 'terrain' list — not both")
    if rows is not None:
        legend = spec.get("legend")
        if not isinstance(legend, dict) or not all(
            isinstance(key, str) and len(key) == 1 and isinstance(value, str)
            for key, value in legend.items()
        ):
            raise RequestError(
                "'rows' needs a 'legend' object mapping single characters to "
                "terrain kinds, such as {\"#\": \"wall\", \".\": \"normal\"}"
            )
        author_legend = dict(legend)
        if not isinstance(rows, list) or not all(isinstance(row, str) for row in rows):
            raise RequestError("'rows' must be a list of strings, one per map row")
        if len(rows) != height:
            raise RequestError(f"'rows' has {len(rows)} rows; the map is {height} high")
        for y, row in enumerate(rows):
            if len(row) != width:
                raise RequestError(
                    f"row {y} is {len(row)} characters; the map is {width} wide"
                )
            for x, char in enumerate(row):
                kind = legend.get(char)
                if kind is None:
                    raise RequestError(
                        f"row {y} column {x} uses {char!r}, which the legend does "
                        f"not define"
                    )
                if kind != default_terrain:
                    terrain[(x, y)] = kind
    elif entries is not None:
        if not isinstance(entries, list):
            raise RequestError("'terrain' must be a list of {kind, squares} entries")
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or set(entry) != {"kind", "squares"}:
                raise RequestError(
                    f"terrain entry #{index} must be {{\"kind\": ..., \"squares\": "
                    f"[[x, y], ...]}}"
                )
            kind = entry["kind"]
            if not isinstance(kind, str):
                raise RequestError(f"terrain entry #{index} kind must be a terrain name")
            squares = entry["squares"]
            if not isinstance(squares, list):
                raise RequestError(f"terrain entry #{index} squares must be a list")
            for value in squares:
                terrain[
                    parse_square(value, f"terrain entry #{index} square", width, height)
                ] = kind

    default_elevation = spec.get("default_elevation", 0)
    if isinstance(default_elevation, bool) or not isinstance(default_elevation, int):
        raise RequestError(
            f"'default_elevation' must be a whole number of feet, got "
            f"{default_elevation!r}"
        )
    elevation: dict[tuple[int, int], int] = {}
    raw_elevation = spec.get("elevation", [])
    if not isinstance(raw_elevation, list):
        raise RequestError(
            "'elevation' must be a list of [x, y, feet] entries, such as [[3, 4, 20]]"
        )
    for index, entry in enumerate(raw_elevation):
        if (
            not isinstance(entry, list)
            or len(entry) != 3
            or any(isinstance(value, bool) or not isinstance(value, int) for value in entry)
        ):
            raise RequestError(
                f"elevation entry #{index} must be [x, y, feet], got {entry!r}"
            )
        square = parse_square(entry[:2], f"elevation entry #{index} square", width, height)
        if square in elevation:
            raise RequestError(
                f"elevation entry #{index} names square [{square[0]}, {square[1]}] "
                f"again; it is already {elevation[square]} ft"
            )
        elevation[square] = int(entry[2])

    features: dict[str, MapFeatureRecord] = {}
    raw_features = spec.get("features", [])
    if not isinstance(raw_features, list):
        raise RequestError("'features' must be a list of feature objects")
    for index, entry in enumerate(raw_features):
        if not isinstance(entry, dict):
            raise RequestError(f"feature #{index} must be an object")
        for key in sorted(set(entry) - FEATURE_KEYS):
            raise RequestError(
                f"feature #{index} has unknown key {key!r}. Valid keys: "
                f"{', '.join(sorted(FEATURE_KEYS))}"
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RequestError(f"feature #{index} needs a non-empty 'name'")
        if name in features:
            raise RequestError(f"two features are named {name!r}; names must be unique")
        initially_open = entry.get("initially_open", False)
        if not isinstance(initially_open, bool):
            raise RequestError(f"feature {name!r} initially_open must be true or false")
        orientation = entry.get("orientation")
        if orientation is not None and orientation not in DOOR_ORIENTATIONS:
            raise RequestError(
                f"feature {name!r} orientation must be one of: "
                f"{', '.join(DOOR_ORIENTATIONS)}; got {orientation!r}"
            )
        kind = str(entry.get("kind", "door"))
        if not kind.strip():
            # The format asks every feature what it is, and journal recovery
            # re-reads what this writes — the same trap the map's own name sets.
            raise RequestError(f"feature {name!r} 'kind' must be non-empty text")
        if kind == "door" and orientation is None:
            # ``service.maps._feature_entry`` refuses this on the ``map.edit``
            # surface in these words; a second wording for one rule is how a
            # caller learns two formats. The tail is here and not there because
            # ``kind`` defaults to "door" only in a spec: a caller who wrote a
            # lever and left the kind out would otherwise be refused for a door
            # they never mentioned, and told to fix the wrong thing.
            defaulted = (
                "" if "kind" in entry
                else "; a feature that names no 'kind' is a door"
            )
            raise RequestError(
                f"feature {name!r} is a door, so it needs 'orientation' "
                f"(horizontal or vertical){defaulted}"
            )
        linked_to = entry.get("linked_to")
        if linked_to is not None and (
            not isinstance(linked_to, str) or not linked_to.strip()
        ):
            raise RequestError(
                f"feature {name!r} linked_to must name a feature; got {linked_to!r}"
            )
        features[name] = MapFeatureRecord(
            id=name,
            at=parse_square(entry.get("square"), f"feature {name!r} square",
                            width, height),
            kind=kind,
            orientation=orientation,
            # Written out whichever kind the fixture is, because ``to_grid``
            # only falls back to the hardcoded door pair when the record carries
            # none — and a spec's lever is entitled to say its square stays
            # floor in both states.
            terrain=TerrainPair(
                closed=str(entry.get("closed_terrain", "door-closed")),
                open=str(entry.get("open_terrain", "door-open")),
            ),
            # ``initially_open`` needs no matching requirement. The document
            # format demands a door's ``state`` for the same reason it demands
            # its orientation, and ``map.edit`` refuses a door without one — but
            # a spec that omits it is not silent about the answer the way an
            # omitted orientation is. ``False`` is a real answer, the one every
            # door is authored in, and it is written out here as ``"closed"``,
            # so the captured document is complete either way.
            state="open" if initially_open else "closed",
            linked_to=linked_to,
        )

    named = {default_terrain, *terrain.values()}
    for record in features.values():
        assert record.terrain is not None
        named.update((record.terrain.closed, record.terrain.open))
    unknown = sorted(kind for kind in named if kind not in terrain_table)
    if unknown:
        # Word for word what ``Encounter._adopt_map`` says about a hand-built
        # battle map, because it is one rule and the caller should not be able
        # to tell which surface caught it. Said *here* so it can be said about
        # the kind the caller wrote rather than about the glyph this function
        # would have invented for it.
        defined = ", ".join(sorted(terrain_table)) or "none"
        raise RequestError(
            f"the map names terrain the loaded content does not define: "
            f"{', '.join(unknown)}. Defined: {defined}"
        )

    document = MapDocument.flat(
        name=map_name,
        width=width,
        height=height,
        default_terrain=default_terrain,
        terrain=terrain,
        default_elevation=default_elevation,
        elevation=elevation,
        features=tuple(features.values()),
        legend=author_legend,
        provenance=INLINE_PROVENANCE,
    )
    size = len(json.dumps(as_payload(document), ensure_ascii=False).encode("utf-8"))
    if size > MAX_MAP_BYTES:
        raise RequestError(
            f"this map writes out to {size} bytes, over the {MAX_MAP_BYTES} byte "
            f"limit for a map document; a {width}x{height} grid leaves room for "
            f"fewer heights and features than this"
        )
    return document


def action_from_journal(arguments: Mapping[str, Any]) -> Action:
    def point(name: str) -> int | Point | None:
        value = arguments.get(name)
        if value is None or isinstance(value, int):
            return value
        return (int(value[0]), int(value[1]))

    toward = arguments.get("toward")
    aimed: str | Point | None
    if isinstance(toward, str) or toward is None:
        aimed = toward
    else:
        aimed = (int(toward[0]), int(toward[1]))
    direction = arguments.get("direction")
    return Action(
        kind=ActionKind(str(arguments["kind"])),
        target=arguments.get("target"),
        attack=arguments.get("attack"),
        item=arguments.get("item"),
        spell=arguments.get("spell"),
        slot_level=arguments.get("slot_level"),
        to_position=point("to_position"),
        targets=tuple(arguments.get("targets") or ()),
        center=point("center"),
        direction=(
            (int(direction[0]), int(direction[1])) if direction is not None else None
        ),
        toward=aimed,
        path=tuple((int(step[0]), int(step[1])) for step in arguments.get("path") or ()),
        feature=arguments.get("feature"),
        set_open=arguments.get("set_open"),
        to_level=arguments.get("to_level"),
        # The twin of the hand-written tuple in ``Action.as_dict``, and it has
        # to be kept in step with it: ``encounters.act`` journals both of these,
        # and a field rebuilt here as its default replays as a *different
        # action* than the one recorded. Dropping ``as_bonus_action`` turned a
        # bonus-action Dash back into an ordinary one, so the action that
        # legitimately followed it was refused and the resume failed outright.
        facing=arguments.get("facing"),
        movement_mode=(
            MovementMode(str(movement_mode))
            if (movement_mode := arguments.get("movement_mode")) is not None
            else None
        ),
        as_bonus_action=bool(arguments.get("as_bonus_action", False)),
        natural=tuple(int(face) for face in arguments.get("natural") or ()),
    )
