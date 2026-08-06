# Content packs

Your campaign's creatures, spells, conditions, items, and structured reference
catalog entries, in the format the engine already uses.

A pack is one JSON file. The bundled SRD 5.2.1 slice is not a special case — it is
loaded by the same parser, through the same validation, and merged by the same
rules as anything you write. There is one format, and we eat it too.

## Quick start

Put a file in `.fivee-sim/content/` at the root of your campaign repository:

```json
{
  "pack": "crimson-vale",
  "version": "1.0",
  "provenance": "Original content, © 2026 Example Campaign",
  "creatures": [
    {
      "name": "Vale Stalker",
      "team": "monsters",
      "ac": 14,
      "max_hp": 22,
      "hit_dice": "4d8+4",
      "speed": 40,
      "abilities": { "strength": 14, "dexterity": 16, "constitution": 12 },
      "attacks": [
        { "name": "Claw", "attack_bonus": 5, "damage": "1d6+3",
          "damage_type": "slashing", "kind": "melee", "reach": 5 }
      ],
      "provenance": "Original content",
      "unmodelled_facts": [
        { "feature": "Blood Scent", "code": "unsupported_tracking_advantage" }
      ]
    }
  ]
}
```

That is enough in an installed plugin. Claude Code exports `CLAUDE_PROJECT_DIR`;
on hosts without a project-root variable, the bundled skill detects the workspace
directory and loads it with `content.configure`. For a direct server launch, set
`FIVEE_SIM_PROJECT_DIR` to the campaign repository root.

Then, from a shell or an assistant driving `fivee`:

- **`fivee content.status`** — what is loaded, from where, and under which mode.
- **`fivee content.validate --paths '["…"]'`** — check a pack without loading it.
  Use this while writing.
- **`fivee content.configure`** — load packs, or switch whether the bundled slice
  is included.
- **`fivee catalog.search`**, **`fivee catalog.get`**, **`fivee catalog.table`** —
  bounded discovery, one structured record, and one paged printed table.

`fivee help <operation>` gives any of them its arguments and a line to paste.

## Where content comes from

In precedence order, lowest first:

1. **the bundled SRD 5.2.1 slice**, unless the mode is `exclude`;
2. **`FIVEE_SIM_CONTENT`** — an `os.pathsep`-separated list of files or
   directories. A directory is scanned for `*.json`, in sorted order;
3. **`$FIVEE_SIM_PROJECT_DIR/.fivee-sim/content/`**, with
   `$CLAUDE_PROJECT_DIR/.fivee-sim/content/` as a compatibility fallback, used
   only when `FIVEE_SIM_CONTENT` is unset — so exporting the variable does not
   silently also load whatever sits in the repository you happen to be standing
   in;
4. **paths given to `content.configure`** during the session.

`FIVEE_SIM_BUILTIN` is `include` (the default) or `exclude`.

### Excluding the bundled content

`exclude` is not just a filter. It is what lets you run this engine on entirely
your own material with no SRD content loaded, which is a licensing posture rather
than a preference.

Two conditions survive `exclude`: **`unconscious`** and **`prone`**. The stepper
applies them itself when a creature drops to 0 hit points, so removing them would
make a creature falling over crash the fight. `content.status` lists them under
`retained_conditions` so the exception is visible rather than silent, and a pack of
your own defining either name replaces the retained row.

## Sections

Every section is optional, and record shapes match the bundled files.

### `creatures`

Required: `name`, `ac`, `max_hp`, `provenance`. Optional: `team`, `speed`,
`climb_speed`, `swim_speed`, `fly_speed`, `burrow_speed`,
`terrain_cost_overrides`, `darkvision`,
`blindsight`, `tremorsense`, `truesight`, `death_rule`, `hit_dice`, `abilities`,
`save_bonuses`, `attacks`,
`attacks_per_action`, `bonus_actions`, `surrender_when_last`, `redirect_attack`,
`pack_tactics`, `undead_fortitude`, `spells`, `spell_slots`, `spell_save_dc`,
`spell_attack_bonus`, `spellcasting_ability`, `items`, `conditions`,
`condition_immunities`, `initiative_bonus`, `passive_perception`,
`skill_bonuses`,
`unmodelled_facts`, legacy
`unmodelled`, `immunities`,
`resistances`, `vulnerabilities`, `overrides`.

