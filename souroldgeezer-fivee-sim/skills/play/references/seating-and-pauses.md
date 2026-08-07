# Core seating

Load this core for every run. Human transport, failure handling, and pause/resume
are separate conditional references linked directly from `SKILL.md`.

## roster.json

```json
{
  "id": "play-1",
  "mode": "play",
  "adventure": "/abs/path/to/module.md",
  "seed": 20260805,
  "adventure_id": "adv-1",
  "game_master": {"kind": "agent"},
  "council": {"communication": "fictional"},
  "seats": [
    {"name": "Thora", "kind": "agent", "temperament": "bold",
     "voice": "blunt, soldierly, jokes when frightened", "sheet": {...}},
    {"name": "Kesh", "kind": "agent", "temperament": "cautious",
     "voice": "quiet, asks one question too many", "sheet": {...}}
  ],
  "tool_check": {"Thora": "none", "Kesh": "none"}
}
```

`kind` is `agent` or `human`; it changes only the transport used to ask the
seat, never council eligibility, information, or ownership of a turn.
`game_master.kind` follows the same rule. `mode` is `play` unless the request
explicitly activated `playtest`.

`council.communication` defaults to `fictional`. Set it to `table-wide` only
when the table explicitly opts into out-of-character discussion across fictional
separation. A `sheet` is a combatant spec accepted by the engine; bundled parties
in `../assets/pregens.json` already have that shape.

## Player tool inventory

Every agent player's first response must list every tool and scope or say `none`;
record it under `tool_check`. For Claude Code, exactly `Read (player-visible/** only)`
is the intended confined profile. Codex does not apply
that profile, so a Codex seat with reported tools is in **honour-system mode**.
Treat a broader Claude inventory the same way.

Append each honour-system classification, seat, and reported tools to
`transcript.md`; in playtest mode also append it to `findings.jsonl` and report
it. Continue without asking for approval solely because tools are present. The
player still must not use them or seek the adventure.

Re-ask after every re-spawn or resume and record the new inventory before new
player-facing material. A new child may have different tools. `none` or the exact
confined Claude profile belongs only in `tool_check`; every other inventory also
belongs in the chronology and, in playtest mode, the findings and report.

## Agent seats

Keep one child per agent player alive so private experience persists. Dispatch
participants in the same council pass together; no responder sees another answer
from that pass. Spread temperaments such as cautious, bold, thorough, and social.

The coordinator stores what each seat witnesses in `seats/<name>.md`, but never
feeds that file back to a live child. It is rehydration material only. The full
transcript, another seat's memory, module identity, and run sheet never enter a
player prompt.

## council.json

Record bounded current state rather than raw discussion:

```json
{
  "status": "open",
  "encounter_id": "enc-1",
  "decision_owners": ["Thora"],
  "participants": ["Thora", "Kesh"],
  "pass": 1,
  "extension_passes_since_checkpoint": 0,
  "current_plan": "At most 200 words of shared, table-only strategy.",
  "open_questions": [],
  "ready": ["Kesh"]
}
```

`participants` is the visibility boundary. Build `current_plan` only from
narration and facts those participants shared. Keep it at or below 200 words.
`open_questions` holds exact player-facing questions still awaiting answers.
Close the council after commitment or persist the next pass before any pause.
