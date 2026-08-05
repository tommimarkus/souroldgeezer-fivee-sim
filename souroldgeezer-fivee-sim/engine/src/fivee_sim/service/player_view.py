"""The battlefield brief: what a seat at the table is allowed to be told.

``encounter.state`` is the GM's view and reports everything — every creature's
hit points, AC, spell slots, items and attacks, and every creature that has been
rolled into initiative whether or not it is on the battlefield yet. That payload
is correct for the seat running the fight and wrong for every other seat, so
this module answers the other question: *given that this is Thora's chair, what
may the response contain?*

**It is an allowlist, and that is the whole design.**
:meth:`~fivee_sim.model.encounter.Encounter._creature_state` emits around thirty
keys today and is an open, growing set — ``facing`` joined it recently — and
several of the keys already there carry capability rather than appearance:
``spell_slots``, ``items``, ``attacks``, ``spells``, ``death_saves``,
``concentrating_on``. A projection written as "drop hp, max_hp and ac" would
therefore leak every field added after the day it was written, silently, and a
green suite would say nothing. So nothing reaches a player because it was not
excluded. Every field is named in exactly one of three sets:

* :data:`CREATURE_SHARED` — what a person sitting at the table can see. Where
  someone is standing, what they are facing, whether they are up, down, dying,
  dead, prone, dodging, whose turn it is.
* :data:`CREATURE_OWN` — your own sheet, in full, and only on your own side.
  ``game-master.md`` is explicit about why this is not withheld: a player cannot
  decide their own movement without knowing what they have left, and hiding it
  "does not create tension, it just makes them guess".
* :data:`CREATURE_NEVER` — absent from a player view whichever side it is on.

A field in none of them does not appear, which is what makes the classification
test in ``tests/test_player_view.py`` the load-bearing one: a model field added
tomorrow lands in no bucket, and the suite fails until somebody decides where it
belongs.

**Two fields are computed here rather than passed through.** ``health_band``
replaces the number with plain language, and it is deliberately lossy: the
ratio, the band's own bounds and an opponent's ``max_hp`` are all withheld
together, because publishing any one of them turns the band back into
arithmetic. ``distance`` is the answer to the question players ask most and the
one the map already determines.

**An undetected creature is omitted, not merely unlabelled.** A hidden ambusher
that appears in the initiative list with a blank name has still been revealed,
so a creature that has not arrived is absent from ``combatants``, absent from
``order``, and — if the turn happens to be theirs — leaves ``turn`` unnamed.
The viewer is the single exception: their own seat is always reported to them.

**What this cannot see, stated because it is invisible from a green run.**
The projection is a function of the snapshot and a name, which is what keeps it
testable and free of encounter state. Concealment that is a *relationship*
rather than a fact — total cover, darkness beyond darkvision, the Invisible
condition against one observer and not another — is computed by
:meth:`~fivee_sim.model.encounter.Encounter._can_see` from the whole fight, and
none of its inputs survive into ``state()``. So "detected" here means "on the
battlefield". Line-of-sight redaction would have to be a second projection
taking the encounter, and it is not this one.

**The web layer serialises; it never redacts.** ``web/http_server.py`` maps the
refusal below onto problem+json and does nothing else, for the reason every
operation body lives in ``service/``: a redaction rule written into an adapter
is a rule the next adapter does not have.

**This is a projection, not an access control, and the difference matters.**
``as=`` is asserted by the caller and authenticated by nothing — the engine has
one per-launch token and no per-seat credential — so a client that can ask for
this brief can equally ask for ``encounter.state`` and get the whole fight. What
this buys is a payload a *cooperating* client can render without the browser
holding secrets it must remember not to draw, which is the failure client-side
hiding actually has. It is not a boundary against a client that does not want to
cooperate, and nothing here should be cited as one.

**The events are projected on the same terms, and they were not always.** For a
release the four operations that answer with a ``state`` narrowed only that, and
handed the same response's ``events`` over whole — where ``damage`` carries the
target's exact ``hp`` and ``max_hp``, ``attack`` the roller's ``total``,
``use_item`` an item's remaining charges, and ``death_save`` the counters. The
brief said "hurt" and the event beside it said 3291/4400. So an event is
classified exactly as a creature is, in three sets that answer for its ``data``
— :data:`EVENT_SHARED`, :data:`EVENT_OWN`, :data:`EVENT_NEVER` — and a fourth
pair, :data:`EVENT_ENVELOPE_SHARED` and :data:`EVENT_ENVELOPE_NEVER`, that
answers for the event's own fields.

**``detail`` is omitted rather than emptied, and the difference is the point.**
It is the GM's rendered sentence — free-form prose that names the AC a swing was
rolled against, the DC a check was made against, and the spell behind an effect —
and prose cannot be allowlisted. ``""`` would say *nothing happened*, and every
event has a detail; an absent key says *this seat is not served this*, which is
the true statement.

**Two residual tells, named because a green suite hides them.** A ``turn`` of
``None`` says an unseen creature is acting: identity is withheld and existence
is not, which is the same thing a table learns when the GM rolls behind a
screen. And a fixture's ``open`` state flips when something operates it, so a
party watching the map can infer that *someone* did — again existence, not
identity. Both are inherent to reporting a live battlefield at all; the
alternative is a payload that lies about the round. A dropped event leaves a
third of the same kind, a gap in ``seq``; see :func:`_event`.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any

from ..kernel.grid import DiagonalRule, Point, as_point, distance_feet
from . import sessions
from .errors import NotFoundError

__all__ = [
    "CREATURE_DERIVED",
    "CREATURE_NEVER",
    "CREATURE_OWN",
    "CREATURE_SHARED",
    "EVENT_ENVELOPE_NEVER",
    "EVENT_ENVELOPE_SHARED",
    "EVENT_LISTS",
    "EVENT_NEVER",
    "EVENT_OWN",
    "EVENT_SHARED",
    "HEALTH_BANDS",
    "STATE_DERIVED",
    "STATE_NEVER",
    "STATE_OWN",
    "STATE_SHARED",
    "briefed",
    "health_band",
    "project",
    "require_seat",
    "view_of",
]

# --- the classification ------------------------------------------------------
#: Visible to every seat. What a person at the table can see of anyone: where
#: they are, which way they are pointing, what has happened to them, and what
#: they did in the open. ``dodging`` and ``disengaged`` are here and
#: ``reaction_available`` is not, and the line between them is the point — the
#: first two are *states* the table watched a creature enter, the third is an
#: unspent resource, which is the same kind of thing as a spell slot.
CREATURE_SHARED: frozenset[str] = frozenset({
    "name",
    "team",
    "position",
    "facing",
    "level",
    "elevation",
    "initiative",
    "conditions",
    "present",
    "conscious",
    "surrendered",
    "dying",
    "dead",
    "stable",
    "dodging",
    "disengaged",
})

#: Your own side's sheet, reported whole. Everything a player needs to take
#: their turn without guessing, and everything an opponent must not be handed:
#: exact hit points, AC, what they can do and how much of it is left.
CREATURE_OWN: frozenset[str] = frozenset({
    "hp",
    "max_hp",
    "ac",
    "speeds",
    "senses",
    "terrain_cost_overrides",
    "death_rule",
    "bonus_actions",
    "redirect_attack",
    "reaction_available",
    "concentrating_on",
    "death_saves",
    "spell_slots",
    "attacks",
    "spells",
    "items",
})

#: Absent from a player view on either side. ``arrival_round`` is the module's
#: schedule rather than anybody's sheet — the round the reinforcements land is
#: the one number that would give an ambush away even after the ambusher has
#: been filtered out of everything else.
CREATURE_NEVER: frozenset[str] = frozenset({"arrival_round"})

#: Added by this module; carried by no creature in the snapshot.
CREATURE_DERIVED: frozenset[str] = frozenset({"health_band", "distance"})

#: The battlefield itself, which everyone is standing in. ``combatants``,
#: ``order`` and ``turn`` are here because the *key* survives, not because its
#: contents pass unread: each names creatures and each is rewritten below
#: against the visible cast.
STATE_SHARED: frozenset[str] = frozenset({
    "round",
    "turn",
    "movement_rule",
    "over",
    "winner",
    "order",
    "map",
    "combatants",
})

#: The turn budget, reported only when the turn is the viewer's. Whose turn it
#: is, is public; how much of it they have left is not.
STATE_OWN: frozenset[str] = frozenset({"turn_state"})

#: ``ongoing_effects`` names the spell behind every running effect and the round
#: it lapses, which is a caster's sheet written from the other end; a viewer's
#: own concentration is reported by ``concentrating_on`` and every condition an
#: effect imposes by ``conditions``, so nothing a player needs is lost with it.
#: ``map_source`` is a path on the host's filesystem and belongs to no seat.
STATE_NEVER: frozenset[str] = frozenset({"ongoing_effects", "map_source"})

#: Added by this module; carried by no snapshot.
STATE_DERIVED: frozenset[str] = frozenset({"viewer"})

#: The battle map's own block. Small and slow-moving, but classified rather
#: than passed through for the reason the rest of this module exists: it was
#: handed over whole in the first draft, and a security audit found a fixture's
#: ability-check DC arriving in a player's brief — the first entry on
#: ``game-master.md``'s Withhold list. An allowlist that stops one level short
#: of the payload is a denylist wearing the other one's name.
MAP_SHARED: frozenset[str] = frozenset({
    "name",
    "width",
    "height",
    "movement_rule",
    "elevation",
    "levels",
    "features",
})

#: Empty today, and kept so a reader can see that it is empty by decision. The
#: map block carries no creature and no secret of its own; what it carries that
#: a player may not have is one level further down, in each fixture.
MAP_NEVER: frozenset[str] = frozenset()

#: One fixture as the room shows it: where it is, what it is, which storey,
#: whether it stands open, and whether working it costs your action — the last
#: because a player choosing a turn is owed what it costs.
FEATURE_SHARED: frozenset[str] = frozenset({
    "square",
    "kind",
    "level",
    "open",
    "costs_action",
})

#: What a fixture *does elsewhere* and what it *takes*, which is the module's
#: to reveal and not the map file's to publish. ``check`` is a DC before a roll
#: outright; ``affects``, ``requires``, ``blocked_by``, ``linked_to`` and
#: ``trigger`` are the mechanism behind it, and a party handed the wiring has
#: been handed the puzzle.
FEATURE_NEVER: frozenset[str] = frozenset({
    "check",
    "affects",
    "requires",
    "blocked_by",
    "linked_to",
    "trigger",
})

# --- the event classification ------------------------------------------------
#: Which keys of an operation's answer carry events. ``events`` is what ``act``
#: and ``advance`` reply with. ``log`` is ``create``'s, and it is not merely the
#: opening pair a fresh fight has: an idempotent retry answers with
#: ``creation_response``, whose ``log`` is the *whole* log of a fight already in
#: progress. A projection that narrowed only ``events`` would hand that over.
EVENT_LISTS: tuple[str, ...] = ("events", "log")

#: An event's own fields, minus ``data``'s contents. ``actor``, ``target`` and
#: ``turn`` are here because the *key* survives rather than its contents passing
#: unread — exactly as ``combatants`` and ``turn`` sit in :data:`STATE_SHARED` —
#: and ``data`` for the same reason. Each is rewritten below.
EVENT_ENVELOPE_SHARED: frozenset[str] = frozenset({
    "kind",
    "actor",
    "target",
    "seq",
    "round",
    "turn",
    "data",
})

#: The one field a seat is never served. ``detail`` is rendered prose — "Longsword:
#: 19 vs AC 16, hit for 7" — and free-form text cannot be classified key by key,
#: so there is no honest way to serve part of it. See the module docstring for why
#: it is omitted rather than blanked.
EVENT_ENVELOPE_NEVER: frozenset[str] = frozenset({"detail"})

#: What the table watched happen. The line drawn through ``data`` is the one this
#: module draws everywhere: an *observation* is shared and a *sheet* is not.
#:
#: Three of these are judgement calls worth naming.
#:
#: ``hit`` is here and ``total`` is not. At a real table a player learns whether a
#: blow landed the moment it lands, so withholding it would be a payload that lies
#: about the round. What the number would add is arithmetic: a hit at 19 says the
#: target's AC is at most 19 and a miss at 18 says it is at least 19, and a few
#: rounds of those brackets it exactly — which is why ``total`` goes only to the
#: side that rolled it, where it discloses nothing the roller did not already know.
#:
#: ``amount`` is here and ``hp`` is not, and that is the same line one step over.
#: The damage that landed is the roll everyone watched; a player who tracks their
#: own damage against a health band is doing at the table exactly what a table
#: lets them do. What is refused is the *sheet's* numbers, which turn that estimate
#: into the answer.
#:
#: ``damage_type`` is here and ``damage`` is not. A wound's element is the most
#: visible thing about it and the ``damage`` event does not carry it, so withholding
#: it would leave a party unable to say what is hurting them. ``damage`` can go
#: because it is never the only account: every point that actually lands is reported
#: again by the ``damage`` event's ``amount``, so classifying it ``OWN`` costs a
#: player nothing and keeps an attachment's *formula* — ``attach`` sends ``"1d4"``
#: through this key — off the wire.
EVENT_SHARED: frozenset[str] = frozenset({
    # the fight's own clock and cast
    "round",
    "attacker",
    "original_target",
    "redirected_target",
    "targets",
    # where things are and how they got there
    "position",
    "level",
    "origin",
    "destination",
    "squares",
    "from_level",
    "to_level",
    "movement_mode",
    "completed",
    "cost",
    "center",
    "original_position",
    "redirected_position",
    # what the table watched land
    "hit",
    "critical",
    "amount",
    "damage_type",
    "total_drained",
    "affected",
    "applied",
    "saved",
    "success",
    "condition",
    "expiry",
    # the ground, and what is between two creatures
    "cover",
    "total_cover",
    "out_of_range",
    "underwater",
    "underwater_auto_miss",
    # the action economy, which is spent in the open
    "as_bonus_action",
    "action_cost",
    # a fixture, as the room shows it
    "feature",
    "open",
    "automatic",
    # concentration holds or it drops, and the effect ending says so anyway
    "held",
    "started",
})

#: Your own side's rolls, resources and repertoire — the same sheet
#: :data:`CREATURE_OWN` reports, arriving one event at a time.
#:
#: ``natural`` with ``total`` is the roller's attack bonus by subtraction, which is
#: a number off the sheet however it is spelled; ``advantage`` says which
#: circumstance produced the roll. ``attack`` and ``spell`` name one entry of the
#: ``attacks`` and ``spells`` lists that are already ``CREATURE_OWN`` — a table
#: watches a swing and learns that a heavy blade came down, not the catalogue key —
#: and ``item``, ``remaining``, ``slot_level``, ``successes`` and ``failures`` are
#: the resources those lists are spent from. ``movement_left`` is ``turn_state``,
#: which is :data:`STATE_OWN` for the same reason.
EVENT_OWN: frozenset[str] = frozenset({
    "hp",
    "max_hp",
    "natural",
    "total",
    "advantage",
    "attack",
    "spell",
    "slot_level",
    "item",
    "remaining",
    "successes",
    "failures",
    "movement_left",
    "damage",
    "bonus_damage",
    "advantage_bonus_damage",
    "advantage_bonus_reason",
    "detach_after_damage",
})

#: Absent from a player's event whichever side it belongs to.
#:
#: ``dc`` and ``check`` are a difficulty class and the sentence that quotes one —
#: the first entry on ``game-master.md``'s Withhold list, and ``check`` is already
#: :data:`FEATURE_NEVER` one level up. ``linked`` and ``triggered_by`` are the map's
#: wiring, likewise ``NEVER`` on the fixture summary: a party handed the pairing has
#: been handed the puzzle, and they can still watch both fixtures' ``open`` states
#: move. ``arrival_round`` is the module's schedule, matching
#: :data:`CREATURE_NEVER`. ``planned_destination`` and ``planned_to_level`` are the
#: square a cut-short move was *making for* — intent rather than observation, which
#: the battlefield never shows and which the mover already knows because they asked
#: for it.
EVENT_NEVER: frozenset[str] = frozenset({
    "dc",
    "check",
    "linked",
    "triggered_by",
    "arrival_round",
    "planned_destination",
    "planned_to_level",
})

# --- health, in words --------------------------------------------------------
#: Every band this module will ever report, worst first. Plain language on
#: purpose: these are the words a GM says out loud, and none of them is a
#: number wearing a hat.
HEALTH_BANDS: tuple[str, ...] = ("down", "near death", "badly hurt", "hurt", "unhurt")

#: Upper bound of each band as a fraction of maximum hit points, best-last. The
#: bounds live here and are never published — a client told both the band and
#: its edges is a client one subtraction away from a range, and a client told
#: ``max_hp`` as well has the number.
_BAND_CEILINGS: tuple[tuple[float, str], ...] = (
    (0.25, "near death"),
    (0.5, "badly hurt"),
    (1.0, "hurt"),
)


def health_band(hp: int, max_hp: int) -> str:
    """Plain language for a creature's condition, and nothing recoverable.

    Down is its own word rather than the bottom of the scale, because a
    creature at zero is a different fact from a wounded one and the table can
    see which it is anyway.
    """
    if hp <= 0:
        return "down"
    if max_hp <= 0 or hp >= max_hp:
        return "unhurt"
    ratio = hp / max_hp
    for ceiling, name in _BAND_CEILINGS:
        if ratio <= ceiling:
            return name
    return "unhurt"


# --- the projection ----------------------------------------------------------
def _point(value: Any) -> Point:
    """A snapshot position as a point in feet.

    The payload carries ``[x, y]`` because it went through JSON; the grid wants
    a tuple. A bare integer still means feet along the x-axis, which is what
    :func:`~fivee_sim.kernel.grid.as_point` has always widened.
    """
    if isinstance(value, Sequence) and not isinstance(value, str) and len(value) == 2:
        return (int(value[0]), int(value[1]))
    return as_point(int(value))


def _map(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The battlefield, minus each fixture's wiring and its DC.

    ``None`` stays ``None``: a fight on the open plane has no ground, and
    inventing an empty map block would read as one.
    """
    if raw is None:
        return None
    block = {key: value for key, value in raw.items() if key in MAP_SHARED}
    features = block.get("features")
    if isinstance(features, Mapping):
        block["features"] = {
            name: {key: value for key, value in one.items() if key in FEATURE_SHARED}
            for name, one in features.items()
        }
    return block


