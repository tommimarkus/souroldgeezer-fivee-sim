# Seating, pauses, and resume

The roster is the whole of the coordinator's configuration. Everything else —
whether the run is unattended, where it stops, what a resume has to rebuild —
follows from it.

## roster.json

```json
{
  "id": "play-1",
  "mode": "play",
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

## Player tool inventory

Every agent player's first response must list every tool and scope it has or say
`none`; record that response under `tool_check`. For Claude Code, the expected
profile exposes only `Read (player-visible/** only)`; that exact inventory is a
confined profile. Codex does not apply that profile, so a Codex seat with
reported tools is in **honour-system mode**. Treat a Claude Code seat with any
other tool or broader Read scope the same way.

Append each honour-system classification, seat, and reported tools to
`transcript.md`. In playtest mode, also append it to `findings.jsonl` and call
the run honour-system mode in `report.md`. Continue without asking for approval
solely because tools are present. The player still must not use its tools or
seek the adventure, but that instruction is cooperation rather than structural
isolation. An unattended run continues under the same rule.

Re-ask every agent player after a re-spawn or resume, update `tool_check`, and
record any new honour-system inventory before sending new player-facing
material. A new child may have different tools from the one whose answer is on
disk. A `none` answer or the exact expected Claude Code inventory above belongs
only in `tool_check`; every other inventory also belongs in the transcript and,
in playtest mode, `findings.jsonl` and the report.

`kind` is `agent` or `human`, and it is the only thing that changes how a seat is
asked for a decision. `game_master.kind` may be `human`, in which case there is
no game-master agent and the coordinator puts the situation to the person
instead.

`mode` is `play` unless the request explicitly activated `playtest`. **Nobody
human anywhere** means the run is unattended: play it to the end and hand back
the completed replay, plus a report only in playtest mode. **Anyone human** means
it pauses, and the files on disk are what let it start again.

The `sheet` is a combatant spec the engine will accept — name, team, ac, max_hp,
position, attacks, and whatever else the character has. The bundled parties in
`../assets/pregens.json` are already in that shape.

## Unattended operation failures

A failed operation at a table with no human seats is not a user decision. Never
ask for approval or confirmation to continue. Handle it in this order:

1. Read the refusal or error. If the coordinator or game master formed a bad
   request, correct the call and retry. Return an engine-refused player
   declaration and its exact reason to the acting agent instead of changing the
   declaration for them.
2. For a transient failure, re-read authoritative state and make one safe retry.
   Re-spawn a failed role agent from the same bounded resume material. Do not
   repeat an identical state-changing call whose outcome is unknown.
3. If the operation is unsupported or remains unavailable, give the exact
   failure to the game-master seat. The game master makes the smallest workable
   improvised ruling and continues. Prefer supported `fivee` operations,
   including any documented encounter-correction operation exposed by
   `fivee help`, so the engine remains authoritative. If none can represent the
   ruling, keep the manual consequence explicit in the transcript as the
   table's temporary state ledger until the next safe reconciliation point.
   Continue the beat loop. Do not stop merely because no supported operation can
   represent the ruling.

Append an `engine degradation` beat to `transcript.md` with the attempted
operation, exact failure, retry or recovery attempt, improvised ruling,
mechanical state consequence, reconciliation status, and replay impact. When the
engine is available, also write the ruling with `encounter.note --category
ruling`. In playtest mode, append the same evidence as an adjudication entry in
`findings.jsonl` and carry it into `report.md`.

Never fabricate engine JSON, an engine event, or replay coverage. If manual
state was necessary, the final handoff says which interval is off-engine and
that the replay is partial. Engine unavailability alone does not make the run a
user decision: the game master improvises while the table and its audit record
remain usable. Prefer an adjudication without a roll while the engine cannot
roll; record a disclosed outcome rather than inventing a die face.

Only when continuation is genuinely impossible — for example, no game-master
seat can be restored, the adventure itself cannot be read, or no audit record
can be preserved — may the coordinator stop as blocked. Preserve the pause
artifacts and report the exact blocker without asking the user for confirmation.

## Asking a seat

### An agent seat

Message the child spawned for that seat through the host's subagent operation,
keeping it alive across the whole run so it remembers the session. Dispatch
independent agent seats together; do not walk them one at a time.

A resumed run has no live children. Re-spawn each one through the host-specific
dispatch in the main skill and brief it from `seats/<name>.md`, its sheet,
temperament, and voice **and nothing else**. Re-run the player tool inventory
before sending the next scene or brief.

### A human seat

Use the host's user-input operation after printing the narration as ordinary
output so the person can read the scene first. Ask up to four humans in one
pause when the host supports it; beyond that, pause again.

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
3. In playtest mode, `findings.jsonl` has everything noticed so far.
4. `roster.json` records the mode, adventure id, and encounter id in play.

The engine's own state needs no help — the fight is in its journal and the run is
in its adventure document. What you are saving is the *table*: who knows what.

## Resume

1. Read `roster.json`.
2. Re-spawn the game master through the host-specific dispatch; hand it the
   adventure path, mode, and current run position — it re-reads the module,
   which is cheap and exact. In playtest mode, also restore its run-sheet
   position.
3. Re-spawn each agent seat through the host-specific dispatch; hand it **only**
   `seats/<name>.md`, its sheet, its temperament and its voice. Re-run the tool
   inventory and record its classification before sending player-facing
   material.
4. Read the fight back from `fivee encounter.state` or `fivee adventure.state`.
   Never reconstruct mechanical state from the transcript.
5. Say where play stands, and continue.

A resumed player who is briefed from the transcript instead of their own file has
just been told what the rest of the party did while they were elsewhere. That
silently destroys the asymmetry the whole run depends on.
