# Codex dispatch

Load this file only when Codex is the active host.

Codex packaging does not activate Claude agent files as named agents. Use a
minimal bootstrap that tells each child to read its own canonical role file,
ignore the leading YAML frontmatter, follow the remaining role, and then handle
the bounded assignment. Do not read, copy, or inject a role body into the child
prompt. Resolve these packaged paths to absolute paths:

- `../../agents/adventure-prep.md`
- `../../agents/game-master.md`
- `../../agents/typical-player.md`
- `../../agents/play-mechanics.md`

Spawn the disposable `adventure-prep` with `fork_turns="none"`,
`model="gpt-5.6-terra"`, and `reasoning_effort="medium"`. Spawn `game-master`
with `fork_turns="none"` and let model and reasoning effort inherit. Spawn each
`typical-player` with `fork_turns="none"`,
`model="gpt-5.6-terra"`, and `reasoning_effort="medium"`. Spawn each resettable
`play-mechanics` child with `fork_turns="none"`,
`model="gpt-5.6-terra"`, and `reasoning_effort="medium"`.

The prep prompt may add only the adventure path, source digest and format,
active mode, and bounded frame contract. It receives no player or seat private
memory. The game-master prompt may add the source path and digest, module-index
pointer and digest, current entries and locators, party summary including each
seat's rules brief, active mode, and bounded checkpoint. A player prompt may add
**only** its identity, character sheet, gear, rules brief, temperament, voice,
and permitted private rehydration state. Never include the adventure's path,
module text, run sheet, other roster entries, or full transcript. A
play-mechanics prompt may add only the compact mechanical brief:
encounter id, exact adjudicated request, participant labels, baseline/delta
needs, and recovery note when applicable. It receives no adventure or module
material and no player or seat private memory.

Fresh context and allowlisted prompts minimise disclosure; they do not restrict
Codex filesystem or tools. Record player tool inventory before the first scene
or brief and after re-spawn. Report tools as honour-system without pausing.

End `adventure-prep` after its complete manifest and end `play-mechanics` after
its one-beat return. At every live checkpoint—encounter finalization, chapter
boundary, or the six-decision-beat cadence—end and re-spawn the game-master
child through this same canonical-role path. Keep player children live unless
rehydration is required.
