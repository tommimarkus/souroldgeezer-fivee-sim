# Coverage

What this engine actually implements, and what it does not.

**Generated** from the bundled data and the engine's own enums by `uv run python -m fivee_sim.coverage` — do not edit by hand. A test fails if this file drifts from the data.

Rules content is SRD 5.2 under CC-BY-4.0; see [NOTICE](../NOTICE). SRD 5.2 covers only part of the 2024 ruleset, so content absent from the SRD is not available to this project at all.

**This describes the bundled slice.** A campaign can add its own creatures, spells, conditions, and usable items as content packs, or exclude the bundled content entirely and run on its own material — see [CONTENT-PACKS.md](CONTENT-PACKS.md). What a given session actually has loaded is reported by the `content_status` tool, which is the authority when packs are in play; this document is the authority for what ships.

## At a glance

| Category | Supported |
| --- | --- |
| Creatures (stat blocks) | 6 |
| Spells | 4 |
| Conditions | 14 |
| Damage types | 13 |
| Actions | 9 |
| Usable items | 0 bundled — the category is modelled, packs supply it |
| Terrain kinds | 13 built in — packs may add more |
| Classes, species, backgrounds, feats | 0 — not modelled |

The creature and spell lists are a deliberately narrow starting slice, not an attempt at the whole SRD.

## Creatures

| Name | AC | HP | Speed | Attacks and traits | Printed features not implemented |
| --- | --- | --- | --- | --- | --- |
| Goblin Boss | 17 | 21 (6d6) | 30 ft | Scimitar +4, reach 5 ft, 1d6+2 slashing plus 1d4 if the attack roll had advantage; Shortbow +4, range 80/320 ft, 1d6+2 piercing plus 1d4 if the attack roll had advantage | Nimble Escape: Bonus Action Disengage or Hide<br>Redirect Attack: Reaction to swap places with an ally within 5 feet, which becomes the target instead |
| Goblin Warrior | 15 | 10 (3d6) | 30 ft | Scimitar +4, reach 5 ft, 1d6+2 slashing plus 1d4 if the attack roll had advantage; Shortbow +4, range 80/320 ft, 1d6+2 piercing plus 1d4 if the attack roll had advantage | Nimble Escape: Bonus Action Disengage or Hide |
| Ogre | 11 | 68 (8d10+24) | 40 ft | Greatclub +6, reach 5 ft, 2d8+4 bludgeoning | Javelin ranged attack option from its listed gear |
| Skeleton | 14 | 13 (2d8+4) | 30 ft | Shortsword +5, reach 5 ft, 1d6+3 piercing; Shortbow +5, range 80/320 ft, 1d6+3 piercing | Immunity to the Exhaustion and Poisoned conditions |
| Wolf | 12 | 11 (2d8+2) | 40 ft | Bite +4, reach 5 ft, 1d6+2 piercing, on hit: prone against a Medium or smaller target<br>Traits: Pack Tactics — Advantage while a capable ally is within 5 ft of the target | — |
| Zombie | 8 | 15 (2d8+6) | 20 ft | Slam +3, reach 5 ft, 1d8+1 bludgeoning<br>Traits: Undead Fortitude — on a drop to 0 HP, a Constitution save (DC 5 + damage taken) leaves 1 HP instead, unless the damage was Radiant, a Critical Hit, or enough to kill outright | Immunity to the Exhaustion and Poisoned conditions |

## Spells

| Name | Level | Resolution | Damage | Upcast | Area | Concentration | Not implemented |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Fireball | 3 | dexterity save, half on save | 8d6 | +1d6/level | 20 ft sphere | no | Flammable objects in the area start burning |
| Guiding Bolt | 1 | spell attack roll | 4d6 | +1d6/level | single target | no | The next attack roll against a hit target before the end of your next turn has Advantage |
| Hold Person | 2 | wisdom save | — | — | single target | yes | Targets Humanoids only; the engine does not track creature type<br>The target repeats the save at the end of each of its turns, ending the effect on a success<br>Upcasting targets one additional creature per slot level above 2 |
| Shatter | 2 | constitution save, half on save | 3d8 | +1d8/level | 10 ft sphere | no | A Construct has Disadvantage on the save<br>Unattended nonmagical objects in the area also take the damage |

