"""The play pregens are sheets the shipped engine can actually run.

``skills/play/assets/pregens.json`` is handed to seats by the play skill,
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
from fivee_sim.service.specs import attack_from_spec

from . import api

PREGENS = Path(__file__).resolve().parents[2] / "skills/play/assets/pregens.json"


def _parties() -> dict[str, Any]:
    payload = json.loads(PREGENS.read_text(encoding="utf-8"))
    parties = payload["parties"]
    assert isinstance(parties, dict) and parties, "the asset ships at least one party"
    return parties


def _members(party: dict[str, Any]) -> list[dict[str, Any]]:
    return [member["sheet"] for member in party["members"]]


def _ammunition_named_by(sheet: dict[str, Any]) -> set[str]:
    """The ``items`` entries this sheet's own attacks spend rather than use."""
    return {
        str(spec["ammunition"])
        for spec in sheet.get("attacks", ())
        if spec.get("ammunition") is not None
    }


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

    def test_cantrips_belong_to_the_spellcasting_pregens(self, party_name: str) -> None:
        known = spellbook()
        cantrips = {
            str(sheet["name"]): {
                spell
                for spell in sheet.get("spells", ())
                if spell in known and known[spell].level == 0
            }
            for sheet in _members(_parties()[party_name])
        }

        assert cantrips == {
            "Thora": set(),
            "Kesh": set(),
            "Ilma": {"Sacred Flame"},
            "Doran": {"Fire Bolt"},
        }

    def test_character_identities_stay_consistent_between_tiers(
        self, party_name: str
    ) -> None:
        identities = {
            str(member["sheet"]["name"]): {
                key: member.get(key)
                for key in ("class", "species", "background")
            }
            for member in _parties()[party_name]["members"]
        }

        assert identities == {
            "Thora": {"class": "Fighter", "species": "Human", "background": "Soldier"},
            "Kesh": {"class": "Rogue", "species": "Halfling", "background": "Criminal"},
            "Ilma": {"class": "Cleric", "species": "Dwarf", "background": "Acolyte"},
            "Doran": {"class": "Wizard", "species": "Elf", "background": "Sage"},
        }

    def test_every_item_carried_is_one_the_bundled_content_defines(
        self, party_name: str
    ) -> None:
        # The load-bearing case. An undefined item is accepted at create and
        # refused at use, so only a check like this one can see it.
        #
        # Ammunition is exempt for the reason ``content.py`` exempts it from the
        # same cross-check: an arrow is *held*, never *used*, and
        # ``ItemEffect.__post_init__`` refuses a use that does nothing — so it
        # can never be defined and asking for that would report a fact the
        # engine does not act on. Derived per sheet from that sheet's own
        # attacks, so one character's quiver never excuses another's.
        defined = set(item_effects())
        undefined: dict[str, list[str]] = {}
        for sheet in _members(_parties()[party_name]):
            carried = set(sheet.get("items", {}))
            spent = _ammunition_named_by(sheet)
            if loose := sorted(carried - defined - spent):
                undefined[str(sheet["name"])] = loose

        assert undefined == {}

    def test_the_exemption_does_not_swallow_an_ordinary_item(
        self, party_name: str
    ) -> None:
        # The control on the case above: its exemption is a subtraction, and a
        # subtraction wide enough to cover everything would pass it on a party
        # holding nothing but junk. So at least one carried item is held to the
        # unexempted rule — that it is content the engine can actually resolve.
        usable = {
            item
            for sheet in _members(_parties()[party_name])
            for item in sheet.get("items", {})
            if item not in _ammunition_named_by(sheet)
        }

        assert usable, "no member carries anything but ammunition"
        assert usable <= set(item_effects())

    def test_every_attack_that_spends_ammunition_carries_some(
        self, party_name: str
    ) -> None:
        # ``content.py`` reports this for a pack; a sheet posted to
        # ``encounter.create`` goes nowhere near that validator, so the same
        # mistake arrives instead as a refusal on the first shot of the fight.
        empty = [
            f"{sheet['name']}'s {spec['name']} fires {spec['ammunition']!r}"
            for sheet in _members(_parties()[party_name])
            for spec in sheet.get("attacks", ())
            if spec.get("ammunition") is not None
            and not sheet.get("items", {}).get(spec["ammunition"], 0) > 0
        ]

        assert empty == []

    def test_every_ranged_attack_can_reach_further_than_its_own_square(
        self, party_name: str
    ) -> None:
        """The range a fight reads, taken from the built attack and not the JSON.

        ``normal_range`` and ``long_range`` both default to 0 and
        ``max_distance()`` returns exactly that for a ranged attack, so an
        attack that declares neither is refused at every distance:
        ``Shortbow cannot reach (35 ft > 0 ft)``. Every ranged attack these
        sheets shipped was in that state, because they spelt the key ``range``
        and nothing was looking.

        Built through ``attack_from_spec`` rather than read off the file, so a
        key the builder does not read cannot satisfy this the way ``range`` did.
        """
        ranged = [
            (str(sheet["name"]), attack_from_spec(dict(spec)))
            for sheet in _members(_parties()[party_name])
            for spec in sheet.get("attacks", ())
            if spec.get("kind") == "ranged"
        ]

        # Before the property, not inside a guard around it: every party is
        # meant to have someone who can shoot, and a party that lost its last
        # ranged attack must fail here rather than pass by having nothing left
        # to check.
        assert ranged, "no member of this party has a ranged attack"
        assert [name for name, option in ranged if option.max_distance() <= 0] == []

    def test_every_archer_lands_a_shot_across_the_battlefield(
        self, party_name: str
    ) -> None:
        """The range spent in a real fight, not merely kept by the builder.

        The case above proves the attack is *built* with a range; this one
        proves the fight reads it. Same sheets, posted to the real operation,
        with a target 35-to-40 ft off — the distances that answered
        ``Shortbow cannot reach (35 ft > 0 ft)`` before the key was spelt right.
        """
        sheets = _members(_parties()[party_name])
        shooters = {
            str(sheet["name"]): str(spec["name"])
            for sheet in sheets
            for spec in sheet.get("attacks", ())
            if spec.get("kind") == "ranged"
        }
        dummy = {
            "name": "Target Dummy", "team": "monsters", "ac": 10,
            "max_hp": 400, "position": [40, 0],
        }
        created = api.encounter_create([*sheets, dummy], seed=3)
        encounter_id = str(created["encounter_id"])
        state = created["state"]

        # Asserted on what a *resolved* shot carries rather than on the absence
        # of a refusal: an out-of-range attack emits ``data.out_of_range`` and
        # no ``hit`` at all, so a check for the refusal flag reads ``None`` on
        # every event of every other kind and passes whatever happened. Naming
        # the roll instead means a bow that cannot reach fails here for want of
        # a hit, and so does a shot that is never taken.
        rolled: dict[str, str] = {}
        # One full round is enough for everyone to act once; the bound is a
        # runaway guard, and the assertion below is what proves it sufficed.
        for _ in range(len(sheets) * 3):
            actor = str(state["turn"])
            if actor in shooters and actor not in rolled:
                answer = api.encounter_act(
                    encounter_id, "attack",
                    attack=shooters[actor], target="Target Dummy",
                )
                rolled[actor] = "; ".join(
                    str(event["detail"])
                    for event in answer["events"]
                    if event["kind"] == "attack" and "hit" not in event["data"]
                )
            state = api.encounter_advance(encounter_id, view="full")["state"]
            if set(rolled) == set(shooters):
                break

        assert set(rolled) == set(shooters), "an archer never got a turn to shoot"
        assert {name: why for name, why in rolled.items() if why} == {}

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
                # ``in potions`` before the lookup: a quiver entry is an item
                # the engine defines no effect for, and it heals nobody.
                *(
                    i for i in sheet.get("items", {})
                    if i in potions and potions[i].heal is not None
                ),
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