def _creature(
    raw: Mapping[str, Any], *, own: bool, seat: Point, rule: DiagonalRule
) -> dict[str, Any]:
    """One creature as this seat may see it.

    Built by walking the *snapshot's* keys rather than the bucket's, so the
    projected entry keeps the order the model emitted and an unclassified key
    falls out here rather than being renamed into existence.
    """
    permitted = CREATURE_SHARED | CREATURE_OWN if own else CREATURE_SHARED
    entry = {key: value for key, value in raw.items() if key in permitted}
    entry["health_band"] = health_band(int(raw["hp"]), int(raw["max_hp"]))
    entry["distance"] = distance_feet(seat, _point(raw["position"]), rule)
    return entry


def require_seat(names: Collection[str], viewer: str) -> None:
    """Refuse a name this cast does not hold, in the sentence the whole surface uses.

    That refusal is not politeness: a projection keyed on team membership would
    answer an unknown name with a brief in which every creature is an opponent —
    well formed, plausible, and a lie about who asked. The message deliberately
    does not list the cast, because listing it would hand a player-chair client
    the names this projection exists to withhold.

    One sentence with one owner, because two callers ask this question about two
    different things. :func:`project` asks it of a *snapshot*, which is every
    seat there is by the time a fight can be read; ``service/encounters.py``
    asks it of the roster a fight is being *started* from, where the refusal has
    to land before the encounter is created, journaled and given an id. A client
    that learns the refusal from one operation has learnt it from all of them.
    """
    if viewer not in names:
        raise NotFoundError(f"no combatant named {viewer!r} in this encounter")