## Conditions

| Condition | Mechanical effect |
| --- | --- |
| blinded | attacked with advantage, own attacks have disadvantage |
| charmed | tracked; no combat-roll consequences |
| deafened | tracked; no combat-roll consequences |
| frightened | own attacks have disadvantage |
| grappled | speed zero, own attacks have disadvantage |
| incapacitated | incapacitated |
| invisible | attacked with disadvantage, own attacks have advantage |
| paralyzed | incapacitated, speed zero, attacked with advantage, auto fail strength saves, auto fail dexterity saves, melee hits are critical |
| petrified | incapacitated, speed zero, attacked with advantage, auto fail strength saves, auto fail dexterity saves, resists all damage |
| poisoned | own attacks have disadvantage |
| prone | attacked with advantage in melee, attacked with disadvantage at range, own attacks have disadvantage |
| restrained | speed zero, attacked with advantage, own attacks have disadvantage, disadvantage on dexterity saves |
| stunned | incapacitated, attacked with advantage, auto fail strength saves, auto fail dexterity saves |
| unconscious | incapacitated, speed zero, attacked with advantage, auto fail strength saves, auto fail dexterity saves, melee hits are critical |

**Not implemented:** Exhaustion. SRD 5.2 defines it; this engine does not track it.

## Actions

Each combatant may take one action per turn, plus movement: `attack`, `cast`, `move`, `dash`, `disengage`, `dodge`, `use_item`, `interact`, `stand`. Extra Attack is supported as a count of attacks per action. Opportunity attacks are taken automatically when a creature leaves reach without disengaging.

`interact` works a map fixture the actor stands on or next to, on its own storey. By default it is the free object interaction: once per turn, without spending the action. A fixture may cost the action instead, may wait on other fixtures standing open before it will open, and may take an ability check — a failed check spends the budget and moves nothing. It toggles unless the action names `set_open`, which drives the fixture to the state asked for rather than flipping whatever it finds.

`stand` gets a Prone creature back on its feet: no action, but movement equal to half the creature's Speed, rounded down. It is refused when the creature is not Prone, when its Speed is 0 — from the stat block or from a condition such as Grappled — or when the movement left this turn is less than the cost. The auto-play policy behind the batch tools stands a Prone creature at its first legal opportunity each turn.

Also resolved: death saving throws, stabilising, instant death when damage past 0 hit points equals maximum hit points, damage resistance, vulnerability and immunity, and concentration checks when a concentrating creature is damaged. A condition a concentration spell imposed is lifted when that concentration ends — by a failed check, by the caster being incapacitated or killed, or by the caster beginning another concentration spell — unless another effect is still imposing it.

An attack may carry riders, straight from its stat block: bonus damage of a second type on every hit, defended against its own type; extra dice added only when the attack roll actually resolved with Advantage; and an on-hit condition, automatic or applied on a failed save. Rider dice double on a critical hit like any damage dice. An on-hit condition may expire on its own — at the start of the attacker's next turn or the end of the target's next turn — and the expiry fires when that turn slot passes, even if the attacker has died; it never strips a condition something else is still imposing.

Two printed creature traits are modelled as stat-block flags. Pack Tactics grants a creature's attack rolls — weapon and spell alike, opportunity attacks included — Advantage while another member of its team is within 5 feet of the target, conscious, and free of incapacitating conditions; it counts as one Advantage source and cancels against Disadvantage like any other. Undead Fortitude turns damage that would drop the creature to 0 hit points into a Constitution saving throw at DC 5 plus the damage taken, and a success leaves it standing at 1 hit point — bypassed when any of the damage was Radiant, the hit was a critical, or the overflow was enough to kill outright.

A creature at 0 hit points is a legal target, not an untouchable one: an attack, an area effect it stands inside, and a usable item all reach it. Each costs it one death saving throw failure, two if the damage came from a critical hit — and an attack from within 5 feet of an Unconscious creature is always a critical hit. Only a dead creature is refused as a target.

## Damage types

