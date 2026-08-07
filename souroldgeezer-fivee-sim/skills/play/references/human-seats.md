# Human seats

Load this reference only when at least one seat is human.

## Handing over the live view

After creating the adventure and linking its first encounter, the interval
controller directly runs
`fivee --run <adv-id> serve`. The command
starts the local server or reuses the running one. Read the returned `viewer_url`;
do not reuse a URL remembered from an earlier launch.

For each human player seat, URL-encode the adventure id and URL-encode that
seat's combatant name. Insert the query immediately before the `#` launch-token
fragment in the returned URL:

```text
<viewer origin and path>?adventure=<URL-encoded adventure id>&as=<URL-encoded seat>#<launch token>
```

Construct one URL per human seat and hand it only to that named seat. This lets
the page follow the ongoing adventure and render the engine's player-safe
projection for that chair. It is for a cooperating table, not per-seat
authentication: any client holding the launch token can ask the local engine for
a different view.

The launch token authorizes the whole local API, including reads, writes, and
server shutdown; `as` selects a projection but grants no security boundary.
Give the URL only to a human who shares the engine operator's trust. Never hand
it to an untrusted or separate-trust participant.

The server listens on loopback, so the URL works in a browser on the same machine
as `fivee`; it is not a phone or other table-device link unless the engine gains a
separately designed network and authentication boundary.

Treat the whole constructed URL as ephemeral secret-bearing handoff. Never put
the launch token or live URL in the roster, never put it in a checkpoint, never
put it in the transcript, never put it in seat memory, and never put it in a
report. Durable artifacts already carry the adventure id and seat; if an intent
is useful, record only that a live view must be reconstructed, without a token.

## Asking a human seat

The interval controller cannot ask the user directly. After user-visible
narration, it sends the root one bounded human-seat prompt. The root uses the
host's user-input operation, asks up to four humans in one pause when supported,
and relays the answer to the same live controller. The controller retains every
live child and the exclusive table-artifact write lease while waiting. Beyond
four humans, pause again. Offer two or three plausible actions and let free text
carry anything else—the options are a convenience, never a menu.

During council the user-input prompt asks for the same bounded fields as an agent: a `TABLE` proposal or
response, optional `SAY`, at most one exact `GM QUESTION`, and `READY: yes|no`.
After council, ask only a decision owner for its separate `COMMIT`.

Ordinary council has one proposal pass and one response/revision pass. A human
may request an extra pass **one pass at a time**. After **two extra extension
passes**, the live controller checkpoints the council before any further extension: update
`council.json` with `current_plan`, `open_questions`, `ready`, pass number, and
decision owners, then compact active council context to those fields. Never use
the raw discussion to continue. A human may then request another single pass;
checkpoint again after every two further extensions. This preserves discussion
without creating an unbounded context interval.

## Dice

Do not ask a human to roll or report a die face. Once the seat has made its
choice, resolve it through the engine exactly as for an agent seat; the engine
rolls every die and the human receives its result. A roll never creates a second
user-input pause.
