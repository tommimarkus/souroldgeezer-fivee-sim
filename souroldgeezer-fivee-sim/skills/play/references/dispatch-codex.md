# Codex dispatch

Load this file only when Codex is the active host.

Codex packaging does not activate Claude agent files as named agents. Read
`../../agents/game-master.md`, `../../agents/typical-player.md`, and
`../../agents/encounter-sim.md`; remove or ignore leading YAML frontmatter and
inject the remaining role body into the matching child prompt.

Spawn `game-master` with `fork_turns="none"` and let model and reasoning effort
inherit. Spawn each `typical-player` with `fork_turns="none"`,
`model="gpt-5.6-terra"`, and `reasoning_effort="medium"`. Spawn each resettable
`encounter-sim` mechanical context with `fork_turns="none"`,
`model="gpt-5.6-terra"`, and `reasoning_effort="medium"`.

The game-master prompt may add the adventure path, party, active mode, and its
bounded checkpoint. A player prompt may add **only** its character sheet,
temperament, voice, and permitted private rehydration state. Never include the
adventure's path, module text, run sheet, other roster entries, or full
transcript. A mechanical prompt may add only encounter id, exact adjudicated
request, participant labels, and baseline/delta needs.

Fresh context and allowlisted prompts minimise disclosure; they do not restrict
Codex filesystem or tools. Record player tool inventory before the first scene
or brief and after re-spawn. Report tools as honour-system without pausing.

At every live checkpoint—encounter finalization, chapter boundary, or the
six-decision-beat cadence—end and re-spawn the game-master child through this
same role-body path. End each mechanical child after its bounded return. Keep
player children live unless rehydration is required.
