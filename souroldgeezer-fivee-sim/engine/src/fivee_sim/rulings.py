"""Where SRD 5.2.1 does not decide, and what this engine decided instead.

A simulator cannot decline to answer.  Where the printed rules leave a question
to the table, or say something no field here can carry, somebody has already
made the call — and until this module existed those calls lived only in the
docstring of whichever function made them.  A docstring is found by someone
already reading that function, which is precisely not the person who is about
to invalidate it.

Four things follow from that, and they are the whole design:

**Every entry names a trigger.**  :attr:`Ruling.revisit` says what would make
the decision wrong.  The Loading gate is correct today *and* names the change
that ends it; that sentence is worth more than the decision it qualifies.

**Every entry that governs code points at the code.**  A site is checked
against the source tree, so a rename turns the register red rather than stale.
The matching ``# ruling: <code>`` comment at the site closes the loop from the
other direction, and ``tests/test_rulings.py`` derives both halves.  This is
the ``unmodelled_facts`` lesson: a declaration nobody is obliged to write ends
up measuring who was looking.

**Only genuinely open readings are graded.**  An approximation has no rules
question — the printed rule is clear and we model it coarser on purpose — so
grading it against outside opinion would invent a controversy.  Only
:attr:`RulingKind.SRD_SILENT` entries carry a real :class:`Concurrence`.

**Citations, never quotations.**  :attr:`Ruling.basis` names a section and a
page.  This module ships, and rules prose does not ship.

Provenance of every citation: SRD 5.2.1 (see NOTICE).  Nothing here reproduces
third-party rules text or names a third-party source; the survey behind the
:class:`Concurrence` verdicts is repo-internal and does not ship.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ._generated_document import write_generated_document


class RulingKind(StrEnum):
    """Why a decision had to be made at all."""

    #: No printed rule answers the question.  We invented one.
    SRD_SILENT = "srd_silent"
    #: The printed rule is clear; we model it at a coarser granularity.
    APPROXIMATION = "approximation"
    #: The SRD says it and no field here can carry it.
    SCHEMA_CEILING = "schema_ceiling"
    #: Deliberately outside what this engine simulates.
    OUT_OF_SCOPE = "out_of_scope"
    #: Closed by a release.  Kept because prior reviews still cite it.
    SUPERSEDED = "superseded"


class Concurrence(StrEnum):
    """How our answer sits against readings outside the printed text.

    Only :attr:`RulingKind.SRD_SILENT` entries are graded.  The distinction the
    survey has to keep is between an *authoritative* answer — official errata
    and designer rulings — and a *common* one, which is evidence about what
    players expect rather than about what the rule is.
    """

    #: Not an open reading: the printed rule is clear and we model it coarser.
    NOT_A_RULES_QUESTION = "not_a_rules_question"
    #: Genuinely open, and nobody authoritative has answered it.
    NO_EXTERNAL_RULING = "no_external_ruling"
    #: Our answer is the one most tables land on.
    MATCHES_COMMON_READING = "matches_common_reading"
    #: We knowingly sit off the common reading.
    DIVERGES = "diverges"


@dataclass(frozen=True, slots=True)
class Ruling:
    """One adjudication, and what would overturn it."""

    #: Stable identifier.  Shares its shape with ``content.py``'s omission codes
    #: so the two are comparable strings rather than two spellings of one idea.
    code: str
    kind: RulingKind
    #: What the printed rules leave open, in one line.
    question: str
    #: What this engine does about it.
    decision: str
    #: Why this rather than the obvious alternative.
    because: str
    #: SRD citations: section name, and a page where one pins it.  Never
    #: quoted rules text — see the module docstring.
    basis: tuple[str, ...]
    #: How the decision sits against outside readings.  Meaningful only for
    #: :attr:`RulingKind.SRD_SILENT`; everything else is
    #: :attr:`Concurrence.NOT_A_RULES_QUESTION`.
    concurrence: Concurrence
    #: What would make this ruling wrong.  The field the register exists for,
    #: and required of every live entry by ``tests/test_rulings.py``.  Empty is
    #: legal only for a closed one, which has nothing left to overturn.
    revisit: str = ""
    #: ``path/from/fivee_sim.py:Symbol`` or ``...:Class.method``.  Empty for a
    #: ruling about something the engine cannot express: there is no line to
    #: point at, and that absence is the ruling.
    sites: tuple[str, ...] = ()
    #: ``unmodelled_facts`` codes that bundled records carry *because* of this
    #: ruling.  Named rather than matched by string: a record's code says "this
    #: record drops a printed feature" and a ruling says "the engine decided
    #: this", and collapsing the two vocabularies would lose that difference.
    #: ``tests/test_rulings.py`` checks each one still occurs in the data.
    omission_codes: tuple[str, ...] = ()
    #: CalVer release that closed it, for :attr:`RulingKind.SUPERSEDED`.
    superseded_by: str = ""


def _ruling(**kwargs: object) -> Ruling:
    """Constructor that defaults ``concurrence`` from ``kind``.

    Everything but an open reading is ``NOT_A_RULES_QUESTION``, and repeating
    that on every entry invites someone to grade an approximation by accident.
    """
    kind = kwargs["kind"]
    kwargs.setdefault(
        "concurrence",
        Concurrence.NOT_A_RULES_QUESTION
        if kind is not RulingKind.SRD_SILENT
        else Concurrence.NO_EXTERNAL_RULING,
    )
    return Ruling(**kwargs)  # type: ignore[arg-type]


RULINGS: tuple[Ruling, ...] = (
    # --- no printed rule answers it ---------------------------------------
    _ruling(
        code="climb_cost_boundary",
        kind=RulingKind.SRD_SILENT,
        question=(
            "The SRD prices a climb but never says when a rise stops being ground "
            "you walk up and starts being a face you climb. That trigger is the DM's."
        ),
        decision=(
            "Three bands on the height change alone: under SLOPE_DIFFICULT_FEET the "
            "square costs its ordinary terrain price, up to CLIMB_FEET it is Difficult "
            "Terrain, and above it the climb surcharge applies on top of the step."
        ),
        because=(
            "A grid answers per square with no DM in the loop, so the trigger has to be "
            "a number. The cost therefore jumps at the boundary — a 5-foot rise onto "
            "ordinary ground costs 10 feet and a 6-foot rise costs 17 — and that step is "
            "the price of ruling a boundary at all rather than inventing a graduated "
            "scale the printed rules do not have."
        ),
        basis=(
            "SRD 5.2.1, Climbing, Swimming, and Crawling",
            "SRD 5.2.1, Difficult Terrain",
        ),
        concurrence=Concurrence.NO_EXTERNAL_RULING,
        revisit=(
            "Both constants are module-level and neither is settable by a content pack. "
            "A campaign that wants a different threshold has to patch the kernel. Make "
            "them pack-settable before anyone argues about the number."
        ),
        sites=("kernel/grid.py:step_cost_feet",),
    ),
    _ruling(
        code="cross_storey_sight_needs_a_link",
        kind=RulingKind.SRD_SILENT,
        question=(
            "The SRD has no three-dimensional cover model, so nothing says what a "
            "creature one storey up can see or be shot by."
        ),
        decision=(
            "A floor is opaque: a target on another level has total cover, unless the "
            "square the effect is measured from carries a map-authored sight link "
            "naming that level, which then grants sight to the whole of it."
        ),
        because=(
            "Opaque by default is what stops a fighter shooting the ceiling out from "
            "under someone standing at the same square one level up. The exception is "
            "authored per square because the map knows where the balcony is and the "
            "rules do not."
        ),
        basis=("SRD 5.2.1, Cover",),
        # An opaque floor with authored openings is the shape most tables use;
        # the coarseness below sits inside that reading rather than against it.
        concurrence=Concurrence.MATCHES_COMMON_READING,
        revisit=(
            "The link is read at the origin square only and grants a whole level at "
            "once: a creature one square back from a balcony rail sees nothing, and one "
            "on the far side of the upper floor is seen regardless of distance or what "
            "stands between. Narrow it when a map needs sight between specific squares."
        ),
        sites=("model/encounter.py:Encounter._cover_from_square",),
    ),
    _ruling(
        code="sight_ignores_elevation",
        kind=RulingKind.SRD_SILENT,
        question=(
            "Within one storey the SRD says nothing about ground height blocking "
            "sight. Whether a ridge hides what is behind it is the DM's call."
        ),
        decision=(
            "Line of sight is computed from squares alone. Elevation never blocks a "
            "line and never grants one; it reaches movement cost and nothing else."
        ),
        because=(
            "The grid primitives are two-dimensional by design. A height-aware line "
            "needs a volume model the map document does not carry, and inventing one "
            "inside a sight test would put a second, disagreeing notion of terrain "
            "beside the one movement already uses."
        ),
        basis=("SRD 5.2.1, Cover", "SRD 5.2.1, Vision and Light"),
        # The printed sight test is object-based, and an unoccupied higher square
        # is not an object that blocks vision — so flat sight is what the
        # procedure produces, not a departure from it.
        concurrence=Concurrence.MATCHES_COMMON_READING,
        revisit=(
            "The moment a map needs a rampart or a ridge to break line of sight, or "
            "elevation gains a second consumer beyond step cost."
        ),
        sites=("kernel/grid.py:has_line_of_sight",),
    ),
    _ruling(
        code="speed_reduction_reaches_every_movement_mode",
        kind=RulingKind.SRD_SILENT,
        question=(
            "Exhaustion reduces 'your Speed' by feet per level. A creature with "
            "more than one Speed has to choose which one applies to a numeric "
            "reduction stated once, in the singular."
        ),
        decision=(
            "The reduction comes off every movement mode a creature has — walk, "
            "climb, swim, fly, and burrow alike — clamped at 0, never negative."
        ),
        because=(
            "Grappled's Speed clause reads identically ('Your Speed is 0') and this "
            "engine already reads it as covering every mode: _do_move refuses "
            "regardless of movement_mode. Reading Exhaustion's numeric clause the "
            "other way would make the same three words mean two different things in "
            "one condition table. The Speed glossary entry reinforces it: a creature "
            "with more than one Speed chooses which to use for a given move, so the "
            "modes are alternatives drawing on one budget rather than independent "
            "totals — a reduction a creature could dodge by choosing to fly would be "
            "no reduction at all."
        ),
        basis=("SRD 5.2.1, Conditions, Exhaustion", "SRD 5.2.1, Rules Glossary, Speed"),
        concurrence=Concurrence.NO_EXTERNAL_RULING,
        revisit=(
            "No survey of outside readings backs this yet — it is argued from the "
            "table's own Grappled precedent and the Speed glossary alone. Revisit if "
            "a printed clause ever states a Speed reduction that is meant to apply to "
            "one named mode only, which would mean the modes are not always drawing "
            "on one shared budget."
        ),
        sites=("model/creature.py:Creature.speed_for",),
    ),
    # --- the printed rule is clear; we model it coarser --------------------
    _ruling(
        code="loading_capped_per_turn",
        kind=RulingKind.APPROXIMATION,
        question=(
            "Loading caps the weapon at one attack per activation — an action, a "
            "Bonus Action or a Reaction — and a turn may hold more than one."
        ),
        decision="The gate is per turn: one Loading shot per turn, tracked on the turn state.",
        because=(
            "Behaviourally identical under everything this stepper does today. Nothing "
            "here consults a bonus-action flag when attacking, and the only reaction "
            "attack picks a melee option, so it can never be a Loading one."
        ),
        basis=("SRD 5.2.1, Equipment, Weapon Properties, Loading",),
        revisit=(
            "Give the stepper a Bonus Action attack or a ranged reaction and the two "
            "readings part company. That is the moment to move the flag off the turn "
            "state and onto whatever represents an activation."
        ),
        sites=("model/encounter.py:Encounter._do_attack",),
    ),
    _ruling(
        code="declared_climb_zeroes_the_rise",
        kind=RulingKind.APPROXIMATION,
        question=(
            "A Climb Speed lets you ignore the extra cost of climbing. It says "
            "nothing about whether the ground you are on is still Difficult Terrain."
        ),
        decision=(
            "Electing the climb movement mode sets the height change to zero, which "
            "exempts the mover from the slope band as well as from the climb surcharge."
        ),
        because=(
            "The two bands are decided by one number, so exempting the surcharge alone "
            "would mean passing the mode down into the kernel primitive — and that "
            "function deliberately takes no creature."
        ),
        basis=(
            "SRD 5.2.1, Climbing, Swimming, and Crawling",
            "SRD 5.2.1, Difficult Terrain",
        ),
        revisit=(
            "A climber crossing a gentle rise through undergrowth pays ordinary cost "
            "where the printed rules would still charge Difficult Terrain. Split the "
            "surcharge from the band when a map makes that difference matter."
        ),
        sites=("model/encounter.py:Encounter._step_cost",),
    ),
    _ruling(
        code="movement_mode_ungated_by_terrain",
        kind=RulingKind.APPROXIMATION,
        question=(
            "A Swim Speed needs water, a Burrow Speed needs something to burrow "
            "through, a Fly Speed needs open air. The printed rule assumes the "
            "terrain a mode needs is actually there before a creature draws on it."
        ),
        decision=(
            "The turn's movement budget is the highest of every mode a creature "
            "has, with no check that the square it occupies offers what that mode "
            "requires. A swim speed counts on dry land; a burrow speed counts in "
            "open air; a fly speed counts underground."
        ),
        because=(
            "This predates the wave for swim, climb and fly; burrow joined the "
            "same rule rather than inventing a gate for one mode alone. A grid "
            "square carries a terrain price, not a per-mode legality flag, and "
            "adding one is a single decision for all five modes together."
        ),
        basis=("SRD 5.2.1, Rules Glossary, Speed",),
        revisit=(
            "A real gate is one decision covering all five modes at once, not a "
            "burrow-shaped patch. The day a map needs a creature refused a swim "
            "move on dry ground is the day to design it, for every mode together."
        ),
        sites=("model/encounter.py:Encounter._fresh_turn_state",),
    ),
    _ruling(
        code="cylinder_height_unread",
        kind=RulingKind.APPROXIMATION,
        question=(
            "The SRD gives a Cylinder both a radius and a height. Areas here are "
            "two-dimensional."
        ),
        decision=(
            "A cylinder resolves as its radius. The height is parsed, validated and "
            "carried on the spell, and resolution never reads it."
        ),
        because=(
            "Declared explicitly rather than silently ignored: a pack author who "
            "transcribes the printed height gets a record that keeps it, so the day "
            "areas gain a third dimension the data is already there."
        ),
        basis=("SRD 5.2.1, Spells, Areas of Effect",),
        revisit=(
            "Any spell whose outcome differs by height — something that spares a "
            "creature under or over the cylinder — is resolved wrongly today with no "
            "diagnostic anywhere."
        ),
        sites=("kernel/spells.py:Spell.height",),
    ),
    _ruling(
        code="interlude_expires_no_timed_effect",
        kind=RulingKind.APPROXIMATION,
        question=(
            "An ongoing effect ends on a turn boundary: a phase, and the creature "
            "whose turn it is. An interlude has no turns and no rounds to end."
        ),
        decision=(
            "Nothing anchored to a turn boundary expires inside an interlude. The "
            "release runs from advancing a turn, which an interlude refuses, so an "
            "effect applied in one holds until the chapter is finalized."
        ),
        because=(
            "A beat is not a turn boundary, and expiring on one would have to invent "
            "the parts a boundary is made of — whose turn ended, and how many have "
            "passed since the effect landed. Both answers would be made up, and one "
            "of them silently decides how long a rider lasts."
        ),
        basis=("SRD 5.2.1, Spells, Duration", "SRD 5.2.1, The Order of Combat"),
        revisit=(
            "A condition imposed during an interlude walks into the next chapter, "
            "because conditions are carried state and the effect ledger holding it "
            "is not — so the thing that would have lifted it does not cross. Any "
            "content that lands a timed rider out of combat needs this before it is "
            "playable; today the coarseness is invisible and permanent."
        ),
        sites=("model/encounter.py:Encounter._begin_beat",),
    ),
    _ruling(
        code="interlude_beat_restores_the_budget",
        kind=RulingKind.APPROXIMATION,
        question=(
            "The action economy belongs to a turn: one action, one bonus action, a "
            "movement budget. Outside combat the printed rules track none of it."
        ),
        decision=(
            "Each named act opens a fresh beat — movement back to the actor's speed, "
            "action and bonus action unspent, reaction restored — so nothing "
            "accumulates across an interlude and no budget ever runs out."
        ),
        because=(
            "This stepper knows turns and nothing else, so the choice was which turn "
            "to give a beat rather than whether to give it one. A budget that ran out "
            "would strand a party 30 feet into a mill floor with no way to keep "
            "walking; refreshing puts the only cap inside a single act, where it still "
            "charges real terrain, and leaves the number of acts to the caller — which "
            "is where the printed rules leave it."
        ),
        basis=("SRD 5.2.1, Exploration", "SRD 5.2.1, Your Turn"),
        revisit=(
            "Two consequences pull opposite ways and neither is visible from a green "
            "run: one act cannot exceed the actor's speed, so a long walk is several "
            "journal beats rather than one, and nothing bounds the beats, so a "
            "creature can attack once per act for as long as the caller keeps asking. "
            "An interlude that has to *cost* something — an exploration turn, a chase, "
            "ammunition — has to decide what a beat is before it can charge for it."
        ),
        sites=("model/encounter.py:Encounter._begin_beat",),
    ),
    _ruling(
        code="temp_hp_grant_takes_the_higher_value",
        kind=RulingKind.APPROXIMATION,
        question=(
            "Temporary Hit Points, They Don't Stack: the recipient chooses whether "
            "to keep what they have or take the new grant. This engine has no "
            "player-choice channel at grant time."
        ),
        decision=(
            "Creature.grant_temp_hp takes the higher of what the creature already "
            "carries and what is offered, rather than asking anyone."
        ),
        because=(
            "There is no channel through which a grant can pause and put the "
            "choice to the recipient, and the higher value is the choice a player "
            "would make anyway whenever the two amounts differ, so defaulting to "
            "it costs nothing a real choice would have kept."
        ),
        basis=("SRD 5.2.1, Temporary Hit Points",),
        revisit=(
            "The day a grant can carry a real choice — an interactive session "
            "where the recipient answers a prompt rather than the engine picking "
            "for them — is the day this reverts to the printed rule."
        ),
        sites=("model/creature.py:Creature.grant_temp_hp",),
    ),
    _ruling(
        code="effect_release_drops_the_whole_condition",
        kind=RulingKind.APPROXIMATION,
        question=(
            "A cumulative condition like Exhaustion is held at a level, and more "
            "than one source can be adding to it. The printed rule tracks the "
            "level; nothing says an ending effect should remove more than its own "
            "contribution."
        ),
        decision=(
            "Encounter._release_effect calls remove_condition(effect.condition) "
            "outright once it is the last ledger effect holding that condition, "
            "dropping the whole entry rather than the levels this one effect "
            "contributed."
        ),
        because=(
            "The ledger's stacked/remaining guards protect a condition already "
            "held before this effect began, or still held by another live "
            "effect, but neither guard is re-checked at release time: a level "
            "added by something outside the ledger — a table ruling, most "
            "concretely — after this effect started is not accounted for and is "
            "stripped along with it."
        ),
        basis=(
            "SRD 5.2.1, Conditions, Exhaustion",
            "SRD 5.2.1, Rules Glossary, Concentration",
        ),
        revisit=(
            "A pack that imposes one level of a cumulative condition through a "
            "timed or concentration effect, on a creature that also picks up a "
            "level from a table ruling or another channel while that effect is "
            "still active, is misresolved the moment the first effect lapses. "
            "Give remove_condition's levels parameter a real caller here before "
            "that content ships."
        ),
        sites=("model/encounter.py:Encounter._release_effect",),
    ),
    # --- the SRD says it and no field can carry it -------------------------
    _ruling(
        code="no_trait_vocabulary",
        kind=RulingKind.SCHEMA_CEILING,
        question=(
            "208 of the SRD's 336 stat blocks carry traits, across 124 distinct "
            "trait names. Magic Resistance and Legendary Resistance lead the count."
        ),
        decision=(
            "Traits are named booleans on the creature model. Three exist. Every other "
            "trait is transcribed as prose nothing executes, or not at all."
        ),
        because=(
            "A boolean per trait was the cheapest thing that worked for three, and it "
            "defers the design question rather than answering it: what shape does a "
            "trait take such that 124 of them are data?"
        ),
        basis=("SRD 5.2.1, Monsters",),
        revisit=(
            "This is the ceiling every further creature sits behind — a pack cannot "
            "add one, because the gap is code rather than transcription. Designing the "
            "vocabulary is a plan of its own, not a fix."
        ),
    ),
    _ruling(
        code="no_skill_or_proficiency_concept",
        kind=RulingKind.SUPERSEDED,
        question="216 of 336 stat blocks print skills. Ability checks take a proficiency.",
        decision=(
            "A creature carries printed skill bonuses and a map-fixture check may name "
            "the skill it wants, so the bundled records that dropped their skills no "
            "longer declare an unsupported_creature_skills omission. Passive Perception "
            "stayed unmodelled and is now its own entry."
        ),
        because=(
            "The ceiling was one field and one consumer wide, not a design question: "
            "stat blocks print skill totals rather than proficiencies, so the same "
            "printed-absolute shape save_bonuses already used carried them, and the "
            "engine's one ability-check site only needed to be able to name a skill."
        ),
        basis=("SRD 5.2.1, Proficiency", "SRD 5.2.1, Monsters"),
        superseded_by="2026.08.69",
    ),
    _ruling(
        code="skills_are_printed_absolutes",
        kind=RulingKind.SCHEMA_CEILING,
        question=(
            "A skill bonus is a Proficiency Bonus, possibly doubled by Expertise, added "
            "to an ability modifier, and Help can grant Advantage on the check."
        ),
        decision=(
            "A creature carries a flat printed total per skill. No proficiency bonus, "
            "Expertise, or Help exists, and only a map-fixture check can name a skill — "
            "the standalone check operation still takes its modifier from the caller."
        ),
        because=(
            "Stat blocks print the total, so a transcriber never has the breakdown to "
            "enter, and deriving one would mean inventing a level the monster does not "
            "have. The same shape save_bonuses already used carries it."
        ),
        basis=("SRD 5.2.1, Proficiency", "SRD 5.2.1, Monsters"),
        revisit=(
            "A player character whose Proficiency Bonus rises cannot be modelled by "
            "changing one number, and nothing can grant Advantage by helping. Hide, "
            "Search and Study remain unbuildable for the same reason."
        ),
    ),
    _ruling(
        code="passive_perception_transcribed_only",
        omission_codes=(
            "unsupported_passive_perception",
        ),
        kind=RulingKind.SCHEMA_CEILING,
        question="333 of 336 stat blocks print a Passive Perception.",
        decision=(
            "A creature record may carry the printed number and nothing reads it. The "
            "bundled records that print one still declare it as an omission."
        ),
        because=(
            "There is no Hide, Search, or Study action for it to be compared against, "
            "so a consumer would have to be invented before the field could do "
            "anything. Carrying it keeps a faithful transcription from being re-derived "
            "later; declaring it keeps the coverage report from claiming a simulation "
            "that does not exist."
        ),
        basis=("SRD 5.2.1, Perception", "SRD 5.2.1, Monsters"),
        revisit=(
            "The moment a hidden creature is expressible, this number is what an "
            "onlooker's passive Perception must beat, and the omission codes on five "
            "bundled records become closable."
        ),
    ),
    _ruling(
        code="tremorsense_carried_and_unconsumed",
        kind=RulingKind.SCHEMA_CEILING,
        question=(
            "Tremorsense pinpoints a creature's location within range and "
            "explicitly 'doesn't count as a form of sight.'"
        ),
        decision=(
            "Creature.tremorsense is transcribed and reported. Encounter._can_see, "
            "the engine's only sight predicate, has no rung for it and no rule "
            "consults the field."
        ),
        because=(
            "Every consumer of _can_see only has 'can see' and 'cannot see' to "
            "choose between, and answering True for a Tremorsense-only observer "
            "would wrongly cancel the unseen-target Disadvantage against a "
            "creature it has pinpointed but still cannot see. The same "
            "declared-but-inert standing as passive_perception_transcribed_only: "
            "carrying a printed number with no consumer to spend it on."
        ),
        basis=("SRD 5.2.1, Rules Glossary, Tremorsense",),
        revisit=(
            "The day this engine gains a pinpoint-without-sight state — "
            "something between 'can see' and 'cannot see' — is the day "
            "Tremorsense has a rung of its own to occupy in _can_see."
        ),
    ),
    _ruling(
        code="touch_range_transcribed_as_five_feet",
        omission_codes=(
            "unsupported_touch_range",
        ),
        kind=RulingKind.SCHEMA_CEILING,
        question=(
            "A spell's range may be Touch. A spell record carries a range in feet, "
            "where zero already means unranged."
        ),
        decision=(
            "Touch is transcribed as 5 feet, and the bundled records declare it "
            "as an omission."
        ),
        because=(
            "Zero is taken: it is the value that skips the range check entirely, so a "
            "literal transcription of Touch would make the spell reach the whole map. "
            "Five feet is the nearest honest number."
        ),
        basis=("SRD 5.2.1, Spells, Range",),
        revisit=(
            "Touch and a 5-foot range differ wherever a rule turns on contact rather "
            "than distance. The trap is the transcription: a pack author who writes "
            "zero for Touch gets unlimited range with no diagnostic."
        ),
    ),
    _ruling(
        code="no_recharge_mechanic",
        kind=RulingKind.SCHEMA_CEILING,
        question=(
            "88 of the SRD's stat blocks gate an ability behind a Recharge roll at "
            "the start of the creature's turn."
        ),
        decision=(
            "Nothing in the engine reads Recharge. The catalog transcribes the value "
            "as a printed fact; no kernel, model, or service code consults it."
        ),
        because=(
            "An action's availability is decided by the turn budget, which counts "
            "actions rather than tracking per-ability state. Recharge needs a "
            "per-ability cooldown the action model has no place for."
        ),
        basis=("SRD 5.2.1, Monsters, Recharge",),
        revisit=(
            "Any creature whose threat is its breath weapon is unsimulatable — it "
            "either never uses it or uses it every round, and neither is the fight."
        ),
    ),
    _ruling(
        code="long_rest_exhaustion_removal_is_unreachable",
        kind=RulingKind.SCHEMA_CEILING,
        question=(
            "Exhaustion names a Long Rest as the thing that removes a level, and "
            "sets no other way to shed one."
        ),
        decision=(
            "This engine models no rest of any length. The only channel that "
            "reaches a carried combatant's Exhaustion level is "
            "``adventures.link_encounter``'s caller-supplied ``recovery`` delta, "
            "which is already where a caller says a long rest happened, for hit "
            "points and every other carried field."
        ),
        because=(
            "A combat stepper has rounds and turns, not the minutes or hours a rest "
            "takes, so there is no clock here for a long rest to finish against. "
            "``recovery`` already exists for exactly this shape of gap and needed no "
            "new field to carry Exhaustion's."
        ),
        basis=("SRD 5.2.1, Conditions, Exhaustion", "SRD 5.2.1, Resting"),
        revisit=(
            "Simulating rest at all — even a bare 'a long rest happened' operation "
            "between encounters — would give this a real site to point at instead "
            "of a caller-supplied number, and this entry should close in favour of "
            "one that names it."
        ),
    ),
    _ruling(
        code="no_round_clock_for_durations",
        kind=RulingKind.SUPERSEDED,
        question=(
            "225 SRD spells carry a real duration and 133 are Concentration. "
            "Durations are printed in rounds, minutes and hours."
        ),
        decision=(
            "A spell may declare a duration in rounds, and an ongoing effect it "
            "creates is released when that many rounds have passed. Hold Person "
            "carries its 1-minute cap and no longer declares it as an omission."
        ),
        because=(
            "The round counter the encounter already advanced was the whole missing "
            "half: nothing in the effect ledger read it. A concentration spell now "
            "ends on whichever arrives first, its cap or a broken concentration."
        ),
        basis=("SRD 5.2.1, Spells, Duration", "SRD 5.2.1, Rules Glossary, Concentration"),
        superseded_by="2026.08.69",
    ),
    # --- deliberately outside what this engine simulates --------------------
    _ruling(
        code="object_and_world_effects",
        omission_codes=(
            "unsupported_object_effect",
            "unsupported_limb_regrowth",
        ),
        kind=RulingKind.OUT_OF_SCOPE,
        question=(
            "Many spells act on the world as well as on creatures: igniting "
            "flammable objects, damaging unattended ones, regrowing a severed limb."
        ),
        decision=(
            "The engine resolves effects on creatures. World and object effects are "
            "declared per record as omissions and are the table's to narrate."
        ),
        because=(
            "Objects have no representation here and giving them one is a simulation "
            "of a different size. Declaring the gap per record keeps the omission "
            "visible to whoever reads the spell."
        ),
        basis=("SRD 5.2.1, Spells",),
        revisit=(
            "A fight whose outcome turns on burning the rope bridge is one this engine "
            "cannot adjudicate, and it will not say so at the time."
        ),
    ),
    _ruling(
        code="out_of_combat_time",
        omission_codes=(
            "unsupported_casting_time",
            "unsupported_short_rest_benefit",
        ),
        kind=RulingKind.OUT_OF_SCOPE,
        question=(
            "Casting times run to minutes, and some effects grant the benefit of a "
            "rest. A combat stepper has rounds."
        ),
        decision=(
            "Anything longer than an action is modelled as an ordinary action-cost "
            "cast, with the real casting time declared per record as an omission."
        ),
        because=(
            "The alternative is refusing to model the spell at all, which loses its "
            "damage and healing as well as its timing."
        ),
        basis=("SRD 5.2.1, Spells, Casting Time", "SRD 5.2.1, Resting"),
        revisit=(
            "A ten-minute cast used mid-fight is a real rules error the engine will "
            "accept without comment. An adventure that leans on ritual timing needs "
            "this before it can be simulated."
        ),
    ),
    # --- closed, and kept because earlier reviews still cite them -----------
    _ruling(
        code="surprise_had_no_initiative_rider",
        kind=RulingKind.SUPERSEDED,
        question=(
            "Surprise is Disadvantage on one Initiative roll, and Invisible grants "
            "Advantage on it. Neither rider reached the roll."
        ),
        decision=(
            "Incapacitated and Invisible now carry their Initiative riders, and the "
            "Initiative roll consults a channel separate from ordinary ability checks."
        ),
        because=(
            "The plumbing was already complete end to end — the roll went through the "
            "advantage machinery with the creature's conditions — and delivered nothing "
            "because the one flag it read was missing from the row that owned it. "
            "Splitting the channels came second: the flag reused for Initiative had a "
            "second caller in map-fixture ability checks, and would otherwise have "
            "granted an Invisible creature Advantage on every lever-pull."
        ),
        basis=("SRD 5.2.1, Rules Glossary, Incapacitated", "SRD 5.2.1, Rules Glossary, Invisible"),
        superseded_by="2026.08.66",
    ),
    _ruling(
        code="opportunity_attack_fixed_five_feet",
        kind=RulingKind.SUPERSEDED,
        question=(
            "An opportunity attack triggers when a creature leaves your reach. The "
            "trigger tested a fixed 5 feet, so a reach-10 creature never got one."
        ),
        decision="The trigger reads the melee option's own reach, and requires line of sight.",
        because=(
            "92 of 336 SRD stat blocks have a melee attack beyond 5 feet. All six "
            "bundled creatures have reach 5, so nothing was visibly wrong and nothing "
            "failed — which is what made it worth fixing before more content arrived."
        ),
        basis=("SRD 5.2.1, Rules Glossary, Opportunity Attack",),
        superseded_by="2026.08.66",
    ),
    _ruling(
        code="invisible_advantage_unconditional",
        kind=RulingKind.SUPERSEDED,
        question=(
            "Invisible stops helping against a creature that can somehow see you. "
            "The engine applied its Advantage and Disadvantage unconditionally."
        ),
        decision=(
            "The condition row no longer carries the attack flags at all; sight "
            "derives them, for weapon and spell attacks alike."
        ),
        because=(
            "The fix was a deletion. Two derivations of one rule existed and "
            "disagreed, and removing the unconditional copy exposed a third caller "
            "carrying neither sight term — so the cast path had been silently "
            "disagreeing with the swing path about who is visible."
        ),
        basis=("SRD 5.2.1, Rules Glossary, Invisible",),
        superseded_by="2026.08.66",
    ),
    _ruling(
        code="thrown_attack_kind",
        kind=RulingKind.SUPERSEDED,
        question=(
            "20 SRD stat blocks print a Melee or Ranged attack. The attack kind had "
            "two members, so a thrown weapon had to be transcribed as one or the other."
        ),
        decision=(
            "A thrown rider on the ranged kind: melee within reach, ranged beyond it. "
            "The Ogre's javelin no longer takes close-combat Disadvantage at 5 feet."
        ),
        because=(
            "A third enum member would have type-checked and run and been wrong in "
            "five places, because every melee test in the tree is two-valued and a "
            "non-exhaustive identity chain is not flagged. A new member fails "
            "silently here; a rider fails loudly."
        ),
        basis=("SRD 5.2.1, Equipment, Weapon Properties, Thrown",),
        superseded_by="2026.08.66",
    ),
    _ruling(
        code="initiative_is_dex_only",
        kind=RulingKind.SUPERSEDED,
        question=(
            "110 of 332 SRD stat blocks print an Initiative bonus that is not the "
            "Dexterity modifier. The engine derived initiative from the modifier."
        ),
        decision=(
            "A stat block may print its own Initiative bonus, which replaces the "
            "derivation. The tie-break stays on the Dexterity modifier."
        ),
        because=(
            "All six bundled creatures happened to agree, so nothing was wrong and "
            "nothing would have failed — a pack transcribing any other SRD monster "
            "was simply wrong a third of the time, with no diagnostic anywhere."
        ),
        basis=("SRD 5.2.1, Initiative",),
        superseded_by="2026.08.66",
    ),
    _ruling(
        code="no_condition_immunity",
        kind=RulingKind.SUPERSEDED,
        question=(
            "81 SRD stat blocks list condition immunities. The creature model carried "
            "damage immunity, resistance and vulnerability, and no condition equivalent."
        ),
        decision=(
            "A creature names the conditions it can never gain, and a condition the "
            "table does not define is still a legal immunity to declare."
        ),
        because=(
            "Two of the six bundled creatures already declared the gap as an "
            "unmodelled fact, which is the ledger doing its job: the omission was "
            "written down before it was closed."
        ),
        basis=("SRD 5.2.1, Monsters, Condition Immunities",),
        superseded_by="2026.08.66",
    ),
)


#: Ordering for the report: open readings first, closed ones last, because the
#: first question a reader has is "what is still undecided?"
_KIND_ORDER: tuple[RulingKind, ...] = (
    RulingKind.SRD_SILENT,
    RulingKind.APPROXIMATION,
    RulingKind.SCHEMA_CEILING,
    RulingKind.OUT_OF_SCOPE,
    RulingKind.SUPERSEDED,
)

_KIND_HEADING: dict[RulingKind, tuple[str, str]] = {
    RulingKind.SRD_SILENT: (
        "Where the rules do not decide",
        "No printed rule answers these. The engine had to, because a simulator "
        "cannot hand the question back to the table.",
    ),
    RulingKind.APPROXIMATION: (
        "Modelled coarser than printed",
        "The rule is clear and the engine deliberately models it at a different "
        "granularity. Each entry names the case where the two part company.",
    ),
    RulingKind.SCHEMA_CEILING: (
        "Said by the rules, unsayable here",
        "The record schema has no field for these, so transcribing harder produces "
        "nothing. Closing one is a code change, not content.",
    ),
    RulingKind.OUT_OF_SCOPE: (
        "Outside what this engine simulates",
        "Deliberate boundaries. The engine will not warn you when a fight turns on one.",
    ),
    RulingKind.SUPERSEDED: (
        "Closed",
        "Kept because earlier reviews still cite them, and because a reopened "
        "question should find its own history.",
    ),
}

_CONCURRENCE_LABEL: dict[Concurrence, str] = {
    Concurrence.NOT_A_RULES_QUESTION: "not a rules question",
    Concurrence.NO_EXTERNAL_RULING: "no outside ruling exists",
    Concurrence.MATCHES_COMMON_READING: "matches the common reading",
    Concurrence.DIVERGES: "diverges from the common reading",
}


def render_markdown() -> str:
    """The rulings report, generated from :data:`RULINGS`.

    Prose rather than a totals table, unlike ``coverage.py``: a count of
    adjudications tells a reader nothing, and the whole value of an entry is the
    sentence saying what would overturn it.
    """
    lines: list[str] = []

    def add(line: str = "") -> None:
        lines.append(line)

    add("# Rulings")
    add()
    add(
        "Where SRD 5.2.1 does not decide, and what this engine decided instead. "
        "Generated from `fivee_sim.rulings`; every entry that governs code is "
        "pinned to that code by a test. Rules content remains CC-BY-4.0; see "
        "[NOTICE](../NOTICE)."
    )
    add()
    add(
        "Each entry carries a **revisit** line: the change that would make the "
        "decision wrong. That is the part worth reading before you touch the "
        "surrounding code."
    )
    for kind in _KIND_ORDER:
        entries = [ruling for ruling in RULINGS if ruling.kind is kind]
        if not entries:
            continue
        heading, blurb = _KIND_HEADING[kind]
        add()
        add(f"## {heading}")
        add()
        add(blurb)
        for ruling in entries:
            add()
            add(f"### `{ruling.code}`")
            add()
            add(f"**Question.** {ruling.question}")
            add()
            add(f"**Decision.** {ruling.decision}")
            add()
            add(f"**Why.** {ruling.because}")
            if ruling.revisit:
                add()
                add(f"**Revisit when.** {ruling.revisit}")
            if ruling.superseded_by:
                add()
                add(f"**Closed in.** {ruling.superseded_by}")
            add()
            if kind is RulingKind.SRD_SILENT:
                add(f"**Outside readings.** {_CONCURRENCE_LABEL[ruling.concurrence]}.")
                add()
            add(f"Basis: {'; '.join(ruling.basis)}.")
            if ruling.sites:
                add()
                add(f"Governs: {', '.join(f'`{site}`' for site in ruling.sites)}.")
    add()
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    return write_generated_document(
        argv,
        source_file=__file__,
        default_filename="RULINGS.md",
        render=render_markdown,
    )


if __name__ == "__main__":
    raise SystemExit(main())