class _Cast:
    """Who is in this fight, whose side the viewer is on, and who they can see.

    Computed once and handed to both halves of a briefed answer, because the
    ``state`` and the ``events`` in one response have to agree about the cast:
    a creature filtered out of ``combatants`` and named by an event beside it
    would be revealed by the payload that redacted them.
    """

    __slots__ = ("seats", "side", "visible", "withheld")

    def __init__(self, snapshot: Mapping[str, Any], viewer: str) -> None:
        combatants: Sequence[Mapping[str, Any]] = snapshot.get("combatants", ())
        self.seats: dict[str, Mapping[str, Any]] = {
            str(one["name"]): one for one in combatants
        }
        require_seat(self.seats.keys(), viewer)
        self.side: str = str(self.seats[viewer]["team"])
        # On the battlefield, or the viewer themselves — a seat is always
        # reported to the person sitting in it, including on the round before
        # they arrive.
        self.visible: set[str] = {
            name for name, one in self.seats.items()
            if bool(one.get("present", True)) or name == viewer
        }
        self.withheld: set[str] = set(self.seats) - self.visible


def _mentions(value: Any, names: Collection[str]) -> bool:
    """Whether any of ``names`` appears as a string anywhere inside ``value``.

    Compared whole rather than as a substring, so a visible ``Grelka`` is not
    mistaken for a withheld ``Grelk``. It can afford to be exact because the one
    field that would have carried a name inside prose — ``detail`` — is gone by
    the time this runs.
    """
    if isinstance(value, str):
        return value in names
    if isinstance(value, Mapping):
        return any(
            _mentions(key, names) or _mentions(one, names) for key, one in value.items()
        )
    if isinstance(value, Sequence):
        return any(_mentions(one, names) for one in value)
    return False


