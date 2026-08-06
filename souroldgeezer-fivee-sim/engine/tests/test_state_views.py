"""``view=delta|live|full``: how much of the fight a write answers with.

A write used to answer with the whole state every time, and a fight's state is
mostly things that did not move. :data:`SHEET_KEYS` / :data:`LIVE_KEYS` said
which half a fight can move; this is what spends that classification.

**Three views, three different reasons to exist.**

* ``full`` is the payload as it always was, and it is what establishes a
  baseline. ``encounter.create`` and ``encounter.resume`` default to it, because
  a delta against nothing is not a smaller answer, it is an unusable one.
* ``live`` is every combatant with its sheet dropped and a ``sheet_sha256`` in
  its place. It needs **no baseline** — only that the caller saw the sheets once
  — so it is what a caller uses when it cannot promise it holds the last answer.
* ``delta`` is what changed since the last payload this seat was served. It is
  the default for ``encounter.act`` and ``encounter.advance``.

**The composition rule, which is the whole of the correctness story here:
``as=`` runs first and ``view`` runs over its output.** The brief decides who
and what a seat may be told; the view decides how much of that to repeat. A
delta computed over the fight's own snapshot and narrowed afterwards would have
to re-derive the brief's classification over a diff — and a diff that mentions a
creature is a disclosure whether or not it carries any of that creature's
fields. Running the view second makes the safety property structural rather than
argued: every name and every key a delta can mention came out of the brief,
because the brief is the only thing the diff ever saw.

**Membership is absolute, values are differential.** A roster in a delta is the
*complete, ordered* list of who is there, each entry thinned to the keys that
moved. That is what expresses a creature arriving, dying, or dropping out of
sight: "gone" is absence from a list that claims to be total, not a deletion the
receiver has to infer. A per-creature diff alone could say "changed" and never
"no longer yours to see".

**The applier below is the contract, written out.** :func:`apply_delta` is this
file's own implementation of the paragraph the skills publish — deliberately not
a function imported from the engine, because a round trip through one function
proves only that it is its own inverse. What has to hold is that an independent
reader, following the published rule, reconstructs the payload byte for byte.

**And the fight drives itself off that reconstruction.** :func:`busy_fight`
decides its next action by reading the payload it rebuilt from the deltas, never
by refetching the state. A fixture that consulted the authority between turns
would keep resynchronising itself and could not tell a correct delta from one
that merely looked plausible.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping
from typing import Any

import pytest

from fivee_sim.model import encounter as model
from fivee_sim.service import sessions as _sessions
from fivee_sim.service import views
from fivee_sim.service.errors import RequestError
from fivee_sim.service.replay import canonical_sha256, sheet_sha256
from fivee_sim.web import routes

from . import api

FIXTURE = "synthetic test fixture, not SRD content"

VIEWER = "Thora"
ALLY = "Kesh"
FOE = "Grelk"
MOOK = "Skrit"
LATECOMER = "Zzaxil"

#: Room enough to walk, and a fight that lasts long enough to kill somebody.
HALL: dict[str, Any] = {
    "name": "muster hall",
    "width": 12,
    "height": 8,
    "rows": [
        "############",
        "#..........#",
        "#..........#",
        "#..........#",
        "#..........#",
        "#..........#",
        "#..........#",
        "############",
    ],
    "legend": {"#": "wall", ".": "normal"},
}

#: The sealed box from ``test_player_brief.py``: a 3x3 room with no opening, so
#: a creature inside it is behind total cover from everywhere outside.
VAULT: dict[str, Any] = {
    "width": 7,
    "height": 7,
    "rows": [
        ".......",
        ".......",
        ".......",
        ".......",
        "....###",
        "....#.#",
        "....###",
    ],
    "legend": {".": "normal", "#": "wall"},
}


#: Two squares each, and every one of the ten distinct: a fight where the
#: fixture's own movement can put two creatures on one square refuses the move
#: rather than exercising the delta. Each pair also keeps its owner within reach
#: of somebody on the other side in either configuration, so a round is a round
#: of blows and not a round of walking.
HOME: dict[str, list[list[int]]] = {
    VIEWER: [[10, 10], [10, 15]],
    FOE: [[15, 10], [15, 15]],
    ALLY: [[30, 10], [30, 15]],
    MOOK: [[35, 10], [35, 15]],
    LATECOMER: [[35, 20], [30, 20]],
}

#: Chebyshev, which is what ``5-5-5`` makes a diagonal worth.
REACH = 5


def _attack(name: str, bonus: int, damage: str) -> dict[str, Any]:
    return {
        "name": name,
        "attack_bonus": bonus,
        "damage": damage,
        "damage_type": "slashing",
        "kind": "melee",
        "provenance": FIXTURE,
    }


def hero_spec() -> dict[str, Any]:
    return {
        "name": VIEWER,
        "team": "party",
        "ac": 16,
        "max_hp": 400,
        "speed": 30,
        "darkvision": 60,
        "facing": "north",
        "bonus_actions": ["dash"],
        "items": {"Potion of Healing": 3},
        "attacks": [_attack("Longsword", 5, "1d8+3")],
        "position": HOME[VIEWER][0],
        "provenance": FIXTURE,
    }


def ally_spec() -> dict[str, Any]:
    return {**hero_spec(), "name": ALLY, "position": HOME[ALLY][0], "facing": None}


def foe_spec() -> dict[str, Any]:
    return {
        "name": FOE,
        "team": "monsters",
        "ac": 12,
        "max_hp": 400,
        "attacks": [_attack("Rustcleaver", 6, "2d6+4")],
        "position": HOME[FOE][0],
        "provenance": FIXTURE,
    }


def mook_spec() -> dict[str, Any]:
    """Small enough to die inside the script, so ``dead`` has somewhere to move."""
    return {
        "name": MOOK,
        "team": "monsters",
        "ac": 5,
        "max_hp": 4,
        "attacks": [_attack("Serrated bite", 2, "1d4")],
        "position": HOME[MOOK][0],
        "provenance": FIXTURE,
    }


def latecomer_spec() -> dict[str, Any]:
    """Absent when the fight opens: the seat is told nothing about it at all."""
    return {
        "name": LATECOMER,
        "team": "monsters",
        "ac": 12,
        "max_hp": 40,
        "attacks": [_attack("Chitin rake", 4, "1d6+2")],
        "position": HOME[LATECOMER][0],
        "arrival_round": 3,
        "provenance": FIXTURE,
    }


def roster() -> list[dict[str, Any]]:
    return [hero_spec(), ally_spec(), foe_spec(), mook_spec(), latecomer_spec()]


#: Which top-level keys hold a roster, and which hold one creature. Read off the
#: model rather than written here, because a shape this file spelled out would be
#: a second copy of the model's own answer and would agree with it by hand.
ROSTER_KEYS: frozenset[str] = frozenset(model.STATE_ROSTERS) | frozenset(
    model.BRIEF_ROSTERS
)
ENTRY_KEYS: frozenset[str] = frozenset(model.STATE_ENTRIES) | frozenset(
    model.BRIEF_ENTRIES
)


def apply_delta(held: Mapping[str, Any], delta: Mapping[str, Any]) -> dict[str, Any]:
    """The receiver's rule, exactly as the skills publish it.

    Start from what you hold. Drop every path the delta says is gone. Then, for
    every other key: a roster's list is *complete and ordered* — keep exactly
    those names, in that order, overlaying each entry's keys onto the entry you
    already had (or taking it whole when you had none). A single creature
    overlays the same way. Anything else replaces its value outright. A key the
    delta does not mention did not move.
    """
    result: dict[str, Any] = json.loads(json.dumps(held))
    for path in delta.get("dropped", []):
        _drop(result, str(path).split("/"))
    for key, value in delta.items():
        if key == "dropped":
            continue
        if key in ROSTER_KEYS:
            previous = {
                str(entry["name"]): entry
                for entry in result.get(key, [])
                if isinstance(entry, dict)
            }
            result[key] = [
                {**previous.get(str(entry["name"]), {}), **entry} for entry in value
            ]
        elif key in ENTRY_KEYS:
            result[key] = {**result.get(key, {}), **value}
        else:
            result[key] = value
    return result


def _drop(payload: MutableMapping[str, Any], path: list[str]) -> None:
    """Remove one path, whose only shapes are ``key``, ``you/key``, ``r/name/key``."""
    if len(path) == 1:
        payload.pop(path[0], None)
        return
    if len(path) == 2:
        entry = payload.get(path[0])
        if isinstance(entry, dict):
            entry.pop(path[1], None)
        return
    roster = payload.get(path[0])
    if isinstance(roster, list):
        for entry in roster:
            if isinstance(entry, dict) and str(entry.get("name")) == path[1]:
                entry.pop(path[2], None)


def forget_baselines(encounter_id: str) -> None:
    """Stand in for a server that never served this caller anything.

    A recovered session, a second engine on the same encounter directory, or a
    restart: all of them reach a live fight holding no memory of what this seat
    was last sent. Reaching into the session is the honest way to arrange that
    in-process — ``tests/api.py`` is one service call per function, and a
    "forget" door there would be test machinery on the engine's own surface.
    """
    _sessions.session_for(api.STATE, encounter_id).last_payload.clear()


def busy_fight(
    seed: int = 20260806,
    viewer: str | None = None,
    view: str | None = None,
    rounds: int = 6,
) -> list[dict[str, Any]]:
    """A long, adversarial fight, and every answer it gave, in order.

    Not a two-turn toy: it runs until somebody is dead, a reinforcement has
    arrived, conditions have been imposed and lifted, hit points have gone down
    and back up, and combatants have moved and turned. The first entry is
    ``create``'s answer, which is the baseline every later delta is against.

    **It steers by the reconstruction**, so the fixture is a consumer of the
    thing under test rather than an observer of it: an illegal action means the
    rebuilt payload disagreed with the fight about whose turn it was.

    For a seated run the seat can only act on its own turn, so the other
    creatures are driven through the GM's chair — a different seat, a different
    baseline, and the interference between the two is itself under test.
    """
    created = api.encounter_create(
        roster(), seed=seed, map=HALL, viewer=viewer, view="full"
    )
    encounter_id = str(created["encounter_id"])
    answers: list[dict[str, Any]] = [created]
    held: dict[str, Any] = created["state"]

    def record(answer: dict[str, Any]) -> None:
        nonlocal held
        answers.append(answer)
        held = (
            apply_delta(held, answer["state_delta"])
            if "state_delta" in answer
            else answer["state"]
        )

    def act(**arguments: Any) -> None:
        record(api.encounter_act(encounter_id, viewer=viewer, view=view, **arguments))

    for index in range(rounds):
        for _ in range(len(roster())):
            if held["over"]:
                return answers
            mine, reachable = _whose_turn(held, viewer)
            if mine is None:
                _gm_takes_the_turn(encounter_id)
            else:
                if reachable:
                    act(kind="attack", target=reachable[0])
                act(
                    kind="move",
                    to_position=HOME[mine][index % 2],
                    facing="south" if index % 2 else "north",
                )
                if index == 1 and mine == VIEWER:
                    act(kind="use_item", item="Potion of Healing")
            record(api.encounter_advance(encounter_id, viewer=viewer, view=view))
        if index == 0:
            api.encounter_condition(encounter_id, VIEWER, "exhaustion", levels=2)
        if index == 2:
            api.encounter_condition(encounter_id, VIEWER, "exhaustion", applied=False)
    return answers


def _whose_turn(
    held: Mapping[str, Any], viewer: str | None
) -> tuple[str | None, list[str]]:
    """Who this caller may move, and who they can reach — from the rebuild alone.

    Reach is checked rather than assumed for the reason the squares are laid out
    the way they are: a swing at somebody fifteen feet away is refused, and a
    fixture that lets the engine refuse its actions is testing its own arithmetic
    instead of the delta.
    """
    if viewer is None:
        acting = held["turn"]
        # A reinforcement holds a place in initiative from round one and cannot
        # act until its round comes up, so "whose turn is it" and "who can act"
        # are two questions. Asking only the first is how the fixture ends up
        # driving a creature that has not arrived.
        if acting is None or not _entry(held, str(acting))["present"]:
            return None, []
        side = _team_of(held, str(acting))
        origin = _position_of(held, str(acting))
        return str(acting), sorted(
            str(one["name"])
            for one in held["combatants"]
            if one["team"] != side
            and not one["dead"]
            and one["present"]
            and _apart(origin, one["position"]) <= REACH
        )
    if not held["your_turn"]:
        return None, []
    return VIEWER, sorted(
        str(one["name"]) for one in held["enemies"] if one["distance"] <= REACH
    )


def _apart(one: list[int], other: list[int]) -> int:
    return max(abs(one[0] - other[0]), abs(one[1] - other[1]))


def _entry(snapshot: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    return next(one for one in snapshot["combatants"] if str(one["name"]) == name)


def _team_of(snapshot: Mapping[str, Any], name: str) -> str:
    return str(_entry(snapshot, name)["team"])


def _position_of(snapshot: Mapping[str, Any], name: str) -> list[int]:
    return list(_entry(snapshot, name)["position"])


def _gm_takes_the_turn(encounter_id: str) -> None:
    """Somebody the seat is not swings at somebody, so the fight is a fight.

    Nothing is recorded from this: it runs in the GM's chair, whose baseline is a
    different one, and the point is only that the seat's next answer has
    somewhere to have come from.
    """
    whole = api.encounter_state(encounter_id)
    acting = whole["turn"]
    if acting is None:
        return
    mine, reachable = _whose_turn(whole, None)
    if mine is not None and reachable:
        api.encounter_act(encounter_id, "attack", target=reachable[0], view="full")


def replayed(answers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every answer applied in turn, so the last entry is the reconstructed state."""
    held = answers[0]["state"]
    rebuilt = [held]
    for answer in answers[1:]:
        held = (
            apply_delta(held, answer["state_delta"])
            if "state_delta" in answer
            else answer["state"]
        )
        rebuilt.append(held)
    return rebuilt


