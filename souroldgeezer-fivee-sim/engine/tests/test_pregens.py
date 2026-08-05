"""The playtest pregens are sheets the shipped engine can actually run.

``skills/playtest/assets/pregens.json`` is handed to seats by the playtest skill,
and nothing in this suite loaded it until healing arrived. The trap it guards is
specific and quiet: a creature may **hold an item no content defines**, and
``encounter.create`` accepts the sheet without a word — the refusal arrives at
``use``, in the round the potion was the answer. A spell name behaves the same
way. Neither is visible from reading the asset.

Every case derives what it checks from the asset itself rather than listing
names, so a party or a character added to the file is covered the day it lands
rather than the day somebody remembers this file exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fivee_sim.content import item_effects, spellbook

from . import api

PREGENS = Path(__file__).resolve().parents[2] / "skills/playtest/assets/pregens.json"


def _parties() -> dict[str, Any]:
    payload = json.loads(PREGENS.read_text(encoding="utf-8"))
    parties = payload["parties"]
    assert isinstance(parties, dict) and parties, "the asset ships at least one party"
    return parties


def _members(party: dict[str, Any]) -> list[dict[str, Any]]:
    return [member["sheet"] for member in party["members"]]


PARTY_NAMES = sorted(_parties())


@pytest.mark.parametrize("party_name", PARTY_NAMES)
class TestEveryPartyRuns:
    def test_the_sheets_start_a_fight_as_written(self, party_name: str) -> None:
        # The asset's own claim is that a sheet is "an engine combatant spec,
        # accepted as-is by encounter.create". Posted whole, keys included.
        sheets = _members(_parties()[party_name])
        opponent = {
            "name": "Bandit", "team": "monsters", "ac": 12, "max_hp": 11,
            "position": [40, 0],
            "attacks": [{"name": "Scimitar", "attack_bonus": 3, "damage": "1d6+1",
                         "damage_type": "slashing", "kind": "melee"}],
        }

        result = api.encounter_create([*sheets, opponent], seed=1)

        started = {c["name"]: c for c in result["state"]["combatants"]}
        assert set(started) == {sheet["name"] for sheet in sheets} | {"Bandit"}
        # The distinguishing values, not just the roster: an unknown key is
        # refused outright, but a key accepted and quietly dropped would leave
        # the count and the names both correct.
        for sheet in sheets:
            entry = started[sheet["name"]]
            assert entry["max_hp"] == sheet["max_hp"]
            assert entry.get("items", {}) == sheet.get("items", {})
            assert set(entry.get("spells", ())) == set(sheet.get("spells", ()))

    def test_every_spell_named_is_one_the_engine_executes(self, party_name: str) -> None:
        known = spellbook()
        named = {
            spell
            for sheet in _members(_parties()[party_name])
            for spell in sheet.get("spells", ())
        }

        assert named <= set(known), f"not executable: {sorted(named - set(known))}"

    def test_every_item_carried_is_one_the_bundled_content_defines(
        self, party_name: str
    ) -> None:
        # The load-bearing case. An undefined item is accepted at create and
        # refused at use, so only a check like this one can see it.
        defined = item_effects()
        carried = {
            item
            for sheet in _members(_parties()[party_name])
            for item in sheet.get("items", {})
        }

        assert carried <= set(defined), f"undefined: {sorted(carried - set(defined))}"

    def test_the_party_can_restore_hit_points_somehow(self, party_name: str) -> None:
        # The party-level property, not a per-character one: who carries the
        # healing is a balance decision that may move between tiers, but a party
        # with no source at all cannot survive an adventuring day, and every
        # attrition number measured from it says more about that than about the
        # module under test.
        heals = spellbook()
        potions = item_effects()
        sources = [
            name
            for sheet in _members(_parties()[party_name])
            for name in (
                *(s for s in sheet.get("spells", ()) if heals[s].heal is not None),
                *(i for i in sheet.get("items", {}) if potions[i].heal is not None),
            )
        ]

        assert sources, "no member has a healing spell or a healing item"

    def test_a_caster_whose_healing_scales_declares_the_ability_it_scales_on(
        self, party_name: str
    ) -> None:
        # ``add_spellcasting_modifier`` is opt-in on the spell and reads
        # ``spellcasting_ability`` off the caster. A sheet that knows such a
        # spell without declaring the ability heals its flat dice and looks
        # entirely correct doing it.
        known = spellbook()
        casters = [
            (sheet["name"], scaling, sheet.get("spellcasting_ability"))
            for sheet in _members(_parties()[party_name])
            if (scaling := [
                name
                for name in sheet.get("spells", ())
                if known[name].add_spellcasting_modifier
            ])
        ]

        # Asserted before the property itself: a guard that only fires when the
        # population is non-empty passes vacuously the day the last scaling
        # spell leaves the asset, and would then be a case that cannot fail.
        assert casters, "no pregen knows a spell whose healing scales"
        assert [name for name, _, ability in casters if not ability] == []