`skill_bonuses` maps a skill name to the stat block's printed *absolute*
modifier — SRD stat blocks print totals such as "Perception +5", and this
engine models no character level or proficiency bonus to derive one from, so
the printed total is what ships. Keys are plain strings, never a closed enum:
`service/primitives.check` already accepts a free-form `skill` label validated
only for non-blankness, and a pack's skill stays as open a name as a pack's
condition. Consumed by a map fixture's `check`, whose optional `skill` field
is documented in [MAPS.md](MAPS.md); a creature with no printed bonus for the
named skill rolls its raw ability modifier, unchanged.

`initiative_bonus` is a stat block's printed Initiative score, used in place of
the Dexterity modifier when present — SRD 5.2.1, *Initiative*: "Your
Initiative score equals 10 plus your Dexterity modifier," but the printed line
on a stat block is that creature's own authority, and it disagrees with the
modifier on roughly a third of the SRD monster catalog (the Aboleth prints +7
against a −1 Dexterity modifier; the Balor +14 against +2). Omit it and a
creature rolls its Dexterity modifier exactly as before — `0` is a legitimate
printed bonus and is honoured as such rather than treated as "not stated." The
tie-break on equal initiative totals always reads the Dexterity modifier,
never this field: that is the SRD's own tie-break rule, not a stand-in for the
bonus.

`passive_perception` is a stat block's printed Passive Perception, carried but
never consumed — there is no Hide, Search, Stealth, or Perception action
anywhere in this engine for it to reach. It is transcription-only, in the same
standing `hit_dice` holds: a printed Passive Perception does not always equal
`10 + Wisdom modifier`, which is why it is a fact to write down rather than a
number to derive, and it is accepted and validated (a plain integer, `None`
when omitted rather than defaulted to `0`) so a pack can carry it faithfully
even though nothing yet reads it. Adding this field does **not** satisfy a
record's `unsupported_passive_perception` omission code — that code means the
engine cannot *act on* the fact, and a field nothing consumes has not changed
that.

`burrow_speed` is wired in exactly like `climb_speed`, `swim_speed`, and
`fly_speed`: it counts toward the turn's movement budget and is a selectable
`movement_mode` (`"burrow"`) at ordinary terrain cost. This engine models no
terrain gating for *any* movement mode — a swim speed already applies on dry
land, and a fly speed applies regardless of what is underneath — so `burrow`
does not invent a "digging through solid ground" mechanic either; that
consistency, not a gap specific to burrowing, is why none exists.

`truesight` adds a rung to the sight rule (`Encounter._can_see`), above the
rung `blindsight` already granted. SRD 5.2.1, *Truesight* — within range,
vision "pierces through" Darkness (including magical) and Invisibility (the
only two of its five listed effects with any mechanical presence here; visual
illusions, transformations, and the Ethereal Plane are not modelled). Unlike
Blindsight, Truesight carries no SRD "even if you have the Blinded condition"
exemption, so this engine's blinded observer gets nothing from it even in
range; Total Cover still blocks both.

`tremorsense` is transcription-only, in the same standing `passive_perception`
holds — carried and validated, never consumed. It is deliberately **not** a
rung on the sight ladder: SRD 5.2.1, *Tremorsense* — "Tremorsense can't detect
creatures or objects in the air, and it doesn't count as a form of sight."
That pinpoints a location without granting sight of what's there, a third
state between "can see" and "cannot see" that this engine has no channel for.
Treating it as a sight rung would cancel the unseen-target Disadvantage
against an Invisible creature the observer has merely pinpointed but still
cannot see — a live wrong answer on a core mechanic, not just an unmodelled
detail. `tremorsense` is kept and reported for the same reason
`passive_perception` is: an accepted key that does nothing must say so, not
pretend to, until this engine gains a pinpoint-without-sight concept to spend
it on.

`condition_immunities` is a plain list of condition names, never validated
against the active condition table: an immunity is a declarative refusal, not
a lookup, so a stat block can be immune to a condition this engine has no
table row for (SRD 5.2.1's Zombie and Skeleton both print immunity to
Exhaustion, which this engine does not model). A creature carrying one of
these names never gains that condition from an attack rider, a spell, an item,
or a GM ruling.

