# Coverage

What this engine actually implements, and what it does not.

**Generated** from the bundled data and the engine's own enums by `uv run python -m fivee_sim.coverage` — do not edit by hand. A test fails if this file drifts from the data.

Rules content is SRD 5.2 under CC-BY-4.0; see [NOTICE](../NOTICE). SRD 5.2 covers only part of the 2024 ruleset, so content absent from the SRD is not available to this project at all.

**This describes the bundled slice.** A campaign can add its own creatures, spells, conditions, and usable items as content packs, or exclude the bundled content entirely and run on its own material — see [CONTENT-PACKS.md](CONTENT-PACKS.md). What a given session actually has loaded is reported by the `content_status` tool, which is the authority when packs are in play; this document is the authority for what ships.

## At a glance

| Category | Supported |
| --- | --- |
| Creatures (stat blocks) | 4 |
| Spells | 4 |
| Conditions | 14 |
| Damage types | 13 |
| Actions | 7 |
| Usable items | 0 bundled — the category is modelled, packs supply it |
| Classes, species, backgrounds, feats | 0 — not modelled |

The creature and spell lists are a deliberately narrow starting slice, not an attempt at the whole SRD.

## Creatures

| Name | AC | HP | Speed | Attacks | Printed features not implemented |
| --- | --- | --- | --- | --- | --- |
| Goblin Warrior | 15 | 10 (3d6) | 30 ft | Scimitar +4, reach 5 ft, 1d6+2 slashing; Shortbow +4, range 80/320 ft, 1d6+2 piercing | Nimble Escape: Bonus Action Disengage or Hide<br>Both attacks deal an extra 1d4 damage if the attack roll had Advantage |
| Ogre | 11 | 68 (8d10+24) | 40 ft | Greatclub +6, reach 5 ft, 2d8+4 bludgeoning | Javelin ranged attack option from its listed gear |
| Wolf | 12 | 11 (2d8+2) | 40 ft | Bite +4, reach 5 ft, 1d6+2 piercing | Pack Tactics: Advantage when an ally is within 5 feet of the target<br>Bite knocks a Medium or smaller target Prone on a failed Strength save |
| Zombie | 8 | 15 (2d8+6) | 20 ft | Slam +3, reach 5 ft, 1d8+1 bludgeoning | Undead Fortitude: on dropping to 0 HP, a Constitution save (DC 5 + damage taken) leaves it at 1 HP instead, unless the damage was Radiant or from a Critical Hit<br>Immunity to the Exhaustion and Poisoned conditions |

## Spells

| Name | Level | Resolution | Damage | Upcast | Area | Concentration | Not implemented |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Fireball | 3 | dexterity save, half on save | 8d6 | +1d6/level | 20 ft radius | no | Flammable objects in the area start burning |
| Guiding Bolt | 1 | spell attack roll | 4d6 | +1d6/level | single target | no | The next attack roll against a hit target before the end of your next turn has Advantage |
| Hold Person | 2 | wisdom save | — | — | single target | yes | Targets Humanoids only; the engine does not track creature type<br>The target repeats the save at the end of each of its turns, ending the effect on a success<br>Upcasting targets one additional creature per slot level above 2 |
| Shatter | 2 | constitution save, half on save | 3d8 | +1d8/level | 10 ft radius | no | A Construct has Disadvantage on the save<br>Unattended nonmagical objects in the area also take the damage |

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

Each combatant may take one action per turn, plus movement: `attack`, `cast`, `move`, `dash`, `disengage`, `dodge`, `use_item`. Extra Attack is supported as a count of attacks per action. Opportunity attacks are taken automatically when a creature leaves reach without disengaging.

Also resolved: death saving throws, stabilising, instant death when damage past 0 hit points equals maximum hit points, damage resistance, vulnerability and immunity, and concentration checks when a concentrating creature is damaged. A condition a concentration spell imposed is lifted when that concentration ends — by a failed check, by the caster being incapacitated or killed, or by the caster beginning another concentration spell — unless another effect is still imposing it.

