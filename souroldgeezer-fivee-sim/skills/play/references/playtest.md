# Playtest workflow

Load this reference only when the active mode is `playtest`. Ordinary play does
not collect findings, run evaluation batches, or write an author-facing report.

## Establish the test inventory

Use the disposable adventure-prep child's full semantic inventory before the
first scene to establish the private run sheet: scenes, encounters, NPCs,
treasure, stated DCs, assumed route, and material omissions. Keep it from
players. Measure unused content and pacing against it.

Before the first interval, the root persists it as the private durable
`.fivee-sim/plays/<id>/run-sheet.json`, with stable entry IDs, a
`module_index_id` that references the corresponding `module-index.json` entry,
and per-entry status/pacing fields. Never send it to a player. During a live
interval, only the controller updates the artifact. At every encounter or
chapter checkpoint, put only its pointer, digest, current entry ids, and run
position in `checkpoint.json`. On a fresh interval's game-master spawn, the game master alone
reads the pointer and relevant current entries; neither controller nor root
receives the whole run sheet. A fresh report context may read the durable
artifact after live intervals end.

The interval controller is also the live harness: observe rather than narrate,
append each finding when it occurs, and never steer choices toward an expected
or optimal route. It does not forward raw findings, hidden run-sheet state, or
worker reasoning to the root; the root receives artifact pointers/digests and
user-visible interval results only.

## Measure the fights

For each authored encounter, compare the played result with a seeded batch:

```bash
fivee analytics.rounds --iterations 200 --seed 20260805 --json '{"combatants": [ ... ]}'
```

Report `p10`, median, `p90`, casualty tails, and resource tails. State what the
batch cannot see: auto-play is greedy, never casts control, never operates a
fixture, does not husband slots, values no item but healing, and fights a fresh
party. It is a floor, not a verdict, and cannot measure accumulated attrition.

## Write findings as they happen

Add beside the ordinary artifacts:

| File | Purpose |
|---|---|
| `findings.jsonl` | blockers, rulings, unused content, pacing, and divergences appended when observed |
| `report.md` | author-facing deliverable |

Never reconstruct findings from the transcript at the end. The entry path is
`references/report-format.md`; from here read
[the report contract](report-format.md) for injection, blockers,
adjudication notes, unused content, difficulty, attrition, pacing, divergences,
legibility, and reproducibility.

Do not log a normal SRD-supported action merely because the module omitted it.
Use an adjudication note when continuing required a material module-specific
fact, procedure, DC, consequence, or route assumption, or an engine/catalog
limit materially affected play. Reserve divergence for a materially different
route or approach that challenges authored assumptions.

The master seed fixes every roll the engine made, not what people or language
models chose to try.

## State the test limits

Close `report.md` with severity-ordered changes followed by limits:

- Agent players probe ambiguity, dead ends, and pacing; they are not evidence
  about fun, tone, or whether a twist lands.
- Player briefs and fresh contexts minimise disclosure but are not access
  control. Report actual `tool_check` and confined-profile/honour-system status.
- One run is one path; offer multiple seeded runs when branching matters.
- State each engine limit that bore on a ruling; encounter-sim owns the list.

Link the finalized `fivee --run <adv-id> adventure.replay <adv-id>` so the author can inspect the run.
