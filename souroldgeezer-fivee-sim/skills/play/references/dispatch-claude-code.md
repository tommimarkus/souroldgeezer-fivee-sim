# Claude Code dispatch

Load this file only when Claude Code is the active host.

Before live intervals, the root may spawn the named agent `adventure-prep` with
only adventure path, source digest and format, active mode, and bounded frame
contract. It receives no player or seat private memory. End it after its complete
manifest; the root validates and writes initial artifacts before granting the
live write lease.

For every live interval the root spawns the named agent `play-controller`. Give
it only the redacted table bootstrap, mode, current engine/adventure IDs,
artifact pointers and digests, bounded rehydration, opaque game-master launch
fields, and the 800-token return contract. Never give it module text, hidden
module state, current locators, full roster, transcript, raw council, or raw
engine traffic. The root communicates only the four allowed frame types and
does not spawn or message live roles around the controller.

At controller startup confirm its `Agent` tool is present. Nested delegation
requires a subagent depth that leaves `Agent` available to this first-level
controller. If host policy removes it, return the ordinary bounded `blocked`
frame; the root must not inline the live roles or recurring relay as a fallback.

The `play-controller` owns the named child agents `game-master`,
`typical-player`, and `play-mechanics`. It spawns a fresh named `game-master` and
one fresh named `typical-player` per agent seat at every interval. The game
master receives source path and digest, module-index pointer and digest, current
entry IDs, party summary including each seat's rules brief, active mode, and its
bounded private checkpoint. It resolves current IDs to locators itself. In
playtest it also receives the run-sheet pointer, digest, and current IDs, never
the whole run sheet.

Each player receives only identity, character sheet, gear, rules brief,
temperament, voice, its `seats/<name>.md` rehydration, and any participant-scoped
bounded council projection. It never receives the adventure path or text, run
sheet, other roster entries, or full transcript. Record player tool inventory
before the first player-facing material in every interval. The expected
`Read (player-visible/** only)` inventory is confined-profile; anything broader
is honour-system and is reported without pausing.

For each decision beat the controller spawns a fresh named `play-mechanics`
agent with only run id, canonical operation name, resource identifiers,
adjudicated request, argument values, participant labels, baseline/delta needs,
and recovery note when applicable. It receives no adventure or module text and
no player or seat private memory. End it after its bounded one-beat return. The
named `encounter-sim` agent remains available for direct encounter-sim
workflows; never use it as the live play beat child.

Claude Code discovers every packaged named agent and applies its shared
frontmatter, including tools, model, and effort; do not reproduce or override
the canonical role body. At the six-beat, encounter, or chapter boundary the
controller flushes artifacts, terminates every named child and descendant,
returns its bounded interval frame, and ends. The root starts a fresh named
`play-controller` when play continues.
