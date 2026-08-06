"""A face the caller rolled, carried from the request down to the resolution.

The kernel's own cases live in ``test_kernel.py``. What is asserted here is the
*thread*: that a face reported over the API reaches the roll it was meant for,
that a fight remembers it, and that the boundary refuses what it should.

**The replay case is the load-bearing one.** A supplied face is an input to the
fight rather than something derived from the seed, so a journal that does not
record it replays as a different fight — and every other test here would still
pass. ``encounters.act`` builds its journalled ``arguments`` by hand, which is
the same hand-written list that dropped ``to_level`` once and
``movement_mode``/``as_bonus_action`` once more.
"""

from __future__ import annotations

from typing import Any

import pytest

from fivee_sim.service.errors import RequestError

from . import api
from .conftest import REPLAY_GOBLIN, REPLAY_HERO, advance_encounter_to


def _fight(seed: int = 41) -> str:
    """Thora in reach of the goblin, wound forward to her turn.

    The shared fixture stands them 10 ft apart, which a Longsword cannot cross —
    every case here is about the *roll*, so the swing has to be legal first.
    """
    goblin = dict(REPLAY_GOBLIN) | {"position": [10, 5]}
    encounter_id = str(
        api.encounter_create([dict(REPLAY_HERO), goblin], seed=seed)["encounter_id"]
    )
    advance_encounter_to(encounter_id, "Thora")
    return encounter_id


def _attack_event(result: dict[str, Any]) -> dict[str, Any]:
    return next(event for event in result["events"] if event["kind"] == "attack")


class TestTheFaceReachesTheRoll:
    # Two unrelated seeds for each case, so none of them can pass by the draw
    # happening to agree with the reported face.
    SEEDS = [41, 977]

    @pytest.mark.parametrize("seed", SEEDS)
    def test_a_supplied_twenty_crits_whatever_the_seed_would_have_rolled(
        self, seed: int
    ) -> None:
        result = api.encounter_act(
            _fight(seed), "attack", target="Goblin", attack="Longsword", natural=20
        )
        event = _attack_event(result)
        # The face itself, not only its consequence. A drawn roll crits 1 time in
        # 20, so `critical` alone would let an engine that ignored the reported
        # face pass here about one run in four hundred.
        assert event["data"]["natural"] == 20
        assert event["data"]["critical"] is True

    @pytest.mark.parametrize("seed", SEEDS)
    def test_a_supplied_one_misses_whatever_the_seed_would_have_rolled(
        self, seed: int
    ) -> None:
        result = api.encounter_act(
            _fight(seed), "attack", target="Goblin", attack="Longsword", natural=1
        )
        event = _attack_event(result)
        # `hit is False` on its own is a weak oracle here — +5 against AC 15
        # misses on any drawn roll under 10, so it would pass more often than
        # not against an engine that never read the face at all.
        assert event["data"]["natural"] == 1
        assert event["data"]["hit"] is False

    def test_the_face_the_caller_rolled_is_the_one_narrated_back(self) -> None:
        result = api.encounter_act(
            _fight(), "attack", target="Goblin", attack="Longsword", natural=13
        )
        event = _attack_event(result)
        assert event["data"]["natural"] == 13
        # And it reaches the prose a table actually hears, not just the payload.
        assert "d20 [13]" in event["detail"]


class TestTheBoundaryRefuses:
    def test_a_face_the_die_does_not_have_is_refused(self) -> None:
        encounter_id = _fight()
        with pytest.raises(RequestError, match="between 1 and 20"):
            api.encounter_act(
                encounter_id, "attack", target="Goblin", attack="Longsword", natural=21
            )

    def test_two_faces_for_a_flat_roll_are_refused(self) -> None:
        encounter_id = _fight()
        with pytest.raises(RequestError, match="one face"):
            api.encounter_act(
                encounter_id, "attack", target="Goblin", attack="Longsword", natural=[11, 12]
            )

    def test_a_refused_face_does_not_spend_the_action(self) -> None:
        # A refusal that had already consumed the attack would leave the caller
        # unable to retry with a corrected face, which is the whole point of
        # refusing rather than coercing. The engine charged the swing *before*
        # rolling it until this case was written.
        encounter_id = _fight()
        with pytest.raises(RequestError, match="between 1 and 20"):
            api.encounter_act(
                encounter_id, "attack", target="Goblin", attack="Longsword", natural=99
            )
        result = api.encounter_act(
            encounter_id, "attack", target="Goblin", attack="Longsword", natural=17
        )
        assert _attack_event(result)["data"]["natural"] == 17

    def test_a_face_reported_for_an_action_that_rolls_nothing_is_refused(self) -> None:
        # Silently dropping it would tell somebody their roll counted when it
        # did not, which is worse than saying no.
        encounter_id = _fight()
        with pytest.raises(RequestError, match="rolls no d20"):
            api.encounter_act(encounter_id, "dodge", natural=14)


