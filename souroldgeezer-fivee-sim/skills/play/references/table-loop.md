# Table loop

The interval controller owns this protocol. Root receives only user narration,
a human prompt, a blocker, or the final bounded checkpoint.

## Mechanical context and briefs

The controller owns a resettable mechanical context and invokes packaged
`fivee.py` directly for one decision beat. Use `--select` for every bounded
control result; unselected raw engine output never enters a table artifact, the
game master, or root. Supply the chair identity through `--as` for every chair
payload: it is the engine-owned redaction boundary. Never derive or project a
chair view from `encounter.state`.

The request arrives as a run id, canonical operation name, resource
identifiers, and argument values. Never accept or relay a constructed shell
command. Use the one permitted help lookup to discover current syntax, then
apply only the supplied values plus required `--select` or `--as` projection
fields. Copy each closed-set value exactly as help spells it, including case;
do not normalize one from prose or an example. This exact-value rule also
governs the semantic fields sent to the conditional fallback. If any semantic
argument changes after a refusal, use a fresh idempotency key for the corrected
call; the old key belongs to the refused argument set.

Read one authoritative snapshot and use exactly one mechanical-context
invocation for the beat, not one invocation per chair. The opening brief and a
movement update appear immediately from engine state. Establish one full
baseline when a seat joins by requesting `encounter.brief` with the run,
encounter, and chair identifiers. After change, deliver one per-seat delta by
requesting `encounter.resume` with those identifiers and a `delta` view value
before that seat chooses.

If returned `view` is `full`, accept it as a new baseline. From one snapshot,
relay only one named chair payload at a time. Never repeat a full brief or full
baseline on a council pass/response when state is unchanged.

`brief-cursors.json` records encounter, generation, delivered `state_sha256`,
and delivery status only after successful relay. Unknown acknowledgement or a
re-spawn forces `encounter.brief` and one new chair-safe baseline; do not request
a `delta` view until the re-baseline is acknowledged. This recovery exception
does not permit repeats in ordinary council.

Use at most one help call and one corrected call; never identical retry, and a
second failure blocks. The
packaged `play-mechanics` role is a conditional one-beat fallback only when the
controller's direct launcher capability is unavailable. It receives the same
compact request, no module/transcript/player memory, returns bounded OUTCOME,
EVIDENCE, STATE DELTA, RECOVERY, and NEXT, then terminates. Never load the
encounter-sim in the live beat.

## Party council

The GM names eligible present seats whose characters can communicate. Each gets
only its own player brief; never another seat's brief. `table-wide` requires
roster opt-in.

- `TABLE`: out-of-character strategy. It is transcript-only, never changes world
  state, and is not displayed live.
- `SAY`: audible in-world. Journal it immediately as a bounded `encounter.note`
  entry attributed to its speaker; the GM decides who hears and what follows.
- `COMMIT`: separate final declaration by the acting seat for its own action.

Run one proposal pass and one response pass or one revision pass, ending early when all
are ready. Dispatch agent participants together. A material/plan-breaking event
reopens council. The plan is advisory; only the acting seat's COMMIT resolves.

Player return, at most 120 words: `TABLE` (60), optional `SAY` (30), at most one
optional `GM QUESTION` (30), and `READY: yes|no`. COMMIT is separate, 80 words.
The coordinator relays fields, then appends chronology to `transcript.md` and
seat-witnessed memory; after relay it drops raw returns. Table-only/OOC discussion
stays transcript-only and is never displayed live. `council.json` retains
only participants, pass, `current_plan`, `open_questions`, and readiness. Raw or
full discussion is never sent to the game master; it receives audible SAY,
COMMIT, and a bounded table-only plan summary. Never send raw council or COMMIT
to root.

Human transport is conditionally loaded from `human-seats.md`; the same live
controller receives the answer.

## Rules questions from a player

Hold only that seat's choice. The coordinator relays the exact question to the
game master. The game master owns the query and adjudication; the mechanical context
executes `fivee catalog.search`, then one `fivee catalog.get` or
`fivee catalog.table`. Relay the GM's bounded player-facing answer before the
player chooses. Never reveal adventure material, hidden state, monster
statistics, unrevealed identity, search neighbors, or machine paths.

## Reactions to dice

Every human and agent seat receives the engine's natural face; the
engine owns every die. Invite one brief in-character reaction without outcome
narration. Give a natural 1/20, dropped face, or death save its own beat; fold an
ordinary face into the next prompt.