def _side_of(raw: Mapping[str, Any], cast: _Cast) -> str | None:
    """Whose event this is: the actor's side, or the target's when there is no actor.

    One boolean per event rather than one per key, and the order matters. An
    ``attack`` names the swinger first, so the roll, the weapon and the damage
    breakdown go to the side that made them and not to the side they landed on —
    which is what stops a foe's swing at your ally publishing the foe's attack
    bonus. A ``damage`` event carries no actor at all: it is emitted about the
    creature that took the hit, so its ``hp`` and ``max_hp`` are that creature's
    and reach only their own side.
    """
    for key in ("actor", "target"):
        name = raw.get(key)
        if name:
            entry = cast.seats.get(str(name))
            return None if entry is None else str(entry["team"])
    return None


def _event(raw: Mapping[str, Any], *, own: bool, cast: _Cast) -> dict[str, Any] | None:
    """One event as this seat may see it, or ``None`` if it may not see it at all.

    Built by walking the event's own keys for the reason :func:`_creature` walks
    the snapshot's: an unclassified key falls out here rather than being renamed
    into existence.

    **An event that still names a creature this seat cannot see is dropped
    whole**, and that check is deliberately made on the *projected* entry rather
    than on ``actor`` and ``target`` alone. Several ``data`` keys carry creature
    names — ``targets``, ``attacker``, ``original_target``, ``redirected_target``
    — and a fourth bucket for "keys that are names" would be one more list to
    keep in step with the model. Sweeping the finished entry needs no such list
    and cannot miss the name a key added tomorrow carries. It is a second guard
    over an allowlist, not a denylist standing in for one: nothing reaches this
    point that a bucket did not already admit.

    Dropping, rather than blanking the name, is the same answer ``combatants``
    and ``order`` give — an ambusher reported with a blank name has still been
    revealed. It leaves a gap in ``seq``, which is existence without identity and
    exactly the residual the module docstring already names for ``turn``.

    ``turn`` is the one name held against the cast rather than dropped for,
    because it is a stamp and not the event's subject: it is nulled exactly as
    :func:`project` nulls the snapshot's ``turn``, so that ``round 2 begins``
    still reaches a table whose next combatant has not arrived.

    ``arrival`` needs no exemption and gets none. ``_arrive_for_round`` marks the
    creature present *before* it emits, so by the time this runs they are in the
    visible cast and the general rule admits the event on its own.
    """
    entry = {key: value for key, value in raw.items() if key in EVENT_ENVELOPE_SHARED}
    permitted = EVENT_SHARED | EVENT_OWN if own else EVENT_SHARED
    data = entry.get("data")
    if isinstance(data, Mapping):
        entry["data"] = {
            key: value for key, value in data.items() if key in permitted
        }
    if entry.get("turn") in cast.withheld:
        entry["turn"] = None
    if _mentions(entry, cast.withheld):
        return None
    return entry