There is deliberately no `hp`, `temp_hp`, `position`, or `arrival_round`.
Starting damage, a starting Temporary Hit Points buffer, placement, and
reinforcement timing are all per-instance — set them when adding a combatant
to an encounter, not in its reusable stat block. `temp_hp` follows `hp`'s
precedent rather than `skill_bonuses`': no SRD 5.2.1 stat block prints a
starting damage buffer any more than it prints a starting hit-point total, so
it belongs on a combatant spec, not here.

`hit_dice` is accepted and validated as a string but consumed by no rule: the
engine rolls no hit points and models no rest, so it is kept as a faithful part
of the transcription rather than because anything reads it. Write it if you are
transcribing a stat block; expect no behaviour from it either way.

Attacks carry their own bonus and damage expression rather than deriving them from
ability scores and proficiency. That is how a stat block presents an attack, and it
keeps your data a transcription rather than a bet on derivation rules.

An attack may also carry riders, the way stat blocks hang extras off a hit.
`bonus_damage` with `bonus_damage_type` is extra damage on every hit — rolled
separately, doubled on a critical hit, and defended (resistance, vulnerability,
immunity) against its own type, the way a mephit's claw adds fire to its
slashing. `advantage_bonus_damage` adds dice of the main damage type only when
the attack roll actually resolved with Advantage, after every source has
combined and cancelled — the goblin pattern. Set
`advantage_bonus_with_adjacent_ally` to also apply those dice when a capable ally
stands within 5 feet of the target. `on_hit_condition` names a
condition the hit imposes, and any loaded condition qualifies, including one
your own pack defines. Give `on_hit_save_ability` and `on_hit_save_dc` together
to let the target save first; leave both out and the condition is automatic on
a hit. `on_hit_expiry` is `"none"` (the default — the condition lasts until
something removes it), `"start_of_attacker_next_turn"`, or
`"end_of_target_next_turn"`. The timed forms expire when that turn slot passes,
even if the attacker has died by then, and expiry never strips a condition
something else is still imposing — a stat block that starts with it, or another
effect still holding it.

An attachment rider sets `on_hit_attach`, `attached_damage`,
`attached_damage_type`, and optionally `detach_after_damage`. A hit fastens the
attacker to the target; the damage repeats automatically at the start of the
attacker's turns until the source detaches after taking at least the threshold.

`ammunition` names an entry in the wielder's own `items` — the same "quantity
is the charge count" pool described under `items` below, not a separate
tally — that this ranged attack spends one of per shot. Only a `"ranged"`
attack may carry it. `loading` marks a weapon that can fire at most once per
turn regardless of Extra Attack; the engine enforces that limit per **turn**,
where SRD 5.2.1's Loading property states it per **activation** (action, Bonus
Action, or Reaction). Those coincide today only because nothing in the engine
yet lets one creature take a Reaction attack with a Loading weapon
mid-someone-else's-turn; the day it does, per-turn stops being equivalent to
per-activation and the enforcement will need to move with it.

`thrown: true` is how you write a stat block's **"Melee or Ranged Attack Roll:
+6, reach 5 ft. or range 30/120 ft."** line — the Ogre's Javelin, and the same
shape on twenty other SRD stat blocks. It is the SRD Thrown weapon property: a
melee weapon that also enables a ranged attack. Give the attack `"kind":
"ranged"` with the range it is thrown to, and `reach` for the distance it is
still held at:

```json
{
  "name": "Javelin", "attack_bonus": 6, "damage": "2d6+4",
  "damage_type": "piercing", "kind": "ranged", "reach": 5,
  "normal_range": 30, "long_range": 120, "thrown": true,
  "ammunition": "Javelin"
}
```

Inside `reach` the swing resolves as a **melee** attack: no close-combat
Disadvantage for an adjacent enemy, no long-range band, the melee underwater
rule rather than the ranged one, and it is the attack an Opportunity Attack
swings — so a creature carrying nothing but javelins threatens the square
beside it. Beyond `reach` it is a shot and every ranged rule applies as before.

`ammunition` combines with it the way the weapon does: **a throw spends one, a
stab spends none** — the javelin only leaves your hand when you throw it — but
the count is still the javelins you are holding, so a thrower who has thrown
them all is refused the stab too. Only a `"ranged"` attack may set `thrown`,
and it needs a `normal_range` or `long_range`, because a thrown weapon with
nowhere to be thrown is refused at every square but the attacker's own.

