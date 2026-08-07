# Pause and resume

Load this reference only immediately before a human pause or when resuming a
saved run.

## What a pause leaves behind

Before a controller returns its write lease:

1. `transcript.md` is current through the last resolved beat.
2. Every `seats/<name>.md` contains only what that seat witnessed.
3. In playtest mode, `findings.jsonl` and `run-sheet.json` are current.
4. `roster.json` names mode, adventure, seats, and encounter in play.
5. `council.json` holds participants, pass, rolling plan, open questions, and
   readiness without raw discussion.
6. `brief-cursors.json` records acknowledged chair delivery ownership.
7. `checkpoint.json` holds the bounded interval and game-master checkpoint.
8. `module-index.json` remains complete; the checkpoint names only its pointer,
   digest, source hash, and current IDs.

The engine journal already owns the fight. These files save the table: who
knows what and what happens next. The controller flushes them, terminates every
descendant, and returns only pointers/digests and the bounded public interval
frame to the root.

## Resume

1. The root reads `roster.json` only long enough to recover mode, seat bootstrap,
   source pointer, run ID, and current engine/adventure IDs. It does not open the
   transcript, seat memory, module-index locators, run sheet, council returns, or
   game-master private checkpoint. It gives a fresh controller the redacted
   bootstrap, artifact pointers/digests, and bounded rehydration contract.
2. The fresh controller reads `checkpoint.json`, `seats/<name>.md`,
   `council.json` when open, and `brief-cursors.json`. It treats the
   `module-index.json` and playtest `run-sheet.json` as opaque current pointers
   with digests; it never opens their hidden contents or receives current
   locators.
3. Spawn a fresh game master with source path and hash, mode, bounded private
   checkpoint component, current run position, module-index pointer/digest, and
   current IDs. In playtest include the run-sheet pointer, digest, and current
   IDs, not the whole run sheet. The game master validates the index pointer and
   digest, resolves only the current IDs to their line or page locators, reads
   those sections, and recomputes the adventure source hash. On a source hash
   mismatch, refuse to mix saved state with changed source text. Return a blocker
   requiring an explicit restart-or-resume decision: restart preparation against
   the changed source, or restore the original source and resume. Do not narrate
   meanwhile.
4. Re-spawn each agent player fresh with only identity, sheet, gear, rules brief,
   temperament, voice, and `seats/<name>.md`; when participating in an open
   council, add only bounded `current_plan`, `open_questions`, pass, decision
   owners, and readiness. Never hand any role the full transcript. Re-run tool
   inventory before new player-facing material.
5. Restore an open council at its recorded pass with the same transports. Give
   the game master only its table-only plan and exact addressed questions.
6. Read authoritative mechanics from `fivee encounter.state` or
   `fivee adventure.state` through a fresh one-beat mechanics child; never
   reconstruct state from chronology.
7. If the roster has a human player seat, ask mechanics to run `fivee serve`.
   Read its fresh `viewer_url` and reconstruct each fresh seat URL using the human-seats
   procedure and send it to the root only as that seat's ephemeral human prompt.
   Do this after a server replacement too: launch tokens change, so a URL from
   the prior server is invalid. Never restore a live URL from a saved artifact.
8. Send the root one bounded user-visible statement of where play stands and
   continue inside the same interval.

`brief-cursors.json` records acknowledged recipient ownership, not merely what
the server last produced. Every fresh player is a new context generation: before
its next decision, force `encounter.brief <id> --as "<seat>"`, relay the fresh
chair-safe baseline, and update `state_sha256` only after successful delivery. A
missing or unknown acknowledgement takes the same recovery path. Do not use
`--view delta` until re-baseline succeeds; this recovery exception never permits
full re-fanout on an ordinary council pass or response.

In playtest mode, `run-sheet.json` is the private durable inventory. The game
master alone reads its pointer, digest, run position, and relevant current
entries. Never supply the whole run sheet to the root or controller.

A player rehydrated from the transcript instead of its private file learns what
the rest of the party did elsewhere and silently destroys the run's asymmetry.
