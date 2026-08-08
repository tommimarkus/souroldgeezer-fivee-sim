# Claude Code dispatch

Load only on Claude Code. Prepared Markdown and native JSON use `fivee-play.py`
with no `adventure-prep` model. On a helper fallback only, spawn the named agent
`adventure-prep` with source/digest/mode/private staging and never player or
seat private memory.

The root spawns the named agent `play-controller` with a minimal bootstrap of
mode, artifact pointers/digests, current IDs, seat kinds, and write lease. Never
embed content/maps/scenes/full pregens, the adventure path, module text, run
sheet, or full transcript.

The play-controller owns and concurrently spawns the named `game-master` and
all `typical-player` agents. The game-master receives the party rules brief and
private current-index pointers; each player receives only identity, sheet, gear,
rules brief, temperament, voice, and its memory reference. Players report tool
inventory before any scene or brief. The GM lazy-reads its opening section while
those handshakes run.

The controller uses the direct `fivee.py` launcher: require `--select`
for control results and `--as` for chair payloads. Give mechanics a run id,
canonical operation name, resource identifiers, and argument values, never a
constructed command; the controller discovers current CLI syntax. No
`play-mechanics` model is spawned on this common path. If the narrow launcher
grant is unavailable, spawn named `play-mechanics` as a conditional one-beat
fallback with no module, transcript, or player-private memory.

Agent tool depth or nested-agent policy can block controller delegation. Then
return `blocked` and the lease; never make root perform a role. The controller
alone writes artifacts and ends at the interval boundary.
