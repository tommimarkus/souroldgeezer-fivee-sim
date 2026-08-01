"""Route timing evidence is deterministic and separate from combat resolution."""

from __future__ import annotations

import pytest

from fivee_sim.analytics.scenario import response_window, travel_timing


def test_two_dashes_cover_the_105_feet_from_sluice_to_wheelhouse() -> None:
    timing = travel_timing(distance_feet=105, speed_feet=30, dash=True)

    assert timing.as_dict() == {
        "distance_feet": 105,
        "speed_feet": 30,
        "dash": True,
        "start_delay_rounds": 0,
        "feet_per_round": 60,
        "travel_rounds": 2,
        "arrival_after_rounds": 2,
    }


def test_a_three_round_response_leaves_one_round_to_set_an_interception() -> None:
    result = response_window(
        distance_feet=105,
        speed_feet=30,
        dash=True,
        response_after_rounds=3,
    )

    assert result["traveller"]["arrival_after_rounds"] == 2
    assert result["response_after_rounds"] == 3
    assert result["lead_rounds"] == 1
    assert result["can_intercept"] is True


@pytest.mark.parametrize(
    ("distance", "speed", "message"),
    [(0, 30, "distance_feet"), (10, 0, "speed_feet")],
)
def test_invalid_route_inputs_are_refused(
    distance: int, speed: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        travel_timing(distance_feet=distance, speed_feet=speed)


def test_a_negative_response_delay_is_refused() -> None:
    with pytest.raises(ValueError, match="response_after_rounds"):
        response_window(
            distance_feet=105,
            speed_feet=30,
            response_after_rounds=-1,
        )
