"""``encounter.correct`` at the service boundary: validation, journal, recovery.

Step 1 built and pinned the model's own rules in ``tests/test_correction.py``.
This is the layer above it — the caller-typed validation
:meth:`~fivee_sim.model.encounter.Encounter.correct` deliberately leaves to the
caller, the durable write, the journal replay a resumed fight relies on, and
the bundle a corrected fight still has to export cleanly.
"""

from __future__ import annotations

import pytest

from fivee_sim.model.encounter import EncounterError
from fivee_sim.service import replay as replay_service
from fivee_sim.service import sessions as sessions_service
from fivee_sim.service.errors import NotFoundError, RequestError

from . import api
from .conftest import REPLAY_GOBLIN, REPLAY_HERO, mapless_fight

REASON = "the fireball never landed"


class TestASuccessfulCorrection:
    def test_it_reports_the_encounter_id_and_nothing_of_the_state(self) -> None:
        # Mirrors ``condition``'s own return shape, and for the same reason:
        # keeping ``correct`` out of ``_answered``'s ``as=``/``view``
        # composition, which only the four operations that answer with
        # ``state`` need.
        encounter_id = mapless_fight(seed=401)

        result = api.encounter_correct(encounter_id, {"Thora": {"ac": 11}}, REASON)

        assert result.keys() == {"encounter_id"}
        assert result["encounter_id"] == encounter_id
        state = api.encounter_state(encounter_id)
        assert next(row for row in state["combatants"] if row["name"] == "Thora")["ac"] == 11

    def test_it_journals_the_reason_and_the_state_it_was_given(self) -> None:
        encounter_id = mapless_fight(seed=403)

        api.encounter_correct(encounter_id, {"Thora": {"ac": 11}}, REASON)

        attempt = api.replay_export(encounter_id, format_version=2)["bundle"]["attempts"][-1]
        assert attempt["operation"] == "encounter_correct"
        assert attempt["arguments"]["reason"] == REASON
        assert attempt["arguments"]["state"] == {"Thora": {"ac": 11}}

    def test_several_combatants_are_corrected_in_one_call(self) -> None:
        encounter_id = mapless_fight(seed=405)

        api.encounter_correct(
            encounter_id, {"Thora": {"ac": 11}, "Goblin": {"ac": 12}}, REASON
        )

        by_name = {row["name"]: row for row in api.encounter_state(encounter_id)["combatants"]}
        assert by_name["Thora"]["ac"] == 11
        assert by_name["Goblin"]["ac"] == 12


class TestValidatedWholeThenAppliedWhole:
    def test_a_refusal_for_the_second_combatant_leaves_the_first_untouched(self) -> None:
        encounter_id = mapless_fight(seed=407)
        before = api.encounter_state(encounter_id)
        before_ac = next(r for r in before["combatants"] if r["name"] == "Thora")["ac"]

        with pytest.raises(RequestError, match="cannot be corrected"):
            api.encounter_correct(
                encounter_id,
                {"Thora": {"ac": 11}, "Goblin": {"speeds": {}}},
                REASON,
            )

        after = api.encounter_state(encounter_id)
        after_ac = next(r for r in after["combatants"] if r["name"] == "Thora")["ac"]
        assert after_ac == before_ac


