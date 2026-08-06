# Rulings

Where SRD 5.2.1 does not decide, and what this engine decided instead. Generated from `fivee_sim.rulings`; every entry that governs code is pinned to that code by a test. Rules content remains CC-BY-4.0; see [NOTICE](../NOTICE).

Each entry carries a **revisit** line: the change that would make the decision wrong. That is the part worth reading before you touch the surrounding code.

## Where the rules do not decide

No printed rule answers these. The engine had to, because a simulator cannot hand the question back to the table.

### `climb_cost_boundary`

**Question.** The SRD prices a climb but never says when a rise stops being ground you walk up and starts being a face you climb. That trigger is the DM's.

**Decision.** Three bands on the height change alone: under SLOPE_DIFFICULT_FEET the square costs its ordinary terrain price, up to CLIMB_FEET it is Difficult Terrain, and above it the climb surcharge applies on top of the step.

**Why.** A grid answers per square with no DM in the loop, so the trigger has to be a number. The cost therefore jumps at the boundary — a 5-foot rise onto ordinary ground costs 10 feet and a 6-foot rise costs 17 — and that step is the price of ruling a boundary at all rather than inventing a graduated scale the printed rules do not have.

**Revisit when.** Both constants are module-level and neither is settable by a content pack. A campaign that wants a different threshold has to patch the kernel. Make them pack-settable before anyone argues about the number.

**Outside readings.** no outside ruling exists.

Basis: SRD 5.2.1, Climbing, Swimming, and Crawling; SRD 5.2.1, Difficult Terrain.

Governs: `kernel/grid.py:step_cost_feet`.

### `cross_storey_sight_needs_a_link`

**Question.** The SRD has no three-dimensional cover model, so nothing says what a creature one storey up can see or be shot by.

**Decision.** A floor is opaque: a target on another level has total cover, unless the square the effect is measured from carries a map-authored sight link naming that level, which then grants sight to the whole of it.

**Why.** Opaque by default is what stops a fighter shooting the ceiling out from under someone standing at the same square one level up. The exception is authored per square because the map knows where the balcony is and the rules do not.

**Revisit when.** The link is read at the origin square only and grants a whole level at once: a creature one square back from a balcony rail sees nothing, and one on the far side of the upper floor is seen regardless of distance or what stands between. Narrow it when a map needs sight between specific squares.

**Outside readings.** matches the common reading.

Basis: SRD 5.2.1, Cover.

Governs: `model/encounter.py:Encounter._cover_from_square`.

### `sight_ignores_elevation`

**Question.** Within one storey the SRD says nothing about ground height blocking sight. Whether a ridge hides what is behind it is the DM's call.

**Decision.** Line of sight is computed from squares alone. Elevation never blocks a line and never grants one; it reaches movement cost and nothing else.

**Why.** The grid primitives are two-dimensional by design. A height-aware line needs a volume model the map document does not carry, and inventing one inside a sight test would put a second, disagreeing notion of terrain beside the one movement already uses.

**Revisit when.** The moment a map needs a rampart or a ridge to break line of sight, or elevation gains a second consumer beyond step cost.

**Outside readings.** matches the common reading.

Basis: SRD 5.2.1, Cover; SRD 5.2.1, Vision and Light.

Governs: `kernel/grid.py:has_line_of_sight`.

## Modelled coarser than printed

The rule is clear and the engine deliberately models it at a different granularity. Each entry names the case where the two part company.

### `loading_capped_per_turn`

**Question.** Loading caps the weapon at one attack per activation — an action, a Bonus Action or a Reaction — and a turn may hold more than one.

**Decision.** The gate is per turn: one Loading shot per turn, tracked on the turn state.

**Why.** Behaviourally identical under everything this stepper does today. Nothing here consults a bonus-action flag when attacking, and the only reaction attack picks a melee option, so it can never be a Loading one.

**Revisit when.** Give the stepper a Bonus Action attack or a ranged reaction and the two readings part company. That is the moment to move the flag off the turn state and onto whatever represents an activation.

Basis: SRD 5.2.1, Equipment, Weapon Properties, Loading.

Governs: `model/encounter.py:Encounter._do_attack`.

### `declared_climb_zeroes_the_rise`

**Question.** A Climb Speed lets you ignore the extra cost of climbing. It says nothing about whether the ground you are on is still Difficult Terrain.

**Decision.** Electing the climb movement mode sets the height change to zero, which exempts the mover from the slope band as well as from the climb surcharge.

**Why.** The two bands are decided by one number, so exempting the surcharge alone would mean passing the mode down into the kernel primitive — and that function deliberately takes no creature.

**Revisit when.** A climber crossing a gentle rise through undergrowth pays ordinary cost where the printed rules would still charge Difficult Terrain. Split the surcharge from the band when a map makes that difference matter.