def _events(
    raw: Sequence[Mapping[str, Any]], cast: _Cast
) -> list[dict[str, Any]]:
    """One operation's account of what just happened, narrowed to a seat."""
    projected = []
    for one in raw:
        entry = _event(one, own=_side_of(one, cast) == cast.side, cast=cast)
        if entry is not None:
            projected.append(entry)
    return projected


def _projected(snapshot: Mapping[str, Any], viewer: str, cast: _Cast) -> dict[str, Any]:
    """The body of :func:`project`, over a cast its caller has already computed."""
    rule = DiagonalRule(snapshot.get("movement_rule") or DiagonalRule.FIVE_FIVE_FIVE)
    seat = _point(cast.seats[viewer]["position"])
    shown = [one for name, one in cast.seats.items() if name in cast.visible]

    view = {key: value for key, value in snapshot.items() if key in STATE_SHARED}
    view["map"] = _map(snapshot.get("map"))
    view["combatants"] = [
        _creature(one, own=one["team"] == cast.side, seat=seat, rule=rule)
        for one in shown
    ]
    view["order"] = [
        name for name in snapshot.get("order", ()) if name in cast.visible
    ]
    turn = snapshot.get("turn")
    view["turn"] = turn if turn in cast.visible else None
    if turn == viewer and "turn_state" in snapshot:
        view["turn_state"] = dict(snapshot["turn_state"])
    view["viewer"] = viewer
    return view


