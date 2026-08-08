# Codex dispatch

Load only on Codex. Prepared Markdown and native JSON use
`scripts/fivee-play.py`. A requested setup fallback spawns `adventure-prep`
with `fork_turns="none"`, reads `../../agents/adventure-prep.md`, and receives
only source, digest, mode, and staging.

Root spawns one `play-controller` with `fork_turns="none"`; it reads
`../../agents/play-controller.md`. Send only mode, artifact pointers/digests,
IDs, kinds, and write lease: the minimal bootstrap. Never copy a role body,
the adventure's path, module text, run sheet, or full transcript into a prompt. Each child reads its own
canonical role.

The controller concurrently spawns `game-master` with `fork_turns="none"`;
it reads `../../agents/game-master.md`, inherits model/effort, and gets the
party rules brief from `inputs/party-gm.json`. Spawn each `typical-player` with
`fork_turns="none"`, `model="gpt-5.6-terra"`, and
`reasoning_effort="medium"`; it reads `../../agents/typical-player.md`. Each
player prompt contains only identity, character sheet, gear, rules brief,
temperament, voice, seat memory, and current chair payload. It returns tool
inventory before any scene or brief.

The controller drives direct `fivee.py`: control results use `--select`; chair
payloads use `--as`. Give it a run ID, canonical operation name, resource identifiers,
and argument values; it discovers syntax. Never construct the command.
`play-mechanics` is a fallback only when the launcher is unavailable; spawn it
with `fork_turns="none"`. It reads `../../agents/play-mechanics.md`, receives no
module, transcript, or private memory, and ends after one beat.
