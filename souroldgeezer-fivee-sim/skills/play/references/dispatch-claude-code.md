# Claude Code dispatch

Load this file only when Claude Code is the active host.

Spawn the named agent `adventure-prep` with only the adventure path, source
digest and format, active mode, and bounded frame contract; it receives no
player or seat private memory. End it after its complete manifest. Spawn the
named agent `game-master` with the source path and digest, module-index pointer
and digest, only current entries and locators, a party summary including each
seat's rules brief, active mode, and current bounded checkpoint when present.
Spawn the named agent `typical-player` once per agent seat with only its
identity, character sheet, gear, rules brief, temperament, voice, and permitted
rehydration state. Claude Code discovers the packaged named agents and applies
their frontmatter, including tools, model, and effort; do not reproduce or
override it.

For each decision beat, spawn the named `play-mechanics` agent with only the
compact mechanical brief: encounter id, exact adjudicated request, participant
labels, which seats need a baseline or delta, and a recovery note when
applicable. It receives no adventure or module text and no player or seat
private memory. End it after its bounded one-beat return. The existing named
`encounter-sim` agent remains available for direct encounter-sim workflows; do
not load it as the live play beat child.

Record player tool inventory before the first scene/brief and after re-spawn.
The expected `Read (player-visible/** only)` inventory is confined-profile;
anything broader is honour-system and is reported without pausing.

At every live checkpoint—encounter finalization, chapter boundary, or the
six-decision-beat cadence—end and re-spawn the game-master agent through this
same dispatch. Player agents remain live unless pause/resume or a failure
requires rehydration.
