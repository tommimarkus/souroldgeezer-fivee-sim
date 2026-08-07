# Pause and resume

Load this reference only immediately before a human pause or when resuming a
saved run.

## What a pause leaves behind

Before stopping:

1. `transcript.md` is current through the last resolved beat.
2. Every `seats/<name>.md` contains only what that seat witnessed.
3. In playtest mode, `findings.jsonl` is current.
4. `roster.json` names the mode, adventure, and encounter in play.
5. `council.json` has participants, pass, rolling plan, open questions, and
   readiness without raw discussion.
6. `checkpoint.json` has the live coordinator/GM schema from the main skill.

The engine journal already owns the fight. These files save the table: who knows
what and what happens next.

## Resume

1. Read `roster.json`, `checkpoint.json`, and `council.json` when open.
2. Re-spawn the game master through current-host dispatch with the adventure
   path, mode, its bounded checkpoint component, and current run position. In
   playtest mode include the run-sheet position, not the whole run sheet.
3. Re-spawn each agent player with only its sheet, temperament, voice, and
   `seats/<name>.md`; when participating in an open council, add only the bounded
   `current_plan`, `open_questions`, pass, decision owners, and readiness. Never
   hand any role the full transcript. Re-run tool inventory before new material.
4. Restore an open council at its recorded pass with the same transports. Give
   the game master only its table-only plan and exact addressed questions.
5. Read authoritative mechanics from `fivee encounter.state` or
   `fivee adventure.state` through the resettable mechanical context; never
   reconstruct state from chronology.
6. If the roster has a human player seat, run `fivee serve` again. Reconstruct
   each seat's fresh URL from the returned `viewer_url` using the human-seats
   procedure and hand it only to that seat before continuing. Do this after a
   server replacement too: launch tokens change, so a URL from the prior server
   is invalid. Never restore a live URL from any saved artifact.
7. Say where play stands and continue.

`brief-cursors.json` records acknowledged recipient ownership, not merely what
the server last produced. Every re-spawned player is a new context generation:
before its next decision, force `encounter.brief <id> --as "<seat>"`, relay the
fresh chair-safe baseline, and update `state_sha256` only after successful
delivery. A missing or unknown acknowledgement takes the same recovery path. Do
not use `--view delta` until re-baseline succeeds; this recovery exception never
permits full re-fanout on an ordinary council pass or response.

In playtest mode, `run-sheet.json` is the private durable inventory. Re-spawn
the game master with its pointer, digest, run position, and only the relevant
current entries; do not inject the whole run sheet into live context.

A player rehydrated from the transcript instead of its private file learns what
the rest of the party did elsewhere and silently destroys the run's asymmetry.
