# Codex dispatch

Load this file only when Codex is the active host.

Codex packaging does not activate Claude agent files as named agents. Use a
minimal bootstrap that tells each child to read its own canonical role file,
ignore the leading YAML frontmatter, follow the remaining role, and handle only
its bounded assignment. Do not read, copy, or inject a role body into a child
prompt. Resolve these packaged paths to absolute paths:

- `../../agents/play-controller.md`
- `../../agents/adventure-prep.md`
- `../../agents/game-master.md`
- `../../agents/typical-player.md`
- `../../agents/play-mechanics.md`

Before live intervals, the root may spawn disposable `adventure-prep` with
`fork_turns="none"`, `model="gpt-5.6-terra"`, and
`reasoning_effort="medium"`. The root validates its manifest, writes initial
artifacts, and ends it before granting the live write lease.

For every live interval the root spawns `play-controller` with
`fork_turns="none"` and lets model and reasoning effort inherit. Its prompt may
add only the redacted table bootstrap, mode, current engine/adventure IDs,
artifact pointers and digests, bounded rehydration, the opaque game-master
launch fields, and the 800-token return contract. Never add module text, hidden
module state, current locators, full roster, transcript, raw council, or raw
engine traffic. The root communicates only the four allowed frame types and
does not spawn or message live roles around the controller.

The controller owns all live children. It spawns `game-master` with
`fork_turns="none"` and lets model and reasoning effort inherit. It spawns each
`typical-player` with `fork_turns="none"`, `model="gpt-5.6-terra"`, and
`reasoning_effort="medium"`. It spawns each fresh `play-mechanics` child with
`fork_turns="none"`, `model="gpt-5.6-terra"`, and
`reasoning_effort="medium"`. Every child uses the same minimal-bootstrap rule
and reads its own canonical role profile.

The prep prompt may add only adventure path, source digest and format, active
mode, and bounded frame contract; it receives no player or seat private memory.
The game-master prompt may add source path and digest, module-index pointer and
digest, current entry IDs, party summary including each seat's rules brief,
active mode, and bounded private checkpoint. The game master resolves current
IDs to locators and reads those sections; the controller never receives the
locators or module text. In playtest it also receives the run-sheet pointer,
digest, and current IDs, never the whole run sheet.

A player prompt may add **only** identity, character sheet, gear, rules brief,
temperament, voice, its `seats/<name>.md` rehydration, and the participant-scoped
bounded council projection. Never include the adventure's path, module text,
run sheet, other roster entries, or full transcript. A play-mechanics prompt may
add only the compact mechanical brief: run id, canonical operation name,
resource identifiers, adjudicated request, argument values, participant labels,
baseline/delta needs, and recovery note when applicable. It
receives no adventure or module material and no player or seat private memory.

Fresh context and allowlisted prompts minimise disclosure; they do not restrict
Codex filesystem or tools. The controller records player tool inventory before
the first player-facing scene or brief in every interval. Report tools as
honour-system without pausing.

End `adventure-prep` after its complete manifest and `play-mechanics` after its
one-beat return. At the six-beat, encounter, or chapter boundary, the controller
flushes artifacts, terminates the game master, every player, mechanics, and any
descendant, returns the bounded interval frame, and ends. The root starts a new
controller with `fork_turns="none"` when play continues.