class TestRefusals:
    def test_a_blank_reason_is_refused(self) -> None:
        encounter_id = mapless_fight(seed=409)

        with pytest.raises(RequestError, match="reason must not be blank"):
            api.encounter_correct(encounter_id, {"Thora": {"ac": 11}}, "   ")

    def test_an_oversized_reason_is_refused_by_the_service(self) -> None:
        encounter_id = mapless_fight(seed=411)

        with pytest.raises(RequestError, match="reason must be at most"):
            api.encounter_correct(encounter_id, {"Thora": {"ac": 11}}, "x" * 5000)

    def test_an_empty_state_map_is_refused(self) -> None:
        encounter_id = mapless_fight(seed=413)

        with pytest.raises(RequestError, match="state must name at least one combatant"):
            api.encounter_correct(encounter_id, {}, REASON)

    def test_an_empty_per_combatant_change_is_refused(self) -> None:
        encounter_id = mapless_fight(seed=415)

        with pytest.raises(RequestError, match="'Thora' names no fields to change"):
            api.encounter_correct(encounter_id, {"Thora": {}}, REASON)

    def test_an_unknown_combatant_is_404_not_400(self) -> None:
        encounter_id = mapless_fight(seed=417)

        with pytest.raises(
            NotFoundError, match="no combatant named 'Bob' in this encounter"
        ):
            api.encounter_correct(encounter_id, {"Bob": {"ac": 11}}, REASON)

    def test_an_uncorrectable_key_is_refused_by_name(self) -> None:
        encounter_id = mapless_fight(seed=419)

        with pytest.raises(RequestError, match="'speeds' cannot be corrected"):
            api.encounter_correct(encounter_id, {"Thora": {"speeds": {}}}, REASON)

    def test_an_unknown_condition_name_is_refused(self) -> None:
        encounter_id = mapless_fight(seed=421)

        with pytest.raises(RequestError, match="no condition named 'not-a-condition'"):
            api.encounter_correct(
                encounter_id, {"Thora": {"conditions": ["not-a-condition"]}}, REASON
            )

    def test_a_condition_level_below_one_is_refused(self) -> None:
        encounter_id = mapless_fight(seed=423)

        with pytest.raises(
            RequestError, match=r"condition_levels\['exhaustion'\] is 0"
        ):
            api.encounter_correct(
                encounter_id,
                {"Thora": {"condition_levels": {"exhaustion": 0}}},
                REASON,
            )

    def test_hp_above_max_hp_is_refused(self) -> None:
        encounter_id = mapless_fight(seed=425)

        with pytest.raises(RequestError, match="hp 900 cannot exceed max_hp"):
            api.encounter_correct(encounter_id, {"Thora": {"hp": 900}}, REASON)

    def test_a_stated_max_hp_below_one_is_refused(self) -> None:
        encounter_id = mapless_fight(seed=427)

        with pytest.raises(RequestError, match="max_hp must be at least 1"):
            api.encounter_correct(encounter_id, {"Thora": {"max_hp": 0}}, REASON)

    def test_a_non_boolean_flag_string_is_refused(self) -> None:
        # ``parse_carried_flag``'s own guard: the model's ``bool(...)`` would
        # read "false" as truthy, which is the defect this closes.
        encounter_id = mapless_fight(seed=429)

        with pytest.raises(RequestError, match="stable must be true or false"):
            api.encounter_correct(encounter_id, {"Thora": {"stable": "false"}}, REASON)

    def test_initiative_after_the_first_turn_is_refused(self) -> None:
        encounter_id = mapless_fight(seed=431)
        api.encounter_advance(encounter_id)

        with pytest.raises(
            RequestError,
            match="initiative can only be corrected before the first turn is taken",
        ):
            api.encounter_correct(encounter_id, {"Thora": {"initiative": 9}}, REASON)

    def test_initiative_in_an_interlude_is_refused(self) -> None:
        created = api.encounter_create(
            [dict(name="Thora", team="party", ac=16, max_hp=30)], mode="exploration"
        )
        encounter_id = str(created["encounter_id"])

        with pytest.raises(RequestError, match="an interlude has no initiative to correct"):
            api.encounter_correct(encounter_id, {"Thora": {"initiative": 9}}, REASON)

    def test_a_finalized_encounter_is_refused(self) -> None:
        encounter_id = mapless_fight(seed=433)
        api.encounter_finalize(encounter_id)

        with pytest.raises(RequestError, match=f"encounter {encounter_id!r} is finalized"):
            api.encounter_correct(encounter_id, {"Thora": {"ac": 11}}, REASON)


