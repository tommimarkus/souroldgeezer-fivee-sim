# A whole adventure as one replay

Several encounters can belong to one **adventure** — an ordered run that carries
the party between them, which the encounter-sim skill drives. Keep its id as the
global selector: **`fivee --run <adv-id> adventure.replay <adv-id>`** composes
that run into a single bundle: the
adventure's identity, then every member encounter's finalized replay nested
verbatim, in order. It reports the chapter count, the encounters, the path, and
the SHA-256.

**A chapter is a fight or an interlude, and the envelope says which.** Each
chapter record carries its `mode` — `combat` or `exploration` — copied from the
frozen bundle rather than recomputed, so a composed run reads as the day it was:
arrival, conversation, ambush, aftermath. An interlude's chapter carries no
initiative and no rounds, and carries what happened instead — the moves, the
notes with their speakers, and every check rolled against that chapter's id. A
run whose non-combat scenes were never linked as chapters composes to its fights
alone and quietly loses the rest of the day, which is why the encounter-sim and
play skills both say to link them.

**Recovery belongs to the boundary before the following chapter.** When an
adventure link records `recovery`, the composed chapter preserves that exact
delta and its optional caller-written `recovery_note`. The viewer presents it as
a selectable transition before the chapter, including a before-to-after summary
for the fields that changed. It never infers whether the boundary was a short
rest, long rest, healing, or something else. Old bundles and ordinary links that
omit `recovery` have no extra boundary; an explicit empty recovery still has one
because the caller recorded that the story boundary happened.

```bash
fivee --run adv-1 adventure.list                      # this run on disk
fivee --run adv-1 adventure.replay adv-1              # into this run's replays root
fivee --run adv-1 adventure.replay adv-1 --path /abs/somewhere.json
```

Four things to say when you hand one over, because each is something a reader
will otherwise assume:

- **Every chapter must be finalized first.** The composer reads
  `encounter.finalize`'s artifact off disk, so a fight that was never finalized —
  or whose artifact has since been removed — is refused by name, never substituted.
  Run `fivee --run <adv-id> encounter.finalize <id>` on each and compose again. The *run* need not
  be closed: `adventure.finalize` stops new encounters being linked and is not a
  precondition here.
- **Nothing is re-derived, and that is correctness rather than economy.** No
  session starts and no action replays. A fight replayed under whatever kernel is
  loaded today can end a hit point away from where it was recorded, and because
  the next chapter's opening state was carried out of the previous one's ending
  state, the run would stop hanging together while the integrity hashes went on
  agreeing with themselves. **Chapters freeze at `encounter.finalize`**; composing
  only stacks them.
- **It is always a file, never inline.** An envelope holds a whole chapter's
  bundle per fight, so a run of any length clears the ceiling a single export
  inlines under. Quote the path.
- **`replay.list` will not list it, and no `viewer_url` comes back.** Both filter
  on the single-encounter replay format, so a composed run stays absent from the
  listing and out of the served chooser even though it lands in the replays root
  beside ordinary exports. So hand over **the path**, not a link — that is the one
  way a reader reaches it. The viewer page itself does play it: open or drop the
  file and a **Chapter** picker appears in the header. Choosing a recovery entry
  shows the recorded pre-chapter transition; choosing its chapter plays the
  fight or interlude. Continuous playback visits the transition before beginning
  that chapter. That works with no server at all, which is just as well, since
  the chooser will never offer it. `fivee --run <adv-id> replay.validate`
  understands it too, and the composer validates before publishing a byte, so a
  chapter corrupted on disk is refused rather than shipped inside the run.

Validating one back means sending it as `bundle`, and a run's bundle is far too
large to fit on a command line — pipe it in rather than substituting it:

```bash
{ printf '{"bundle":'; cat /abs/somewhere.json; printf '}'; } |
  fivee --run adv-1 replay.validate --json -
```
