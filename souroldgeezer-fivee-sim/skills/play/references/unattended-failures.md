# Unattended operation failures

Load this reference only after an operation fails at a table with no human seats
while the engine remains available.

Engine availability is a hard gate. If `fivee` cannot start or reach the engine,
checkpoint and preserve the table artifacts, pause even when unattended, and
escalate the exact failure to the user. Do not continue or improvise without the
engine.

For an operation failure against an available engine, a refusal is not a user
decision; never ask for approval or confirmation.

1. Read the exact refusal. If the coordinator, game master, or mechanical context
   formed a bad request, correct the call and retry. Return an engine-refused
   declaration and exact reason to its owner instead of changing it.
2. For a transient failure, re-read authoritative state and make one safe retry.
   Re-spawn a failed role from bounded checkpoint material. Do not repeat an
   identical state-changing call whose outcome is unknown.
3. If the operation is unsupported or still refused while the engine remains
   available, give the exact failure to the game-master seat. It makes the
   smallest workable improvised ruling. Prefer a supported encounter-correction
   operation. Otherwise keep the manual consequence explicit in the
   transcript's temporary state ledger until the next safe reconciliation.
   Continue the beat loop; do not stop merely because no supported operation can
   represent the ruling.

Append an `engine degradation` beat to `transcript.md` with the attempted
operation, exact failure, retry or recovery, ruling, mechanical state
consequence, reconciliation status, and replay impact. When available, record
the ruling with `encounter.note --category ruling`. In playtest mode, append the
same evidence to `findings.jsonl` and carry it into `report.md`.

Never fabricate engine JSON, an event, or replay coverage. Mark a manual
consequence as outside replay coverage until it is reconciled. Prefer an
adjudication without a new die when the engine cannot execute that operation.

For a non-engine blocker, stop only when continuation is genuinely impossible—no
game-master seat can be restored, the adventure cannot be read, or no audit
record can be preserved. Preserve artifacts and report the exact blocker without
asking for confirmation.
