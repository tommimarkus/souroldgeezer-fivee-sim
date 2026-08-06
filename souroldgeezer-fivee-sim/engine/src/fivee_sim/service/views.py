"""How much of the fight a write answers with: ``view=delta|live|full``.

A fight's state is mostly things that did not move, and until now every write
answered with all of it — roughly 700 bytes per combatant per turn, to tell a
caller what it already knew.
:data:`~fivee_sim.model.encounter.SHEET_KEYS` classified which half a turn can
change; this is what spends that classification on the wire.

**The composition rule, which is the only delicate thing here.** ``as=`` runs
first and ``view`` runs over its output — always, in every operation, without an
exception to remember. :func:`~fivee_sim.service.encounters.project` narrows the
answer to a seat, and only then is the narrowed answer diffed against the
narrowed answer that seat was served last.

Doing it the other way round would have been the natural shape — diff the fight,
then redact the diff — and it is wrong twice over. A diff *mentions* a creature,
and mentioning one the seat cannot see is a disclosure whether or not any of its
fields came with it: "somebody you cannot see just moved" is precisely what the
brief's absent-rather-than-redacted rule exists to withhold. And narrowing
afterwards would mean a second implementation of the brief's classification, one
that reads patches rather than payloads, kept in step with the first by hand.
CLAUDE.md records four leaks already found in this area and every one of them
was a second thing that had to be kept in step.

Running the view second makes it structural instead. :func:`state_delta` is
generic: it is handed two payloads and told which keys hold rosters, and it
knows nothing about seats, hit points or cover. So when the two payloads are
briefs, every name and every key it can possibly emit came out of a brief —
there is nothing else for it to have got them from.

**What a delta is against: the payload this server last served this seat.**
Held in memory on the :class:`~fivee_sim.service.sessions.Session`, keyed by the
seat, and three things forced it.

*Caller-supplied would not work for the caller it is for.* ``fivee`` is a
one-shot process; the thing that actually holds the previous answer is the
agent, in its context, which is exactly the bandwidth this phase is spending.

*Recomputing the baseline is not the same as remembering it.*
:meth:`~fivee_sim.model.encounter.Encounter.unseen_by` asks the **live**
encounter about total cover, so ``brief_of(an old snapshot, seat)`` is not the
payload that seat was actually handed. Diffing against a recomputation would
silently keep a creature that has since stepped behind a wall, and would send a
*partial* entry for one that has just come out from behind it. What was sent is
a fact; what would have been sent is a re-derivation.

*It makes shape differences ordinary.* ``resume`` staples a ``map_source`` that
``act`` does not carry; a fight can gain a map. Remembering the bytes turns each
of those into an ordinary ``dropped`` path rather than a special case.

This is server-held state about a caller, which is a shape the storage work has
otherwise been removing. Two things keep it honest: it is **in memory only** —
nothing is written, nothing is recovered, and a journal never sees it — and
**losing it is never an error**. No baseline means the answer is ``full``, and
the response says ``view: "full"`` rather than the view that was asked for. A
recovered session, a second server, a restart and a fresh seat all land there.

**The digest is over what ``full`` would have answered, on this same request.**
Under ``as=`` that is the digest of the *brief*, not of the snapshot behind it —
a seat handed a digest it cannot recompute would be the one case with no drift
detection, which is the case that needs it most. A caller applies the delta,
digests the result, and on a mismatch refetches: ``encounter.state`` for the GM,
``encounter.brief`` for a seat.

**Events are not deltable, and that is a decision rather than an omission.** An
event is not a value that changed, it is a thing that happened: there is no
previous event to diff it against, and the sequence *is* the payload. They flow
whole on all three views. Under ``as=`` they are already narrowed by
:meth:`~fivee_sim.model.encounter.Encounter.brief_events`, and ``detail`` is
already dropped outright there because prose cannot be allowlisted — a diff of
prose would be worse than either.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..model.encounter import (
    BRIEF_ENTRIES,
    BRIEF_ROSTERS,
    STATE_ENTRIES,
    STATE_ROSTERS,
    live_of,
    state_delta,
)
from .errors import RequestError
from .replay import canonical_sha256, sheet_sha256
from .sessions import Session

__all__ = ["VIEWS", "parse_view", "viewed"]

#: The whole payload, as every write answered before this existed.
FULL = "full"

#: Every combatant, sheets replaced by a digest. Needs no baseline.
LIVE = "live"

#: What changed since this seat's last payload. Falls back to :data:`FULL`.
DELTA = "delta"

#: In the order a refusal lists them: cheapest first, and the fallback last.
VIEWS: tuple[str, ...] = (DELTA, LIVE, FULL)


def parse_view(value: str | None, default: str) -> str:
    """One of :data:`VIEWS`, or a refusal naming the three that work.

    A plain :class:`~fivee_sim.service.errors.RequestError`, like every other
    argument this layer refuses: ``service/`` may not know what a status code is,
    and ``web/http_server.py`` maps this onto problem+json. The route table
    refuses an unknown value first for a request that arrives over HTTP, so this
    is the guard for every other door — ``tests/api.py`` reaches these functions
    with no adapter in front of them at all.
    """
    if value is None:
        return default
    if value not in VIEWS:
        raise RequestError(f"view must be one of: {', '.join(VIEWS)}")
    return value


def viewed(
    session: Session,
    result: dict[str, Any],
    viewer: str | None,
    view: str,
) -> dict[str, Any]:
    """One operation's answer, cut to the view — and the baseline moved on.

    Called **after** the seat projection and never before it, for the reason the
    module docstring gives at length.

    The baseline is recorded whatever view was served, including ``full``, since
    what makes a payload a baseline is that the caller now holds it. It is a deep
    copy: the dictionary handed in is the one the journal recorded and the one an
    idempotent retry replays, and a baseline that aliased it would be rewritten
    by the next turn it was supposed to be a record of.
    """
    payload = result.get("state")
    if not isinstance(payload, dict):
        return result
    digest = canonical_sha256(payload)
    held = session.last_payload.get(viewer)
    session.last_payload[viewer] = deepcopy(payload)
    rest = {key: value for key, value in result.items() if key != "state"}
    if view == LIVE:
        return {**rest, "state_live": _live_payload(payload, viewer),
                "state_sha256": digest, "view": LIVE}
    if view == DELTA and held is not None:
        rosters = BRIEF_ROSTERS if viewer is not None else STATE_ROSTERS
        entries = BRIEF_ENTRIES if viewer is not None else STATE_ENTRIES
        return {
            **rest,
            "state_delta": state_delta(held, payload, rosters=rosters, entries=entries),
            "state_sha256": digest,
            "view": DELTA,
        }
    return {**result, "state_sha256": digest, "view": FULL}


def _live_payload(payload: dict[str, Any], viewer: str | None) -> dict[str, Any]:
    """The same payload with every creature's printed sheet replaced by its digest.

    The middle view exists because ``delta`` has one precondition a caller cannot
    always meet — that the server still holds what it last sent — and ``full``
    pays the whole cost of not meeting it. This one needs only that the caller
    saw the sheets once, which is what ``create`` gave it, so it is what a client
    reaches for when it cannot promise it is in step.

    ``name`` is kept beside the live half rather than dropped with the rest of
    the sheet: it is what the receiver keys its held sheets by, and a roster
    matched by position instead would be one reordering away from swapping two
    creatures' sheets.
    """
    rosters = BRIEF_ROSTERS if viewer is not None else STATE_ROSTERS
    entries = BRIEF_ENTRIES if viewer is not None else STATE_ENTRIES
    thinned: dict[str, Any] = dict(payload)
    for key in rosters:
        roster = payload.get(key)
        if isinstance(roster, list):
            thinned[key] = [_live_entry(one) for one in roster]
    for key in entries:
        entry = payload.get(key)
        if isinstance(entry, dict):
            thinned[key] = _live_entry(entry)
    return thinned


def _live_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {"name": entry["name"], **live_of(entry), "sheet_sha256": sheet_sha256(entry)}