Basis: SRD 5.2.1, Climbing, Swimming, and Crawling; SRD 5.2.1, Difficult Terrain.

Governs: `model/encounter.py:Encounter._step_cost`.

### `cylinder_height_unread`

**Question.** The SRD gives a Cylinder both a radius and a height. Areas here are two-dimensional.

**Decision.** A cylinder resolves as its radius. The height is parsed, validated and carried on the spell, and resolution never reads it.

**Why.** Declared explicitly rather than silently ignored: a pack author who transcribes the printed height gets a record that keeps it, so the day areas gain a third dimension the data is already there.

**Revisit when.** Any spell whose outcome differs by height — something that spares a creature under or over the cylinder — is resolved wrongly today with no diagnostic anywhere.

Basis: SRD 5.2.1, Spells, Areas of Effect.

Governs: `kernel/spells.py:Spell.height`.

## Said by the rules, unsayable here

The record schema has no field for these, so transcribing harder produces nothing. Closing one is a code change, not content.

### `no_trait_vocabulary`

**Question.** 208 of the SRD's 336 stat blocks carry traits, across 124 distinct trait names. Magic Resistance and Legendary Resistance lead the count.

**Decision.** Traits are named booleans on the creature model. Three exist. Every other trait is transcribed as prose nothing executes, or not at all.

**Why.** A boolean per trait was the cheapest thing that worked for three, and it defers the design question rather than answering it: what shape does a trait take such that 124 of them are data?

**Revisit when.** This is the ceiling every further creature sits behind — a pack cannot add one, because the gap is code rather than transcription. Designing the vocabulary is a plan of its own, not a fix.

Basis: SRD 5.2.1, Monsters.

### `no_skill_or_proficiency_concept`

**Question.** 216 of 336 stat blocks print skills. Ability checks take a proficiency.

**Decision.** No skill or proficiency concept exists anywhere in the engine. The check primitive takes a skill name as a label and its modifier from the caller.

**Why.** Combat resolution never needed one, so nothing forced the design. The label keeps the caller honest about what it is rolling without the engine pretending to know.

**Revisit when.** A transcribed creature silently drops its skills and its passive Perception. Any content that turns on being good at something — hiding, spotting an ambush — cannot be expressed.

Basis: SRD 5.2.1, Proficiency; SRD 5.2.1, Monsters.

### `touch_range_transcribed_as_five_feet`

**Question.** A spell's range may be Touch. A spell record carries a range in feet, where zero already means unranged.

**Decision.** Touch is transcribed as 5 feet, and the bundled records declare it as an omission.

**Why.** Zero is taken: it is the value that skips the range check entirely, so a literal transcription of Touch would make the spell reach the whole map. Five feet is the nearest honest number.

**Revisit when.** Touch and a 5-foot range differ wherever a rule turns on contact rather than distance. The trap is the transcription: a pack author who writes zero for Touch gets unlimited range with no diagnostic.

Basis: SRD 5.2.1, Spells, Range.

### `no_recharge_mechanic`

**Question.** 88 of the SRD's stat blocks gate an ability behind a Recharge roll at the start of the creature's turn.

**Decision.** Nothing in the engine reads Recharge. The catalog transcribes the value as a printed fact; no kernel, model, or service code consults it.

**Why.** An action's availability is decided by the turn budget, which counts actions rather than tracking per-ability state. Recharge needs a per-ability cooldown the action model has no place for.

**Revisit when.** Any creature whose threat is its breath weapon is unsimulatable — it either never uses it or uses it every round, and neither is the fight.

Basis: SRD 5.2.1, Monsters, Recharge.

### `no_round_clock_for_durations`

**Question.** 225 SRD spells carry a real duration and 133 are Concentration. Durations are printed in rounds, minutes and hours.

**Decision.** An ongoing effect expires on a turn boundary — a phase and the creature whose turn ends it — or it lasts until something releases it. There is no elapsed-time clock, so a printed duration has nowhere to go.

**Why.** Turn-boundary anchors are what an attack rider's condition actually needs, and they were enough for every effect the engine could resolve when the ledger was written.

**Revisit when.** A concentration spell runs until concentration breaks rather than until its minute is up, so it is strictly more powerful here than in print. Hold Person is the bundled case.

Basis: SRD 5.2.1, Spells, Duration; SRD 5.2.1, Rules Glossary, Concentration.

## Outside what this engine simulates

Deliberate boundaries. The engine will not warn you when a fight turns on one.

### `object_and_world_effects`

**Question.** Many spells act on the world as well as on creatures: igniting flammable objects, damaging unattended ones, regrowing a severed limb.

**Decision.** The engine resolves effects on creatures. World and object effects are declared per record as omissions and are the table's to narrate.