`bonus_actions` currently accepts `dash` and `disengage`; callers pass
`as_bonus_action: true` when using that budget. `surrender_when_last` lets the
batch policy yield when no capable ally remains. `redirect_attack` spends the
target's reaction to exchange places with an adjacent Small or Medium ally and
make that ally the target instead. `death_rule` is `instant` for combatants that
die at 0 HP or `death_saves` for characters; pack creatures default to `instant`.

Two printed traits are flags on the creature rather than anything you write out.
`pack_tactics: true` gives its attack rolls — weapon and spell alike — Advantage
whenever another member of its team is within 5 feet of the target, conscious, and
free of any incapacitating condition, your pack's conditions included. It counts as
one Advantage source and cancels against Disadvantage like any other.
`undead_fortitude: true` gives it the drop-to-0 save: damage that would reduce it
to 0 hit points triggers a Constitution saving throw, DC 5 plus the damage taken,
and on a success it stands at 1 hit point instead — unless any of that damage was
Radiant, the hit was a critical, or the overflow was enough to kill it outright.

`unmodelled_facts` is where you identify mechanics the engine does not implement.
Each entry is an object with a stable `code` and any other bounded structured facts
needed to identify the omission; it must not contain copied rules or descriptive
prose. The assistant checks these entries before promising a feature will fire.
Older campaign packs may keep using the legacy `unmodelled` string list.

### `spells`

Required: `name`, `level`, `provenance`. Optional: `school`,
`requires_attack_roll`, `attack_kind`, `save_ability`, `damage`, `damage_type`,
`heal`, `temp_hp`, `half_on_save`, `upcast_damage`, `upcast_heal`,
`upcast_temp_hp`,
`add_spellcasting_modifier`, `shape`, `radius`, `length`, `size`, `width`,
`height`,
`range_feet` — optional, but a named-target spell that omits it is warned about
(see below) — `max_targets`, `action_cost` (`action` by default or
`bonus_action`),
`condition`, `concentration`, `duration_rounds`, `unmodelled_facts`, legacy
`unmodelled`, `overrides`.

A spell cannot both require an attack roll and offer a saving throw. `shape` is
one of `sphere`, `cone`, `line`, `cube`, `emanation`, or `cylinder`, and pairs
with the measurement that gives it extent: `radius` for a sphere or an
emanation, `length` for a cone or line (a line also takes `width`, fixed at 5
ft), `size` for a cube, and both `radius` (its base) and `height` for a
cylinder. An area rolls its damage once and compares every creature's save
against that single total.

Emanation and cylinder are SRD 5.2.1's other two area templates (p.181 and
p.180). The distinction that matters: an **emanation's origin isn't included**
in its area — SRD 5.2.1 makes this the caster's own square, an opt-in the
engine does not carry, so it always excludes the caster — while a
**cylinder's origin is included**, the same as a sphere centred on `center`.
An emanation therefore needs no `center`, `direction`, or `toward` at all — it
pours from the caster like a cone or line. A cylinder's `height` is required
alongside `radius` but never consulted at resolution: the engine's areas are
2-D, and `height` is stored so the record says what the spell does rather than
silently dropping it.

An attack-roll spell may set `attack_kind` to `"melee"` or `"ranged"`. It defaults
to `"ranged"` so packs written before this field existed keep their behaviour. The
kind matters when a capable enemy is within 5 ft and can see the caster: ranged
spell attacks take the same close-combat Disadvantage as ranged weapon attacks;
melee spell attacks do not.

`heal` is a healing dice expression resolved once for every chosen target;
`upcast_heal` adds its dice for every slot level above the spell's base level.
Healing a creature at 0 HP restores it to the fight and the slot is spent by the
same cast that produced the healing — no parallel item charge is needed.

