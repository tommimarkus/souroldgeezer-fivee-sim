"""Conditions expressed as data rather than scattered branches.

Every condition's combat-relevant consequences live in one table. Adding a
condition means adding a row, and the attack/save code reads the table instead of
growing another special case.

The table is **passed in**, not read from module state. :data:`EFFECTS` holds the
SRD conditions and is the default, but a content pack can define its own, so every
function that consults a condition takes the table it should consult. The reason is
the same one that makes every rolling function take an explicit ``Random``: state
read from ambient module scope is state a caller cannot control, and a fight that
silently consults a different table than the one it was built with is exactly the
drift this engine exists to prevent.

A condition is therefore identified by a plain ``str``. :class:`Condition` remains
as named constants for the SRD set — it is a ``StrEnum``, so its members *are*
strings and index the same table rows a pack's names do.

All provenance of :data:`EFFECTS`: SRD 5.2.1 (see NOTICE).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from .dice import Advantage, resolve_advantage
from .rules import Ability


class UnknownCondition(KeyError):
    """A condition was referenced that the active table does not define."""


class Condition(StrEnum):
    BLINDED = "blinded"
    CHARMED = "charmed"
    DEAFENED = "deafened"
    FRIGHTENED = "frightened"
    GRAPPLED = "grappled"
    INCAPACITATED = "incapacitated"
    INVISIBLE = "invisible"
    PARALYZED = "paralyzed"
    PETRIFIED = "petrified"
    POISONED = "poisoned"
    PRONE = "prone"
    RESTRAINED = "restrained"
    STUNNED = "stunned"
    UNCONSCIOUS = "unconscious"


@dataclass(frozen=True, slots=True)
class ConditionEffect:
    """The mechanical consequences of one condition."""

    incapacitated: bool = False
    speed_zero: bool = False
    #: Attack rolls against the afflicted creature.
    attacked_with_advantage: bool = False
    attacked_with_disadvantage: bool = False
    #: Prone is directional, and scoped by **distance, not by weapon**: advantage
    #: from within 5 ft, disadvantage from beyond it, whatever the attack. Both
    #: names are historical and pack-facing, so they stay — see
    #: ``melee_hits_are_critical`` below, which kept its name for the same reason.
    attacked_with_advantage_in_melee: bool = False
    attacked_with_disadvantage_at_range: bool = False
    #: The afflicted creature's own attack rolls.
    own_attacks_have_advantage: bool = False
    own_attacks_have_disadvantage: bool = False
    #: The afflicted creature's own ability checks. Initiative is one.
    own_ability_checks_have_advantage: bool = False
    own_ability_checks_have_disadvantage: bool = False
    #: Initiative-only riders, *additional* to the two flags above rather than a
    #: substitute for them: Initiative is itself a Dexterity ability check, so a
    #: condition that grants Advantage or Disadvantage on ability checks generally
    #: also reaches Initiative, but the reverse must not hold. These exist for the
    #: clauses that name Initiative specifically and nothing else — Incapacitated's
    #: and Invisible's Surprise riders — so a clause scoped to Initiative cannot
    #: leak into every other ability check a creature makes.
    initiative_advantage: bool = False
    initiative_disadvantage: bool = False
    #: Sight consequences consumed by stateful model-layer rules. ``cannot_see``
    #: belongs to the observer; ``unseen`` belongs to the possible subject.
    cannot_see: bool = False
    unseen: bool = False
    auto_fail_strength_saves: bool = False
    auto_fail_dexterity_saves: bool = False
    #: Weighting a Dexterity save rather than deciding it. Restrained is the SRD
    #: condition that needs this, and the distinction is the whole point: a
    #: Restrained creature still rolls, and can still succeed. Only Dexterity has
    #: these flags because no condition in the table bears on any other ability's
    #: save; the Advantage half exists for the Dodge action and for packs.
    advantage_on_dexterity_saves: bool = False
    disadvantage_on_dexterity_saves: bool = False
    #: Paralyzed and Unconscious turn melee hits into critical hits.
    melee_hits_are_critical: bool = False
    resists_all_damage: bool = False
    #: SRD 5.2.1 p.179: "A condition doesn't stack with itself; a recipient
    #: either has a condition or doesn't. The Exhaustion condition is an
    #: exception to that rule." An exception to a rule about conditions is a
    #: fact about a condition, so it lives here rather than as code keyed on
    #: a name — no flag or check in this module may test a condition's string
    #: against ``"exhaustion"``.
    cumulative: bool = False
    #: SRD 5.2.1 p.180: "D20 Tests encompass the four main d20 rolls of the
    #: game: ability checks, attack rolls, and saving throws. If something in
    #: the game affects D20 Tests, it affects all three." A penalty here
    #: therefore reaches every ability check, attack roll, and saving throw a
    #: creature makes — never a subset of them. Per level rather than flat: a
    #: held condition is always at some level (an ordinary one is
    #: permanently 1), and ``penalty_per_level * level`` is what lets the
    #: consumption path treat a cumulative and a non-cumulative condition
    #: identically, with no ``if cumulative`` branch anywhere downstream of
    #: this field.
    d20_test_penalty_per_level: int = 0


#: Every flag a condition may set. Content-pack validation reports this list when a
#: pack names a flag that does not exist, so it is derived rather than retyped.
EFFECT_FLAGS: tuple[str, ...] = tuple(ConditionEffect.__dataclass_fields__)

_NO_EFFECT = ConditionEffect()

# Keyed by ``str`` rather than by ``Condition`` so a pack's table and this one are
# the same type. The keys below are still enum members; ``StrEnum`` hashes by value,
# so ``EFFECTS["prone"]`` and ``EFFECTS[Condition.PRONE]`` are the same lookup.
EFFECTS: dict[str, ConditionEffect] = {
    Condition.BLINDED: ConditionEffect(
        attacked_with_advantage=True,
        own_attacks_have_disadvantage=True,
        cannot_see=True,
    ),
    # Charmed and Deafened carry no combat-roll consequences; they are tracked so
    # narration and targeting restrictions can see them.
    Condition.CHARMED: _NO_EFFECT,
    Condition.DEAFENED: _NO_EFFECT,
    Condition.FRIGHTENED: ConditionEffect(
        own_attacks_have_disadvantage=True,
        own_ability_checks_have_disadvantage=True,
    ),
    Condition.GRAPPLED: ConditionEffect(
        speed_zero=True,
        own_attacks_have_disadvantage=True,
    ),
    Condition.INCAPACITATED: ConditionEffect(
        incapacitated=True,
        initiative_disadvantage=True,  # Surprised.
    ),
    # Invisible's "Attacks Affected" clause is deliberately *not* two flags here.
    # It ends "If a creature can somehow see you, you don't gain this benefit
    # against that creature", and a row in this table knows one condition and no
    # creatures — it cannot ask whether the attacker sees the target. So the whole
    # clause is derived from ``unseen`` by ``Encounter._sight_advantage``, which
    # can. The flags stayed set here for a while as well, which meant the rule was
    # stated twice and the unconditional copy was wrong against anything with
    # Blindsight; Advantage never stacking is the only reason that was invisible.
    Condition.INVISIBLE: ConditionEffect(
        initiative_advantage=True,  # Surprise.
        unseen=True,
    ),
    Condition.PARALYZED: ConditionEffect(
        incapacitated=True,
        speed_zero=True,
        attacked_with_advantage=True,
        auto_fail_strength_saves=True,
        auto_fail_dexterity_saves=True,
        melee_hits_are_critical=True,
    ),
    Condition.PETRIFIED: ConditionEffect(
        incapacitated=True,
        speed_zero=True,
        attacked_with_advantage=True,
        auto_fail_strength_saves=True,
        auto_fail_dexterity_saves=True,
        resists_all_damage=True,
    ),
    Condition.POISONED: ConditionEffect(
        own_attacks_have_disadvantage=True,
        own_ability_checks_have_disadvantage=True,
    ),
    Condition.PRONE: ConditionEffect(
        own_attacks_have_disadvantage=True,
        attacked_with_advantage_in_melee=True,
        attacked_with_disadvantage_at_range=True,
    ),
    # Restrained weights the Dexterity save; it does not decide it. Automatic
    # failure of Strength and Dexterity saves belongs to the four conditions that
    # state it — Paralyzed, Petrified, Stunned, Unconscious — and to no others.
    Condition.RESTRAINED: ConditionEffect(
        speed_zero=True,
        attacked_with_advantage=True,
        own_attacks_have_disadvantage=True,
        disadvantage_on_dexterity_saves=True,
    ),
    # Stunned carries no Speed 0 clause, and neither does the Incapacitated
    # condition it confers. The three neighbouring conditions here each state Speed
    # 0 outright, which is what makes its absence a rule rather than an oversight.
    Condition.STUNNED: ConditionEffect(
        incapacitated=True,
        attacked_with_advantage=True,
        auto_fail_strength_saves=True,
        auto_fail_dexterity_saves=True,
    ),
    Condition.UNCONSCIOUS: ConditionEffect(
        incapacitated=True,
        speed_zero=True,
        attacked_with_advantage=True,
        auto_fail_strength_saves=True,
        auto_fail_dexterity_saves=True,
        melee_hits_are_critical=True,
    ),
}

ConditionTable = Mapping[str, ConditionEffect]


def effect_of(condition: str, table: ConditionTable = EFFECTS) -> ConditionEffect:
    """The effects of one condition, or a report of what the table does define.

    Raising rather than defaulting to no effect is the point: a misspelled condition
    that quietly does nothing produces a fight that looks right and resolves wrongly.
    """
    try:
        return table[condition]
    except KeyError:
        available = ", ".join(sorted(table)) or "none"
        raise UnknownCondition(
            f"no condition named {condition!r}; the active content defines: {available}"
        ) from None


def effects_of(
    conditions: Iterable[str], table: ConditionTable = EFFECTS
) -> tuple[ConditionEffect, ...]:
    return tuple(effect_of(condition, table) for condition in conditions)


def is_incapacitated(conditions: Iterable[str], table: ConditionTable = EFFECTS) -> bool:
    return any(effect.incapacitated for effect in effects_of(conditions, table))


def speed_is_zero(conditions: Iterable[str], table: ConditionTable = EFFECTS) -> bool:
    return any(effect.speed_zero for effect in effects_of(conditions, table))


def d20_test_penalty(conditions: Mapping[str, int], table: ConditionTable = EFFECTS) -> int:
    """The total penalty every D20 Test a creature makes must subtract.

    SRD 5.2.1 p.180: "If something in the game affects D20 Tests, it affects
    all three [ability checks, attack rolls, and saving throws]" — so this is
    the one number consulted for all three, never a per-kind variant.

    ``conditions`` is a name-to-level mapping, matching
    :attr:`~fivee_sim.model.creature.Creature.conditions` exactly, so a caller
    can pass a creature's own conditions unchanged. Each held condition
    contributes ``d20_test_penalty_per_level * level`` — uniformly, whether
    the condition is cumulative or not, since an ordinary condition's level is
    always 1.
    """
    return sum(
        effect_of(condition, table).d20_test_penalty_per_level * level
        for condition, level in conditions.items()
    )


def _count_ability_check_sources(
    *,
    conditions: Iterable[str],
    extra_advantage: int,
    extra_disadvantage: int,
    condition_effects: ConditionTable,
) -> tuple[int, int]:
    advantage_sources = extra_advantage
    disadvantage_sources = extra_disadvantage
    for condition in conditions:
        effect = effect_of(condition, condition_effects)
        if effect.own_ability_checks_have_advantage:
            advantage_sources += 1
        if effect.own_ability_checks_have_disadvantage:
            disadvantage_sources += 1
    return advantage_sources, disadvantage_sources


def compute_ability_check_advantage(
    *,
    conditions: Iterable[str],
    extra_advantage: int = 0,
    extra_disadvantage: int = 0,
    condition_effects: ConditionTable = EFFECTS,
) -> Advantage:
    """Collect every source of Advantage and Disadvantage on an ability check."""
    advantage_sources, disadvantage_sources = _count_ability_check_sources(
        conditions=conditions,
        extra_advantage=extra_advantage,
        extra_disadvantage=extra_disadvantage,
        condition_effects=condition_effects,
    )
    return resolve_advantage(
        advantage_sources=advantage_sources,
        disadvantage_sources=disadvantage_sources,
    )


def compute_initiative_advantage(
    *,
    conditions: Iterable[str],
    extra_advantage: int = 0,
    extra_disadvantage: int = 0,
    condition_effects: ConditionTable = EFFECTS,
) -> Advantage:
    """Collect Advantage and Disadvantage on an Initiative roll specifically.

    Initiative is a Dexterity ability check, so every general ability-check
    source counts here too — plus the ``initiative_advantage`` /
    ``initiative_disadvantage`` riders that name Initiative alone. All sources
    are tallied together before the SRD combination rule (any Advantage plus
    any Disadvantage yields neither) is applied once, across the whole set.
    """
    conditions = tuple(conditions)
    advantage_sources, disadvantage_sources = _count_ability_check_sources(
        conditions=conditions,
        extra_advantage=extra_advantage,
        extra_disadvantage=extra_disadvantage,
        condition_effects=condition_effects,
    )
    for condition in conditions:
        effect = effect_of(condition, condition_effects)
        if effect.initiative_advantage:
            advantage_sources += 1
        if effect.initiative_disadvantage:
            disadvantage_sources += 1
    return resolve_advantage(
        advantage_sources=advantage_sources,
        disadvantage_sources=disadvantage_sources,
    )


def compute_save_advantage(
    *,
    conditions: Iterable[str],
    ability: Ability,
    extra_advantage: int = 0,
    extra_disadvantage: int = 0,
    condition_effects: ConditionTable = EFFECTS,
) -> Advantage:
    """Collect every source of Advantage and Disadvantage on one saving throw.

    The counterpart of
    :func:`~fivee_sim.kernel.actions.compute_attack_advantage`, and counting rather
    than short-circuiting for the same reason: any Advantage plus any Disadvantage
    yields neither, so both tallies have to be complete before deciding.

    ``extra_advantage`` is how the Dodge action reaches a saving throw — the model
    layer knows who is Dodging, this module knows only conditions.

    This is deliberately independent of whether the save auto-fails. A forced
    failure still rolls its dice, and Disadvantage still costs two of them; deciding
    Advantage only for saves that could succeed would make the size of the RNG draw
    depend on the conditions a creature holds, and desynchronise the analytics
    replay from live play.

    Every condition is resolved through :func:`effect_of` whatever the ability, so a
    name the table does not define is reported rather than silently skipped.
    """
    advantage_sources = extra_advantage
    disadvantage_sources = extra_disadvantage
    for condition in conditions:
        effect = effect_of(condition, condition_effects)
        if ability is not Ability.DEXTERITY:
            continue
        if effect.advantage_on_dexterity_saves:
            advantage_sources += 1
        if effect.disadvantage_on_dexterity_saves:
            disadvantage_sources += 1
    return resolve_advantage(
        advantage_sources=advantage_sources,
        disadvantage_sources=disadvantage_sources,
    )
