"""Sheet and live: which of a combatant's reported keys a fight can move.

``Encounter._creature_state`` emits one flat dictionary per combatant and every
key of it is re-sent on every response. Most of those keys cannot change while
the fight runs — a stat block's AC, its senses, the names of its attacks — so
re-sending them is bandwidth and nothing else. :data:`SHEET_KEYS` and
:data:`LIVE_KEYS` say which is which.

**This is a bandwidth claim, never a rules claim.** Nothing here decides what a
creature may do or what anybody may see; it decides only what a later response
has to repeat. That is why it is a second classification of the same keys rather
than a replacement for the brief's: ``ENEMY_VISIBLE_KEYS`` /
``ENEMY_WITHHELD_KEYS`` answers *may this seat see it*, and this pair answers
*can the fight move it*. A key is classified twice because there are two
questions, and ``tests/test_player_brief.py`` derives its pair from the model
exactly as this file derives this one — neither imports the other's constants,
because two independent derivations of one payload is the point.

**A wrong classification must be loud, not silent.** Declaring a field static
that the fight in fact moves would, once a delta response exists, serve a caller
a stale value forever with nothing failing. Two things stop that, and only the
second is a guarantee:

* :class:`TestNoSheetFieldMovesInAFight` drives a fight through every mutator
  this roster can reach and asserts no sheet digest moved. It is a real check
  and it is not a proof — it covers the fights it runs.
* :func:`~fivee_sim.service.replay.sheet_sha256` is the proof. It is computed
  over the sheet *as serialised*, freshly, every time. If a declared-static
  field ever moves, the digest moves with it and a caller holding the old one
  refetches. Correctness therefore does not depend on the classification being
  right — only bandwidth does.

The digest is **per combatant** rather than one over the whole roster, and that
choice is load-bearing rather than incidental: a roster digest can always be
derived from the per-combatant ones, and per-combatant ones can never be
recovered from a roster digest. One creature whose sheet moved must not make the
other five refetch.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from itertools import combinations
from random import Random
from typing import Any

from fivee_sim.model import encounter as model
from fivee_sim.model.encounter import Encounter
from fivee_sim.service import sessions
from fivee_sim.service.replay import canonical_sha256, sheet_sha256

from . import api
from .conftest import fighter

FIXTURE = "synthetic test fixture, not SRD content"

HERO = "Thora"
FOE = "Grelk"
LATECOMER = "Zzaxil"

#: Room enough for the roster to stand apart and for the hero to step off its
#: square and back. Feet, like every position on the wire; the walls are the
#: border row and column.
ROOM: dict[str, Any] = {
    "name": "sparring floor",
    "width": 10,
    "height": 6,
    "rows": [
        "##########",
        "#........#",
        "#........#",
        "#........#",
        "#........#",
        "##########",
    ],
    "legend": {"#": "wall", ".": "normal"},
}

#: Where the hero stands — within a longsword's reach of the foe — and where it
#: retreats to, which is deliberately *out* of that reach: leaving it provokes
#: the foe's Opportunity Attack, and that is the only thing in this script that
#: spends a reaction.
HOME = [5, 5]
AWAY = [5, 20]


def hero_spec() -> dict[str, Any]:
    """The seat that spends things: slots, charges, movement, a facing.

    Hit points large enough that three rounds of being hit cannot end the fight
    — a fight that ends stops the driver below mid-script, and a short sample is
    the one way this file could pass by not looking.
    """
    return {
        "name": HERO,
        "team": "party",
        "ac": 16,
        "max_hp": 300,
        "speed": 30,
        "darkvision": 60,
        "facing": "north",
        "bonus_actions": ["dash"],
        "spells": ["Guiding Bolt"],
        "spell_slots": {"1": 3},
        "spell_attack_bonus": 6,
        "spell_save_dc": 14,
        "items": {"Potion of Healing": 2},
        "attacks": [
            {
                "name": "Longsword",
                "attack_bonus": 5,
                "damage": "1d8+3",
                "damage_type": "slashing",
                "kind": "melee",
                "provenance": FIXTURE,
            }
        ],
        "position": HOME,
        "provenance": FIXTURE,
    }


def foe_spec() -> dict[str, Any]:
    return {
        "name": FOE,
        "team": "monsters",
        "ac": 12,
        "max_hp": 300,
        "attacks": [
            {
                "name": "Rustcleaver",
                "attack_bonus": 6,
                "damage": "2d6+4",
                "damage_type": "slashing",
                "kind": "melee",
                "provenance": FIXTURE,
            }
        ],
        "position": [10, 5],
        "provenance": FIXTURE,
    }


def latecomer_spec() -> dict[str, Any]:
    """Absent when the fight opens, so ``present`` has somewhere to move."""
    return {
        "name": LATECOMER,
        "team": "monsters",
        "ac": 12,
        "max_hp": 40,
        "position": [20, 20],
        "arrival_round": 2,
        "provenance": FIXTURE,
    }


def fight(seed: int = 20260806) -> str:
    created = api.encounter_create(
        [hero_spec(), foe_spec(), latecomer_spec()], seed=seed, map=ROOM
    )
    return str(created["encounter_id"])


def by_name(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(entry["name"]): dict(entry) for entry in snapshot["combatants"]}


def emitted_creature_keys() -> set[str]:
    """Every key a combatant entry can carry, off a fight that reaches them all.

    Held against a real payload rather than a list written into this file, for
    ``test_player_brief``'s reason: a literal set pins the model against a copy
    of itself and both halves get edited in the same commit.
    """
    return {key for entry in by_name(api.encounter_state(fight())).values() for key in entry}


def busy_fight() -> list[dict[str, dict[str, Any]]]:
    """One fight, sampled after every operation, driven through what it can reach.

    Every combatant's entry after each step, so a sheet key that moves once in
    the middle of round two is as visible as one that moves at the start. The
    script spends a slot, drinks a charge, steps off a square and back, takes
    and deals damage, gains a condition, dodges, and lets a latecomer arrive.
    """
    encounter_id = fight()
    seen = [by_name(api.encounter_state(encounter_id))]

    def step(run: Callable[[], Any]) -> None:
        run()
        seen.append(by_name(api.encounter_state(encounter_id)))

    # Speed is reported after a held condition's reduction, so this moves the
    # ``speeds`` block without touching a single printed Speed. See
    # :meth:`Creature.speed_for`.
    step(lambda: api.encounter_condition(encounter_id, HERO, "exhaustion", levels=2))
    step(lambda: api.encounter_condition(encounter_id, FOE, "prone"))

    # The action first and the move second, in that order: round one swings from
    # HOME, where the foe is in reach, and only then walks out of it. A melee
    # attack after the retreat would have nothing to reach.
    spends: list[Callable[[], Any]] = [
        lambda: api.encounter_act(encounter_id, "attack", target=FOE),
        lambda: api.encounter_act(encounter_id, "use_item", item="Potion of Healing"),
        lambda: api.encounter_act(
            encounter_id, "cast", spell="Guiding Bolt", slot_level=1, target=FOE
        ),
    ]
    for round_index in range(len(spends)):
        for _ in range(len(seen[0])):
            turn = api.encounter_state(encounter_id)["turn"]
            if turn == HERO:
                step(spends[round_index])
                destination = AWAY if round_index % 2 == 0 else HOME
                step(
                    lambda where=destination: api.encounter_act(  # type: ignore[misc]
                        encounter_id, "move", to_position=where, facing="south"
                    )
                )
            elif turn == FOE:
                step(lambda: api.encounter_act(encounter_id, "dodge"))
            step(lambda: api.encounter_advance(encounter_id))
    return seen


def digests(entries: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    return {name: sheet_sha256(entry) for name, entry in entries.items()}


class TestTheSplitIsTotal:
    """Every key lands in exactly one bucket, or the suite says who has to decide.

    Two frozen sets rather than one filter, for the reason the brief's pair uses
    two: an allowlist alone answers "is this static?" and not "has anybody
    looked at this?" A new field would default to live, which is the safe
    direction and also indistinguishable from a field somebody considered and
    called live.
    """

    def test_every_creature_field_the_model_emits_is_classified_exactly_once(
        self,
    ) -> None:
        emitted = emitted_creature_keys()

        buckets = {
            "SHEET_KEYS": model.SHEET_KEYS,
            "LIVE_KEYS": model.LIVE_KEYS,
        }
        unclassified = sorted(emitted - set().union(*buckets.values()))
        assert not unclassified, (
            "Encounter._creature_state emits these and nobody has decided "
            "whether the fight can move them, so a delta response cannot know "
            "whether to repeat them. Put each in exactly one of SHEET_KEYS "
            "(nothing in the fight writes it) or LIVE_KEYS (something does — "
            "and when you cannot establish which, it is this one): "
            + ", ".join(unclassified)
        )
        for (left, one), (right, other) in combinations(buckets.items(), 2):
            assert not one & other, (
                f"{left} and {right} both claim "
                f"{', '.join(sorted(one & other))}; a field has one answer"
            )

    def test_neither_bucket_declares_a_key_the_model_never_emits(self) -> None:
        """The other direction, which a covering check alone does not give.

        A key renamed out of ``_creature_state`` leaves its classification
        behind, and a stale entry in either set reads exactly like a decision
        somebody made about a field that still exists.
        """
        emitted = emitted_creature_keys()

        stale = sorted((model.SHEET_KEYS | model.LIVE_KEYS) - emitted)
        assert not stale, (
            "SHEET_KEYS or LIVE_KEYS classifies these and "
            "Encounter._creature_state emits none of them; either the fixture "
            "stopped reaching them or the field is gone: " + ", ".join(stale)
        )

    def test_the_fixture_reaches_the_fields_the_model_only_sometimes_emits(
        self,
    ) -> None:
        """The vacuity guard, and it is the same one the brief needed.

        ``level`` and ``elevation`` appear only on a battle map, so a mapless
        roster would let both cases above pass while two real fields stayed
        undecided. ``facing`` is here for the opposite reason: it used to be
        conditional too, and this is what pins that it no longer is.
        """
        emitted = emitted_creature_keys()

        assert {"facing", "level", "elevation"} <= emitted
        assert len(emitted) > 25, (
            f"only {len(emitted)} creature keys were sampled; the derivation "
            f"has stopped reading a real payload"
        )

    def test_an_unclassified_key_falls_to_the_live_half(self) -> None:
        """The safe direction, built in rather than remembered.

        ``live_of`` is the complement of ``SHEET_KEYS`` over the payload's own
        keys, not a second allowlist — so a field added to ``_creature_state``
        and classified by nobody is re-sent every turn. That costs bandwidth,
        which is what this split is for; the alternative is dropping it, which
        would be a correctness bug wearing an optimisation's clothes.
        """
        entry = {"name": HERO, "hp": 3, "invented_yesterday": 7}

        assert model.live_of(entry)["invented_yesterday"] == 7
        assert "invented_yesterday" not in model.sheet_of(entry)

    def test_the_two_halves_reassemble_the_payload(self) -> None:
        """No key is lost between them, for any entry the model actually emits."""
        for entry in by_name(api.encounter_state(fight())).values():
            assert model.sheet_of(entry) | model.live_of(entry) == entry
            assert not set(model.sheet_of(entry)) & set(model.live_of(entry))


class TestNoSheetFieldMovesInAFight:
    """The classification held against a fight rather than against a reading.

    This is a check, not a proof — it covers the fight it runs. The proof that a
    wrong answer here cannot corrupt a caller is the digest, and that lives in
    :class:`TestTheDigestMakesAWrongClassificationLoud`.
    """

    def test_a_combatants_sheet_digest_holds_still_across_a_whole_fight(
        self,
    ) -> None:
        seen = busy_fight()
        opening = digests(seen[0])

        for index, entries in enumerate(seen):
            for name, entry in entries.items():
                changed = sorted(
                    key for key, value in model.sheet_of(entry).items()
                    if model.sheet_of(seen[0][name]).get(key) != value
                )
                assert sheet_sha256(entry) == opening[name], (
                    f"{name}'s sheet moved at step {index}: "
                    f"{', '.join(changed)} changed. Either the fight writes it "
                    f"— in which case it belongs in LIVE_KEYS — or something "
                    f"writes it that should not."
                )

    def test_the_fight_that_holds_them_still_moved_plenty(self) -> None:
        """The vacuity guard for the case above: a fight that did nothing passes it.

        Named live keys rather than a count, because a count would be satisfied
        by twenty steps of the same creature's hit points. Each of these is a
        different writer in ``model/encounter.py``, and ``speeds`` is the one
        that surprises: no printed Speed is ever assigned, but a held
        condition's reduction is folded into what gets reported.
        """
        seen = busy_fight()
        moved = {
            key
            for name in seen[0]
            for entry in seen
            for key, value in entry[name].items()
            if seen[0][name].get(key) != value
        }

        assert len(seen) > 15, f"only {len(seen)} steps were sampled"
        assert {
            "hp", "position", "facing", "conditions", "speeds", "present",
            "spell_slots", "items", "dodging", "reaction_available",
        } <= moved, sorted(moved)
        assert moved <= model.LIVE_KEYS

    def test_the_speeds_a_combatant_reports_are_live_because_a_condition_moves_them(
        self,
    ) -> None:
        """The one key a reading of the dataclass gets wrong.

        ``speed``, ``climb_speed`` and the rest are never assigned anywhere in
        the engine, so the fields look static. What the payload carries is
        ``Creature.speed_for``, which subtracts the held conditions' reduction —
        so two levels of Exhaustion move a block whose inputs never moved.
        """
        encounter_id = fight()
        before = by_name(api.encounter_state(encounter_id))[HERO]["speeds"]

        api.encounter_condition(encounter_id, HERO, "exhaustion", levels=2)
        after = by_name(api.encounter_state(encounter_id))[HERO]["speeds"]

        assert before["walk"] == 30
        assert after["walk"] == 20
        assert "speeds" in model.LIVE_KEYS


class TestTheDigestMakesAWrongClassificationLoud:
    """Why a wrong answer above costs bandwidth and never correctness."""

    def test_a_sheet_digest_moves_when_a_declared_static_field_moves(self) -> None:
        """The whole safety argument, in one case.

        ``ac`` is declared static because nothing in this engine assigns it.
        Reached around the outside here on purpose: the point is not that the
        engine can change it, it is that *if* anything ever does — a new effect,
        a pack, a defect — the digest moves and a caller holding the old one
        refetches rather than believing it.
        """
        encounter_id = fight()
        session = sessions.session_for(api.STATE, encounter_id)
        before = sheet_sha256(by_name(api.encounter_state(encounter_id))[HERO])

        session.encounter.creatures[HERO].ac = 4093
        after = sheet_sha256(by_name(api.encounter_state(encounter_id))[HERO])

        assert before != after

    def test_one_combatants_sheet_moving_leaves_the_others_alone(self) -> None:
        """Why the digest is per combatant and not one over the roster.

        A roster-wide digest would answer "something moved" and send a caller
        back for all of them. Per combatant, a caller refetches the one.
        """
        encounter_id = fight()
        session = sessions.session_for(api.STATE, encounter_id)
        before = digests(by_name(api.encounter_state(encounter_id)))

        session.encounter.creatures[HERO].ac = 4093
        after = digests(by_name(api.encounter_state(encounter_id)))

        assert after[HERO] != before[HERO]
        assert {name: value for name, value in after.items() if name != HERO} == {
            name: value for name, value in before.items() if name != HERO
        }

    def test_a_caller_can_recompute_the_digest_from_what_it_received(self) -> None:
        """Computed over the sheet *as serialised*, so the recipe is public.

        Recomputed here from the constants rather than by calling ``sheet_of``,
        because a caller holding the payload and the two key sets is exactly
        what has to be able to check it.
        """
        for entry in by_name(api.encounter_state(fight())).values():
            expected = canonical_sha256(
                {key: value for key, value in entry.items() if key in model.SHEET_KEYS}
            )

            assert sheet_sha256(entry) == expected

    def test_two_combatants_with_different_sheets_have_different_digests(self) -> None:
        entries = by_name(api.encounter_state(fight()))

        assert len({sheet_sha256(entry) for entry in entries.values()}) == len(entries)


class TestFacingIsUnconditional:
    """The one key whose *presence* used to depend on the fight.

    A delta cannot express an absent key: absent and null are the same edit when
    the receiver is applying a patch, so a combatant whose facing was cleared
    and one whose facing was never tracked would arrive identical. Reporting
    ``None`` costs one key per combatant and makes the two distinguishable.
    """

    def test_every_combatant_reports_a_facing_even_when_nobody_set_one(self) -> None:
        entries = by_name(api.encounter_state(fight()))

        missing = sorted(name for name, entry in entries.items() if "facing" not in entry)
        assert not missing, (
            "these combatants report no facing at all, so a delta cannot tell a "
            "cleared facing from an untracked one: " + ", ".join(missing)
        )
        assert entries[HERO]["facing"] == "north"
        assert entries[FOE]["facing"] is None

    def test_the_model_reports_one_without_a_map_or_a_service_call(self) -> None:
        """Held against the model directly, since the payload is the model's.

        A mapless fight built here rather than through ``api``: the key is
        unconditional in ``_creature_state``, not made unconditional by anything
        a service module does on the way out.
        """
        fight_without_ground = Encounter(
            [fighter(HERO, position=(0, 0)), fighter(FOE, team="monsters", position=(15, 15))],
            Random(1),
        )

        for entry in fight_without_ground.state()["combatants"]:
            assert entry["facing"] is None
            assert "level" not in entry