`temp_hp` grants Temporary Hit Points, mirroring `heal` exactly — one dice
expression, rolled once and shared by every chosen target, scaled by
`upcast_temp_hp` for every slot level above the base like `upcast_heal` scales
`heal` — but it is a separate field, not a flag on `heal`. SRD 5.2.1,
*Temporary Hit Points*: they "can't be added to your Hit Points, healing
can't restore them, and receiving Temporary Hit Points doesn't count as
healing" — so a shared field would collapse two numbers a stat block or a
spell can print separately into one, and every existing `heal` record would
need to keep meaning only healing. `add_spellcasting_modifier` never reaches
`temp_hp`, only `heal`: no bundled SRD spell scales a temp-HP grant by the
caster's modifier, and the pairing above is opt-in on both sides for exactly
that reason. **They Don't Stack** on the *engine's* side of the grant: a
target already carrying Temporary Hit Points keeps whichever total is higher
rather than adding the two together. SRD 5.2.1 gives the choice to the
*recipient* when some remain and more arrive; this engine has no
player-choice channel at grant time, so "take the higher" is a deliberate
simplification of that rule, not the rule itself. A grant never restores a
creature at 0 Hit Points to consciousness, and it is reported as its own
`grant_temp_hp` event rather than folded into `heal`'s — a log entry that
read "heal" for something the SRD says is explicitly not healing would be
the exact confusion the rule is warning about.

`add_spellcasting_modifier` adds the **caster's** ability modifier to the
healing, once, however high the slot. SRD healing spells are written that way —
Cure Wounds is "2d8 plus your spellcasting ability modifier" — and a shared
record cannot hold a number that differs per caster, so it arrives at resolution
instead. It reads `spellcasting_ability` off the creature casting it; a creature
that names none contributes nothing, which is why the pair is opt-in on both
sides and why every pack written before it kept its numbers. It never touches
damage: SRD damage spells print their dice in full.

`max_targets` caps how many creatures may be **named** on one cast, and naming more
is refused rather than quietly trimmed. It does not apply to an area spell: there,
the radius decides who is caught, and the cap is ignored. That is why every bundled
area spell can leave `max_targets` at its default of 1 without shrinking to a single
creature.

`range_feet` is worth declaring even though nothing forces you to. `0` already
means "resolve with no range check at all", so a record that leaves the field out
is indistinguishable from one deliberately declaring unlimited reach — and Cure
Wounds and Regenerate are both Range: Touch, where the honest transcription of
"Touch" is to name no number at all. Omit it on a named-target spell and you get a
spell castable across the entire map, so validation warns: write the printed range
in feet, `5` for Touch, or `0` if the spell targets only the caster or genuinely
has no range to check.

It stays a warning rather than a refusal because your existing packs keep loading —
a spell record has only ever needed `name`, `level` and `provenance`, and that
promise is not ours to withdraw. An area spell is exempt from the warning
altogether: its range is measured from its point of origin (a sphere, cube, or
cylinder) or pours out of the caster (a cone, line, or emanation) rather than
being named on the cast.

`action_cost` mirrors the item field of the same name below: `action` by default,
or `bonus_action` for the handful of spells SRD 5.2.1 prints with "Casting Time:
Bonus Action" — Healing Word and Mass Healing Word among them. It spends the
matching budget regardless of `as_bonus_action`; that flag only ever refuses an
ordinary spell cast as a bonus action, the same way an item's does.

`duration_rounds` caps how long an ongoing effect this spell imposes lasts,
counted in this engine's **rounds** — never in the minutes or hours SRD 5.2.1
prints. A round is 6 seconds, so an SRD minute is 10 rounds: transcribe by that
conversion, not by copying the printed number. Hold Person is "Concentration,
up to 1 minute", so its record carries `"duration_rounds": 10`, not `1`. `0`
means no cap, the same reading `range_feet`'s `0` gives — a spell with no
ongoing effect has nothing to cap, and a record written before this field
existed reads exactly as unbounded as it always did. Concentration and this cap
are independent constraints on the same effect: whichever release reaches it
first — a failed Constitution save, the caster's own Incapacitated or death,
starting a second Concentration effect, or the round counter reaching the cap —
is the one that ends it.

### `conditions`

Required: `name`, `provenance`. Optional: `effects`, `description`,
`unmodelled_facts`, legacy `unmodelled`, `overrides`.

```json
{ "name": "vale-cursed",
  "description": "The vale's hunger gnaws; every strike comes harder.",
  "effects": { "own_attacks_have_disadvantage": true },
  "provenance": "Original content" }
```

