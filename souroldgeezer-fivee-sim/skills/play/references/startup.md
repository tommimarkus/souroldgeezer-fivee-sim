# File-first startup

Load only this reference at root; the interval controller owns seating and the
table loop. Collect adventure path, mode, seed, GM/seat kinds, party file/id,
opening scene, and optional members/indexes. Run:

```bash
python3 <skill-dir>/../../scripts/fivee-play.py init --config CONFIG \
  --adventure ADVENTURE --mode play --seed SEED --gm-kind agent \
  --party-file PARTY --party-id PARTY_ID --opening-scene ID \
  [--seat-kind NAME=agent] [--member NAME] [--prepared-index INDEX]
```

The helper validates configured content, maps, and scenes; calls
`adventure.create` once through JSON stdin; retains the distinct run and
adventure IDs; and selects the engine by run ID. It atomically publishes roster
v3 and the play artifacts. Older rosters are refused because their workspaces
are unsupported. No startup-path `jq` dependency exists.

Prepared Markdown and Forge `fivee-sim-adventure-source` JSON use deterministic
indexes, so prepared Markdown needs no `adventure-prep` child. Other JSON,
unstructured Markdown, unresolved links, or ambiguous locators request prep;
unsupported recognized versions are refused. Playtest without a matching
semantic inventory retains one semantic prep pass.

Fallback `adventure-prep` receives only source, mode, digest, and a private prep
staging directory. It writes partial files and returns a manifest;
`fivee-play.py publish-prep` validates and publishes them. Allow one bounded
correction, then block.

Never embed content, maps, scenes, or full pregen bodies in a root or controller
bootstrap payload. Pass artifact paths, digests, counts, and versions. The
direct launcher common path spawns no `play-mechanics`; `play-mechanics` is only
a conditional fallback when the controller lacks launcher capability.

After `ready` or `reused`, load exactly one host adapter and spawn a fresh
controller. Do not load seating or table-loop references at root.
