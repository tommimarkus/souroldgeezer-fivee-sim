"""``encounter.correct`` at the service boundary: validation, journal, recovery.

Step 1 built and pinned the model's own rules in ``tests/test_correction.py``.
This is the layer above it — the caller-typed validation
:meth:`~fivee_sim.model.encounter.Encounter.correct` deliberately leaves to the
caller, the durable write, the journal replay a resumed fight relies on, and
the bundle a corrected fight still has to export cleanly.
"""

from __future__ import annotations

from typing import Any

import pytest

from fivee_sim.model.encounter import CORRECTABLE_KEYS, EncounterError
from fivee_sim.service import replay as replay_service
from fivee_sim.service import sessions as sessions_service
from fivee_sim.service.errors import (
    IdempotencyConflictError,
    NotFoundError,
    RequestError,
)

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


class TestTheNestedCollectionsAreTypedToo:
    """``items``, ``spell_slots`` and ``conditions``, past their outer shape.

    All three used to be checked for their *container* and no further, so the
    coercion of what was inside them happened in the model, several layers
    below the last thing that could turn a mistake into a sentence. A JSON
    list, object or ``null`` inside one raised a bare ``TypeError``, which is
    outside the ``ValueError`` family every layer between here and the
    transport catches — so ``web/http_server.py``'s catch-all answered **500**
    with the raw interpreter message, and, before the model was made to compute
    before it writes, the fields already written stayed written.

    The sentences mirror ``condition_levels``'s, which had the idiom right from
    the start: name the key, name the index inside it, show what arrived.
    """

    #: One malformed nested value per shape a JSON caller can actually send,
    #: with the sentence each must produce. ``str`` values are deliberately
    #: absent: ``int("12")`` is a coercion this layer has always allowed.
    REFUSED: dict[str, tuple[dict[str, Any], str]] = {
        "an item count that is a list": (
            {"items": {"Rope": [1, 2]}}, r"items\['Rope'\] must be a whole number",
        ),
        "an item count that is null": (
            {"items": {"Rope": None}}, r"items\['Rope'\] must be a whole number",
        ),
        "an item count that is an object": (
            {"items": {"Rope": {}}}, r"items\['Rope'\] must be a whole number",
        ),
        "an item count that is not a number at all": (
            {"items": {"Rope": "many"}}, r"items\['Rope'\] must be a whole number",
        ),
        "a spell slot count that is a list": (
            {"spell_slots": {1: [1]}}, r"spell_slots\[1\] must be a whole number",
        ),
        "a spell slot count that is null": (
            {"spell_slots": {1: None}}, r"spell_slots\[1\] must be a whole number",
        ),
        "a spell slot level that is not a number": (
            {"spell_slots": {"x": 1}}, r"spell_slots level 'x' must be a whole number",
        ),
        "conditions given as a bare number": (
            {"conditions": 5}, r"conditions must be a list of condition names",
        ),
        "conditions given as null": (
            {"conditions": None}, r"conditions must be a list of condition names",
        ),
        "conditions given as one bare name": (
            {"conditions": "prone"}, r"conditions must be a list of condition names",
        ),
    }

    @pytest.mark.parametrize("case", sorted(REFUSED))
    def test_it_is_refused_by_name_rather_than_crashing(self, case: str) -> None:
        changes, sentence = self.REFUSED[case]
        encounter_id = mapless_fight(seed=901)

        with pytest.raises(RequestError, match=sentence):
            api.encounter_correct(encounter_id, {"Thora": changes}, REASON)

    @pytest.mark.parametrize("case", sorted(REFUSED))
    def test_it_leaves_the_combatant_untouched_and_emits_no_event(
        self, case: str
    ) -> None:
        # ``ac`` rides along as the witness for the same reason the model's own
        # case uses it: it is written first, so it is what used to survive a
        # refusal. A correction is applied whole or not at all, and "not at
        # all" has to include the audit trail — a fight that was changed and
        # says nothing about it is worse than one that refused loudly.
        changes, sentence = self.REFUSED[case]
        encounter_id = mapless_fight(seed=903)
        before = api.encounter_state(encounter_id)
        before_events = len(api.encounter_log(encounter_id)["events"])

        with pytest.raises(RequestError, match=sentence):
            api.encounter_correct(
                encounter_id, {"Thora": {"ac": 99, **changes}}, REASON
            )

        assert api.encounter_state(encounter_id) == before
        after_events = api.encounter_log(encounter_id)["events"]
        assert len(after_events) == before_events
        assert [one for one in after_events if one["kind"] == "correction"] == []

    def test_a_bare_string_is_refused_rather_than_read_letter_by_letter(self) -> None:
        # The sharpest of the three, because it used to *almost* work: a string
        # is iterable, so ``{str(n) for n in "prone"}`` asked for five
        # conditions named 'p', 'r', 'o', 'n' and 'e' and the refusal a caller
        # read named 'e'. Naming the real mistake is the whole point.
        encounter_id = mapless_fight(seed=905)

        with pytest.raises(
            RequestError, match="conditions must be a list of condition names"
        ) as refused:
            api.encounter_correct(
                encounter_id, {"Thora": {"conditions": "prone"}}, REASON
            )

        assert "no condition named 'e'" not in str(refused.value)

    def test_the_legal_shapes_of_all_three_still_land(self) -> None:
        # The guard against a fix that refuses everything.
        encounter_id = mapless_fight(seed=907)

        api.encounter_correct(
            encounter_id,
            {"Thora": {
                "items": {"Rope": 2},
                "spell_slots": {1: 3},
                "conditions": ["poisoned"],
            }},
            REASON,
        )

        row = next(
            one for one in api.encounter_state(encounter_id)["combatants"]
            if one["name"] == "Thora"
        )
        assert row["items"] == {"Rope": 2}
        # ``int`` keys in the payload the model holds. JSON has no integer key,
        # so the same slot reads back as ``"1"`` once it has been over the
        # wire — which is why the level is parsed rather than passed through,
        # and what ``TestRecoveryRoundTrips`` exercises for real.
        assert row["spell_slots"] == {1: 3}
        # The payload reports the names; the levels ride in ``condition_levels``.
        assert row["conditions"] == ["poisoned"]


