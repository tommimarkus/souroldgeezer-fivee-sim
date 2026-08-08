# Codex dispatch

Load only on Codex. Prepared Markdown and native JSON use
`scripts/fivee-play.py` with no `adventure-prep` model. If setup requests
fallback, spawn one child with `fork_turns="none"`; it reads
`../../agents/adventure-prep.md`, receives only source/digest/mode/staging, and
no player or seat private memory.

The root spawns one `play-controller` with `fork_turns="none"`. Its minimal bootstrap
contains only mode, artifact pointers/digests, current IDs, kinds,
and returned write lease—never content/maps/scenes/full pregens, the adventure's path,
module text, run sheet, or full transcript. The child reads its own
canonical `../../agents/play-controller.md`; never inject or copy a role body.

The controller concurrently spawns one `game-master` and every `typical-player`
with `fork_turns="none"`. The game-master reads
`../../agents/game-master.md`, inherits the controller model/effort, and receives
the party rules brief through `inputs/party-gm.json` plus private index pointers. Each
typical-player reads `../../agents/typical-player.md` with
`model="gpt-5.6-terra"`, `reasoning_effort="medium"`, and receives only its own
identity, character sheet, gear, rules brief, temperament, voice, and seat-memory
reference. Its first return is tool inventory before any scene or brief.
The player prompt contains only that character sheet, gear, rules brief,
temperament, voice, current chair payload, and witnessed memory.

The controller drives the direct `fivee.py` launcher. Every control
result uses `--select`; every chair payload uses `--as`. Give it a run id,
canonical operation name, resource identifiers, and argument values, never a
constructed command; it discovers current CLI syntax. Thus the ordinary path
spawns no `play-mechanics` model. Only when the direct launcher capability is
unavailable may it spawn the conditional fallback reading
`../../agents/play-mechanics.md` with `fork_turns="none"`; that child receives no
module, transcript, or player/seat private memory and ends after one beat.

The controller alone writes table artifacts, returns the bounded frame, and
ends. Root starts a fresh controller when play continues.