A creature at 0 hit points is a legal target, not an untouchable one: an attack, an area effect it stands inside, and a usable item all reach it. Each costs it one death saving throw failure, two if the damage came from a critical hit — and an attack from within 5 feet of an Unconscious creature is always a critical hit. Only a dead creature is refused as a target.

## Damage types

acid, bludgeoning, cold, fire, force, lightning, necrotic, piercing, poison, psychic, radiant, slashing, thunder.

## Not supported

Stated explicitly because absence is invisible in the data above, and because a caller who assumes one of these exists will get a wrong answer rather than an error.

**Character building.** Classes, subclasses, species and lineages, backgrounds, feats, ability-score generation, levelling, and multiclassing. Combatants are described directly by their statistics — armour class, hit points, attacks, save bonuses — the way a stat block presents them. There is no notion of a character sheet that derives those numbers.

**Equipment beyond simple usable items.** Simple usable items *are* modelled: potions, flasks, and doses of poison — one use that heals, deals damage, or applies a condition, held in a quantity that is also its charge count. None ship in the bundled slice, so potions reach a session through a content pack. Nothing beyond that use is modelled. Weapons and armour as objects that derive attack bonuses and armour class, scrolls, attunement, encumbrance, ammunition, and charges tracked separately from quantity are all absent. An attack carries its own bonus and damage expression; nothing models the object producing it.

**Spell resources beyond slots.** Spell lists per class, preparation rules, ritual casting, cantrip scaling by level, components, and material costs. A combatant simply holds a set of spell names and a count of slots per level.

**Anything outside a fight.** Exploration, travel, downtime, resting and recovery, skills and proficiencies as a system, social interaction, and the adventuring day. Resources do not regenerate; an encounter begins and ends.

**Battlefield geometry.** Positions are feet along a single axis. Reach, ranged bands, and spell radii work; facing, flanking, cover, difficult terrain, elevation, and movement around obstacles do not exist.

**Timed durations.** Concentration is tracked, and ending it lifts the condition the spell imposed. Elapsed time is not: the 'up to 1 minute' cap on a concentration spell never expires it, a spell's repeat saving throw at the end of the target's turn is not rolled, and a condition applied by an item or set directly on a stat block lasts until something removes it. A condition that should wear off on its own does not.

**Reactions other than opportunity attacks.** Readied actions, Shield and similar reaction spells, Parry, and legendary or lair actions. Each combatant has one reaction per round and only ever spends it on an opportunity attack.

**A fight that carries on over the dying.** An encounter ends as soon as one side has nobody conscious left, so a side reduced to dying creatures counts as beaten. Their death saves stop with the fight: a downed creature can never roll the natural 20 that would put it back on its feet, and a mutual knockout is reported as a draw rather than decided by whichever side recovers first. Damage to a creature at 0 hit points is fully modelled — an attack, an area spell, and an item all reach one — but this is about when the fight stops being simulated. Measured on the bundled stat blocks, counting the dying as still in the fight would lengthen a reported fight by 58% to 131%, and 30% to 46% of every round reported would be one in which nobody acts at all — more still once a caster is involved. Nothing in the auto-play policy that drives a batch finishes a downed creature off or takes the Help action to stabilise one, so those rounds are an empty room rather than a fight.

**Monster instant death.** SRD 5.2 has a monster die the instant it drops to 0 hit points, where a character instead falls unconscious and makes death saving throws. Every combatant here is treated as a character, so any creature that drops begins the dying state.

**Conditions imposed on a creature that is already down.** A spell or item that imposes a condition applies it only to a conscious target. Damage from the same effect still lands on a dying creature, and still costs it a death saving throw failure; the condition does not follow.

## Checking at runtime

`lookup_rule` with no topic lists every loaded condition, spell, creature, and item. With a topic it returns that entry, including the pack it came from and its `unmodelled` field. A miss means the subject is not loaded — it is refused rather than invented.

`content_status` reports which packs are loaded, whether the bundled slice is included, and any encounter still running on content from before the last change. `content_validate` checks a pack without loading it.

`encounter_log` pages the full event and action history of a fight in progress, stamped with rounds and turns — the record to recap or replay from, where `encounter_state` is only the view of now.