A pack may name new conditions but **not new kinds of effect**. The flags are the
consequences the rules engine already knows how to apply:

`incapacitated`, `speed_zero`, `attacked_with_advantage`,
`attacked_with_disadvantage`, `attacked_with_advantage_in_melee`,
`attacked_with_disadvantage_at_range`, `own_attacks_have_advantage`,
`own_attacks_have_disadvantage`, `own_ability_checks_have_advantage`,
`own_ability_checks_have_disadvantage`, `initiative_advantage`,
`initiative_disadvantage`, `cannot_see`, `unseen`,
`auto_fail_strength_saves`, `auto_fail_dexterity_saves`,
`advantage_on_dexterity_saves`, `disadvantage_on_dexterity_saves`,
`melee_hits_are_critical`, `resists_all_damage`.

Ability-check flags apply to initiative and to checks made while interacting with
a map fixture. The standalone `check` tool takes only a caller-supplied modifier,
not a combatant, so it has no creature conditions to read.

`cannot_see` marks a condition that stops its bearer seeing; `unseen` marks one
that stops others seeing its bearer. The encounter consumes those sight flags when
deciding whether a nearby enemy imposes close-combat Disadvantage on a ranged
attack, whether an Opportunity Attack may be made at all, and — the part worth
knowing before reaching for the flags above it — **what sight does to an attack
roll**. An attacker its target cannot see attacks with Advantage; an attacker who
cannot see its target attacks with Disadvantage. That is the whole of the bundled
Invisible condition's "Attacks Affected" clause, withdrawal included: a condition
that declares `unseen` hides its bearer from everything except an observer with
Blindsight in range, and against that observer the pair simply does not apply.

So a pack that wants Invisible's shape declares `unseen` and stops. Declaring
`attacked_with_disadvantage` and `own_attacks_have_advantage` as well is a
different rule, not a louder version of the same one: those two are unconditional
and no observer's senses withdraw them. Both remain available, because a pack may
genuinely want that — a curse that makes its bearer easy to hit whether or not
anyone can see them. Total cover, another storey, allies, and Incapacitated
enemies also do not impose the close-combat penalty.

Failing a save and being bad at one are different flags on purpose.
`auto_fail_dexterity_saves` decides the outcome; `disadvantage_on_dexterity_saves`
only weights it, and the creature can still succeed. Setting both leaves the
automatic failure in charge.

`melee_hits_are_critical` is scoped by **distance, not by weapon**, and the name is
historical. It upgrades any attack roll that lands from within 5 ft — a swing, a
shot, or a spell attack — which is how SRD 5.2.1 words the clause on Paralyzed and
Unconscious. Beyond 5 ft it does nothing.

`attacked_with_advantage_in_melee` and `attacked_with_disadvantage_at_range` are the
directional pair, and they are scoped the same way — **distance, not weapon**. Their
names are historical too, and kept so packs that set them keep working.
`attacked_with_advantage_in_melee` applies to any attack made from within 5 ft;
`attacked_with_disadvantage_at_range` applies to any attack made from beyond it. Set
both together for the Prone shape SRD 5.2.1 states: "An attack roll against you has
Advantage if the attacker is within 5 feet of you. Otherwise, that attack roll has
Disadvantage." A ranged attack drawn point-blank on a capable, seeing enemy also
has close-combat Disadvantage, so that source cancels Prone's Advantage. A reach
weapon swung from 10 ft gets Disadvantage from Prone.

The three flags that read on attack rolls — `attacked_with_advantage`,
`own_attacks_have_disadvantage`, and their siblings — apply to spell attack rolls
as well as weapon ones, because the rules treat both as the same D20 Test.

A condition with no flags is legal, and is tracked without combat consequences —
useful for something narration cares about and dice do not.

### `terrain`

Required: `name`, `provenance`. Optional: `effects`, `description`,
`unmodelled_facts`, legacy `unmodelled`, `overrides`. Effects are
`move_cost_multiplier`, `passable`, `opaque`, `cover`,
and `underwater`. Underwater terrain activates weapon restrictions and fire
resistance; a creature using its Swim speed ignores the ordinary doubled cost.
A creature's `terrain_cost_overrides` names kinds whose extra multiplier it
ignores, for burrowing or otherwise specialised movement through that material.

### `items`

