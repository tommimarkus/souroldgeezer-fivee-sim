# Module preparation

`scripts/fivee-play.py init` owns ordinary preparation. Prefer a supplied index
whose source SHA-256 matches. Otherwise reuse the cache keyed by source SHA-256
and indexer version, or index structured Markdown deterministically from
ATX/Setext headings, explicit Chapter/Scene/Encounter prefixes, hierarchy, and
resolvable local links. A `fivee-sim-adventure-source` JSON document at format
version 1 is a second deterministic source: the helper validates its unique IDs,
relationships, source-ordered exact line locators, content payloads, and
optional play facts, then adapts its structural fields to module-index v1.
Arbitrary JSON still needs fallback preparation, and a recognized unsupported
version is refused. Ordinary play is structural only and never performs gap or
omission analysis.

PDF, unstructured text, unresolved structure, or ambiguous locators require the
fallback `adventure-prep` role. In playtest, prefer a matching semantic
inventory; native source JSON is not a substitute for that inventory. Otherwise
retain one full semantic inventory pass for scenes,
encounters, NPCs, treasure, stated DCs, assumed route, and omissions.

## Stable module-index v1

The private `.fivee-sim/plays/<id>/module-index.json` remains:

```json
{"schema_version":1,"source_path":"/absolute/adventure.md","source_sha256":"<sha256>","source_format":"markdown","entries":[{"id":"m0001","kind":"scene","title":"The yard","locator":{"line_start":41,"line_end":88},"related_ids":["m0002"]}]}
```

Entries are source-ordered; IDs are stable by source order. Titles come from a
heading, explicit key, or neutral label and never reveal a secret. Each locator
is an inclusive line range or printed page; every `related_ids` value resolves
inside the complete index. The index contains no scene prose, boxed text, map,
stat block, or secret body. A playtest `run-sheet.json` references module-index
entry IDs rather than copying source.

## Fallback publication

Supply a private `.fivee-sim/prep/<id>/` prep-staging directory. The fallback
agent writes only `module-index.json.partial` and, for playtest, inventory
`.partial` files there. It returns a complete manifest with `schema_version`,
`complete: true`, source digest, ordered file paths, publish names, kinds, and
file digests—never bulk frames or module bodies.

Run `fivee-play.py publish-prep`. The helper validates schema, source digest,
source ordering, unique IDs, line/page locators, references, manifest digests,
and completeness before atomic rename/publication. An unreadable or incomplete
artifact gets one bounded correction naming only the invariant; a second
failure is blocked. Players never receive staging, index, inventory, source
path, or module text.
