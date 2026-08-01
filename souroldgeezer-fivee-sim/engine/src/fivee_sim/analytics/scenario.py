"""Deterministic timing evidence for events that happen around a fight.

The encounter stepper remains authoritative for combat.  This module answers a
narrower route-level question the stepper cannot: how many whole rounds elapse
before a traveller reaches a triggered event, and how much lead that leaves over
a stated response delay.  It deliberately does not invent travel decisions,
reinforcements, or adventure state; callers supply those authored facts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil
from typing import Any


@dataclass(frozen=True, slots=True)
class TravelTiming:
    """The whole-round arrival time for one fixed route."""

    distance_feet: int
    speed_feet: int
    dash: bool
    start_delay_rounds: int
    feet_per_round: int
    travel_rounds: int
    arrival_after_rounds: int

    def as_dict(self) -> dict[str, int | bool]:
        return asdict(self)


def travel_timing(
    *,
    distance_feet: int,
    speed_feet: int,
    dash: bool = False,
    start_delay_rounds: int = 0,
) -> TravelTiming:
    """Return the first whole round boundary at which a route is complete."""
    if distance_feet < 1:
        raise ValueError(f"distance_feet must be at least 1: {distance_feet}")
    if speed_feet < 1:
        raise ValueError(f"speed_feet must be at least 1: {speed_feet}")
    if start_delay_rounds < 0:
        raise ValueError(
            "start_delay_rounds cannot be negative: "
            f"{start_delay_rounds}"
        )
    feet_per_round = speed_feet * (2 if dash else 1)
    travel_rounds = ceil(distance_feet / feet_per_round)
    return TravelTiming(
        distance_feet=distance_feet,
        speed_feet=speed_feet,
        dash=dash,
        start_delay_rounds=start_delay_rounds,
        feet_per_round=feet_per_round,
        travel_rounds=travel_rounds,
        arrival_after_rounds=start_delay_rounds + travel_rounds,
    )


def response_window(
    *,
    distance_feet: int,
    speed_feet: int,
    response_after_rounds: int,
    dash: bool = False,
    start_delay_rounds: int = 0,
) -> dict[str, Any]:
    """Compare one traveller's arrival with an authored timed response."""
    if response_after_rounds < 0:
        raise ValueError(
            "response_after_rounds cannot be negative: "
            f"{response_after_rounds}"
        )
    traveller = travel_timing(
        distance_feet=distance_feet,
        speed_feet=speed_feet,
        dash=dash,
        start_delay_rounds=start_delay_rounds,
    )
    lead = response_after_rounds - traveller.arrival_after_rounds
    return {
        "traveller": traveller.as_dict(),
        "response_after_rounds": response_after_rounds,
        "lead_rounds": lead,
        "can_intercept": lead >= 0,
    }


__all__ = ["TravelTiming", "response_window", "travel_timing"]
