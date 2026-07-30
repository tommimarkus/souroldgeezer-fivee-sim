# Content packs

Your campaign's creatures, spells, conditions, and items, in the format the engine
already uses.

A pack is one JSON file. The bundled SRD 5.2 slice is not a special case — it is
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
      "unmodelled": ["Blood Scent — advantage on tracking a wounded creature"]
    }
  ]
}
```

That is enough. The engine finds it with no configuration, because Claude Code
exports `CLAUDE_PROJECT_DIR` and the engine looks in `.fivee-sim/content/` under it.

Then, in a session:

- **`content_status`** — what is loaded, from where, and under which mode.
- **`content_validate`** — check a pack without loading it. Use this while writing.
- **`content_configure`** — load packs, or switch whether the bundled slice is
  included.

## Where content comes from

In precedence order, lowest first:

1. **the bundled SRD 5.2 slice**, unless the mode is `exclude`;
2. **`FIVEE_SIM_CONTENT`** — an `os.pathsep`-separated list of files or
   directories. A directory is scanned for `*.json`, in sorted order;
3. **`$CLAUDE_PROJECT_DIR/.fivee-sim/content/`**, used only when
   `FIVEE_SIM_CONTENT` is unset — so exporting the variable does not silently also
   load whatever sits in the repository you happen to be standing in;
4. **paths given to `content_configure`** during the session.

`FIVEE_SIM_BUILTIN` is `include` (the default) or `exclude`.

### Excluding the bundled content

`exclude` is not just a filter. It is what lets you run this engine on entirely
your own material with no SRD content loaded, which is a licensing posture rather
than a preference.

Two conditions survive `exclude`: **`unconscious`** and **`prone`**. The stepper
applies them itself when a creature drops to 0 hit points, so removing them would
make a creature falling over crash the fight. `content_status` lists them under
`retained_conditions` so the exception is visible rather than silent, and a pack of
your own defining either name replaces the retained row.

## Sections

Every section is optional, and record shapes match the bundled files.

### `creatures`

Required: `name`, `ac`, `max_hp`, `provenance`. Optional: `team`, `speed`,
`hit_dice`, `abilities`, `save_bonuses`, `attacks`, `attacks_per_action`, `spells`,
`spell_slots`, `spell_save_dc`, `spell_attack_bonus`, `items`, `conditions`,
`unmodelled`, `immunities`, `resistances`, `vulnerabilities`, `overrides`.

There is deliberately no `hp` or `position`. A creature starts a fight at full hit
points, and position is per-instance — you set it when you add a combatant to an
encounter, not in the stat block.

Attacks carry their own bonus and damage expression rather than deriving them from
ability scores and proficiency. That is how a stat block presents an attack, and it
keeps your data a transcription rather than a bet on derivation rules.

An attack may also carry riders, the way stat blocks hang extras off a hit.
`bonus_damage` with `bonus_damage_type` is extra damage on every hit — rolled
separately, doubled on a critical hit, and defended (resistance, vulnerability,
immunity) against its own type, the way a mephit's claw adds fire to its
slashing. `advantage_bonus_damage` adds dice of the main damage type only when
the attack roll actually resolved with Advantage, after every source has
combined and cancelled — the goblin pattern. `on_hit_condition` names a
condition the hit imposes, and any loaded condition qualifies, including one
your own pack defines. Give `on_hit_save_ability` and `on_hit_save_dc` together
to let the target save first; leave both out and the condition is automatic on
a hit. `on_hit_expiry` is `"none"` (the default — the condition lasts until
something removes it), `"start_of_attacker_next_turn"`, or
`"end_of_target_next_turn"`. The timed forms expire when that turn slot passes,
even if the attacker has died by then, and expiry never strips a condition
something else is still imposing — a stat block that starts with it, or another
effect still holding it.

`unmodelled` is where you name printed features the engine does not implement. It
is not decoration: Claude is instructed to check it before promising a trait will
fire, so a trait you list is a trait nobody will be surprised by.

### `spells`

Required: `name`, `level`, `provenance`. Optional: `school`,
`requires_attack_roll`, `save_ability`, `damage`, `damage_type`, `half_on_save`,
`upcast_damage`, `shape`, `radius`, `range_feet`, `max_targets`, `condition`,
`concentration`, `unmodelled`, `overrides`.

A spell cannot both require an attack roll and offer a saving throw. Set `radius`
together with `"shape": "sphere"` for an area spell; an area rolls its damage once
and compares every creature's save against that single total.

`max_targets` caps how many creatures may be **named** on one cast, and naming more
is refused rather than quietly trimmed. It does not apply to an area spell: there,
the radius decides who is caught, and the cap is ignored. That is why every bundled
area spell can leave `max_targets` at its default of 1 without shrinking to a single
creature.

### `conditions`

Required: `name`, `provenance`. Optional: `effects`, `description`, `unmodelled`,
`overrides`.

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
`own_attacks_have_disadvantage`, `auto_fail_strength_saves`,
`auto_fail_dexterity_saves`, `advantage_on_dexterity_saves`,
`disadvantage_on_dexterity_saves`, `melee_hits_are_critical`,
`resists_all_damage`.

Failing a save and being bad at one are different flags on purpose.
`auto_fail_dexterity_saves` decides the outcome; `disadvantage_on_dexterity_saves`
only weights it, and the creature can still succeed. Setting both leaves the
automatic failure in charge.

`melee_hits_are_critical` is scoped by **distance, not by weapon**, and the name is
historical. It upgrades any attack roll that lands from within 5 ft — a swing, a
shot, or a spell attack — which is how SRD 5.2 words the clause on Paralyzed and
Unconscious. Beyond 5 ft it does nothing.

`attacked_with_advantage_in_melee` and `attacked_with_disadvantage_at_range` are the
directional pair, and they are scoped the same way — **distance, not weapon**. Their
names are historical too, and kept so packs that set them keep working.
`attacked_with_advantage_in_melee` applies to any attack made from within 5 ft;
`attacked_with_disadvantage_at_range` applies to any attack made from beyond it. Set
both together for the Prone shape SRD 5.2 states: "An attack roll against you has
Advantage if the attacker is within 5 feet of you. Otherwise, that attack roll has
Disadvantage." A bow drawn point-blank on such a creature therefore gets Advantage,
and a reach weapon swung from 10 ft gets Disadvantage.

The three flags that read on attack rolls — `attacked_with_advantage`,
`own_attacks_have_disadvantage`, and their siblings — apply to spell attack rolls
as well as weapon ones, because the rules treat both as the same D20 Test.

A condition with no flags is legal, and is tracked without combat consequences —
useful for something narration cares about and dice do not.

### `items`

Required: `name`, `use`, `provenance`. Optional: `description`, `unmodelled`,
`overrides`.

An item is a **use with a known effect**, and nothing more. Inside `use`: `heal`,
`damage` with `damage_type`, `save_ability` with `save_dc` and `half_on_save`, and
`condition`. At least one of `heal`, `damage`, or `condition` must be present — an
item that does nothing costs an action for no reason, so it is refused.

```json
[
  { "name": "Potion of Healing", "use": { "heal": "2d4+2" },
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

Use one with `encounter_act(kind="use_item", item="Potion of Healing")`. It spends
the action. Healing defaults to the user; damage and conditions need a `target`.
Targeting another creature requires being within 5 ft.

## Rules the loader enforces

### `provenance` is required

At pack level and on every record. Once SRD and original material can sit in one
session, "where did this come from?" has to be answerable per entry — and
`lookup_rule` answers it, in the `source` and `provenance` fields.

### Unknown keys are errors

A pack that writes `attack_bonuses` for `attack_bonus` would otherwise load a
creature that fights wrongly and looks entirely fine. So an unrecognised section or
record key fails the load and names the valid ones.

### A name collision is reported, not resolved

Two packs defining `Vale Stalker` fail, and the error names both files. To replace
something deliberately, say so on the record:

```json
{ "name": "Goblin Warrior", "ac": 16, "max_hp": 12,
  "overrides": true, "provenance": "House rules" }
```

Precisely:

- **Within one level** — two packs claiming a name is an error *even if both say
  `overrides`*, because packs at one level load in path order and the winner would
  be an accident of filenames.
- **Across levels** — the later level wins. That ordering is declared, so a
  `content_configure` pack may override a project pack, which may override a
  built-in.
- **`overrides` with nothing to override** is a *warning*, not an error. In
  `exclude` mode it is the normal case, but it also catches a misspelled name.

### Validation merges first, then cross-checks

A spell naming `"condition": "vale-cursed"` is valid exactly when some pack in the
merged set defines it. No per-file check can know that, so `content_validate`
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

`content_configure` builds a **new** registry; it never mutates the one in use.

**Encounters already in progress keep the content they started with.** This is not
laziness — switching to `exclude` mid-fight would otherwise strip the very creature
currently taking its turn. `content_status` lists any encounter running on older
content under `encounters_on_older_content`, so the divergence is visible rather
than mysterious. Start a new encounter to use new content.

Analytics binds its registry once, when called. A reconfiguration landing part-way
through a batch would make the result unreproducible from its seed, which is the
one property those numbers rest on.

A **failed** `content_configure` changes nothing. The error carries every
diagnostic, not the first, and the content you had keeps working.

## Two things to know

**`simulate_rounds` does not use items.** The auto-play policy attacks, casts, and
closes distance; it never drinks a potion. Items on a combatant are simply ignored
in a batch. Play the fight by hand if items matter to the question.

**Bundled items: none.** The category is modelled but the SRD slice ships no items,
so potions reach a session through a pack. That is a data gap, not a missing
feature.

## Licence note

The bundled slice is SRD 5.2 material under CC-BY-4.0; see the plugin's
[NOTICE](../NOTICE). **Your packs are yours.** Nothing in this project licenses
your content or asks you to attribute it to anyone, and the `provenance` field
exists so your material is never mistaken for ours — or ours for yours.

Content you write is not checked against the SRD, and should not be. The
restriction that only SRD-traceable names enter *bundled* data is a constraint on
what this project may redistribute, not on what you may load into your own game.
