# File-first startup

Load only this startup reference at the root. The interval controller owns the
full seating and table-loop protocols.

Collect the adventure path, mode, seed, GM/seat kinds, party file/id, optional
member names, prepared index or playtest inventory, and optional opening scene.
Run the packaged stdlib helper:

```bash
python3 <skill-dir>/../../scripts/fivee-play.py init --config CONFIG \
  --adventure ADVENTURE --mode play --seed SEED --gm-kind agent \
  --party-file PARTY --party-id PARTY_ID [--seat-kind NAME=agent] \
  [--member NAME] [--prepared-index INDEX] [--opening-scene ID]
```

`jq` is required and checked before writes. The helper validates configured
content, maps, and scenes in their final locations; starts `fivee serve`; checks
`content.status`; creates the adventure idempotently; and uses its ID for the
play-artifact directory. It atomically publishes roster v2, checkpoint,
transcript, council, cursor, module index, party projections, and seat memories.
Its compact stdout contains metadata only.

Prepared Markdown uses deterministic indexing or a cache keyed by source
SHA-256 and indexer version, so the common prepared Markdown path spawns no
`adventure-prep` model. A matching prepared index is accepted first. PDF,
unstructured Markdown, unresolved links, or ambiguous locators return a prep
requirement. Playtest similarly accepts a matching semantic inventory; without
one it retains one semantic prep pass.

For fallback, give `adventure-prep` only the source, mode, digest, and a private
`.fivee-sim/prep/<id>/` prep-staging directory. It writes `.partial` files and
returns a compact manifest. Run `fivee-play.py publish-prep`; only the helper
validates schema, digest, ordering, references, completeness, and atomically
publishes. Give one bounded correction for a failed invariant, then block.

Never embed content bodies, maps, scenes, or full pregen bodies in a root or
controller bootstrap payload. Pass only artifact paths/digests/counts/versions;
the controller reads seat/GM projection references. The direct launcher common
path spawns no `play-mechanics` model. `play-mechanics` is only a conditional
fallback when the controller lacks its narrow launcher capability.

After setup reports `ready` or idempotent `reused`, load exactly one host
dispatch adapter and spawn one fresh controller. Do not load the full seating
or table-loop references at root. A v1 roster remains resume-compatible and is
never rewritten; v2 resolves its input references without exposing sibling
seats.