class TestIdempotency:
    def test_a_retry_under_the_same_key_returns_the_first_result_and_applies_once(
        self,
    ) -> None:
        encounter_id = mapless_fight(seed=435)

        first = api.encounter_correct(
            encounter_id, {"Thora": {"ac": 11}}, REASON, request_id="fix-1"
        )
        second = api.encounter_correct(
            encounter_id, {"Thora": {"ac": 99}}, "a different reason entirely",
            request_id="fix-1",
        )

        assert second == first
        state = api.encounter_state(encounter_id)
        assert next(r for r in state["combatants"] if r["name"] == "Thora")["ac"] == 11


class TestRecoveryAndExport:
    def test_a_correction_survives_recovery(self) -> None:
        encounter_id = mapless_fight(seed=437)

        api.encounter_correct(encounter_id, {"Thora": {"ac": 11}}, REASON)
        api.STATE.sessions.clear()
        recovered = api.encounter_resume(encounter_id)

        row = next(r for r in recovered["state"]["combatants"] if r["name"] == "Thora")
        assert row["ac"] == 11

    def test_a_corrected_fight_exports_a_valid_bundle(self) -> None:
        # The bug ``condition``'s own docstring records: skip the stamp and the
        # checkpoint and a bundle carrying this mutator's events fails
        # ``validate_replay``.
        encounter_id = mapless_fight(seed=439)

        api.encounter_correct(encounter_id, {"Thora": {"ac": 11}}, REASON)
        bundle = api.replay_export(encounter_id, format_version=2)["bundle"]

        assert all(event["timestamp"] for event in bundle["events"])
        assert bundle["checkpoints"][-1]["state"] == bundle["latest_state"]
        assert replay_service.validate_replay(bundle) == []

        api.encounter_finalize(encounter_id)



class TestTheStructuralRefusalsHaveOneOwner:
    """The model states them; this layer restates nothing.

    Two doors reach the same three structural refusals. The service asks for
    them *before* ``audited_primitive`` because a correction is applied whole
    or not at all — a refusal discovered while writing the second combatant
    would leave the first one corrected — and the model keeps them because
    every caller that does not come through here still needs them, most of all
    ``sessions._replay_correct``, which a recovery drives with no service layer
    in front of it.

    That is two callers for one sentence, and the drift would be silent: each
    layer's other tests assert only against its own copy, so a reworded refusal
    would leave both suites green and the two doors answering the same mistake
    differently. These hold them equal, which is why
    :meth:`~fivee_sim.model.encounter.Encounter.check_correctable` is public.
    """

    def _model_refusal(
        self, encounter_id: str, changes: dict[str, object], match: str
    ) -> str:
        encounter = sessions_service.session_for(api.STATE, encounter_id).encounter
        with pytest.raises(EncounterError, match=match) as refused:
            encounter.correct("Thora", changes, reason=REASON)
        return str(refused.value)

    def _service_refusal(
        self, encounter_id: str, changes: dict[str, object], match: str
    ) -> str:
        with pytest.raises(RequestError, match=match) as refused:
            api.encounter_correct(encounter_id, {"Thora": changes}, REASON)
        return str(refused.value)

    def _both_agree(
        self, encounter_id: str, changes: dict[str, object], match: str
    ) -> None:
        assert self._service_refusal(encounter_id, changes, match) == self._model_refusal(
            encounter_id, changes, match
        )

    def test_an_uncorrectable_field_is_refused_in_the_models_own_words(self) -> None:
        self._both_agree(mapless_fight(seed=441), {"speeds": {}}, "cannot be corrected")

    def test_a_field_that_is_not_a_field_at_all_is_too(self) -> None:
        self._both_agree(mapless_fight(seed=443), {"hitpoints": 3}, "cannot be corrected")

    def test_an_initiative_the_fight_has_walked_past_is_too(self) -> None:
        encounter_id = mapless_fight(seed=445)
        api.encounter_advance(encounter_id)

        self._both_agree(encounter_id, {"initiative": 20}, "initiative")

    def test_an_interludes_missing_initiative_is_too(self) -> None:
        created = api.encounter_create(
            [dict(REPLAY_HERO), dict(REPLAY_GOBLIN)], seed=447, mode="exploration"
        )

        self._both_agree(str(created["encounter_id"]), {"initiative": 20}, "initiative")