**Why.** Objects have no representation here and giving them one is a simulation of a different size. Declaring the gap per record keeps the omission visible to whoever reads the spell.

**Revisit when.** A fight whose outcome turns on burning the rope bridge is one this engine cannot adjudicate, and it will not say so at the time.

Basis: SRD 5.2.1, Spells.

### `out_of_combat_time`

**Question.** Casting times run to minutes, and some effects grant the benefit of a rest. A combat stepper has rounds.

**Decision.** Anything longer than an action is modelled as an ordinary action-cost cast, with the real casting time declared per record as an omission.

**Why.** The alternative is refusing to model the spell at all, which loses its damage and healing as well as its timing.

**Revisit when.** A ten-minute cast used mid-fight is a real rules error the engine will accept without comment. An adventure that leans on ritual timing needs this before it can be simulated.

Basis: SRD 5.2.1, Spells, Casting Time; SRD 5.2.1, Resting.

## Closed

Kept because earlier reviews still cite them, and because a reopened question should find its own history.

### `surprise_had_no_initiative_rider`

**Question.** Surprise is Disadvantage on one Initiative roll, and Invisible grants Advantage on it. Neither rider reached the roll.

**Decision.** Incapacitated and Invisible now carry their Initiative riders, and the Initiative roll consults a channel separate from ordinary ability checks.

**Why.** The plumbing was already complete end to end — the roll went through the advantage machinery with the creature's conditions — and delivered nothing because the one flag it read was missing from the row that owned it. Splitting the channels came second: the flag reused for Initiative had a second caller in map-fixture ability checks, and would otherwise have granted an Invisible creature Advantage on every lever-pull.

**Closed in.** 2026.08.66

Basis: SRD 5.2.1, Rules Glossary, Incapacitated; SRD 5.2.1, Rules Glossary, Invisible.

### `opportunity_attack_fixed_five_feet`

**Question.** An opportunity attack triggers when a creature leaves your reach. The trigger tested a fixed 5 feet, so a reach-10 creature never got one.

**Decision.** The trigger reads the melee option's own reach, and requires line of sight.

**Why.** 92 of 336 SRD stat blocks have a melee attack beyond 5 feet. All six bundled creatures have reach 5, so nothing was visibly wrong and nothing failed — which is what made it worth fixing before more content arrived.

**Closed in.** 2026.08.66

Basis: SRD 5.2.1, Rules Glossary, Opportunity Attack.

### `invisible_advantage_unconditional`

**Question.** Invisible stops helping against a creature that can somehow see you. The engine applied its Advantage and Disadvantage unconditionally.

**Decision.** The condition row no longer carries the attack flags at all; sight derives them, for weapon and spell attacks alike.

**Why.** The fix was a deletion. Two derivations of one rule existed and disagreed, and removing the unconditional copy exposed a third caller carrying neither sight term — so the cast path had been silently disagreeing with the swing path about who is visible.

**Closed in.** 2026.08.66

Basis: SRD 5.2.1, Rules Glossary, Invisible.

### `thrown_attack_kind`

**Question.** 20 SRD stat blocks print a Melee or Ranged attack. The attack kind had two members, so a thrown weapon had to be transcribed as one or the other.

**Decision.** A thrown rider on the ranged kind: melee within reach, ranged beyond it. The Ogre's javelin no longer takes close-combat Disadvantage at 5 feet.

**Why.** A third enum member would have type-checked and run and been wrong in five places, because every melee test in the tree is two-valued and a non-exhaustive identity chain is not flagged. A new member fails silently here; a rider fails loudly.

**Closed in.** 2026.08.66

Basis: SRD 5.2.1, Equipment, Weapon Properties, Thrown.

### `initiative_is_dex_only`

**Question.** 110 of 332 SRD stat blocks print an Initiative bonus that is not the Dexterity modifier. The engine derived initiative from the modifier.

**Decision.** A stat block may print its own Initiative bonus, which replaces the derivation. The tie-break stays on the Dexterity modifier.

**Why.** All six bundled creatures happened to agree, so nothing was wrong and nothing would have failed — a pack transcribing any other SRD monster was simply wrong a third of the time, with no diagnostic anywhere.

**Closed in.** 2026.08.66

Basis: SRD 5.2.1, Initiative.

### `no_condition_immunity`

**Question.** 81 SRD stat blocks list condition immunities. The creature model carried damage immunity, resistance and vulnerability, and no condition equivalent.

**Decision.** A creature names the conditions it can never gain, and a condition the table does not define is still a legal immunity to declare.

**Why.** Two of the six bundled creatures already declared the gap as an unmodelled fact, which is the ledger doing its job: the omission was written down before it was closed.

**Closed in.** 2026.08.66

Basis: SRD 5.2.1, Monsters, Condition Immunities.