class TestTheFightRemembersIt:
    def test_a_supplied_face_survives_the_journal_and_replays_on_resume(self) -> None:
        # The case that catches an unjournalled input. Without the face in the
        # record, the replay re-rolls from the RNG and the recovered fight
        # disagrees with the one the caller was told about.
        encounter_id = _fight()
        api.encounter_act(
            encounter_id, "attack", target="Goblin", attack="Longsword",
            natural=20, request_id="crit",
        )
        before = api.encounter_state(encounter_id)
        api.STATE.sessions.clear()

        recovered = api.encounter_resume(encounter_id)

        assert recovered["recovered"] is True
        assert recovered["state"] == before

    def test_two_different_faces_survive_the_journal_as_two_different_fights(
        self,
    ) -> None:
        # The guard on the case above, and it has to *resume* to be one. That
        # test asserts a recovered fight equals the live one, which a journal
        # recording no face at all could satisfy by re-rolling both the same
        # way. These two recover from records that differ only in the reported
        # face, so a dropped face collapses them together.
        def recovered_goblin_hp(face: int) -> int:
            encounter_id = _fight()
            api.encounter_act(
                encounter_id, "attack", target="Goblin", attack="Longsword", natural=face
            )
            api.STATE.sessions.clear()
            state = api.encounter_resume(encounter_id)["state"]
            return int(
                next(c["hp"] for c in state["combatants"] if c["name"] == "Goblin")
            )

        assert recovered_goblin_hp(1) > recovered_goblin_hp(20)


class TestDeathSaves:
    """The one roll that is not taken on the roller's own action.

    A death save happens at the start of a dying creature's turn, inside
    ``advance`` — so the face is reported there rather than on ``act``. It is
    also the roll a player most wants in their own hand, which is why it is in
    scope at all.
    """

    def _dying(self, seed: int = 41) -> str:
        # hp 0 and not stable is dying, by the derivation the state payload uses.
        # Kesh is here to keep the fight *running*: with Thora down and nobody
        # else standing, the party has lost and no further turn ever begins.
        down = dict(REPLAY_HERO) | {"hp": 0, "position": [30, 30]}
        kesh = dict(REPLAY_HERO) | {"name": "Kesh", "position": [30, 25]}
        return str(
            api.encounter_create(
                [down, kesh, dict(REPLAY_GOBLIN)], seed=seed
            )["encounter_id"]
        )

    def _wind_to_thoras_turn(self, encounter_id: str) -> dict[str, Any]:
        # The save rolls at the *start* of Thora's turn, so the advance that
        # carries the face is the one taken on whoever acts just before her.
        for _ in range(6):
            if api.encounter_state(encounter_id)["turn"] == "Goblin":
                return dict(
                    api.encounter_advance(encounter_id, natural=20, view="full")
                )
            api.encounter_advance(encounter_id)
        raise AssertionError("never reached the turn before Thora's")

    def test_a_reported_twenty_brings_the_dying_back_at_one_hit_point(self) -> None:
        encounter_id = self._dying()
        result = self._wind_to_thoras_turn(encounter_id)
        save = next(e for e in result["events"] if e["kind"] == "death_save")
        assert save["data"]["natural"] == 20
        thora = next(
            c for c in result["state"]["combatants"] if c["name"] == "Thora"
        )
        assert thora["hp"] == 1

    def test_a_face_reported_when_nobody_is_dying_is_refused(self) -> None:
        encounter_id = _fight()
        with pytest.raises(RequestError, match="no death save"):
            api.encounter_advance(encounter_id, natural=20)


class TestThePrimitives:
    def test_a_check_uses_the_face_the_caller_rolled(self) -> None:
        result = api.check(modifier=3, dc=10, natural=15)
        assert result["natural"] == 15
        assert result["success"] is True

    def test_a_save_uses_the_face_the_caller_rolled(self) -> None:
        result = api.save(modifier=0, dc=15, natural=2)
        assert result["natural"] == 2
        assert result["success"] is False

    def test_a_primitive_still_reports_the_seed_it_was_given(self) -> None:
        # Every operation reports its seed. A supplied face does not excuse it
        # from that: the seed still describes everything the caller did not roll.
        result = api.check(modifier=0, dc=10, seed=7, natural=11)
        assert result["seed"] == 7

    def test_advantage_takes_two_faces_and_the_engine_still_chooses(self) -> None:
        result = api.check(modifier=0, dc=10, advantage="advantage", natural=[3, 18])
        assert result["natural"] == 18
        assert "3" in result["detail"] and "18" in result["detail"]

    def test_disadvantage_keeps_the_lower_of_the_two_reported(self) -> None:
        result = api.check(modifier=0, dc=10, advantage="disadvantage", natural=[3, 18])
        assert result["natural"] == 3

    def test_one_face_for_a_check_with_advantage_is_refused(self) -> None:
        with pytest.raises(RequestError, match="two faces"):
            api.check(modifier=0, dc=10, advantage="advantage", natural=5)
