"""Creatures, their attack options, and the state a fight mutates.

Attack options carry an explicit bonus and damage expression rather than deriving
them from ability scores, proficiency, and class features. That is how SRD stat
blocks present attacks, so data stays a faithful transcription instead of relying
on derivation rules the engine would have to invent.

Positions are points on a plane, measured in feet. A bare int is accepted
wherever a position goes and means feet along the x-axis — the original
one-dimensional battlefield is this plane's x-axis, and a scalar caller sees
identical numbers. Distance defaults to the SRD diagonal rule; the encounter
passes its own.

**Construction from a content record lives here**, as :meth:`Creature.from_record`
and :meth:`AttackOption.from_record`, because this module owns creatures and the
field-for-field shape a record maps onto is the transcription described above —
changing one is changing the other, so they read better together than apart.

That places a constraint worth stating: a record arrives as a plain mapping, and
the two values construction cannot derive from it — the condition table the
creature reads its conditions against, and the provenance to fall back on — are
*arguments*. They are not looked up. Both come off a
:class:`~fivee_sim.content.ContentRegistry` in practice, and a registry is
``content``'s concept, one layer above this one; reaching up for it would invert
the direction the whole tree depends on. :func:`fivee_sim.content.make_creature`
is the caller that holds the registry and passes the two values down.

Records reaching :meth:`Creature.from_record` from a pack have already been
validated by ``content``, so construction does not re-check them; a malformed pack
fails at load, with a diagnostic naming the field, rather than half-way into a
fight.

All provenance: SRD 5.2.1 (see NOTICE).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..kernel.actions import AttackKind, RiderExpiry
from ..kernel.conditions import (
    EFFECTS,
    Condition,
    ConditionEffect,
    ConditionTable,
    effect_of,
)
from ..kernel.dice import Dice
from ..kernel.grid import DiagonalRule, Point, as_point, distance_feet
from ..kernel.rules import Ability, DamageType, Size, ability_modifier

__all__ = ["AttackKind", "AttackOption", "Creature", "DeathRule", "RiderExpiry"]

#: Failures that kill. Duplicated from ``model.encounter``, which owns the death
#: save *roll* but cannot be imported here — it imports this module. Damage taken
#: at 0 hit points accrues a failure too, so the threshold is needed on both sides.
DEATH_SAVES_TO_DIE = 3


class DeathRule(StrEnum):
    """What reaching 0 hit points means for this combatant."""

    DEATH_SAVES = "death_saves"
    INSTANT = "instant"


@dataclass(frozen=True, slots=True)
class AttackOption:
    """One attack a creature can make, as printed on a stat block.

    Stat blocks hang riders off a hit, and three shapes cover the printed forms:
    ``bonus_damage`` is a second pool of a different type on every hit (a claw
    that adds fire to its slashing); ``advantage_bonus_damage`` is extra dice of
    the main type only when the attack roll resolved with Advantage (the goblin
    pattern); ``on_hit_condition`` is a condition the hit imposes — automatic,
    or on a failed save when ``on_hit_save_ability`` and ``on_hit_save_dc`` are
    given — that ``on_hit_expiry`` may end on a turn boundary. The condition is
    a plain string on purpose: a pack-defined condition works here exactly as an
    SRD one does.

    ``on_hit_max_size`` gates that condition on how big the target is — the
    Wolf's "if the target is a Medium or smaller creature, it has the Prone
    condition". Unset means the rider is ungated, which is what every other
    printed form in this engine is.

    ``ammunition`` names an entry in the wielder's :attr:`Creature.items` that
    firing this attack spends — the longbow's "twenty arrows", not a property
    of the roll itself, which is why it is only legal alongside :attr:`kind`
    ``RANGED``. ``loading`` marks the SRD Loading property, the same
    restriction for the same reason: a melee weapon has no magazine and no
    fired-and-reloaded rhythm to gate.

    ``thrown`` is the third rider on ``RANGED``, and it is what SRD 5.2.1's
    *"**Melee or Ranged** Attack Roll: +6, reach 5 ft. or range 30/120 ft."*
    line becomes here — the Ogre's Javelin, and the same shape on twenty other
    stat blocks. It is the Thrown weapon property (catalog
    ``583-9-4-8-thrown``) written out: the Javelin is listed under *Simple
    Melee Weapons* with ``Thrown (Range 30/120)``, so it is a melee weapon that
    *enables* a ranged attack. Within :attr:`reach` the weapon is still in
    hand and the swing resolves as melee; past it, the weapon is in the air
    and the swing is a shot. :meth:`resolves_as_melee` is the one place that
    boundary is decided and every rule that cares reads it there.

    **Why a rider and not a third** :class:`AttackKind` **member.** Three
    reasons, in the order that decided it.

    First, :class:`AttackKind` is not ours alone: :attr:`Spell.attack_kind` is
    typed with it and ``content.py`` parses it for spell records. SRD 5.2.1
    prints no "melee or ranged spell attack", so a ``MELEE_OR_RANGED`` member
    would be a word in a closed, pack-facing vocabulary that is legal in one
    consumer and has to be hand-refused in the other — the enum would say a
    value is fine and a second, separate check would say it is not.

    Second, every existing ``is AttackKind.MELEE`` / ``is AttackKind.RANGED``
    test is written as a two-valued question, in this module, in
    ``model/encounter.py`` and in ``analytics/montecarlo.py``. A third member
    does not fail any of them; it falls silently into whichever branch was
    written as the ``else``, and mypy does not flag a non-exhaustive ``is``
    chain. The enum change would have type-checked, run, and been wrong.

    Third, the rider's cost — a second field to keep consistent with ``kind`` —
    is a cost this class already pays twice, for ``ammunition`` and
    ``loading``, with the mechanism already in :meth:`__post_init__`. One more
    clause there is not a new pattern.

    ``kind`` therefore stays ``RANGED``, which is also the safe default if a
    consumer of this field is ever missed: the option degrades to the plain
    ranged weapon it was before, rather than to a melee weapon that has lost
    the ability to be thrown at all.
    """

    name: str
    attack_bonus: int
    damage: Dice
    damage_type: DamageType
    kind: AttackKind = AttackKind.MELEE
    reach: int = 5
    normal_range: int = 0
    long_range: int = 0
    bonus_damage: Dice | None = None
    bonus_damage_type: DamageType | None = None
    advantage_bonus_damage: Dice | None = None
    #: Extend that same rider to a hit made while a capable ally is beside the
    #: target (the common Sneak Attack eligibility shape).
    advantage_bonus_with_adjacent_ally: bool = False
    on_hit_condition: str | None = None
    on_hit_save_ability: Ability | None = None
    on_hit_save_dc: int = 0
    on_hit_expiry: RiderExpiry = RiderExpiry.NONE
    on_hit_max_size: Size | None = None
    #: Attachment riders such as a blood-draining parasite.  The first damage
    #: pool still lands on the hit; ``attached_damage`` repeats at the start of
    #: the attacker's turns until detached.
    on_hit_attach: bool = False
    attached_damage: Dice | None = None
    attached_damage_type: DamageType | None = None
    detach_after_damage: int = 0
    ammunition: str | None = None
    loading: bool = False
    thrown: bool = False
    provenance: str = "SRD 5.2.1"

    def __post_init__(self) -> None:
        # Refused at construction rather than discovered mid-swing: without a
        # type the bonus pool cannot be defended, and without a DC a save cannot
        # be rolled. Content validation reports these first with a diagnostic;
        # this guards direct construction the same way.
        if self.bonus_damage is not None and self.bonus_damage_type is None:
            raise ValueError(
                f"{self.name}: bonus_damage needs bonus_damage_type — the extra "
                f"damage is defended against its own type"
            )
        if self.on_hit_save_ability is not None and self.on_hit_save_dc < 1:
            raise ValueError(
                f"{self.name}: on_hit_save_ability needs an on_hit_save_dc of 1 "
                f"or more"
            )
        if self.on_hit_max_size is not None and self.on_hit_condition is None:
            raise ValueError(
                f"{self.name}: on_hit_max_size needs on_hit_condition — there is "
                f"no condition to ride the hit"
            )
        if self.on_hit_attach and (
            self.attached_damage is None or self.attached_damage_type is None
        ):
            raise ValueError(
                f"{self.name}: on_hit_attach needs attached_damage and "
                "attached_damage_type"
            )
        if self.ammunition is not None and self.kind is not AttackKind.RANGED:
            raise ValueError(
                f"{self.name}: ammunition needs kind RANGED — a melee attack "
                "spends nothing to swing"
            )
        if self.loading and self.kind is not AttackKind.RANGED:
            raise ValueError(
                f"{self.name}: loading needs kind RANGED — a melee attack has "
                "no reload rhythm to gate"
            )
        if self.thrown and self.kind is not AttackKind.RANGED:
            raise ValueError(
                f"{self.name}: thrown needs kind RANGED — it says what happens "
                "inside reach, and a melee attack is already there"
            )
        if self.thrown and not (self.normal_range or self.long_range):
            # A thrown weapon that cannot be thrown is not merely redundant, it
            # is broken: ``max_distance`` answers 0 for a ranged option with no
            # range, so the attack is refused at every square but the
            # attacker's own — the ``range``/``normal_range`` pregen defect.
            raise ValueError(
                f"{self.name}: thrown needs a normal_range or long_range — "
                "there is nowhere to throw it"
            )
        if self.ammunition is not None and not self.ammunition.strip():
            raise ValueError(f"{self.name}: ammunition must not be blank")

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> AttackOption:
        """Build one attack from a validated content record."""
        bonus_type = record.get("bonus_damage_type")
        save_ability = record.get("on_hit_save_ability")
        max_size = record.get("on_hit_max_size")
        return cls(
            name=str(record["name"]),
            attack_bonus=int(record["attack_bonus"]),
            damage=Dice.parse(str(record["damage"])),
            damage_type=DamageType(record["damage_type"]),
            kind=AttackKind(record.get("kind", "melee")),
            reach=int(record.get("reach", 5)),
            normal_range=int(record.get("normal_range", 0)),
            long_range=int(record.get("long_range", 0)),
            bonus_damage=(
                Dice.parse(str(record["bonus_damage"]))
                if record.get("bonus_damage") is not None else None
            ),
            bonus_damage_type=DamageType(bonus_type) if bonus_type is not None else None,
            advantage_bonus_damage=(
                Dice.parse(str(record["advantage_bonus_damage"]))
                if record.get("advantage_bonus_damage") is not None else None
            ),
            advantage_bonus_with_adjacent_ally=bool(
                record.get("advantage_bonus_with_adjacent_ally", False)
            ),
            on_hit_condition=(
                str(record["on_hit_condition"])
                if record.get("on_hit_condition") is not None else None
            ),
            on_hit_save_ability=Ability(save_ability) if save_ability is not None else None,
            on_hit_save_dc=int(record.get("on_hit_save_dc", 0)),
            on_hit_expiry=RiderExpiry(record.get("on_hit_expiry", "none")),
            on_hit_max_size=Size(max_size) if max_size is not None else None,
            on_hit_attach=bool(record.get("on_hit_attach", False)),
            attached_damage=(
                Dice.parse(str(record["attached_damage"]))
                if record.get("attached_damage") is not None else None
            ),
            attached_damage_type=(
                DamageType(record["attached_damage_type"])
                if record.get("attached_damage_type") is not None else None
            ),
            detach_after_damage=int(record.get("detach_after_damage", 0)),
            ammunition=(
                str(record["ammunition"])
                if record.get("ammunition") is not None else None
            ),
            loading=bool(record.get("loading", False)),
            thrown=bool(record.get("thrown", False)),
            provenance=str(record.get("provenance", "SRD 5.2.1")),
        )

    def resolves_as_melee(self, distance: int) -> bool:
        """Whether a swing at ``distance`` is a melee attack rather than a shot.

        The one place the "Melee or Ranged" boundary is decided. Every rule
        that treats the two differently — the close-combat penalty, the
        underwater penalties, the long-range band, whether ammunition is spent
        — asks this rather than reading :attr:`kind`, so they cannot drift into
        disagreeing about the same swing.
        """
        if self.kind is AttackKind.MELEE:
            return True
        return self.thrown and distance <= self.reach

    def melee_capable(self) -> bool:
        """Whether this option can *ever* resolve as a melee attack.

        Derived from :meth:`resolves_as_melee` rather than restating it: the
        closest a swing can come is the option's own reach, so if it is not
        melee there it is melee nowhere. This is the question an Opportunity
        Attack asks, which is why a javelin-carrying creature threatens the
        square beside it.
        """
        return self.resolves_as_melee(self.reach)

    def max_distance(self) -> int:
        # A thrown option's furthest is its throw, not its reach: it is
        # ``RANGED`` and answers here exactly as any other ranged option does.
        if self.kind is AttackKind.MELEE:
            return self.reach
        return self.long_range or self.normal_range

    def has_long_range_penalty(self, distance: int) -> bool:
        if self.resolves_as_melee(distance):
            return False
        return self.normal_range > 0 and distance > self.normal_range


@dataclass(slots=True)
class Creature:
    """A combatant. Mutable: a fight is a sequence of changes to these fields."""

    name: str
    team: str
    ac: int
    max_hp: int
    speed: int = 30
    climb_speed: int = 0
    swim_speed: int = 0
    fly_speed: int = 0
    #: A printed Burrow speed. Wired in exactly like climb, swim, and fly
    #: above: it counts toward the turn's movement budget and is selectable
    #: as an explicit movement mode, at ordinary terrain cost. This engine
    #: models no terrain gating for *any* movement mode — swim speed already
    #: applies on dry land, fly speed applies regardless of what is
    #: underneath — so burrow does not invent one either: there is no
    #: "digging through solid ground" mechanic here, consistent with the
    #: other three rather than a burrow-specific gap.
    burrow_speed: int = 0
    #: Terrain names whose extra movement cost this creature ignores.
    terrain_cost_overrides: frozenset[str] = frozenset()
    darkvision: int = 0
    blindsight: int = 0
    #: A stat block's printed Tremorsense range, carried but never consumed —
    #: transcription-only, following ``passive_perception`` exactly. SRD
    #: 5.2.1, *Tremorsense*: "Tremorsense can't detect creatures or objects in
    #: the air, and it doesn't count as a form of sight." That last clause is
    #: the reason it is not a rung in
    #: :meth:`~fivee_sim.model.encounter.Encounter._can_see` alongside
    #: Truesight and Blindsight: Tremorsense pinpoints a *location*, not
    #: sight of the creature there, and this engine has no third state
    #: between "can see" and "cannot see" to represent that. Wiring it into
    #: ``_can_see`` would wrongly cancel the unseen-target Disadvantage
    #: against an Invisible creature the observer has merely pinpointed. Kept
    #: and reported per the ``hit_dice`` ruling — an accepted key that does
    #: nothing must say so — until this engine gains a pinpoint-without-sight
    #: concept to spend it on.
    tremorsense: int = 0
    #: SRD 5.2.1, Truesight: within range, vision "pierces through" Darkness
    #: (including magical), Invisibility, visual illusions, transformations,
    #: and the Ethereal Plane. Only the first two have any mechanical
    #: presence in this engine, so only those are wired into
    #: :meth:`~fivee_sim.model.encounter.Encounter._can_see`. It sits above
    #: Blindsight on that ladder, but is not a strict superset of it: unlike
    #: Blindsight's "even if you have the Blinded condition," Truesight's SRD
    #: text carries no exemption from the observer's own Blinded condition,
    #: so that still gates it, as does Total Cover.
    truesight: int = 0
    hp: int = -1
    #: A buffer spent before hit points. SRD 5.2.1, *Temporary Hit Points*:
    #: "If you have Temporary Hit Points and take damage, those points are
    #: lost first, and any leftover damage carries over to your Hit Points" —
    #: and they "last until they're depleted or you finish a Long Rest." This
    #: engine models no rest, so nothing here clears them on a timer; a
    #: caller states "the party took a long rest" through
    #: ``service/adventures.py``'s ``recovery`` delta, the same channel that
    #: already carries every other rest-shaped fact across an adventure
    #: boundary.
    #:
    #: They are never Hit Points and never healing: "Temporary Hit Points
    #: can't be added to your Hit Points, healing can't restore them, and
    #: receiving Temporary Hit Points doesn't count as healing," and a grant
    #: to a creature at 0 Hit Points "doesn't restore [it] to consciousness."
    #: Granted only through :meth:`grant_temp_hp`, never :meth:`heal`, which
    #: clamps to ``max_hp``, clears both death-save counters and ``stable``,
    #: and lifts Unconscious — every one of those is wrong for a buffer that
    #: is not healing.
    #:
    #: **They Don't Stack** gives the *recipient* the choice of which set to
    #: keep on receiving more while some remain — a player decision this
    #: engine has no channel for at grant time. :meth:`grant_temp_hp` takes
    #: the higher of the two instead, as a deliberate simplification rather
    #: than the rule itself.
    temp_hp: int = 0
    #: Size category. Defaults to Medium, which is what every record written
    #: before the field existed means — and what a character is unless its
    #: species says otherwise. Read by the rules that gate on how big a target
    #: is; it is *not* consulted for movement cost, because Difficult Terrain is
    #: a property of the square and Small and Medium occupy the same one.
    size: Size = Size.MEDIUM
    abilities: dict[Ability, int] = field(default_factory=dict)
    save_bonuses: dict[Ability, int] = field(default_factory=dict)
    #: A stat block's printed skill modifier, keyed by skill name — a plain
    #: ``str``, never a closed enum: ``service/primitives.check`` already
    #: accepts a free-form ``skill`` label validated only for non-blankness,
    #: and this engine keeps a condition an open string for the same reason a
    #: pack may extend one. The value is the printed *absolute* modifier, not a
    #: proficiency to add: SRD stat blocks print totals ("Perception +5"), and
    #: this engine models no character level or proficiency bonus to derive
    #: one from.
    skill_bonuses: dict[str, int] = field(default_factory=dict)
    attacks: tuple[AttackOption, ...] = ()
    attacks_per_action: int = 1
    #: Action kinds this stat block may take as a Bonus Action.  The strings use
    #: encounter action names but stay strings here to preserve the model's
    #: one-way dependency: creatures do not import the encounter that owns them.
    bonus_actions: frozenset[str] = frozenset()
    #: A simple authored morale endpoint: give up when no conscious ally remains.
    surrender_when_last: bool = False
    #: May spend its reaction to exchange places with an adjacent Small or
    #: Medium ally and make that ally the target of an attack.
    redirect_attack: bool = False
    surrendered: bool = False
    #: Pack Tactics, as a flag: the stat block prints it, the encounter resolves
    #: it, because whether a capable ally is within 5 feet of the target is a
    #: question about the whole fight, not about this creature.
    pack_tactics: bool = False
    #: Undead Fortitude, likewise: the drop-to-0 Constitution save that leaves
    #: the creature at 1 hit point. The encounter resolves it too — the save
    #: needs the fight's dice and the dropping damage's types, and this module
    #: rolls nothing.
    undead_fortitude: bool = False
    spells: tuple[str, ...] = ()
    spell_slots: dict[int, int] = field(default_factory=dict)
    spell_save_dc: int = 10
    spell_attack_bonus: int = 0
    #: Which ability this creature casts with, for the spells that scale their
    #: healing by it. ``None`` rather than a default ability: a sheet that never
    #: said contributes nothing, which is what keeps every pack written before
    #: this field resolving exactly as it did. The DC and the attack bonus stay
    #: flat numbers — a stat block prints those, and deriving them would need a
    #: proficiency bonus no creature here carries.
    spellcasting_ability: Ability | None = None
    #: A stat block's printed Initiative bonus, used in place of the Dexterity
    #: modifier when present. ``None`` rather than a defaulted ``0``, for the
    #: same reason ``spellcasting_ability`` defaults to ``None`` above: a sheet
    #: that never said must roll exactly as it always did, and ``0`` is itself
    #: a legitimate printed bonus that has to stay distinguishable from "not
    #: stated". SRD 5.2.1, *Initiative*: "Your Initiative score equals 10 plus
    #: your Dexterity modifier" — a printed exception overrides that formula,
    #: and 33% of the SRD monster catalog prints one. The tie-break on equal
    #: totals stays on the Dexterity modifier regardless: that is the SRD's own
    #: tie-break rule, not a stand-in for this bonus.
    initiative_bonus: int | None = None
    #: A stat block's printed Passive Perception, carried but never consumed.
    #: ``None`` rather than a defaulted ``0``, following ``initiative_bonus``
    #: exactly: a printed Passive Perception does not always equal
    #: ``10 + Wisdom modifier``, which is why it is a fact to transcribe
    #: rather than a number to derive. Nothing in this engine reads it —
    #: there is no Hide, Search, Stealth, or Perception action here for it to
    #: reach — and per the ``hit_dice`` ruling that is why it is carried and
    #: declared rather than silently dropped: an accepted key that does
    #: nothing must say so, not pretend to.
    passive_perception: int | None = None
    conditions: set[str] = field(default_factory=set)
    concentrating_on: str | None = None
    #: Usable items, name to quantity held. Quantity *is* the charge count.
    items: dict[str, int] = field(default_factory=dict)
    #: The condition table this creature's conditions are read against. An
    #: ``Encounter`` overwrites this with its own so a fight cannot end up with
    #: combatants consulting different rules; standalone use gets the SRD set.
    condition_effects: ConditionTable = field(default_factory=lambda: EFFECTS)
    resistances: frozenset[DamageType] = frozenset()
    immunities: frozenset[DamageType] = frozenset()
    vulnerabilities: frozenset[DamageType] = frozenset()
    #: Conditions this creature can never gain — the SRD's "Condition
    #: Immunities" line, Skeleton and Zombie's Poisoned among 81 SRD stat
    #: blocks. A plain ``str`` set, for the same reason a condition on the
    #: creature itself is a plain ``str``: a pack names its own conditions and
    #: this field must not require an enum member to say one is refused.
    #:
    #: :meth:`add_condition` is the one place immunity actually *refuses* a
    #: condition — every path that imposes one funnels through it, directly or
    #: by way of ``Encounter._apply_condition``, so there is exactly one gate
    #: to keep in step. An attack rider's saving throw is the sole exception
    #: allowed to read this set on its own, and only to decide whether to
    #: *roll* at all: an immune target can never fail a save it would also
    #: never fail to be refused for, so rolling one anyway would needlessly
    #: consume the RNG stream, exactly as the size gate already avoids doing.
    #: That query changes no outcome ``add_condition`` would not have reached
    #: on its own — it only spares a roll nobody needed.
    condition_immunities: frozenset[str] = frozenset()
    #: A point in feet; a scalar is accepted and widened to ``(x, 0)``.
    position: Point | int = (0, 0)
    #: Which of the eight grid directions the creature is looking, or ``None``
    #: for a creature whose facing nobody is tracking. A plain ``str`` rather
    #: than a :class:`~fivee_sim.kernel.grid.Facing` member, for the same reason
    #: a condition is a plain ``str``: a pack may name one and the model must
    #: not require an enum member to say it.
    #:
    #: ``None`` is the default and stays the default. Facing changes no roll, so
    #: a creature nobody set one on has no facing to report — defaulting to
    #: north would add a key to every combatant of every fight for a property
    #: nobody chose, and would claim a fact about where they are looking that
    #: nothing established.
    facing: str | None = None
    #: Which storey of the map the creature stands on. Zero — the ground — for
    #: every fight on a map without storeys, which is nearly all of them. The
    #: level is not part of ``position`` because feet along an axis and a choice
    #: of plane are different kinds of fact: two creatures at the same point on
    #: different levels are not near each other at all.
    level: int = 0
    #: Scenario timing, not a stat-block trait. Round 1 means present when the
    #: encounter is created; a later value keeps the combatant off-map until
    #: that round begins.
    arrival_round: int = 1
    arrived: bool = True
    death_save_successes: int = 0
    death_save_failures: int = 0
    stable: bool = False
    dead: bool = False
    death_rule: DeathRule = DeathRule.DEATH_SAVES
    provenance: str = "SRD 5.2.1"

    def __post_init__(self) -> None:
        if self.arrival_round < 1:
            raise ValueError("arrival_round must be at least 1")
        if self.hp < 0:
            self.hp = self.max_hp
        self.position = as_point(self.position)

    # --- construction -----------------------------------------------------
    @classmethod
    def from_record(
        cls,
        record: Mapping[str, Any],
        *,
        condition_effects: ConditionTable,
        source: str,
        label: str | None = None,
        team: str | None = None,
        position: Point | int = 0,
        level: int = 0,
        arrival_round: int = 1,
    ) -> Creature:
        """Build a fresh creature from a validated content record.

        ``condition_effects`` is the table this creature will read its conditions
        against, and ``source`` the provenance to use when the record does not
        carry its own. Both are passed rather than looked up — see the module
        docstring for why that is the seam.

        ``label`` renames the instance, which matters because combatant names
        identify them: two goblins in one fight need distinct labels.
        """
        return cls(
            name=label or str(record["name"]),
            team=team or str(record.get("team", "monsters")),
            ac=int(record["ac"]),
            max_hp=int(record["max_hp"]),
            speed=int(record.get("speed", 30)),
            climb_speed=int(record.get("climb_speed", 0)),
            swim_speed=int(record.get("swim_speed", 0)),
            fly_speed=int(record.get("fly_speed", 0)),
            burrow_speed=int(record.get("burrow_speed", 0)),
            terrain_cost_overrides=frozenset(
                str(entry) for entry in record.get("terrain_cost_overrides", [])
            ),
            darkvision=int(record.get("darkvision", 0)),
            blindsight=int(record.get("blindsight", 0)),
            tremorsense=int(record.get("tremorsense", 0)),
            truesight=int(record.get("truesight", 0)),
            size=Size(record.get("size", Size.MEDIUM)),
            abilities={
                Ability(key): int(value)
                for key, value in record.get("abilities", {}).items()
            },
            save_bonuses={
                Ability(key): int(value)
                for key, value in record.get("save_bonuses", {}).items()
            },
            skill_bonuses={
                str(key): int(value)
                for key, value in record.get("skill_bonuses", {}).items()
            },
            attacks=tuple(
                AttackOption.from_record(entry) for entry in record.get("attacks", [])
            ),
            attacks_per_action=int(record.get("attacks_per_action", 1)),
            bonus_actions=frozenset(
                str(entry) for entry in record.get("bonus_actions", [])
            ),
            surrender_when_last=bool(record.get("surrender_when_last", False)),
            redirect_attack=bool(record.get("redirect_attack", False)),
            pack_tactics=bool(record.get("pack_tactics", False)),
            undead_fortitude=bool(record.get("undead_fortitude", False)),
            spells=tuple(str(entry) for entry in record.get("spells", [])),
            spell_slots={int(k): int(v) for k, v in record.get("spell_slots", {}).items()},
            spell_save_dc=int(record.get("spell_save_dc", 10)),
            spell_attack_bonus=int(record.get("spell_attack_bonus", 0)),
            spellcasting_ability=(
                Ability(record["spellcasting_ability"])
                if record.get("spellcasting_ability") is not None
                else None
            ),
            initiative_bonus=(
                int(record["initiative_bonus"])
                if record.get("initiative_bonus") is not None
                else None
            ),
            passive_perception=(
                int(record["passive_perception"])
                if record.get("passive_perception") is not None
                else None
            ),
            items={str(k): int(v) for k, v in record.get("items", {}).items()},
            immunities=frozenset(
                DamageType(entry) for entry in record.get("immunities", [])
            ),
            resistances=frozenset(
                DamageType(entry) for entry in record.get("resistances", [])
            ),
            vulnerabilities=frozenset(
                DamageType(entry) for entry in record.get("vulnerabilities", [])
            ),
            condition_immunities=frozenset(
                str(entry) for entry in record.get("condition_immunities", [])
            ),
            conditions={str(entry) for entry in record.get("conditions", [])},
            condition_effects=condition_effects,
            position=position,
            level=level,
            arrival_round=arrival_round,
            provenance=str(record.get("provenance", source)),
            death_rule=DeathRule(record.get("death_rule", DeathRule.INSTANT)),
        )

    # --- derived values ---------------------------------------------------
    def ability_score(self, ability: Ability) -> int:
        return self.abilities.get(ability, 10)

    def ability_mod(self, ability: Ability) -> int:
        return ability_modifier(self.ability_score(ability))

    def save_modifier(self, ability: Ability) -> int:
        """Explicit save bonus if the stat block prints one, else the raw modifier."""
        if ability in self.save_bonuses:
            return self.save_bonuses[ability]
        return self.ability_mod(ability)

    def check_modifier(self, ability: Ability, skill: str | None = None) -> int:
        """Explicit skill bonus if the stat block prints one for ``skill``, else
        the raw ability modifier. Mirrors :meth:`save_modifier`'s shape.
        """
        if skill is not None and skill in self.skill_bonuses:
            return self.skill_bonuses[skill]
        return self.ability_mod(ability)

    @property
    def spellcasting_modifier(self) -> int:
        """The modifier a scaling spell adds, or zero for a sheet that named none."""
        if self.spellcasting_ability is None:
            return 0
        return self.ability_mod(self.spellcasting_ability)

    @property
    def conscious(self) -> bool:
        return not self.dead and self.hp > 0

    @property
    def combat_active(self) -> bool:
        """Still opposing the other teams, rather than dead, down, or yielded."""
        return self.conscious and not self.surrendered and self.arrived

    @property
    def contesting(self) -> bool:
        """Still belongs to a side the fight must resolve, even before arrival."""
        return self.conscious and not self.surrendered

    @property
    def dying(self) -> bool:
        return not self.dead and self.hp == 0 and not self.stable

    @property
    def active(self) -> bool:
        """Able to act: conscious and not held by an incapacitating condition."""
        if not self.combat_active:
            return False
        return not any(self._effect(condition).incapacitated for condition in self.conditions)

    def resists(self, damage_type: DamageType) -> bool:
        if damage_type in self.resistances:
            return True
        return any(self._effect(condition).resists_all_damage for condition in self.conditions)

    def distance_to(
        self, other: Creature, rule: DiagonalRule = DiagonalRule.FIVE_FIVE_FIVE
    ) -> int:
        return distance_feet(as_point(self.position), as_point(other.position), rule)

    def _effect(self, condition: str) -> ConditionEffect:
        return effect_of(condition, self.condition_effects)

    # --- mutation ---------------------------------------------------------
    def add_condition(self, condition: str, *, override_immunity: bool = False) -> bool:
        """Impose ``condition`` and report whether it took hold.

        The one chokepoint every condition-imposing path funnels through —
        an attack rider, a spell, an item and a GM ruling all end up here,
        directly or through :meth:`~fivee_sim.model.encounter.Encounter.
        _apply_condition`. An immunity refuses outright and is checked
        *before* the active table is even consulted, so a creature can be
        immune to a condition the table does not define — SRD 5.2.1's Zombie
        and Skeleton both print immunity to Exhaustion, which this engine has
        no table row for, and that immunity must not need one to be legal.

        ``override_immunity`` exists for exactly one caller:
        :meth:`~fivee_sim.model.creature.Creature.take_damage`, which drops a
        creature Unconscious (and Prone with it) as a structural consequence
        of reaching 0 hit points. That is engine machinery rather than an
        effect being *imposed* on the creature — the same standing the SRD
        condition table itself holds inside ``STRUCTURAL_CONDITIONS`` — so it
        is not something an immunity to Unconscious or Prone can refuse.
        """
        if not override_immunity and condition in self.condition_immunities:
            return False
        # Look the effect up first: an unknown name must be refused before it is
        # recorded, or the creature carries a condition nothing can resolve.
        incapacitates = self._effect(condition).incapacitated
        self.conditions.add(condition)
        if incapacitates:
            self.concentrating_on = None
        return True

    def remove_condition(self, condition: str) -> None:
        self.conditions.discard(condition)

    def damage_after_temp_hp(self, amount: int) -> int:
        """How much of ``amount`` would still reach hit points, unspent.

        A read-only preview of the first thing :meth:`take_damage` does,
        used by :meth:`~fivee_sim.model.encounter.Encounter.
        _undead_fortitude_save` to decide *whether to roll at all* before
        any damage is applied: a creature whose buffer would absorb a hit
        entirely never reaches 0, so a save it can also never be asked to
        make must not consume randomness, for the same RNG-conservation
        reason the size and immunity gates beside it already avoid a roll.
        This does not spend the buffer — only :meth:`take_damage` does that.
        """
        return max(0, amount - self.temp_hp)

    def take_damage(self, amount: int, *, critical: bool = False) -> None:
        """Apply damage that has already been adjusted for resistance.

        Temporary Hit Points are spent first. SRD 5.2.1, *Temporary Hit
        Points*, Lose Temporary Hit Points First: "those points are lost
        first, and any leftover damage carries over to your Hit Points."
        Only the leftover reaches the two rules below — damage a buffer
        fully absorbed never happened to hit points at all, so it plays no
        part in either the drop-to-0 reset or the massive-damage overflow
        that follows it.

        Two different rules meet here and the difference is which side of 0 the
        creature started on.

        *Dropping* to 0 knocks the creature out and begins a fresh dying state, so
        its death saves start from nothing. Damage remaining after the drop kills
        outright if it equals or exceeds the creature's maximum hit points —
        and that remainder, ``overflow``, is computed from the post-buffer
        amount for the same reason: SRD 5.2.1's Instant Death compares "the
        damage" that carried past 0 against the maximum, and a buffer that
        absorbed part of the original hit means less of it ever carried past
        0 in the first place.

        Damage taken *while already* at 0 is the other rule: it costs a death
        saving throw failure, two if it came from a critical hit, and the third
        failure kills. It resets nothing — only regaining hit points or becoming
        stable does that — so the drop-to-0 reset must not run a second time.
        """
        if amount <= 0 or self.dead:
            return
        absorbed = min(self.temp_hp, amount)
        self.temp_hp -= absorbed
        amount -= absorbed
        if amount <= 0:
            return
        already_down = self.hp == 0
        overflow = amount - self.hp
        self.hp = max(0, self.hp - amount)
        if self.hp > 0:
            return
        if self.death_rule is DeathRule.INSTANT:
            self.dead = True
            self.concentrating_on = None
            self.conditions.discard(Condition.UNCONSCIOUS)
            return
        if overflow >= self.max_hp:
            self.dead = True
            self.concentrating_on = None
            self.conditions.discard(Condition.UNCONSCIOUS)
            return
        self.stable = False
        if already_down:
            self.death_save_failures += 2 if critical else 1
            if self.death_save_failures >= DEATH_SAVES_TO_DIE:
                self.dead = True
                self.conditions.discard(Condition.UNCONSCIOUS)
            return
        self.death_save_successes = 0
        self.death_save_failures = 0
        self.concentrating_on = None
        # Structural, not imposed: dropping to 0 hit points is engine
        # machinery, so immunity to Unconscious or Prone does not stop it —
        # see the ruling on ``add_condition``.
        self.add_condition(Condition.UNCONSCIOUS, override_immunity=True)
        self.add_condition(Condition.PRONE, override_immunity=True)

    def heal(self, amount: int) -> None:
        if self.dead or amount <= 0:
            return
        was_down = self.hp == 0
        self.hp = min(self.max_hp, self.hp + amount)
        if was_down and self.hp > 0:
            self.stable = False
            self.death_save_successes = 0
            self.death_save_failures = 0
            self.remove_condition(Condition.UNCONSCIOUS)

    def grant_temp_hp(self, amount: int) -> None:
        """Grant Temporary Hit Points, never routed through :meth:`heal`.

        SRD 5.2.1, *Temporary Hit Points*, They're Not Hit Points or Healing:
        "Temporary Hit Points can't be added to your Hit Points, healing
        can't restore them, and receiving Temporary Hit Points doesn't count
        as healing... If you have 0 Hit Points, receiving Temporary Hit
        Points doesn't restore you to consciousness." ``heal`` clamps to
        ``max_hp``, clears both death-save counters and ``stable``, and
        lifts Unconscious — every one of those is exactly what this clause
        forbids, so a grant needs its own method rather than a shared one.

        They Don't Stack: this engine has no player-choice channel at grant
        time, so it takes the higher of what the creature already carries
        and what is offered, as a deliberate simplification rather than the
        SRD's own recipient's-choice rule.
        """
        if self.dead or amount <= 0:
            return
        self.temp_hp = max(self.temp_hp, amount)
