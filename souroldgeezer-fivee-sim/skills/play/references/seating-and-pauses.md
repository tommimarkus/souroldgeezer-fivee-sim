# Core seating

Load in every interval. Root has already published roster v2; the controller
receives redacted references, not the complete roster.

## roster.json

```json
{"schema_version":2,"mode":"play","seed":20260805,"adventure_id":"adv-1","party_engine":"inputs/party-engine.json","party_gm":"inputs/party-gm.json","game_master":{"kind":"agent","input":"inputs/party-gm.json"},"seats":[{"name":"Thora","kind":"agent","input":"inputs/seats/thora.json","memory":"seats/thora.md"}],"tool_check":{"Thora":"none"}}
```

Kinds are `agent` or `human` and change transport only. `mode` defaults to play.
Each input is an allowlisted projection: engine gets only selected sheets; GM
gets identity/class/species/background/gear/rules; a player gets only its own
identity/sheet/gear/rules/temperament/voice. V1 inline rosters remain readable
on resume and are never rewritten.

Council communication defaults to fictional; table-wide requires explicit
opt-in. Eligibility follows who can communicate, never transport.

## Player tool inventory

Every fresh agent player's first response lists exact tools/scopes or `none`.
For Claude Code, `Read (player-visible/** only)` is the intended confined profile.
Codex seats with reported tools are in **honour-system mode**; broader Claude
access is classified the same way. Record under `tool_check`.

Append honour-system seat/tools to `transcript.md` and, in playtest, to
`findings.jsonl`. Continue without asking for approval; the player must not use
tools or seek the adventure. Re-ask after every re-spawn or resume and record it
before new player-facing material.

## Agent seats

Keep one child per player only for the interval. Persist witnessed memory, end
it, and rehydrate a fresh child from its input reference and `seats/<name>.md`.
Dispatch same-pass participants together. Never prompt with another seat's
memory, full transcript, module identity, or run sheet.

## council.json

```json
{"status":"open","encounter_id":"enc-1","decision_owners":["Thora"],"participants":["Thora"],"pass":1,"extension_passes_since_checkpoint":0,"current_plan":"bounded table-only plan","open_questions":[],"ready":[]}
```

Participants define visibility. `current_plan` is at most 200 words and uses
only shared facts; `open_questions` are exact pending player-facing questions.
Persist any next pass before a pause and close after commitment.
