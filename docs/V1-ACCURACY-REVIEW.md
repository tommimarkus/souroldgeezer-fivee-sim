# v1 accuracy review

A review of whether the simulator's implemented behaviour is *correct* — that its
numbers mean what they claim to mean — against the v1 goal of a bedrock-solid
foundation.

Reviewed at `2dff680` on branch `review/v1-rules-accuracy`. Read-only: nothing in
this review changed engine code.

## Verdict

**The rules kernel is sound. The analytics layer that sits on top of it is not.**

The primitives are in unusually good shape. Dice, advantage collapsing, critical
doubling, damage rounding order, and the closed-form probability math were
attacked hard — by exhaustive enumeration and 200k-sample cross-checks, not by
reading — and came back clean. The bundled SRD transcription reconciles
arithmetically on every creature. Determinism under a seed holds across
`PYTHONHASHSEED` variation.

The defects are all one layer up, where the kernel's correct answers get
assembled into a fight: a corrupted turn budget in `simulate_dpr`, spell attacks
that skip the advantage machinery weapon attacks use, and conditions that no
mechanism can ever remove.

Baseline is green — 472 tests pass, `ruff` and `mypy --strict` clean. Every
finding below is something the suite does not pin.

## How to read the confidence column

There is **no copy of the SRD 5.2.1 rules text in this repo**. Findings are
therefore split by what backs them:

- **code** — provable from the engine's own behaviour (internal inconsistency, a
  closed form disagreeing with its roller, a field nothing reads). Independent of
  any rules recall being right.
