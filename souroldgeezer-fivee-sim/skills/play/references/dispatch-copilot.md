# Copilot dispatch

Load only on GitHub Copilot CLI. Prepared Markdown and native JSON use
`scripts/fivee-play.py` with no `adventure-prep` model. On a helper fallback only,
spawn the named agent `souroldgeezer-fivee-sim:adventure-prep` with source/digest/mode/private
staging and never player or seat private memory.

The root spawns the named agent `souroldgeezer-fivee-sim:play-controller` with a
minimal bootstrap of mode, artifact pointers/digests, current IDs, seat kinds,
and write lease using the `task` tool with `mode="background"`. Never embed
content/maps/scenes/full pregens, the adventure path, module text, run sheet,
or full transcript.

The play-controller owns and concurrently spawns the named
`souroldgeezer-fivee-sim:game-master` and all `souroldgeezer-fivee-sim:typical-player`
agents using the `task` tool or `write_agent` for concurrent dispatch. The
game-master receives the party rules brief and private current-index pointers;
each player receives only identity, sheet, gear, rules brief, temperament,
voice, and its memory reference. Players report tool inventory before any scene
or brief. The GM lazy-reads its opening section while those handshakes run.

The controller uses the direct `fivee.py` launcher: require `--select`
for control results and `--as` for chair payloads. Give mechanics a run id,
canonical operation name, resource identifiers, and argument values, never a
constructed command; the controller discovers current CLI syntax. No
`souroldgeezer-fivee-sim:play-mechanics` model is spawned on this common path. If the
narrow launcher grant is unavailable, spawn named
`souroldgeezer-fivee-sim:play-mechanics` as a conditional one-beat fallback with no
module, transcript, or player-private memory using the `task` tool.

Agent tool depth or nested-agent policy can block controller delegation. Then
return `blocked` and the lease; never make root perform a role. The controller
alone writes artifacts and ends at the interval boundary.

## Tool Access Model

Copilot's task tool API uses categorized tool access rather than scoped
filters (Claude Code) or honour-system declarations (Codex). Each agent is
granted specific tool categories:

- `agent`: Launch, read status, and message other agents via `task`, `list_agents`,
  `read_agent`, `write_agent`
- `execute`: Run commands via bash/powershell (e.g., calling `fivee.py`)
- `read`: Read files and directories
- `edit`: Create and modify files
- `search`: Search files and repositories via grep/glob
- `web`: Fetch URLs
- `todo`: Manage session-local SQL todos

The play-controller agent holds `["agent", "execute", "read", "edit"]`, which
allows it to orchestrate child agents and the launcher directly. The
game-master and typical-player agents hold minimal categories to enforce
boundaries. Player agents hold `["read"]` only — no tools, no execution — to
enforce the boundary that players cannot act outside the engine's permission
model.

## Fallback and Limitations

If Copilot does not recognize the plugin namespace
(`souroldgeezer-fivee-sim:`) or the agent-launch API, fall back to describing
the dispatch pattern and requesting manual escalation. The controller can be
blocked by agent-tool depth limits; in that case return `blocked` without
substituting root execution.

If the narrow launcher grant is unavailable (no direct `fivee.py` command
authority), the conditional `souroldgeezer-fivee-sim:play-mechanics` agent
provides a one-beat fallback using the same categorized tools but without the
direct launcher authority. This fallback holds no module, transcript, or
player-private memory and owns only a single engine operation before returning
control.

## Bounded Play Summary

Every interval owns at most six resolved decision beats (turns), with
checkpointing at encounter finalization and chapter boundaries. The controller
returns a frame capped at **800 stable-proxy tokens**. If play continues, the
root spawns a fresh controller from archived artifact pointers and digests;
controllers are disposable, never reused across intervals.

The session state is carried between intervals via durable artifacts:
`checkpoint.json` for current position and GM state, `seats/` for seat-private
memory, `brief-cursors.json` for acknowledged chair ownership, and
`council.json` for bounded plan state. Fresh rehydration never receives the
full transcript, raw council, or worker reasoning.
