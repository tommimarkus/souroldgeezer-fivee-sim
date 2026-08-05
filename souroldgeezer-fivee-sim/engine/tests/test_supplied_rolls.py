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
    def test_a_supplied_twenty_crits_whatever_the_seed_would_have_rolled(self) -> None:
        # Asserted across two unrelated seeds, so it cannot pass by coinciding
        # with the draw either time.
        for seed in (41, 977):
            encounter_id = _fight(seed)
            result = api.encounter_act(
                encounter_id, "attack", target="Goblin", attack="Longsword", natural=20
            )
            assert _attack_event(result)["data"]["critical"] is True

    def test_a_supplied_one_misses_whatever_the_seed_would_have_rolled(self) -> None:
        for seed in (41, 977):
            encounter_id = _fight(seed)
            result = api.encounter_act(
                encounter_id, "attack", target="Goblin", attack="Longsword", natural=1
            )
            event = _attack_event(result)
            assert event["data"]["hit"] is False

    def test_the_face_the_caller_rolled_is_the_one_narrated_back(self) -> None:
        encounter_id = _fight()
        result = api.encounter_act(
            encounter_id, "attack", target="Goblin", attack="Longsword", natural=13
        )
        assert "13" in _attack_event(result)["detail"]


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

    def test_a_supplied_face_and_a_rolled_one_recover_to_different_fights(self) -> None:
        # The guard on the case above: if resume ignored the face entirely, both
        # fights would recover to whatever the seed rolls and the assertion
        # would pass against a journal that records nothing.
        supplied = _fight()
        api.encounter_act(supplied, "attack", target="Goblin", attack="Longsword", natural=1)
        missed = api.encounter_state(supplied)

        landed_id = _fight()
        api.encounter_act(landed_id, "attack", target="Goblin", attack="Longsword", natural=20)
        landed = api.encounter_state(landed_id)

        def goblin_hp(state: dict[str, Any]) -> int:
            return int(
                next(c["hp"] for c in state["combatants"] if c["name"] == "Goblin")
            )

        assert goblin_hp(missed) > goblin_hp(landed)


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