- **recalled-rules** — depends on a recollection of what the printed rule says.
  Flagged explicitly. One finding in the original set was killed on exactly this
  basis; see [Refuted](#refuted-and-why-that-matters).

Findings are ranked by whether they corrupt numbers a user acts on, not by rules
pedantry.

---

## 1. `simulate_dpr` runs round 1 on the dummy's action budget

**HIGH · code · `analytics/montecarlo.py:502`**

`Encounter.__init__` ends by calling `_begin_turn`, which builds the turn's
budget from whoever won *initiative*. `simulate_dpr` then overwrites the order to
force the attacker first — but never rebuilds that budget:

```python
encounter.order = [attacker.name, dummy.name]
encounter.turn_index = 0          # _turn still belongs to whoever __init__ picked
```

When the dummy wins initiative (roughly half of seeds), the attacker's first
round executes with the dummy's `TurnState`: `movement_left=0`,
`attacks_left=1`.

Measured directly — a 3-attack, speed-30 fighter, reordered exactly as
`simulate_dpr` does it:

| seed | initiative order | turn_state handed to the fighter |
|---|---|---|
| 1, 2, 3, 4, 8 | `['Target', 'Fighter']` | `movement_left=0, attacks_left=1` ❌ |
| 5, 6, 7 | `['Fighter', 'Target']` | `movement_left=30, attacks_left=3` ✅ |

Two distinct corruptions follow.

**Attacks are lost.** Round 1 is capped at one swing regardless of Extra Attack,
and a build that starts at range forfeits the round entirely, because
`movement_left == 0` makes `_closing_move` return `None`. Against the closed form
(4000 iterations, 3 rounds, seed 5):

| build | measured DPR | closed form | error |
|---|---|---|---|
| 1 attack, adjacent | 5.304 | 5.325 | −0.4% (noise) |
| 2 attacks, adjacent | 9.819 | 10.650 | **−7.8%** |
| 3 attacks, adjacent | 14.282 | 15.975 | **−10.6%** |
| 1 attack, 30 ft away | 4.449 | 5.325 | **−16.5%** |

**An illegal extra action is granted.** `_do_attack` sets `action_used` via
`if self._turn.attacks_left == actor.attacks_per_action - 1`. Starting from a
stale `attacks_left=1` on a 3-attack build, the decrement lands on `0`, `0 == 2`
is false, and the action is never marked used — so the policy is then offered a
spell as well. A martial/caster hybrid swings *and* casts in the same turn on the
affected seeds.

**Why it matters.** "Which build does more damage?" is the one question
`simulate_dpr` exists to answer, and this error is systematically signed and
build-dependent: multiattack martials understated, ranged-start melee builds
understated harder, hybrids inflated. Comparing two builds at the same seed
compares them under *different action economies*. The MCP `simulate_dpr` tool
delegates straight here, so these are the numbers a user sees.

**The result also depends on the initiative roll `simulate_dpr` exists to
eliminate.** The whole point of forcing the order is to measure damage
independently of who goes first — but because the budget still comes from the
initiative winner, the reported figure varies with the roll the override was
meant to neutralise. So the numbers are not merely biased; they are unstable
across seeds in a way the tool's contract implies they should not be, and the
error is a function of an input the caller believes has been factored out.

The closed forms in `expectation.py` are correct throughout — the defect is that
the stepper is handed the wrong turn state, so correct valuations get spent
against an illegal budget.

Three of the five finders hit this defect independently, from three different
dimensions (closed-form agreement, rounding order, RNG determinism), each landing
on the same line. It was then reproduced by hand in the parent session.

**Not caught because** `test_extra_attack_raises_expected_damage` asserts only
`twice > once * 1.5`; the corrupted ratio is ~1.8, so it passes.

**Fix surface:** one line at the override site — rebuild the turn state after
reordering instead of inheriting what `__init__` left behind.

---

## 2. Spell attack rolls ignore advantage and forced criticals

**HIGH · code · `model/encounter.py:518`**

`_do_cast` calls `resolve_spell(...)` without passing `advantage=`, so every
spell attack roll resolves at `Advantage.NONE`. `resolve_spell` has no
`forced_critical` parameter at all, so a spell attack can never be upgraded by
Paralyzed or Unconscious.

Weapon attacks in the same class do it properly, via `attack_advantage()` and
`attack_forced_critical()`. The same split is duplicated in the valuation layer:
`montecarlo.py:148` passes both for weapons, `montecarlo.py:199` passes neither
for spells.

Measured over 4000 seeds through the real stepper — a +5 spell attack against a
**Paralyzed** AC-15 target: hit rate **0.558**, crit rate **0.048**. A flat d20.
With the advantage the target's condition grants, it should be ~0.798.

So a Blinded or Frightened caster takes no disadvantage; a Dodging target imposes
none (the `_dodging` map is never consulted on the cast path); a Prone,
Restrained, Blinded, or Paralyzed target grants none.

Secondary instance of the same gap: spell saving throws are rolled at
`kernel/spells.py:182` with no `advantage` argument either, so Dodge's advantage
on Dexterity saves cannot reach them.

**Why it matters.** "Blast the held target or stab it?" under-reports the spell
by roughly 40% relative, and the auto-play policy then picks the weapon for a
reason that isn't real.

---

## 3. A spell-applied condition can never be removed

**HIGH · code · `data/srd/spells.json:47` — architectural, not Hold-Person-specific**

Hold Person declares `"condition": "paralyzed"` and `"concentration": true`. The
engine honours both halves separately and connects neither: `_do_cast` sets
`concentrating_on` and applies the condition; `_apply_damage` rolls a real
Constitution save and on failure clears `concentrating_on` and logs
`loses Hold Person`. The condition stays.

Across the whole engine, the only condition *removals* are `UNCONSCIOUS`/`PRONE`
on healing or death. **No code path ever removes a spell-applied condition.**

Demonstrated: cast Hold Person, break the caster's concentration (log confirms
`loses Hold Person`), kill the caster outright, advance 40 turns — the target's
condition set is still `{'paralyzed'}` and it never acts again. Because
`paralyzed` sets `melee_hits_are_critical`, every melee hit against it from
within 5 ft also stays a critical.

The record's own `unmodelled` list names the end-of-turn repeat save as the
missing exit — which implies concentration *is* an exit that works. It isn't.
`COVERAGE.md` compounds this: it prints `Concentration | yes` and asserts
"concentration checks when a concentrating creature is damaged" — literally true,
materially false, in the document whose stated job is making absence visible.

**Why it matters.** One 2nd-level slot permanently deletes a 68 HP combatant and
doubles all melee damage against it. Any encounter-difficulty or action-economy
conclusion drawn from a fight involving a control spell is wrong.

---

## 4. `Restrained` auto-fails Dexterity saves instead of imposing Disadvantage

**MEDIUM · recalled-rules · `kernel/conditions.py:132`**

The `RESTRAINED` row sets `auto_fail_dexterity_saves=True`. Recollection of both
the 2024 and 2014 wording is that Restrained imposes *Disadvantage* on Dexterity
saves; automatic failure belongs to Paralyzed, Petrified, Stunned, and
Unconscious — all four of which the table already gets right. **This rests on
recall, not a verified source.**

The structural half *is* provable from code: `ConditionEffect` has no flag for
advantage or disadvantage on saving throws at all. So the correct Restrained rule
cannot be expressed by any content pack either, and the same absence is why
Dodge's advantage on Dexterity saves is silently missing.

Impact, Web-then-Fireball (DEX +2, DC 15, 8d6):

| | expected damage |
|---|---|
| engine (auto-fail) | 28.0 |
| correct (Disadvantage, P(save)=0.16) | 25.7 |
| no condition at all | 22.3 |

