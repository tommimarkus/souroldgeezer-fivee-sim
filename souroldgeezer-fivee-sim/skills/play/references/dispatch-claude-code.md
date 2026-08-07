# Claude Code dispatch

Load this file only when Claude Code is the active host.

Spawn the named agent `game-master` with the adventure path, party, active mode,
and current bounded checkpoint when present. Spawn the named agent
`typical-player` once per agent seat with only its character sheet, temperament,
voice, and permitted rehydration state. Claude Code discovers the packaged named
agents and applies their frontmatter, including tools, model, and effort; do not
reproduce or override it.

For each resettable mechanical context, spawn the named `encounter-sim` agent
with only the encounter id, exact adjudicated request, participant labels, and
which seats need a baseline or delta. It receives no adventure text or private
seat memory. End it after its bounded return.

Record player tool inventory before the first scene/brief and after re-spawn.
The expected `Read (player-visible/** only)` inventory is confined-profile;
anything broader is honour-system and is reported without pausing.

At every live checkpoint—encounter finalization, chapter boundary, or the
six-decision-beat cadence—end and re-spawn the game-master agent through this
same dispatch. Player agents remain live unless pause/resume or a failure
requires rehydration.
