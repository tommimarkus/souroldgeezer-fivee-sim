# Unattended operation failures

Load this reference only after an operation fails at a table with no human seats.
A failure is not a user decision; never ask for approval or confirmation.

1. Read the exact refusal. If the coordinator, game master, or mechanical context
   formed a bad request, correct the call and retry. Return an engine-refused
   declaration and exact reason to its owner instead of changing it.
2. For a transient failure, re-read authoritative state and make one safe retry.
   Re-spawn a failed role from bounded checkpoint material. Do not repeat an
   identical state-changing call whose outcome is unknown.
3. If unsupported or still unavailable, give the exact failure to the
   game-master seat. It makes the smallest workable improvised ruling. Prefer a
   supported encounter-correction operation. Otherwise keep the manual
   consequence explicit in the transcript's temporary state ledger until the
   next safe reconciliation. Continue the beat loop; do not stop merely because
   no supported operation can represent the ruling.

Append an `engine degradation` beat to `transcript.md` with the attempted
operation, exact failure, retry or recovery, ruling, mechanical state
consequence, reconciliation status, and replay impact. When available, record
the ruling with `encounter.note --category ruling`. In playtest mode, append the
same evidence to `findings.jsonl` and carry it into `report.md`.

Never fabricate engine JSON, an event, or replay coverage. If state went
off-engine, the handoff identifies the interval and says the replay is partial.
Prefer an adjudication without a roll while the engine cannot roll.

Only when continuation is genuinely impossible—no game-master seat can be
restored, the adventure cannot be read, or no audit record can be preserved—may
the coordinator stop as blocked. Preserve artifacts and report the exact blocker
without asking for confirmation.
