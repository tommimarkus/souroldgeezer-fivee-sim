# Seating, pauses, and resume

The roster is the whole of the harness's configuration. Everything else — whether
the run is unattended, where it stops, what a resume has to rebuild — follows
from it.

## roster.json

```json
{
  "id": "playtest-1",
  "adventure": "/abs/path/to/module.md",
  "seed": 20260805,
  "adventure_id": "adv-1",
  "game_master": {"kind": "agent"},
  "seats": [
    {"name": "Thora",  "kind": "agent", "temperament": "bold",
     "voice": "blunt, soldierly, jokes when frightened", "sheet": {...}},
    {"name": "Kesh",   "kind": "agent", "temperament": "cautious",
     "voice": "quiet, asks one question too many", "sheet": {...}},
    {"name": "Ilma",   "kind": "human", "sheet": {...}}
  ],
  "tool_check": {"Thora": "none", "Kesh": "none"}
}
```

`tool_check` records what each agent seat answered when asked, on its first
message, to list any tools it has. It is the only evidence that the declared
`tools: []` was honoured — an empty list is the honest way to say *no tools*, but
a host reading it as an absent field would grant all of them, and nothing else
would ever say so. Anything other than `"none"` goes into the report as a
downgraded guarantee rather than stopping the run.

Re-ask on resume. A re-spawned agent is a new process and inherits nothing from
the answer the last one gave.

`kind` is `agent` or `human`, and it is the only thing that changes how a seat is
asked for a decision. `game_master.kind` may be `human`, in which case there is
no game-master agent and the harness puts the situation to the person instead.

**Nobody human anywhere** means the run is unattended: play it to the end and
hand back the report. **Anyone human** means it pauses, and the files on disk are
what let it start again.

The `sheet` is a combatant spec the engine will accept — name, team, ac, max_hp,
position, attacks, and whatever else the character has. The bundled parties in
`../assets/pregens.json` are already in that shape.

## Asking a seat

### An agent seat

`SendMessage` to the agent spawned for that seat, keeping it alive across the
whole run so it remembers the session. Batch every agent seat that must act into
one round of parallel messages; do not walk them one at a time.

A resumed run has no live agents. Re-spawn each one and brief it from
`seats/<name>.md` **and nothing else**.

### A human seat

`AskUserQuestion`, after printing the narration as ordinary output so the person
can read the scene first. Up to four humans can be asked in one call — beyond
that, pause again.

Offer two or three plausible actions as options and let the free-text answer
carry anything else. The options are a convenience, not the menu: a real player
says something the list did not have.

```
── The Chantry stair · Ilma ──
The steps go down into water. Something has scratched a
symbol into the wall at shoulder height, recently.

> What do you do?
  [Look closer at the symbol]
  [Test the water's depth]
  [Other: ...]
```

## Asking for a roll

A human turn that needs a d20 costs a **second** pause, because advantage is not
known until the declaration is made. Say how many dice and why:

```
Ilma — the sentry has not seen you. Roll with advantage:
two d20s, and give me both.

> What did they read?   [Other: e.g. 17, 4]
```

Pass them on as the face or faces, and let the engine do the rest:

```bash
fivee encounter.act enc-1 --kind attack --target Sentry --attack Shortbow --natural '[17, 4]'
fivee encounter.advance enc-1 --natural 12    # a dying character's own death save
fivee dice.check --modifier 2 --dc 14 --ability wisdom --natural 9
```

The engine refuses, with a reason worth relaying verbatim:

- the wrong count of faces for the roll's advantage
- a face outside 1–20
- a face for an action that rolls no d20

A seat that answers *"you roll it"* gets the engine's roll — offer that once and
do not keep asking.

## What a pause must leave behind

Every pause can be the last thing that happens for a week. Before you stop:

1. `transcript.md` is current through the last resolved beat.
2. Each `seats/<name>.md` holds what that seat has witnessed, in their voice.
3. `findings.jsonl` has everything noticed so far.
4. `roster.json` records the adventure id and the encounter id in play.

The engine's own state needs no help — the fight is in its journal and the run is
in its adventure document. What you are saving is the *table*: who knows what.

## Resume

1. Read `roster.json`.
2. Re-spawn the game master; hand it the adventure path and its run sheet
   position — it re-reads the module, which is cheap and exact.
3. Re-spawn each agent seat; hand it **only** `seats/<name>.md`, its sheet, its
   temperament and its voice.
4. Read the fight back from `fivee encounter.state` or `fivee adventure.state`.
   Never reconstruct mechanical state from the transcript.
5. Say where play stands, and continue.

A resumed player who is briefed from the transcript instead of their own file has
just been told what the rest of the party did while they were elsewhere. That
silently destroys the asymmetry the whole run depends on.