def project(snapshot: Mapping[str, Any], viewer: str) -> dict[str, Any]:
    """The snapshot as one seat may see it.

    ``viewer`` names a creature in this fight, and a name the fight does not
    hold is refused — see :func:`require_seat` for why that matters.
    """
    return _projected(snapshot, viewer, _Cast(snapshot, viewer))


def briefed(result: dict[str, Any], viewer: str | None) -> dict[str, Any]:
    """One operation's answer, with the fight in it narrowed to a seat.

    Reading a fight was this projection's only door for a release, and every
    operation that *changed* one answered whichever seat posted it with the GM's
    whole snapshot. A player's client could only hide what it had already been
    given, which is precisely the failure the module docstring above says this
    design avoids — inert against devtools, a proxy, or an extension. So the
    four operations that answer with a ``state`` pass it through here.

    ``None`` is *no chair asked*, and returns the result unchanged. That is the
    whole of the additive promise: the CLI and the skills name no seat and get
    back, byte for byte, the answer they always got.

    **The result is rebuilt, never edited.** The dictionary handed in is the one
    the journal recorded and the one an idempotent retry replays, so projecting
    it in place would rewrite a fight's own audit record into one player's brief
    and answer the GM's next retry with it. Every list this rebuilds is a new
    list of new dictionaries for the same reason, one level further down.

    **``state`` and the events are narrowed together**, against one cast. This
    paragraph used to say the opposite — that an ``events`` list was the GM's
    account and no projection touched it, a boundary rather than an oversight.
    It was an oversight. ``events[].data`` carried the exact numbers ``state``
    had just replaced with a band: a ``damage`` event answered a player's own
    swing with ``{"hp": 3291, "max_hp": 4400}`` beside a brief that said "hurt".
    The three event buckets above are what closed it, and the cast is shared so
    that a creature the ``state`` omits is not named by an event beside it.

    What is *not* narrowed is everything else in the answer, which is what it
    always was: ``encounter_id``, ``seed``, the map source, a content warning, a
    recovery warning. None of them is anybody's sheet.
    """
    if viewer is None:
        return result
    snapshot: Mapping[str, Any] = result["state"]
    cast = _Cast(snapshot, viewer)
    briefed_result: dict[str, Any] = {
        **result, "state": _projected(snapshot, viewer, cast)
    }
    for key in EVENT_LISTS:
        found = result.get(key)
        if isinstance(found, list):
            briefed_result[key] = _events(found, cast)
    return briefed_result


def view_of(
    state: sessions.EngineState, encounter_id: str, viewer: str
) -> dict[str, Any]:
    """One encounter, projected for one seat.

    Reads the model's own snapshot rather than
    :func:`~fivee_sim.service.encounters.state_of`, whose extra ``map_source``
    is a host filesystem path. Classified ``NEVER`` regardless, so passing the
    richer dictionary would change nothing — but taking the narrower one means
    the allowlist is never the only thing standing between a player and a path.
    """
    session = sessions.session_for(state, encounter_id)
    return project(session.encounter.state(), viewer)