acid, bludgeoning, cold, fire, force, lightning, necrotic, piercing, poison, psychic, radiant, slashing, thunder.

## Battlefield

Positions are `[x, y]` points in feet on a plane of 5-foot squares. A fight may run mapless — an open, featureless plane — or on a battle map, supplied inline to `encounter_create` and `simulate_rounds`, which adds terrain movement costs, walls, line of sight, cover, pathfinding, and doors. Doors are named map features flipped by the `interact` action; closed they are impassable and block sight.

A door is the common case of a **fixture** — any map feature carrying a state is one, so a lever, a spike, or a sluice gate is the same record with more on it. A fixture may govern squares beyond its own, naming what each becomes in either state in terrain and in ground height alike, may wait on other fixtures standing open, may cost the action rather than the free interaction, and may take an ability check. Working one changes that ground immediately, under whoever is standing on it: entry cost governs entering a square rather than remaining in one, so a creature whose footing turns impassable stays where it is and may walk out. Every square a fixture governs is claimed by exactly one fixture per level, which leaves no precedence to resolve and is what lets a stateless map query agree with the live fight about what a square is.

**Area shapes:** sphere, cone, line, cube. **Cover grades:** none, half, three-quarters, total — graded by corner-counted sight lines; half and three-quarters raise the target's AC against attacks and its Dexterity saving throws against areas, while total cover refuses the attack and excludes the target from an area outright. **Diagonal rules:** `5-5-5`, `5-10-5` — a per-encounter knob governing movement and areas alike; the default prices every diagonal at 5 ft.

Built-in terrain kinds — content packs may define more:

| Kind | Effects |
| --- | --- |
| difficult | movement x2 |
| door-closed | impassable, blocks sight |
| door-open | ordinary ground |
| floor | ordinary ground |
| forest | movement x2, grants half cover |
| half-cover | grants half cover |
| hill | movement x2 |
| mountain | impassable |
| normal | ordinary ground |
| plain | ordinary ground |
| three-quarters-cover | grants three-quarters cover |
| wall | impassable, blocks sight |
| water | movement x2 |

**Ground height** is feet per square, negative for ground below the map's datum, and it is charged to movement only. A rise of under 2 ft across a square is a gentle grade and costs nothing extra; from there up to 5 ft the square is a slope, which counts as difficult terrain and — since difficult terrain is not cumulative — is doubled once however rough the going. Above 5 ft the face is climbed, costing 1 extra foot per foot climbed (2 extra in difficult terrain) on top of the step into the square, and climbing down costs what climbing up costs. Sight, cover, and areas ignore height entirely.

## Not supported

Stated explicitly because absence is invisible in the data above, and because a caller who assumes one of these exists will get a wrong answer rather than an error.

**Character building.** Classes, subclasses, species and lineages, backgrounds, feats, ability-score generation, levelling, and multiclassing. Combatants are described directly by their statistics — armour class, hit points, attacks, save bonuses — the way a stat block presents them. There is no notion of a character sheet that derives those numbers.

**Equipment beyond simple usable items.** Simple usable items *are* modelled: potions, flasks, and doses of poison — one use that heals, deals damage, or applies a condition, held in a quantity that is also its charge count. None ship in the bundled slice, so potions reach a session through a content pack. Nothing beyond that use is modelled. Weapons and armour as objects that derive attack bonuses and armour class, scrolls, attunement, encumbrance, ammunition, and charges tracked separately from quantity are all absent. An attack carries its own bonus and damage expression; nothing models the object producing it.

**Spell resources beyond slots.** Spell lists per class, preparation rules, ritual casting, cantrip scaling by level, components, and material costs. A combatant simply holds a set of spell names and a count of slots per level.

**Anything outside a fight.** Exploration, travel, downtime, resting and recovery, skills and proficiencies as a system, social interaction, and the adventuring day. Resources do not regenerate; an encounter begins and ends.