Over-reports by ~9%, and reports as deterministic something the rules leave a 16%
chance of halving.

---

## 5. `take_damage` wipes accumulated death-save failures

**MEDIUM · code · `model/creature.py:160`**

```python
self.hp = max(0, self.hp - amount)
if self.hp == 0:            # true when *already* at 0, not just on dropping to 0
    ...
    self.death_save_successes = 0
    self.death_save_failures = 0
```

The guard cannot distinguish "just dropped to 0" from "already lying at 0", so
damage to a dying creature clears its progress toward death. Nothing else in the
engine ever decrements those counters — `_death_save` only increments — which
makes this an internal inconsistency independent of any rules reading. (The rules
expectation, for context, is that damage at 0 HP *adds* a failure.)

Confirmed at unit level: failures set to 2, `take_damage(3)` → failures **0**,
`dead=False`.

Reachability is narrow but real. `_do_attack` refuses an unconscious target and
`_spell_targets` filters to `conscious`, but `_do_use_item` refuses only a *dead*
one — so a damaging item (a flask, a dose of poison) lands on a dying creature
and calls `_apply_damage`. Reproduced through the public stepper:
`{'failures': 2}` → `{'failures': 0}`.

The auto-play policy never uses items, so batch analytics are unaffected. This
corrupts stateful, DM-driven play through `encounter_act`.

---

## 6. `shape` is declared, validated in one direction, and read by nothing

**MEDIUM · code · `data/srd/spells.json:17`, guard at `content.py:978`**

Fireball and Shatter declare `"shape": "sphere"`. `content.py` parses it into a
`SpellShape` enum and stores it on the `Spell` dataclass. Grepping
`\.shape|SpellShape|SPHERE|SINGLE` across `src/` and `tests/` returns the
definition, the field, and that one parse site — **zero readers**. Area
resolution branches on `spell.radius` alone; `COVERAGE.md`'s Area column renders
from `radius`; `shape` isn't even serialised into `lookup_rule` output.

The teeth are in the asymmetric guard: `_cross_reference` warns when
`radius and shape is None` — the harmless direction — and is silent on the
inverse.

So a pack author writing `{"shape": "sphere", "damage": "8d6", ...}` and omitting
`radius` because `shape` looks like the field that declares an area gets a
**clean** `content_validate` and a spell that silently hits exactly one creature.

---

## 7. Downed creatures are untargetable, and this is not documented

**MEDIUM · code · `model/encounter.py:347`, `:611`**

`_do_attack` refuses any target that isn't `conscious` ("already down") and
`_spell_targets` filters non-conscious creatures out of areas. Verified: a
Fireball centred exactly on a dying PC's position dealt it **0** while damaging
everyone else in radius.

Consequently a dying creature can only die by failing three death saves or by the
massive-damage overflow rule at the moment it drops. No finishing blow, no area
damage while down.

Compounding it, `Encounter.over` tests `living_teams()`, which counts only
*conscious* creatures — so a fight ends the instant the **last** member of a side
drops, and that side's death saves are never rolled, because no further turns are
taken.

This is narrower than it first looks, and worth stating precisely: while the
team still has a conscious member, death saves *do* run normally. Verified — a
dying PC1 alongside a conscious PC2 rolled `failure, failure, success` over three
rounds via `_begin_turn` → `_death_save`. The gap is only the terminal case.

These may well be deliberate simplifications. The problem is that
`COVERAGE.md`'s "Not supported" section — which exists precisely "because absence
is invisible in the data above" — does not mention any of it.

---

## 8. Spell slot validation runs after state has already been mutated

**MEDIUM · code · `model/encounter.py:514-518`**

`_do_cast` sets `action_used = True` and decrements the slot, *then* calls
`resolve_spell`, which is where the slot-level check lives. Casting Fireball with
a level-2 slot:

```
before: slots={2: 2}  action_used=False
raised ValueError: Fireball is level 3 and cannot be cast with a level 2 slot
after:  slots={2: 1}  action_used=True
```

The slot and the action are consumed for a cast that never happened. Worse, it
escapes as a bare `ValueError`, and `encounter_act` catches only `EncounterError`
— so this surfaces as an unhandled server exception rather than the "illegal
actions are refused with the reason" contract the tool documents.

---

## What was checked and found correct

Worth recording, because the negative results are broad and they are what makes
the kernel trustworthy:

- **Probability distributions.** `_sum_distribution` matches exhaustive
  `itertools.product` enumeration to 1e-12 across six dice shapes plus the
  degenerate `count=0` case. `_natural_distribution` matches the true 2d20 order
  statistics for all three advantage states to 1e-12, each summing to exactly 1.