class TestTheBoundsThatAreDeliberatelyAbsent:
    """What this layer refuses to bound, written down so a tightening is a choice.

    ``max_hp`` has a floor of 1 and ``hp`` a ceiling of ``max_hp``, and the
    absence of anything below them is easy to read as an oversight. It is not:
    :meth:`~fivee_sim.model.creature.Creature.set_hp` says the value is written
    as given because whether a stated total is *plausible* is the table's to
    judge, and a correction is the table overruling the fight. A negative hit
    point total is a state the fight itself reaches; a low armour class means
    everything hits. Both are visible in the payload and both are correctable
    again, so neither needs this layer to have an opinion.

    ``temp_hp`` is the one that is not like the other two, and it gets a floor
    below.
    """

    def test_a_negative_hp_is_accepted_by_design(self) -> None:
        encounter_id = mapless_fight(seed=951)

        api.encounter_correct(encounter_id, {"Thora": {"hp": -50}}, REASON)

        row = self.thora(encounter_id)
        assert row["hp"] == -50
        assert row["conscious"] is False

    def test_a_negative_ac_is_accepted_by_design(self) -> None:
        encounter_id = mapless_fight(seed=953)

        api.encounter_correct(encounter_id, {"Thora": {"ac": -3}}, REASON)

        assert self.thora(encounter_id)["ac"] == -3

    def test_a_negative_temp_hp_is_refused_because_it_amplifies_damage(self) -> None:
        """The one bound that is not a matter of taste.

        ``take_damage`` absorbs with ``absorbed = min(self.temp_hp, amount)``
        and then deals ``amount - absorbed``, so a temporary hit point buffer
        of -5 turns a 10-point blow into a 15-point one. That is not a fight
        the table can see and correct again — it is a creature that quietly
        takes half again as much damage from everything for the rest of the
        encounter.

        It is also a state no other path can produce:
        :meth:`~fivee_sim.model.creature.Creature.grant_temp_hp` refuses an
        ``amount <= 0`` outright, so every grant in the engine is positive.
        A correction that could reach it would be the only writer of a value
        the rest of the engine treats as impossible, which is the line between
        "the table overrules the fight" and "the table breaks it".
        """
        encounter_id = mapless_fight(seed=955)

        with pytest.raises(RequestError, match="temp_hp must be 0 or more, got -5"):
            api.encounter_correct(encounter_id, {"Thora": {"temp_hp": -5}}, REASON)

        assert self.thora(encounter_id)["temp_hp"] == 0

    def test_zero_temp_hp_is_still_how_a_buffer_is_cleared(self) -> None:
        # The floor is at 0, not at 1: "she has no temporary hit points" is a
        # correction a table makes, and a floor of 1 would take it away.
        encounter_id = mapless_fight(seed=957)
        api.encounter_correct(encounter_id, {"Thora": {"temp_hp": 9}}, REASON)

        api.encounter_correct(encounter_id, {"Thora": {"temp_hp": 0}}, REASON)

        assert self.thora(encounter_id)["temp_hp"] == 0

    @pytest.mark.parametrize(
        "key", ["hp", "max_hp", "temp_hp", "ac", "level", "initiative"]
    )
    def test_a_boolean_is_not_a_whole_number(self, key: str) -> None:
        """``True`` is an ``int`` in Python and is not a number a caller meant.

        ``int(True)`` is 1, so ``{"hp": true}`` silently set one hit point —
        a caller who sent the wrong type got a fight changed rather than a
        refusal. The repository already has the idiom in
        ``parse_carried_flag``, which refuses ``stable: 1``. This closes the
        inconsistency in the same direction, so that ``stable: 1`` and
        ``hp: true`` are both mistakes and both say so.
        """
        encounter_id = mapless_fight(seed=959)

        with pytest.raises(RequestError, match=f"{key} must be a whole number"):
            api.encounter_correct(encounter_id, {"Thora": {key: True}}, REASON)

    @staticmethod
    def thora(encounter_id: str) -> dict[str, Any]:
        return next(
            one for one in api.encounter_state(encounter_id)["combatants"]
            if one["name"] == "Thora"
        )


