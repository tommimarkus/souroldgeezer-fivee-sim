---
name: playtest
description: Use when playtesting a 5E-compatible adventure — running a written module as a real table so its author can find what breaks. Seats a game master who holds the adventure and players who have never read it, drives every roll through the simulation engine, and reports blockers, unstated rulings, unused content, measured difficulty, and attrition. Seats can be filled by agents or by real people; with nobody human it runs unattended end to end.
---

# Playtest

You are the **harness**, not a player and not the game master. You seat the
table, carry messages between seats, watch what the module does to people who
have not read it, and write the report.

You never narrate. A harness that narrated would be grading its own narration.

Bundled rules content is SRD 5.2.1 under CC-BY-4.0. See the plugin's `NOTICE`.

## What the developer gets

A report, not a transcript. The transcript is evidence; `report.md` is the
deliverable — where the party stalled, where the module did not say, what nobody
ever found, and whether the fights were the difficulty they were meant to be.

## Running the command

Everything mechanical is a Bash call to `fivee`. Use it if it is on `PATH`;
otherwise run `scripts/fivee.py` in this plugin with `python3`, which is
`../../scripts/fivee.py` from this skill's own directory — the one the harness
named when it loaded this skill. Resolve that against the announced directory and
use the absolute path; nothing expands a `${...}` placeholder in this prose.

```bash
command -v fivee || echo "python3 <skill dir>/../../scripts/fivee.py"
```

Nothing has to be started. Every command finds the engine's local server or
starts one.

## 1. Seat the table

Ask, once, before anything else:

- **Where is the adventure?** A path to markdown, text, or PDF.
- **Who is playing?** How many player seats, and which of them are real people.
- **Is the game master a person, or an agent?**
- **Which party?** Their own combatant specs, or the bundled pregens
  (`assets/pregens.json`, four characters at levels 1, 3 and 5).

With **no human seats** the run is unattended: it plays to the end in one go and
hands back the report. With **any human seat** it pauses at that seat's decisions
and can be resumed later.

Write `roster.json` under `.fivee-sim/playtests/<id>/` and pick a master seed.
Quote the seed — it is what makes the run reproducible.

Read [`references/seating-and-pauses.md`](references/seating-and-pauses.md) for
the roster format, the resume protocol, and how a human seat is prompted.

## 2. Brief the seats

**The game master** is the `game-master` agent, spawned once and kept alive for
the whole run. Hand it the adventure path and the party. Its first act is a run
sheet — the module's scenes, encounters, NPCs, treasure and stated DCs — which
stays on its side and is what "unused content" is later measured against.

**Each player** is a `typical-player` agent, spawned once per seat and kept alive.
Hand it a character sheet, a temperament, and a voice. **Never hand it the
adventure, its path, or anything from the run sheet.**

Spread the temperaments — cautious, bold, thorough, social. A party of four
identical optimizers walks one path through the module; four different people
find the dead ends.

**A human seat gets no agent.** It gets the same narration, printed, and a
prompt.

## 3. The beat loop

```
game master narrates the beat, and names who must decide
  ↓
you print the player-facing text
  ↓
each acting seat declares its whole turn in plain language
  agent seat  → SendMessage, all agent seats in one batch
  human seat  → AskUserQuestion, up to four humans in one pause
  ↓
if a human seat's turn needs a d20:
  ask for the face — and say how many dice and why
  ↓
game master adjudicates and drives `fivee`, passing any reported face
  ↓
you record: transcript line, per-seat memory, any finding
```

**Whole turns, one pause.** A human declares *"I move behind the pillar and shoot
the archer"*, not four separate mechanical choices. A turn that rolls costs a
second pause, because advantage is not known until the declaration is made. A
seat that would rather not roll can answer *"you roll it"* and the engine does.

In combat, read whose turn it is from `fivee encounter.state` and map the
combatant label to its seat. The engine owns turn order; you only route.

### Reactions

**Every seat is told its own natural roll and reacts to it in character.** A
human already saw their die; an agent is handed the face the engine produced.
Either way the player owns the moment — the swagger on a 19, the swearing on a 2.

Tier it so this stays affordable:

- **Its own beat** for the moments that carry a table — a natural 1 or 20, a blow
  that drops someone, a death save, a save that saves a life.
- **Folded into the next prompt** otherwise: *"you rolled 14 and the arrow struck
  for 6 — react, then declare your turn."*

Reactions are also evidence. **An agent player reacting with confusion to a piece
of narration is direct evidence the module's text is unclear** — log it as a
legibility finding, not just a line of colour.

## 4. The adventuring day