def strings_in(payload: Any) -> set[str]:
    """Every string anywhere in a payload, so a leaked name cannot hide in a key."""
    found: set[str] = set()
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            found.add(str(key))
            found |= strings_in(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= strings_in(item)
    elif isinstance(payload, str):
        found.add(payload)
    return found


class TestTheViewIsDeclaredAndRefusedByName:
    def test_an_unknown_view_is_refused_with_the_three_that_work(self) -> None:
        encounter_id = str(api.encounter_create(roster(), seed=1)["encounter_id"])

        with pytest.raises(RequestError, match="view must be one of: delta, live, full"):
            api.encounter_act(encounter_id, "dodge", view="summary")

    def test_create_refuses_before_it_starts_anything(self) -> None:
        """A mistyped argument must not start a fight and then refuse it."""
        before = api.encounter_list()["encounters"]

        with pytest.raises(RequestError, match="view must be one of: delta, live, full"):
            api.encounter_create(roster(), seed=1, view="summary")

        assert api.encounter_list()["encounters"] == before


class TestWhatEachOperationDefaultsTo:
    """``create`` and ``resume`` hand back something to start from; the writes do not.

    A delta is a statement about a payload the caller already has. ``create``
    answers a fight that did not exist a moment ago and ``resume`` answers a
    caller who has just said it lost its place — neither has anything to be a
    delta against, so both default to ``full`` and *establish* the baseline the
    other two spend.
    """

    def test_create_answers_a_whole_state(self) -> None:
        created = api.encounter_create(roster(), seed=7, map=HALL)

        assert created["view"] == "full"
        assert "state" in created and "state_delta" not in created

    def test_resume_answers_a_whole_state(self) -> None:
        encounter_id = str(
            api.encounter_create(roster(), seed=7, map=HALL)["encounter_id"]
        )

        resumed = api.encounter_resume(encounter_id)

        assert resumed["view"] == "full"
        assert "state" in resumed and "state_delta" not in resumed

    def test_act_and_advance_answer_a_delta(self) -> None:
        encounter_id = str(
            api.encounter_create(roster(), seed=7, map=HALL)["encounter_id"]
        )

        acted = api.encounter_act(encounter_id, "dodge")
        advanced = api.encounter_advance(encounter_id)

        assert acted["view"] == "delta" and "state" not in acted
        assert advanced["view"] == "delta" and "state" not in advanced

    def test_a_delta_is_much_smaller_than_the_state_it_stands_for(self) -> None:
        """The claim the whole phase is for, measured rather than assumed."""
        encounter_id = str(
            api.encounter_create(roster(), seed=7, map=HALL)["encounter_id"]
        )
        api.encounter_act(encounter_id, "dodge")

        delta = api.encounter_advance(encounter_id)
        whole = api.encounter_state(encounter_id)

        assert len(json.dumps(delta["state_delta"])) * 4 < len(json.dumps(whole))


class TestDeltaEqualsFull:
    """Applying every delta in sequence reproduces the fight, byte for byte."""

    def test_the_gms_deltas_rebuild_the_state_at_every_step(self) -> None:
        answers = busy_fight()

        rebuilt = replayed(answers)

        for index, answer in enumerate(answers):
            assert canonical_sha256(rebuilt[index]) == answer["state_sha256"], (
                f"the reconstructed state diverged at step {index}"
            )

    def test_the_last_reconstruction_is_the_fight_the_engine_holds(self) -> None:
        """The end of the chain against the authority, not only against a digest."""
        answers = busy_fight()
        encounter_id = str(answers[0]["encounter_id"])

        rebuilt = replayed(answers)[-1]
        authority = api.encounter_state(encounter_id)
        authority.pop("map_source", None)

        assert rebuilt == authority

    def test_a_seats_deltas_rebuild_that_seats_brief_at_every_step(self) -> None:
        answers = busy_fight(viewer=VIEWER)

        rebuilt = replayed(answers)

        for index, answer in enumerate(answers):
            assert canonical_sha256(rebuilt[index]) == answer["state_sha256"], (
                f"the reconstructed brief diverged at step {index}"
            )

    def test_the_last_reconstruction_is_the_brief_the_engine_serves(self) -> None:
        answers = busy_fight(viewer=VIEWER)
        encounter_id = str(answers[0]["encounter_id"])

        rebuilt = replayed(answers)[-1]

        assert rebuilt == api.encounter_brief(encounter_id, VIEWER)

    def test_the_fight_this_runs_over_is_a_long_and_eventful_one(self) -> None:
        """The vacuity guard: a two-turn toy passes everything above."""
        answers = busy_fight()
        states = replayed(answers)
        moved = {
            key
            for one, other in zip(states, states[1:], strict=False)
            for key in one
            if one[key] != other.get(key)
        }
        creature_keys = {
            key
            for one, other in zip(states, states[1:], strict=False)
            for before, after in zip(
                one["combatants"], other["combatants"], strict=False
            )
            for key in before
            if before[key] != after.get(key)
        }
        latecomer = [one for one in states[-1]["combatants"] if one["name"] == LATECOMER]

        assert len(answers) > 20, f"only {len(answers)} answers were sampled"
        assert states[-1]["round"] >= 3, "the fixture never reached the arrival round"
        assert any(one["dead"] for one in states[-1]["combatants"]), (
            "nobody died, so no delta ever reported a death"
        )
        assert latecomer and latecomer[0]["present"], "the reinforcement never arrived"
        assert {"round", "turn", "combatants", "turn_state"} <= moved
        assert {"hp", "position", "conditions", "dead", "present"} <= creature_keys


class TestADeltaNeverOutrunsTheBrief:
    """The composition property, asserted directly and not only through equality.

    :class:`TestDeltaEqualsFull` already implies this — a reconstruction equal to
    the brief cannot contain what the brief does not. It is asserted again, and
    by another route, because that implication is the kind that quietly stops
    holding when somebody changes what a delta is against.
    """

    def test_a_seat_is_never_told_a_name_its_brief_withholds(self) -> None:
        answers = busy_fight(viewer=VIEWER)
        encounter_id = str(answers[0]["encounter_id"])
        cast = {VIEWER, ALLY, FOE, MOOK, LATECOMER}
        visible = strings_in(api.encounter_brief(encounter_id, VIEWER))

        for index, answer in enumerate(answers):
            leaked = (strings_in(answer.get("state_delta", {})) & cast) - visible
            assert not leaked, (
                f"step {index} named {sorted(leaked)} to {VIEWER}, and the brief "
                f"beside it does not"
            )

    def test_an_unarrived_creature_is_absent_from_every_answer_before_it_arrives(
        self,
    ) -> None:
        answers = busy_fight(viewer=VIEWER, rounds=1)

        for index, answer in enumerate(answers):
            assert LATECOMER not in json.dumps(answer), (
                f"step {index} named the reinforcement before it arrived"
            )

    def test_a_delta_never_carries_a_key_an_enemys_brief_withholds(self) -> None:
        answers = busy_fight(viewer=VIEWER)
        allowed = set(model.ENEMY_VISIBLE_KEYS) | {"distance", "health"}

        served: set[str] = set()
        for answer in answers:
            for entry in answer.get("state_delta", {}).get("enemies", []):
                served |= set(entry)

        assert served, "no delta in this fight reported an enemy at all"
        assert served <= allowed, sorted(served - allowed)
        assert not served & (set(model.LIVE_KEYS) - set(model.ENEMY_VISIBLE_KEYS)), (
            "a live key the brief withholds reached the delta"
        )

    def test_a_creature_behind_total_cover_is_named_in_no_delta(self) -> None:
        """The relationship a snapshot cannot carry, run through the view.

        The brief omits a sealed-in foe entirely, so nothing the view does
        afterwards can put it back — and the delta cannot even say a creature it
        may not name has changed, because it never saw one.
        """
        walled = str(
            api.encounter_create(
                [
                    {**hero_spec(), "position": [0, 0]},
                    {**foe_spec(), "position": [25, 25]},
                ],
                seed=41,
                map=VAULT,
                viewer=VIEWER,
            )["encounter_id"]
        )

        answers = [
            api.encounter_act(walled, "dodge", viewer=VIEWER),
            api.encounter_advance(walled, viewer=VIEWER),
            api.encounter_advance(walled, viewer=VIEWER),
        ]

        assert any("state_delta" in one for one in answers), "nothing was deltaed"
        for index, answer in enumerate(answers):
            assert FOE not in json.dumps(answer), f"step {index} named the sealed foe"

    def test_a_seats_delta_never_repeats_what_that_seat_already_holds(self) -> None:
        """The definition, asserted — and it is what proves *which* baseline was used.

        Every reconstruction case above is satisfied by a delta that carries the
        whole payload, so on their own they cannot tell a diff against this
        seat's last brief from a diff against something else entirely. A
        wrong-baseline delta still rebuilds correctly whenever it re-sends more
        than it had to, and a diff against a payload of another shape re-sends
        nearly everything. That is what a mutation collapsing the per-seat
        baseline into one baseline per fight actually did, and every case in this
        class stayed green against it.

        So assert the definition instead of a consequence of it. A delta may
        mention a key only when that key's value is not the one the caller is
        already holding; anything else is the server describing a change that,
        from this seat, did not happen. Rosters and the seat's own entry are
        excluded because they are thinned rather than compared whole — the cases
        above own those — which leaves exactly the flat keys, and those are the
        ones that expose a diff taken against the wrong payload.

        Economy rides along as a second, weaker claim: whatever the keys, an
        answer that stands in for a brief has to be a fraction of its size, or
        the phase bought nothing.
        """
        answers = busy_fight(viewer=VIEWER)
        thinned = set(model.BRIEF_ROSTERS) | set(model.BRIEF_ENTRIES) | {"dropped"}
        held: dict[str, Any] = answers[0]["state"]
        repeated: list[str] = []
        sizes: list[tuple[int, int]] = []

        for answer in answers[1:]:
            if "state_delta" not in answer:
                held = answer["state"]
                continue
            delta = answer["state_delta"]
            repeated += [
                key
                for key, value in delta.items()
                if key not in thinned and held.get(key) == value
            ]
            sizes.append((len(json.dumps(delta)), len(json.dumps(held))))
            held = apply_delta(held, delta)

        assert sizes, "nothing in this fight was answered as a delta"
        assert not repeated, (
            f"the delta re-sent what this seat already held: {sorted(set(repeated))}"
        )
        assert max(size for size, _ in sizes) * 2 < min(whole for _, whole in sizes)

    def test_a_seats_resume_does_not_move_the_gms_baseline(self) -> None:
        """A baseline is what a chair *was served*, and only that.

        Found by rebuilding this module after losing it: ``resume`` assembled its
        payload by calling :func:`state_of`, which is the body of
        ``encounter.state`` and re-anchors the GM. So a seat resuming silently
        re-anchored the GM to a snapshot the GM had never been sent, and the GM's
        next delta was measured from a payload it was not holding. Nothing in the
        seat-side cases could see it: they never look at the other chair.

        The sequence is the one that makes it visible — the fight has to move
        between the GM's last answer and the seat's resume, or the wrong baseline
        and the right one are the same bytes.
        """
        created = api.encounter_create(roster(), seed=20260806, map=HALL, view="full")
        encounter_id = str(created["encounter_id"])
        gm_holds: dict[str, Any] = created["state"]

        # The fight moves with nobody served: this is not a payload any chair holds.
        api.encounter_condition(encounter_id, VIEWER, "exhaustion", levels=2)
        # A seat reads itself back in. The GM was not part of this exchange.
        api.encounter_resume(encounter_id, viewer=VIEWER)

        answer = api.encounter_advance(encounter_id)
        rebuilt = apply_delta(gm_holds, answer["state_delta"])

        assert answer["view"] == views.DELTA
        assert canonical_sha256(rebuilt) == answer["state_sha256"]
        # ``encounter.state`` staples ``map_source``; no write's payload carries it.
        live = api.encounter_state(encounter_id)
        assert rebuilt == {key: value for key, value in live.items() if key != "map_source"}

    def test_one_seats_baseline_is_not_another_seats(self) -> None:
        """Two chairs, two baselines, and no diff across the two.

        One baseline per fight would diff a player's brief against the GM's
        snapshot the first time both used the same encounter — which would break
        the reconstruction *and* put the GM's keys in the player's delta.
        """
        encounter_id = str(
            api.encounter_create(roster(), seed=7, map=HALL)["encounter_id"]
        )
        seat = api.encounter_resume(encounter_id, viewer=VIEWER)

        api.encounter_act(encounter_id, "dodge")
        seat_delta = api.encounter_advance(encounter_id, viewer=VIEWER)

        rebuilt = apply_delta(seat["state"], seat_delta["state_delta"])
        assert canonical_sha256(rebuilt) == seat_delta["state_sha256"]
        assert rebuilt == api.encounter_brief(encounter_id, VIEWER)


class TestArrivalDeathAndDeparture:
    """A patch that can only say "changed" cannot say "gone". This one can."""

    def test_a_roster_in_a_delta_is_the_complete_cast_in_order(self) -> None:
        answers = busy_fight()
        whole = api.encounter_state(str(answers[0]["encounter_id"]))

        rosters = [
            answer["state_delta"]["combatants"]
            for answer in answers
            if "combatants" in answer.get("state_delta", {})
        ]

        assert rosters, "no delta in this fight reported the roster at all"
        for entries in rosters:
            assert [str(one["name"]) for one in entries] == [
                str(one["name"]) for one in whole["combatants"]
            ]

    def test_a_creature_that_leaves_the_brief_leaves_the_receivers_copy(self) -> None:
        """Membership is absolute, so a name dropped from the list is dropped.

        Driven against the diff directly: arranging total cover to *appear*
        mid-fight needs a door a seat can shut on itself, and the property being
        checked is a property of the format rather than of the map.
        """
        before = {"combatants": [{"name": FOE, "hp": 5}, {"name": MOOK, "hp": 2}]}
        after = {"combatants": [{"name": FOE, "hp": 5}]}

        delta = model.state_delta(before, after, rosters=("combatants",))

        assert apply_delta(before, delta) == after
        assert MOOK not in json.dumps(delta)

    def test_a_creature_that_enters_the_brief_arrives_whole(self) -> None:
        before = {"combatants": [{"name": FOE, "hp": 5}]}
        after = {"combatants": [{"name": FOE, "hp": 5}, {"name": MOOK, "hp": 2}]}

        delta = model.state_delta(before, after, rosters=("combatants",))

        assert apply_delta(before, delta) == after
        assert {"name": MOOK, "hp": 2} in delta["combatants"]

    def test_a_roster_nothing_moved_in_is_not_sent_at_all(self) -> None:
        same: dict[str, Any] = {"round": 1, "combatants": [{"name": FOE, "hp": 5}]}

        assert model.state_delta(same, dict(same), rosters=("combatants",)) == {}

    def test_a_key_the_baseline_had_and_this_payload_does_not_is_named_gone(
        self,
    ) -> None:
        """The one thing omission cannot express, so it is said out loud.

        Omission already means "unchanged", so ``dropped`` is what carries
        "gone". The brief's ``turn_state`` really is conditional — it is served
        only on the seat's own turn — and a creature's key set is stable today
        only because ``level`` follows a map that does not come and go.
        """
        before = {
            "turn_state": {"movement_left": 30},
            "combatants": [{"name": FOE, "hp": 5, "level": 0}],
        }
        after = {"combatants": [{"name": FOE, "hp": 5}]}

        delta = model.state_delta(before, after, rosters=("combatants",))

        assert delta["dropped"] == [f"combatants/{FOE}/level", "turn_state"]
        assert apply_delta(before, delta) == after

    def test_a_seats_conditional_turn_state_comes_and_goes_over_a_real_fight(
        self,
    ) -> None:
        """The same property end to end, since the unit case builds its own payloads."""
        answers = busy_fight(viewer=VIEWER)

        dropped = {
            path
            for answer in answers
            for path in answer.get("state_delta", {}).get("dropped", [])
        }

        assert "turn_state" in dropped, (
            "the seat's turn ended without the brief's conditional key being dropped"
        )

    def test_nothing_carries_an_empty_dropped_list(self) -> None:
        answers = busy_fight()

        assert not any(
            answer.get("state_delta", {}).get("dropped") == [] for answer in answers
        )


class TestTheLiveView:
    """Every combatant, no sheet, and a digest saying the sheet still holds."""

    def test_a_live_entry_carries_the_live_half_and_a_sheet_digest(self) -> None:
        encounter_id = str(
            api.encounter_create(roster(), seed=7, map=HALL)["encounter_id"]
        )
        whole = api.encounter_state(encounter_id)

        answer = api.encounter_act(encounter_id, "dodge", view="live")

        assert answer["view"] == "live"
        served = {str(one["name"]): one for one in answer["state_live"]["combatants"]}
        for entry in whole["combatants"]:
            name = str(entry["name"])
            assert set(served[name]) == set(model.live_of(entry)) | {
                "name",
                "sheet_sha256",
            }
            assert served[name]["sheet_sha256"] == sheet_sha256(entry)

    def test_a_caller_holding_the_sheets_rebuilds_the_whole_state(self) -> None:
        encounter_id = str(
            api.encounter_create(roster(), seed=7, map=HALL)["encounter_id"]
        )
        opening = api.encounter_state(encounter_id)
        opening.pop("map_source", None)
        sheets = {str(one["name"]): model.sheet_of(one) for one in opening["combatants"]}

        answer = api.encounter_act(encounter_id, "dodge", view="live")

        rebuilt = dict(answer["state_live"])
        rebuilt["combatants"] = [
            sheets[str(one["name"])]
            | {key: value for key, value in one.items() if key != "sheet_sha256"}
            for one in answer["state_live"]["combatants"]
        ]
        assert canonical_sha256(rebuilt) == answer["state_sha256"]

    def test_the_live_view_needs_no_baseline_at_all(self) -> None:
        """The reason it exists beside ``delta``: it never falls back."""
        encounter_id = str(
            api.encounter_create(roster(), seed=7, map=HALL)["encounter_id"]
        )
        forget_baselines(encounter_id)

        answer = api.encounter_act(encounter_id, "dodge", view="live")

        assert answer["view"] == "live"
        assert "state_live" in answer

    def test_a_seats_live_view_is_still_that_seats_brief(self) -> None:
        """``as=`` first here too, and by the same one line that does it for delta."""
        encounter_id = str(
            api.encounter_create(roster(), seed=7, map=HALL, viewer=VIEWER)[
                "encounter_id"
            ]
        )

        answer = api.encounter_act(encounter_id, "dodge", viewer=VIEWER, view="live")

        assert answer["state_live"]["as"] == VIEWER
        assert LATECOMER not in json.dumps(answer)
        for entry in answer["state_live"]["enemies"]:
            assert set(entry) <= set(model.ENEMY_VISIBLE_KEYS) | {
                "name",
                "distance",
                "health",
                "sheet_sha256",
            }


class TestWhenThereIsNothingToDeltaAgainst:
    """Asking for a delta the server cannot compute gets the full answer, said so.

    Every server on a host reaches the same journals, so a fight can be recovered
    by a process that never served this caller anything. There is nothing to diff
    against and no way to know it — which is exactly why the answer names the
    view it actually served rather than the one that was asked for.
    """

    def test_a_server_holding_no_baseline_answers_full_and_says_so(self) -> None:
        encounter_id = str(
            api.encounter_create(roster(), seed=7, map=HALL)["encounter_id"]
        )
        forget_baselines(encounter_id)

        answer = api.encounter_act(encounter_id, "dodge")

        assert answer["view"] == "full"
        assert "state" in answer and "state_delta" not in answer

    def test_an_idempotent_retry_is_answered_whole(self) -> None:
        """A retry is a caller saying it did not hear the answer.

        Serving that caller a delta would diff against the payload it has just
        told us it may not have.
        """
        encounter_id = str(
            api.encounter_create(roster(), seed=7, map=HALL)["encounter_id"]
        )
        api.encounter_act(encounter_id, "dodge", request_id="once")

        retry = api.encounter_act(encounter_id, "dodge", request_id="once")

        assert retry["view"] == "full"
        assert "state" in retry and "state_delta" not in retry

    def test_a_refetch_puts_a_drifted_caller_back_in_step(self) -> None:
        """The read re-anchors the baseline, which is what makes recovery work.

        Without it the caller refetches, the server keeps diffing against what it
        sent *before* the refetch, and a value that moved away and back between
        the two is missing from every later delta — so the caller drifts again on
        the same key, forever.
        """
        encounter_id = str(
            api.encounter_create(roster(), seed=7, map=HALL)["encounter_id"]
        )
        api.encounter_act(encounter_id, "dodge")

        held = api.encounter_state(encounter_id)
        later = api.encounter_advance(encounter_id)

        assert (
            canonical_sha256(apply_delta(held, later["state_delta"]))
            == later["state_sha256"]
        )


class TestEventsAreNotDeltable:
    """Events flow whole on every view, and that is a decision rather than a gap.

    An event is not a value that changed, it is a thing that happened: there is
    no previous event to diff a new one against, and the sequence is the payload.
    ``detail`` is already dropped outright under ``as=`` because prose cannot be
    allowlisted, and a diff of prose would be worse than either.
    """

    @pytest.mark.parametrize("view", ["delta", "live", "full"])
    def test_every_view_answers_with_the_same_events(self, view: str) -> None:
        encounter_id = str(
            api.encounter_create(roster(), seed=7, map=HALL)["encounter_id"]
        )
        api.encounter_act(encounter_id, "dodge")

        answer = api.encounter_advance(encounter_id, view=view)

        assert [one["kind"] for one in answer["events"]] == ["turn_end", "turn_start"]
        assert "events_delta" not in answer

    def test_a_seats_events_are_still_narrowed_on_a_delta(self) -> None:
        """The view changed the state's size and nothing about the event filter."""
        answers = busy_fight(viewer=VIEWER)

        served = [event for answer in answers for event in answer.get("events", [])]

        assert served, "no events were served"
        for event in served:
            assert "detail" not in event, "prose reached a seat beside its delta"


class TestTheDigestIsOverWhatFullWouldHaveAnswered:
    """One rule for the digest, whichever view and whichever chair asked."""

    def test_the_gms_digest_is_over_the_state(self) -> None:
        encounter_id = str(
            api.encounter_create(roster(), seed=7, map=HALL)["encounter_id"]
        )

        answer = api.encounter_act(encounter_id, "dodge", view="full")

        assert answer["state_sha256"] == canonical_sha256(answer["state"])

    def test_a_seats_digest_is_over_that_seats_brief(self) -> None:
        """Otherwise the one case with no drift detection is the delicate one."""
        encounter_id = str(
            api.encounter_create(roster(), seed=7, map=HALL, viewer=VIEWER)[
                "encounter_id"
            ]
        )

        answer = api.encounter_act(encounter_id, "dodge", viewer=VIEWER, view="full")

        assert answer["state"]["as"] == VIEWER
        assert answer["state_sha256"] == canonical_sha256(answer["state"])

    def test_a_caller_that_applied_a_stale_delta_can_tell(self) -> None:
        """The safety net, exercised rather than described.

        The caller drops one answer on the floor and applies the next delta to
        what it still holds. The result is wrong, and the digest is what says so
        — cheaply, without a second call to find out.
        """
        created = api.encounter_create(roster(), seed=7, map=HALL)
        encounter_id = str(created["encounter_id"])
        held = created["state"]

        api.encounter_act(encounter_id, "dodge")
        later = api.encounter_advance(encounter_id)

        rebuilt = apply_delta(held, later["state_delta"])
        assert canonical_sha256(rebuilt) != later["state_sha256"]


class TestTheRouteTableAndTheServiceAgreeOnTheViews:
    """``web/`` may not import ``service/``, so the two declarations are held equal."""

    def test_the_three_views_are_declared_once_and_mirrored(self) -> None:
        assert tuple(routes._VIEWS) == views.VIEWS

    def test_each_route_declares_the_default_its_operation_uses(self) -> None:
        expected = {
            "encounter.create": views.FULL,
            "encounter.act": views.DELTA,
            "encounter.advance": views.DELTA,
            "encounter.resume": views.FULL,
        }

        declared = {
            route.operation: param.schema["default"]
            for route in routes.ROUTES
            for param in route.params
            if param.name == "view"
        }

        assert declared == expected
