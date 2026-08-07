# Human seats

Load this reference only when at least one seat is human.

## Asking a human seat

After printing narration, use the host's user-input operation. Ask up to four
humans in one pause when supported; beyond that, pause again. Offer two or three
plausible actions and let free text carry anything else—the options are a
convenience, never a menu.

During council ask for the same bounded fields as an agent: a `TABLE` proposal or
response, optional `SAY`, at most one exact `GM QUESTION`, and `READY: yes|no`.
After council, ask only a decision owner for its separate `COMMIT`.

Ordinary council has one proposal pass and one response/revision pass. A human
may request an extra pass **one pass at a time**. After **two extra extension
passes**, checkpoint the council before any further extension: update
`council.json` with `current_plan`, `open_questions`, `ready`, pass number, and
decision owners, then compact active council context to those fields. Never use
the raw discussion to continue. A human may then request another single pass;
checkpoint again after every two further extensions. This preserves discussion
without creating an unbounded context interval.

## Asking for a roll

A human turn needing a d20 may require a second pause because advantage is not
known until the declaration exists. Say how many dice and why. A seat may answer
"you roll it" once and let the engine roll.

Pass reported faces to `fivee` exactly. The engine refuses a wrong face count, a
face outside 1–20, or a face for an action that rolls no d20; relay that reason
verbatim and let the seat decide again.

```text
Ilma — the sentry has not seen you. Roll with advantage:
two d20s, and give me both.

> What did they read?   [Other: e.g. 17, 4]
```
