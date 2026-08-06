# Analysis rather than play

- **`fivee analytics.rounds`** auto-plays the same encounter many times and
  reports win rates, rounds, and per-team HP, casualty, spell-slot, and item-use
  distributions. Use it for "is this encounter too hard?"
- **`fivee analytics.dpr`** measures damage a build lands over N rounds against a
  given `--target-ac`, at a `--distance` you choose (5 ft by default). Use it for
  "is this build actually better?"

Both replay the same stepper live play uses, so their numbers cannot drift from
the rules. Iteration `i` uses `seed + i`, so one iteration reproduces a single
hand-played fight at that seed — handy when a batch result looks wrong and you
want to watch the actual fight.

## What the auto-play policy will and will not do

It takes the action with the **highest expected damage this turn**, placing an area
spell to catch as many enemies as it can without catching an ally. Its blind spots
become yours the moment you quote one of these numbers, so state them when they
bear on the question:

- **It uses healing deliberately, not arbitrary items.** A downed ally is revived
  first; an ally at half HP or below may receive a healing spell or item. Other
  item effects are not valued.
- **It never casts a spell that deals no damage.** Hold Person is loaded,
  implemented, and still never chosen, because valuing a condition means modelling
  the turns it buys the rest of the party. A batch is a **floor** for a control
  build, not a measurement of it.
- **It never operates a map fixture.** No door is opened, no spike pulled, no
  sluice raised. A batch fights the map at the configuration it was handed, so
  measure a fixture by running two batches — one map authored open, one shut —
  rather than expecting the policy to find the lever.
- **It does not husband spell slots.** Best slot first, weapon afterwards.
- **It closes with Dash.** An authored Bonus Action Dash is spent before the
  action so a newly reachable attack can still happen that turn.
- **It is greedy, not tactical.** No focus-fire planning, no retreating, no
  readying. Treat a win rate as "what these statistics do when both sides swing
  hard", not as what a good table would achieve.

`analytics.dpr` returns an `actions` breakdown of what the build actually did. Read
it before trusting a damage figure — a spell that does not appear there was never
cast, and the number is measuring something narrower than you asked for.

When comparing options, hold the seed and iteration count fixed and change one
thing. Report the distribution, not just the mean: `p10`, median, `p90`, and the
resource/casualty tails are the play experience a win percentage hides.