**The third dimension, past what it costs to walk.** Ground height is modelled, and it reaches movement alone — see the Battlefield section. Everything else on the map is measured flat: sight lines, cover, and area templates ignore height entirely, so a ridge screens nobody and a creature atop a cliff is neither harder to hit nor better placed to shoot. Also absent: falling and fall damage, flying and swimming, jumping, a Climb Speed (a creature with one still pays the climb), creature size and squeezing (every combatant occupies one square whatever its printed size), facing, flanking, and forced movement — nothing pushes, drags, or knocks a creature through space, so no one is ever shoved off a ledge.

**Timed durations beyond attack riders.** Concentration is tracked, and ending it lifts the condition the spell imposed. An attack's on-hit condition rider can carry its own clock — expiring at the start of the attacker's next turn or the end of the target's next turn, and the expiry fires even if the attacker has died. Beyond those two anchors, elapsed time is not modelled: the 'up to 1 minute' cap on a concentration spell never expires it, a spell's repeat saving throw at the end of the target's turn is not rolled, and a condition applied by an item or set directly on a stat block lasts until something removes it.

**Reactions other than opportunity attacks.** Readied actions, Shield and similar reaction spells, Parry, and legendary or lair actions. Each combatant has one reaction per round and only ever spends it on an opportunity attack.

**A fight that carries on over the dying.** An encounter ends as soon as one side has nobody conscious left, so a side reduced to dying creatures counts as beaten. Their death saves stop with the fight: a downed creature can never roll the natural 20 that would put it back on its feet, and a mutual knockout is reported as a draw rather than decided by whichever side recovers first. Damage to a creature at 0 hit points is fully modelled — an attack, an area spell, and an item all reach one — but this is about when the fight stops being simulated. Measured on the bundled stat blocks, counting the dying as still in the fight would lengthen a reported fight by 58% to 131%, and 30% to 46% of every round reported would be one in which nobody acts at all — more still once a caster is involved. Nothing in the auto-play policy that drives a batch finishes a downed creature off or takes the Help action to stabilise one, so those rounds are an empty room rather than a fight.

**Monster instant death.** SRD 5.2 has a monster die the instant it drops to 0 hit points, where a character instead falls unconscious and makes death saving throws. Every combatant here is treated as a character, so any creature that drops begins the dying state.

**Skill proficiency on a check.** The check a map fixture takes is a raw ability check, and there is nowhere in the model to make it anything else: a creature carries ability modifiers and no skill proficiencies at all, so there is no Athletics to add, no proficiency bonus, no Expertise, and no Help action to grant Advantage. Set a fixture's DC as if the character were untrained — a DC pitched at a trained bonus will play several points harder than intended. The standalone `check` primitive is the one place a proficiency can be applied at all, and only because its modifier is supplied by the caller rather than read off a creature.

**A batch that works the map.** The auto-play policy behind `simulate_rounds` never operates a map fixture: no door is opened, no spike pulled, no sluice raised. A batch fights the map at the configuration it was handed. Measure what a fixture is worth by running two batches — one map authored open, one shut — rather than by expecting the policy to find the lever.

**Conditions imposed on a creature that is already down.** A spell or item that imposes a condition applies it only to a conscious target. Damage from the same effect still lands on a dying creature, and still costs it a death saving throw failure; the condition does not follow.

## Checking at runtime

`lookup_rule` with no topic lists every loaded condition, spell, creature, and item. With a topic it returns that entry, including the pack it came from and its `unmodelled` field. A miss means the subject is not loaded — it is refused rather than invented.

`content_status` reports which packs are loaded, whether the bundled slice is included, and any encounter still running on content from before the last change. `content_validate` checks a pack without loading it.

`encounter_log` pages the full event and action history of a fight in progress, stamped with rounds and turns — the record to recap or replay from, where `encounter_state` is only the view of now. `replay_export` turns that record into a portable replay bundle — the fight's seed, its map as captured at creation, the starting roster, and every event — or, with `embed`, a single self-contained page that plays the fight back in a browser. `uvtt_export` writes a loaded map as a Universal VTT file — wall polylines derived from the tiles, door portals, and a rendered image — for import into other virtual tabletops.

The `map_*` tools — `map_generate`, `map_load`, `map_save`, `map_render`, `map_edit`, `map_query` — manage battle maps as seeded, editable documents in the running session; what maps exist there, and at which generation, is their answer rather than this document's.