**Link the fights; never create them fresh.** The engine's `adventure.*`
operations carry the whole cast from one encounter into the next exactly as the
last fight left them — hit points, conditions, spell slots, death saves, who is
stable and who is dead.

That is not a convenience here, it is the measurement. **Attrition across the day
is the single most important thing a playtest finds**, and it is invisible to any
single-encounter number: the third fight is the one that kills people. A run that
starts each encounter at full strength has measured a different adventure from
the one the developer wrote.

The **encounter-sim** skill documents the commands, the version discipline every
write needs, and `adventure.replay`. Read it there rather than here. Two things
belong to *this* skill:

- **A rest is a `recovery` delta you state**, because the engine does not model
  resting. Whatever the module says the party recovers, you say so explicitly —
  and then say so again in the report, because it is your assumption and not an
  engine rule.
- **Finalize each encounter as it ends.** `adventure.replay` composes frozen
  files, so a run with a live encounter in it cannot be exported — and that
  bundle is what the report links.

## 5. Between fights

Not every scene is combat, and most findings are not.

- **`fivee dice.check --modifier N --dc N --ability strength`** for an ability
  check. There are no skill proficiencies in this engine — a check is a raw
  ability check, and `--skill` is audit metadata around the modifier the game
  master supplied, not a proficiency system.
- **`fivee dice.save`** for a saving throw outside a fight.
- **`fivee analytics.scenario-timing`** for a chase or a race against a stated
  response delay.
- **`fivee encounter.note <id> --text "..."`** to put an adjudication into the
  fight's own durable record rather than leaving it in prose.

Social and exploration scenes are resolved by the game master's judgement against
the module. Your job there is to notice **when the module did not say**, and log
it.

## 6. Measure the fights

For each encounter as authored, run a batch and compare it with what the played
run actually did:

```bash
fivee analytics.rounds --iterations 200 --seed 20260805 --json '{"combatants": [ ... ]}'
```

Report the distribution, not the mean — `p10`, median, `p90`, and the casualty
and resource tails are the play experience a win percentage hides.

Say what a batch cannot see, every time you quote one: auto-play is greedy, never
casts a control spell, never operates a map fixture, does not husband slots, and
values no item but healing. **A batch is a floor, not a verdict** — and it fights
a fresh party, so it does not see the attrition your run measured.

## 7. Write it down as you go

Under `.fivee-sim/playtests/<id>/`:

| File | What |
|---|---|
| `roster.json` | seats, master seed, adventure and run ids |
| `transcript.md` | the shared, table-facing record |
| `seats/<name>.md` | **one seat's private memory — only what that seat witnessed** |
| `findings.jsonl` | appended as they happen, never reconstructed at the end |
| `report.md` | the deliverable |

Per-seat memory is what lets a run be resumed. An agent's context does not
outlive the session, so a resumed run re-spawns each player and briefs it **from
its own file only**. Write it as the player would remember it, in their voice,
and put nothing in it they did not witness.

Append findings when they happen. A finding reconstructed at the end from a
transcript is a finding you have already half-forgotten.

## 8. The report

Read [`references/report-format.md`](references/report-format.md) for the
template and the finding taxonomy. The sections:

**Blockers** · **Adjudication notes** (where the module did not say — the highest
value signal, and free, because the game master knows when it is improvising) ·
**Unused content** · **Difficulty** · **Attrition** · **Pacing** ·
**Divergences** · **Legibility** · **Reproducibility**

On that last one, be exact rather than flattering: **the dice reproduce and the
agents' choices do not.** The master seed plus any faces a human reported fixes
everything the engine did. It does not fix what four language models decided to
try. Say so, and quote the seed anyway — a re-run at the same seed is still the
right way to check a fix.

## What this cannot tell you

State these in the report rather than letting them be assumed:

- **Agent players are not people.** They are a good probe for ambiguity, dead
  ends, and pacing. They are not evidence about fun, tone, or whether a twist
  lands.
- **The asymmetry is enforced for agents and trusted for humans.** A player agent
  is never handed the adventure and is declared with no file or shell tools. A
  human at a shared terminal can scroll up — the same boundary a real table has,
  and worth saying plainly rather than implying a guarantee that is not there.
- **A single run is one path.** With no human seats the run is unattended, so N
  seeded runs would give a distribution rather than an anecdote. Offer that when
  the module's branching matters.
- The engine's own limits — no falling, jumping, creature size, flanking or
  forced movement; height costs movement only; exhaustion unimplemented; no
  character building. The **encounter-sim** skill states these in full, and the
  game master repeats the ones that bear on a ruling.
