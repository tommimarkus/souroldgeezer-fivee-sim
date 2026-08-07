# Table loop

Load this reference immediately before the first scene. It is the ordinary table
protocol for both play and playtest.

## Mechanical context and briefs

The coordinator owns a **resettable mechanical context** separate from the
persistent narrative game master. Use the packaged `play-mechanics` role for one decision beat,
then end it immediately after its bounded return.
Give it encounter ids and exact adjudicated operation requests, never the module,
run sheet, transcript, or player memories. Reset it after each decision beat and
at every encounter or chapter checkpoint so raw engine traffic does not
accumulate in the coordinator or game-master context.

For a decision beat, the mechanical context reads one authoritative
`encounter.state` snapshot to identify the turn and detect whether state changed.
That raw snapshot remains inside the resettable context. Never derive or project
a player's payload from `encounter.state`; `--as` is the engine-owned redaction
boundary, including creatures hidden by total cover.

Establish **one full baseline** per seat when it first joins an encounter:

```bash
fivee encounter.brief <id> --as "<seat>"
```

Relay that exact chair-safe payload once. After any resolved state change, ask
for one **per-seat delta** for each participant before its next decision:

```bash
fivee encounter.resume <id> --as "<seat>" --view delta
```

Relay `state_delta` without paraphrasing. If the returned `view` is `full`, the
engine lost that chair's baseline; accept its chair-specific state as the new
full baseline and relay it once. Never retry for a delta, never substitute
another chair's baseline, and never send a full brief or full baseline again on
each council pass or response when state is unchanged.

The coordinator owns `.fivee-sim/plays/<id>/brief-cursors.json`. The
play-mechanics child never writes this or any other table artifact. For each seat,
record the encounter id, player-context generation, delivered `state_sha256`,
and delivery status **only after a successful relay**. This cursor records what
the recipient actually holds; never infer ownership from the engine server's
baseline. Mark delivery unknown after a dropped return or missing
acknowledgement.

On a player re-spawn, or whenever its delivery acknowledgement is unknown,
force one fresh `encounter.brief <id> --as "<seat>"` and relay that full
chair-safe baseline before asking for another delta. Do not use `--view delta`
until this re-baseline is acknowledged and its cursor is current. This recovery
exception is not permission to repeat full briefs on a council pass or response.

Use exactly one mechanical-context invocation for the decision beat, not one
invocation per chair. It may emit several sequential delivery frames from that
one snapshot, but each frame contains at most one named chair payload. After all
requested chair frames, it emits one bounded control frame and exits:

```text
STATUS: ok | refused | degraded | blocked
RESULT: at most 160 words; exact arithmetic or refusal and changed public facts
EVIDENCE: encounter id plus event/action indexes or durable artifact paths
BRIEF: absent, or one exact engine payload for one named seat (full | delta)
NEXT: one requested mechanical action or none
```

Do not combine chair payloads into one `BRIEF`, re-read state between chair
frames, or keep the context alive for the next beat. Terminate the child after
the return. If it reports a malformed or unsupported call, give it one bounded
correction naming that call; a second failure is `blocked`, not a reason to load
the full encounter skill into the beat.

Raw state, logs, and reasoning stay in that resettable context. The exact `BRIEF`
exception is immediately relayed only to its named seat and is never sent to the
game master, another seat, or a later prompt. Give the game master only `RESULT`,
`EVIDENCE`, and `NEXT`.

## Party council

After narration and before commitment, open a short council. The game master
names eligible participants: by default present seats whose characters can
communicate in the fiction. A separated seat gets no omniscient channel;
`table-wide` is an explicit roster opt-in. Every participant receives only its
own brief baseline or per-seat delta. Never give one player another seat's brief
or turn unshared private memory into a common fact.

Use three message kinds:

- **`TABLE`** is out-of-character strategy. It consumes no action or fictional
  time and never alerts an enemy or enters world state.
- **`SAY`** is audible in-world speech. Relay it to the game master, which
  decides who hears it, records it where applicable, and returns consequences.
- **`COMMIT`** is a separate final declaration. Only a named decision owner may
  commit its own action; advice never becomes another character's turn.

One proposal pass and one response pass (or revision) is ordinary, ending early when
everyone is ready. Dispatch agent participants in the same pass together. A
material or plan-breaking event reopens a fresh bounded council for the newly
eligible participants; never silently apply the old plan.

Each player return uses only this compact schema, at most **120 words total**:

```text
TABLE: required; at most 60 words
SAY: optional; at most 30 words, or OMIT
GM QUESTION: optional; at most one exact question of 30 words, or OMIT
READY: yes | no
```

The acting seat sends its separate `COMMIT` after council, at most 80 words. Do
not accept prose outside the fields or a transcript/history dump as a return.

The coordinator relays the fields, then appends the labeled exchange to
`transcript.md` and witnessed fields to seat-private memory as out-of-band
chronology. After recording, drop the raw return from the live council working
set; retain only `current_plan`, exact open questions, and readiness in
`council.json`. Never replay raw discussion to a player or game master.

Relay only an exact `GM QUESTION` and return only its player-facing answer. Give
the game master audible `SAY`, final `COMMIT`, and a table-only plan summary of at
most 200 words—never raw/full discussion. Table-only knowledge never becomes
monster, enemy, or NPC knowledge. The plan stays advisory; the acting seat alone
chooses and sends `COMMIT`.

Human transport and its checkpointed extension rule live in `human-seats.md` and
are loaded only when a human participates.

## Rules questions from a player

Hold only that seat's choice. The coordinator relays its exact question to the
game master. The game master owns the query and adjudication; the resettable
mechanical context (`play-mechanics`) executes `fivee catalog.search --query …`, then one
`fivee catalog.get <id>` or `fivee catalog.table <id>`. The player gets only the
requested player-facing answer before the seat chooses—never adventure material, hidden state, monster
statistics, unrevealed identities, search-result neighbors, or machine paths.
If the catalog does not establish the answer, say so and adjudicate only as far
as needed; do not fill the gap from model recollection.

## Reactions to dice

Tell each seat its own natural roll and invite one brief in-character reaction.
A human saw the die; an agent receives the engine's face. Give a natural 1 or 20,
a drop, or a death save its own beat; fold an ordinary result into the next
prompt. The player never narrates the outcome.
