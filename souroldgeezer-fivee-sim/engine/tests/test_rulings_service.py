"""Serving the rulings register: what a caller mid-fight can ask it.

The register's whole point is that a decision is findable by someone who did
not read the function that made it.  A generated document serves the person
reading the repository; this serves the game master who just watched a javelin
take Disadvantage and wants to know whether that was a rule or a choice.
"""

from __future__ import annotations

import pytest

from fivee_sim.rulings import RULINGS, RulingKind
from fivee_sim.service import rulings as rulings_ops
from fivee_sim.service.errors import NotFoundError, RequestError

from . import api


def test_the_listing_carries_every_entry() -> None:
    answer = rulings_ops.listing()
    assert len(answer["rulings"]) == len(RULINGS)
    assert answer["count"] == len(RULINGS)


def test_each_entry_serialises_its_decision_and_its_trigger() -> None:
    by_code = {entry["code"]: entry for entry in rulings_ops.listing()["rulings"]}
    loading = by_code["loading_capped_per_turn"]
    assert loading["kind"] == RulingKind.APPROXIMATION.value
    assert "per turn" in loading["decision"]
    assert "Bonus Action attack" in loading["revisit"]
    assert loading["sites"] == ["model/encounter.py:Encounter._do_attack"]


def test_one_ruling_can_be_asked_for_by_code() -> None:
    answer = rulings_ops.listing(code="climb_cost_boundary")
    assert answer["count"] == 1
    assert answer["rulings"][0]["code"] == "climb_cost_boundary"


def test_filtering_by_kind_narrows_to_the_open_readings() -> None:
    answer = rulings_ops.listing(kind="srd_silent")
    codes = {entry["code"] for entry in answer["rulings"]}
    assert codes == {r.code for r in RULINGS if r.kind is RulingKind.SRD_SILENT}


def test_an_unknown_code_is_refused_by_name() -> None:
    with pytest.raises(NotFoundError, match="no ruling with code 'nope'"):
        rulings_ops.listing(code="nope")


def test_an_unknown_kind_is_refused_and_names_the_legal_ones() -> None:
    with pytest.raises(RequestError, match="unknown ruling kind 'maybe'"):
        rulings_ops.listing(kind="maybe")


def test_the_payload_is_json_and_holds_no_third_party_source() -> None:
    """Same guarantee as the shipped report, on the surface a client reads."""
    import json

    payload = json.dumps(rulings_ops.listing()).lower()
    for forbidden in ("http://", "https://", "sage advice", "d&d", "wizards"):
        assert forbidden not in payload


class TestThroughTheSuiteDoor:
    """Same bodies, reached the way the rest of the suite reaches ``service/``.

    The HTTP contract is pinned separately in ``test_web_http.py`` — this door
    translates nothing, so it cannot stand in for that.
    """

    def test_the_operation_answers_the_whole_register(self) -> None:
        assert api.rules_rulings()["count"] == len(RULINGS)

    def test_the_operation_filters_by_code(self) -> None:
        answer = api.rules_rulings(code="cylinder_height_unread")
        assert [entry["code"] for entry in answer["rulings"]] == ["cylinder_height_unread"]

    def test_an_unknown_code_is_a_named_refusal(self) -> None:
        with pytest.raises(NotFoundError, match="no ruling with code 'wrong'"):
            api.rules_rulings(code="wrong")
