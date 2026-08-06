# Outside readings behind the rulings register

Working notes for the `concurrence` field of
`souroldgeezer-fivee-sim/engine/src/fivee_sim/rulings.py`. One section per
`srd_silent` entry, keyed by ruling code.

**This file does not ship.** It lives in the repository-root `docs/`, outside the
plugin directory, for the same reason `V1-ACCURACY-REVIEW.md` does. It names
third-party sources; the register itself carries only our own four-way
classification, no source name, URL, or text.

## How to read a verdict

Two tiers, and they are never merged:

- **Authoritative** — official errata and the Sage Advice Compendium. These
  settle a reading.
- **Community** — forum threads, blogs, VTT convention. These are evidence about
  what players *expect*, not about what the rule *is*. A strong community lean
  with no authoritative backing still leaves the question open.

Sage Advice is a FAQ rather than errata, so even the authoritative tier answers
"how it is meant to be read" and not "what the text says".

Conclusions are paraphrased. No third-party rules text is reproduced here.

Only `srd_silent` entries are surveyed. An `approximation` has no rules question —
the printed rule is clear and the engine models it coarser on purpose — so
grading one would invent a controversy. `rulings.py` enforces that split, and
`test_rulings.py` fails an entry that claims the wrong side of it.

---

## `climb_cost_boundary`

**Question.** When does a rise stop being ground you walk up and start being a
face you climb?

**Authoritative.** Nothing found. The printed climbing rule gives a single price
— each foot of climb costs an extra foot, doubled in Difficult Terrain, waived if
you have a Climb Speed — and an optional ability check for a slippery surface or
one with few handholds. It never says what counts as a climb in the first place;
that trigger sits with the DM. The 2024 Sage Advice Compendium (returned April
2025) does not address it.

**Community.** No consensus threshold and no table anyone reuses. Discussion
treats the slope-versus-climb question as terrain description, which is exactly
what a grid cannot defer.

**Verdict: `no_external_ruling`.** Our three-band split with a hard boundary is an
invention forced by putting elevation on a grid. Nothing outside contradicts it
and nothing endorses it.

**Consequence recorded in the register.** The two constants are module-level and a
campaign that wants a different threshold must patch the kernel. That is the
`revisit` trigger.

Sources:
- https://app.demiplane.com/nexus/5e/rules/climbing-2024 (rules text, authoritative tier)
- https://www.dndbeyond.com/posts/1950-sage-advice-and-errata-for-the-new-core-rules-is
- https://www.dndbeyond.com/sources/dnd/sae

---

## `sight_ignores_elevation`

**Question.** Within one storey, does ground height block or grant line of sight?

**Authoritative.** The printed line-of-sight procedure is *object*-based: trace a
line between the two spaces, and there is line of sight if at least one such line
avoids anything that blocks vision — a wall, a curtain, dense fog. Ground height
is not among the things that block vision, and high ground carries no printed
sight benefit. The rules explicitly leave the final call to the DM.

**Community.** Consistent lean that high ground grants no automatic sight
advantage in 5E, and that elevation matters for cover rather than for vision.
Several sources note the DM-adjudication escape hatch means tables vary.

**Verdict: `matches_common_reading`.** The engine implements the printed procedure
faithfully: an unoccupied higher square is not a vision blocker, so it does not
enter the trace. Flat sight is not a divergence here — it is what the object-based
procedure produces when nothing in the map declares itself opaque.

**Caveat worth keeping.** This is agreement about the *default*, not a guarantee.
Because the rules hand the last word to the DM, a table that rules a ridge blocks
sight is not playing incorrectly, and the engine cannot express that ruling.

Sources:
- https://www.dungeonsolvers.com/taking-a-look-at-line-of-sight-in-dd-5e/ (community)
- https://www.enworld.org/threads/determining-visibility-raw.676631/ (community)
- https://www.dndbeyond.com/forums/dungeons-dragons-discussion/tips-tactics/62261-high-ground (community)

---

## `cross_storey_sight_needs_a_link`

**Question.** What can a creature one storey up see, and be shot by?

**Authoritative.** Nothing. The printed rules have no three-dimensional cover
model. Total cover blocks line of sight and cannot be targeted directly, but
whether a floor grants it is not stated. Official material leaves vertical cover
to the DM.

**Community.** Broad agreement that a floor blocks — you do not shoot through a
ceiling — and that openings, balconies, and stairwells are where sight passes
between levels. Notably the ambiguity runs deeper than the vertical case: even
whether three-quarters cover blocks line of sight *within* one plane is unsettled,
with most discussion landing on "probably not for half, arguably for
three-quarters".

**Verdict: `matches_common_reading`.** Opaque floor by default, with authored
openings, is the shape most tables and VTTs use.

**Where we are coarser, and it is not a rules divergence.** The sight link is read
at the *origin square only* and grants the *whole target level* at once. So a
creature one square back from a balcony rail sees nothing, and one on the far side
of the upper floor is seen regardless of distance or intervening geometry. That
granularity is an implementation approximation inside a reading that matches; it
is the `revisit` trigger on the entry.

Sources:
- https://www.enworld.org/threads/line-of-sight-ruling.713359/page-3 (community)
- https://dicedungeons.com/blogs/inside/cover-ranged-dnd-2024 (community)

---

## What the survey concluded overall

The useful output was mostly negative, and that is worth stating plainly: for
questions of this kind the authoritative tier is nearly empty. Official material
answers "the DM decides" — which a simulator cannot do, because it has to produce
a number every time.

So the register's job is not to find a citation that blesses each decision. It is
to record which decisions have outside backing (two of three here), which are ours
alone (`climb_cost_boundary`), and — for both kinds — whether the decision is a
**knob or a constant**. A contested decision baked in as a constant is the thing
worth changing, and `climb_cost_boundary` is currently the one that is.