Required: `name`, `use`, `provenance`. Optional: `description`,
`unmodelled_facts`, legacy `unmodelled`, `overrides`.

An item is a **use with a known effect**, and nothing more. Inside `use`: `heal`,
`temp_hp`, `damage` with `damage_type`, `save_ability` with `save_dc` and
`half_on_save`, and `condition`, plus optional `action_cost` (`action` by
default or `bonus_action`). At least one of `heal`, `temp_hp`, `damage`, or
`condition` must be present — an item that does nothing costs an action for no
reason, so it is refused. `temp_hp` mirrors `heal`'s shape and defaults to the
user the same way, but is never routed through it — see the `temp_hp` entry
under `spells` above for why a shared field would be wrong.

```json
[
  { "name": "Potion of Healing", "use": { "heal": "2d4+2" },
    "provenance": "Original content" },
  { "name": "Ward Tonic", "use": { "temp_hp": "2d4+2" },
    "provenance": "Original content" },
  { "name": "Alchemist's Fire",
    "use": { "damage": "1d4", "damage_type": "fire",
             "save_ability": "dexterity", "save_dc": 13 },
    "provenance": "Original content" },
  { "name": "Vale Toxin", "use": { "condition": "poisoned" },
    "provenance": "Original content" }
]
```

Give a creature items with `"items": { "Potion of Healing": 2 }`. **Quantity is the
charge count** — modelling both would be two ways of saying one thing.

Use one with `encounter.act --kind use_item --item "Potion of Healing"`. It spends
its declared action budget. Healing and a temp HP grant default to the user;
damage and conditions need a `target`. Targeting another creature requires
being within 5 ft.

**The same `items` map holds two different kinds of entry, and only one of
them is "an item" in this section's sense.** A `use_item` entry needs a
definition here — `name`, `use`, `provenance` — or the engine has nothing to
resolve. An entry a creature's own `attacks[].ammunition` names needs no
definition at all: it is spent automatically by the attack that names it, one
piece per shot, and refused when the count reaches zero. Defining an item
record for it would not help — ammunition has no `use` block, and an item
whose `use` does nothing is refused at load — so an ammunition name simply
never appears among the item definitions, only in `items` counts and in an
attack's `ammunition` field.

What ammunition does **not** model, deliberately: drawing it is folded into the
attack roll rather than costing a separate action or the free hand SRD 5.2.1
requires for a one-handed loading weapon (catalog `576-9-4-1-ammunition`,
facts `drawing_ammunition_part_of_attack` and
`one_handed_weapon_loading_requires_free_hand`); the 1-minute post-fight
search that recovers half of what was fired, rounded down (same record, fact
`post_fight_recovery`) is not automated — see the adventure `recovery` note
below; magic ammunition (bonuses, special effects per hit) is not modelled at
all; and a coating — poison, oil — applied to ammunition has no count of its
own and is out of scope.

**Post-fight recovery is arithmetic you do, not something the engine
simulates**, and `adventure.link`'s `recovery` has two sharp edges worth
knowing before the first use empties somebody's quiver by accident. First,
`recovery`'s `items` key **replaces** a carried combatant's whole `items` map
— the merge is shallow, so recovering three arrows by naming only `"Arrow"`
silently drops every other item that combatant was carrying. Pass the complete
map back, not a delta. Second, `encounter.state` reports the **ending** count,
not what was spent, so "half of what was fired" is `(started - ending) // 2`
added back to the ending count — read the starting count from your own request
(or the previous fight's `encounter.state` before the shot that started this
one), not from anywhere the engine keeps it for you.

### `catalog`

A catalog record is a facts-only reference identity. Required fields are `id`,
`kind`, `name`, `source_ids`, `pages`, `fact_status`, `facts`, and `provenance`.
Optional fields are `chapter_id`, `parent_id`, `aliases`, `content_ref`,
`unmodelled_facts`, and `overrides`.

`fact_status` is `pending`, `complete`, or `no_structured_facts`. `facts` may hold
JSON scalars, lists, and objects, but not copied body, description, flavor, or
rules prose. A `content_ref` has `section` and `name` and links the identity to one
loaded executable record. The tools derive simulation support from that link and
its omissions: `reference_only`, `partial`, or `executable`.

### `catalog_tables`