class TestIdempotency:
    def test_a_changed_correction_under_the_same_key_conflicts_and_applies_once(
        self,
    ) -> None:
        encounter_id = mapless_fight(seed=435)

        api.encounter_correct(
            encounter_id, {"Thora": {"ac": 11}}, REASON, request_id="fix-1"
        )
        with pytest.raises(IdempotencyConflictError, match="different request"):
            api.encounter_correct(
                encounter_id,
                {"Thora": {"ac": 99}},
                "a different reason entirely",
                request_id="fix-1",
            )

        state = api.encounter_state(encounter_id)
        assert next(r for r in state["combatants"] if r["name"] == "Thora")["ac"] == 11
        corrections = [
            attempt
            for attempt in api.replay_export(encounter_id, format_version=2)["bundle"][
                "attempts"
            ]
            if attempt["operation"] == "encounter_correct"
        ]
        assert len(corrections) == 1
        assert corrections[0]["arguments"]["reason"] == REASON


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


class TestRecoveryRoundTrips:
    """Every correctable field, written to disk and replayed back off it.

    ``test_a_correction_survives_recovery`` above proved one field, and one
    field is what ``ac`` is: a plain integer that survives JSON because
    everything survives JSON. The fields that can actually break are the ones
    with a shape — a mapping whose **keys** are ``int`` in Python and ``str``
    after a round trip, a condition set that routes through the effect ledger
    rather than onto a dict, an initiative that re-sorts the order and
    re-derives a turn budget.

    ``sessions._replay_correct`` hands the journalled arguments straight to
    :meth:`~fivee_sim.model.encounter.Encounter.correct` with no service layer
    in front of them, so what is replayed is what the *caller* sent, not what
    ``_checked_correction`` normalised it into. ``spell_slots`` is the sharp
    case and the reason this class exists: it survives only because the model
    coerces its keys with ``int()``, which accepts ``"1"`` as readily as ``1``.
    Nothing asserted that, so a model narrowed to ``{k: int(v)}`` would have
    left every recovered fight holding a spell slot at a level named ``"1"``
    that no lookup for level ``1`` would ever find.

    The samples are held against :data:`CORRECTABLE_KEYS` rather than listed
    and trusted, for the reason ``tests/test_correction.py``'s own table is: a
    key added to the model with no sample here would otherwise be a field
    nobody had ever recovered.
    """

    #: One correction per correctable key, grouped where a group is the point.
    #: ``conditions`` and ``condition_levels`` travel together because they are
    #: one field in two keys; ``hp``/``max_hp``/``temp_hp`` because a maximum
    #: that pulls hit points down is the interesting case, not three writes.
    SAMPLES: dict[str, dict[str, Any]] = {
        "the hit point block": {"hp": 7, "max_hp": 44, "temp_hp": 5},
        "the condition set and its levels": {
            "conditions": ["poisoned", "exhaustion"],
            "condition_levels": {"exhaustion": 3},
        },
        "a condition set alone": {"conditions": ["blinded"]},
        "the armour class": {"ac": 11},
        "the death saves": {"death_saves": {"successes": 1, "failures": 2}},
        "the state flags": {"stable": True, "dead": True, "surrendered": True},
        "arrival": {"present": False},
        "the spell slots": {"spell_slots": {1: 3, 2: 1}},
        "the carried items": {"items": {"Rope": 2, "Torch": 1}},
        "where it stands": {"position": [10, 10], "level": 0, "facing": "north"},
        "the initiative roll": {"initiative": 99},
    }

    def test_the_samples_cover_every_correctable_key(self) -> None:
        # Derived rather than trusted: the sample table is only as good as its
        # agreement with the model's own declaration.
        covered = {key for sample in self.SAMPLES.values() for key in sample}
        assert covered == set(CORRECTABLE_KEYS)

    @pytest.mark.parametrize("case", sorted(SAMPLES))
    def test_it_replays_off_disk_to_the_state_it_was_corrected_into(
        self, case: str
    ) -> None:
        encounter_id = mapless_fight(seed=941)

        api.encounter_correct(encounter_id, {"Thora": self.SAMPLES[case]}, REASON)
        live = self.thora(api.encounter_state(encounter_id))
        api.STATE.sessions.clear()
        recovered = api.encounter_resume(encounter_id)

        # The whole combatant, not the corrected keys: a correction that landed
        # and a recovery that dropped a field the correction did not name are
        # the same class of divergence and this is the assertion that sees both.
        assert self.thora(recovered["state"]) == live

    def test_a_spell_slot_level_survives_as_a_number_rather_than_a_string(self) -> None:
        # The named hazard, asserted on the key itself rather than only through
        # the equality above — which would pass if both sides were ``"1"``.
        encounter_id = mapless_fight(seed=943)

        api.encounter_correct(
            encounter_id, {"Thora": {"spell_slots": {1: 3}}}, REASON
        )
        api.STATE.sessions.clear()
        recovered = api.encounter_resume(encounter_id)

        slots = self.thora(recovered["state"])["spell_slots"]
        assert slots == {1: 3}
        assert all(isinstance(level, int) for level in slots)

    def test_a_multi_combatant_correction_replays_for_all_of_them(self) -> None:
        encounter_id = mapless_fight(seed=945)

        api.encounter_correct(
            encounter_id,
            {
                "Thora": {"ac": 11, "items": {"Rope": 2}},
                "Goblin": {"ac": 12, "conditions": ["poisoned"]},
            },
            REASON,
        )
        live = {r["name"]: r for r in api.encounter_state(encounter_id)["combatants"]}
        api.STATE.sessions.clear()
        recovered = api.encounter_resume(encounter_id)

        back = {r["name"]: r for r in recovered["state"]["combatants"]}
        assert back == live
        assert back["Thora"]["items"] == {"Rope": 2}
        assert back["Goblin"]["conditions"] == ["poisoned"]

    def test_a_corrected_initiative_replays_the_order_it_produced(self) -> None:
        # Initiative is the one correction that moves something other than the
        # creature it names: the order re-sorts and the opening turn budget is
        # re-derived off whoever ends up on top.
        encounter_id = mapless_fight(seed=947)

        api.encounter_correct(encounter_id, {"Thora": {"initiative": 99}}, REASON)
        live = api.encounter_state(encounter_id)
        api.STATE.sessions.clear()
        recovered = api.encounter_resume(encounter_id)

        assert recovered["state"]["turn"] == live["turn"]
        assert [r["name"] for r in recovered["state"]["combatants"]] == [
            r["name"] for r in live["combatants"]
        ]

    @staticmethod
    def thora(state: dict[str, Any]) -> dict[str, Any]:
        return next(one for one in state["combatants"] if one["name"] == "Thora")



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