- **Closed form vs roller.** A 30-cell matrix over advantage × forced-critical ×
  {none, resisted, vulnerable, immune, resisted+vulnerable}, verified by 200k-sample
  Monte Carlo against the *full* kernel paths — all within 0.06.
- **Rounding order.** All four damage sites (`effective_damage`, `resolve_spell`,
  `resolve_item_use`, `expected_damage`) apply the identical sequence:
  clamp-at-0 → halve-on-save with floor → resistance/vulnerability/immunity last.
  Swept against exhaustive enumeration including negative modifiers that clamp
  (`1d6-1`, `2d4-5`, `1d4-3`) and a 0-count expression. Zero divergences.
- **Determinism.** 12 full event transcripts plus a 25-iteration batch hashed
  identically under `PYTHONHASHSEED` 0/1/2/12345. Structural, not luck:
  `compute_attack_advantage` counts sources rather than short-circuiting, and
  every condition query is an order-free `any()`.
- **Analytics really does replay the stepper.** `simulate_rounds(iterations=1)`
  matched `build_encounter` + `run_encounter` across 40 seeds on scenarios the
  pinned test does not cover.
- **SRD transcription.** All four creatures reconcile arithmetically: attack
  bonus = ability modifier + PB in every case; `hit_dice` average equals `max_hp`
  including CON-per-die (Ogre 8d10+24 = 68); Zombie's `save_bonuses {wisdom: 0}`
  = WIS −2 + PB 2. No transcription errors found. Every recalled stat-block trait
  is disclosed in `unmodelled`.
- **Core rules arithmetic.** `proficiency_bonus` = `2 + (level-1)//4`, rejects
  level < 1. `ability_modifier` is genuine floor division (9 → −1; the classic
  truncation bug is absent). Criticals double dice and not the flat modifier.
  `forced_critical` upgrades an existing hit rather than manufacturing one.
  Advantage collapsing correctly returns neither when both are present, and does
  not stack a third die.

## Refuted, and why that matters

One finding was killed by the adversarial pass, and it is the most instructive
result in this review.

**Claim:** saving throws have no natural-20/natural-1 auto-resolution, unlike
attack rolls — an inconsistency in `D20Test.success`.

**Refuted.** In SRD 5.2.1 / the 2024 rules, the natural-20 and natural-1 rules are
scoped *to attack rolls only*. Rolling 20 or 1 on an ability check or a saving
throw has no special mechanical effect; death saving throws are a separately
named special case. The engine's three-way split — `AttackRoll` yes,
`_death_save` yes, `D20Test` no — is therefore **exactly right**, and
`expectation.py` correctly mirrors it. The mistaken recall appears to come from
One D&D playtest material that did not survive into the final rules.

Two lessons this pins for the audit itself:

1. Without a primary source in the repo, rules-recall findings are the least
   reliable category — and they fail *toward* inventing defects in correct code.
   Finding 4 is the one surviving item in that category and should be checked
   against the actual SRD text before anyone changes the table.
2. The engine got a subtle 2024-vs-2014 distinction right that two independent
   reviewers initially got wrong. That is evidence for the kernel, not against it.

## Suggested order of work

1. **Finding 1** — one-line fix, largest numeric impact, corrupts the tool's
   headline output.
2. **Findings 2 and 3** — both real rules gaps in stateful play; 3 needs a
   duration/effect-registry design, so it is the only item here that is not a
   small change.
3. **Findings 5, 6, 8** — small, self-contained correctness fixes.
4. **Finding 4** — verify against the SRD text first, then correct the table;
   likely wants a new `ConditionEffect` flag for save advantage/disadvantage,
   which also unblocks Dodge.
5. **Finding 7** — decide whether it is a limitation or a defect, then either
   implement it or write it into `COVERAGE.md`'s "Not supported".

## Method

Five independent finders (closed-form agreement, rounding order, RNG
determinism, data fidelity, rules fidelity), then one adversarial refuter per
finding prompted to *disprove* it from the code and to default to refuted when
evidence was thin. 14 agents, ~1.08M tokens. Findings 1, 4, 5 were additionally
reproduced by hand in the parent session; findings 7 and 8 were found there.

**This was tuned for impact, not for exhaustiveness.** Finders were capped at
three findings each and told not to pad, and only medium-and-above findings went
to the refute pass. So "five dimensions clean" means no *high-impact* defect
survived on those axes — it is not a completeness claim, and a longer-tail sweep
would likely surface more small items. Three finders also duplicated the same
defect (Finding 1), so the effective breadth of distinct coverage is narrower
than the agent count suggests.