A catalog table requires `id`, `name`, `section_id`, `page`, `fact_status`, typed
`columns`, `rows`, `source_row_count`, `omissions`, and `provenance`; `overrides`
is optional. Column types are `string`, `integer`, `number`, and `boolean`. Each
row contains ordered `cells`; a cell has `value`, optional `numeric_value`, and an
optional structured `omission_code` when a prose-only cell is deliberately not
copied. A complete table must account for every printed source row.

## Rules the loader enforces

### `provenance` is required

At pack level and on every record. Once SRD and original material can sit in one
session, "where did this come from?" has to be answerable per entry — and
`rules.lookup` answers it, in the `source` and `provenance` fields.

### Unknown keys are errors

A pack that writes `attack_bonuses` for `attack_bonus` would otherwise load a
creature that fights wrongly and looks entirely fine. So an unrecognised section or
record key fails the load and names the valid ones.

### An identity collision is reported, not resolved

Two packs defining the same executable name or catalog/table `id` fail, and the
error names both files. To replace something deliberately, say so on the record:

```json
{ "name": "Goblin Warrior", "ac": 16, "max_hp": 12,
  "overrides": true, "provenance": "House rules" }
```

Precisely:

- **Within one level** — two packs claiming a name is an error *even if both say
  `overrides`*, because packs at one level load in path order and the winner would
  be an accident of filenames.
- **Across levels** — the later level wins. That ordering is declared, so a
  `content.configure` pack may override a project pack, which may override a
  built-in.
- **`overrides` with nothing to override** is a *warning*, not an error. In
  `exclude` mode it is the normal case, but it also catches a misspelled name.

### Validation merges first, then cross-checks

A spell naming `"condition": "vale-cursed"` is valid exactly when some pack in the
merged set defines it. No per-file check can know that, so `content.validate`
performs the merge and then checks references across it.

An unresolved **condition** is an error — nothing could apply it. An unresolved
**spell or item name on a creature** is a warning, because the engine refuses those
at use time with a clear reason rather than crashing, so a pack meant to be combined
with another still loads. You would rather hear about it now than mid-fight.

### Paths

Only `*.json` is read. A symlink inside a scanned directory that resolves outside
it is refused with the reason rather than followed — you declared the directory,
not wherever a link points. Files over 4 MiB fail cleanly instead of stalling
session start.

## Reloading during a session

`content.configure` builds a **new** registry; it never mutates the one in use.

**Encounters already in progress keep the content they started with.** This is not
laziness — switching to `exclude` mid-fight would otherwise strip the very creature
currently taking its turn. `content.status` lists any encounter running on older
content under `encounters_on_older_content`, so the divergence is visible rather
than mysterious. Start a new encounter to use new content.

Analytics binds its registry once, when called. A reconfiguration landing part-way
through a batch would make the result unreproducible from its seed, which is the
one property those numbers rest on.

A **failed** `content.configure` changes nothing. The error carries every
diagnostic, not the first, and the content you had keeps working.

## Two things to know

**`analytics.rounds` uses healing, not arbitrary item tactics.** The auto-play
policy revives a downed ally and heals one at half HP or below with a healing
spell or item, respecting action and Bonus Action costs. It does not value other
item effects, control spells, or long-term resource conservation.

**Bundled items: one.** The SRD slice ships the **Potion of Healing** — 2d4+2, a
Bonus Action, on the drinker or an ally within 5 ft — and nothing else, because
it is the one SRD consumable every clause of which this vocabulary can say. The
thrown ones (Acid, Alchemist's Fire, Holy Water) are each blocked on something
absent rather than unwritten: an item use has no range, its `save_dc` is a fixed
number where the SRD derives one from the thrower, and there is no ongoing
damage. Those are missing features, not a data gap — a pack cannot work around
them either.

## Licence note

The bundled slice is SRD 5.2.1 material under CC-BY-4.0; see the plugin's
[NOTICE](../NOTICE). **Your packs are yours.** Nothing in this project licenses
your content or asks you to attribute it to anyone, and the `provenance` field
exists so your material is never mistaken for ours — or ours for yours.

Content you write is not checked against the SRD, and should not be. The
restriction that only SRD-traceable names enter *bundled* data is a constraint on
what this project may redistribute, not on what you may load into your own game.
