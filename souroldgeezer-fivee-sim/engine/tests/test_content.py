"""Content packs: loading, validating, merging, and the tools that expose them.

Two themes run through this file.

**Diagnostics are the product.** A campaign author's main interaction with this
feature is being told what is wrong with their JSON, so most tests assert on the
message, not merely that something failed.

**A green built-in suite proves nothing about pack content.** Every other test
module exercises the SRD conditions, which are ``Condition`` enum members and so
behave like strings *and* like enums. A pack's condition is a plain ``str``. The
tests under :class:`TestCustomConditions` are the ones that would catch a leftover
``.value``, and they deliberately drive the narration and state paths rather than
only the arithmetic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from random import Random
from typing import Any

import pytest

from fivee_sim import content as content_module
from fivee_sim.content import (
    BuiltinMode,
    ContentError,
    Severity,
    load_packs,
    make_creature,
    monster_records,
    validate,
)
from fivee_sim.kernel.actions import compute_attack_advantage
from fivee_sim.kernel.conditions import EFFECTS, Condition, ConditionEffect
from fivee_sim.kernel.dice import Advantage
from fivee_sim.kernel.spells import SpellShape
from fivee_sim.mcp_server import server as api
from fivee_sim.model.creature import Creature
from fivee_sim.model.encounter import Action, ActionKind, Encounter

from .conftest import advance_to

# The bundled slice's own size, read from the data rather than written down. Both
# are what ``docs/COVERAGE.md`` reports; deriving them means adding a monster or a
# condition updates the expectation instead of breaking an unrelated tool test.
BUNDLED_CREATURES = len(monster_records())
BUNDLED_CONDITIONS = len(Condition)

CAMPAIGN: dict[str, Any] = {
    "pack": "crimson-vale",
    "version": "1.0",
    "provenance": "Original content, (c) 2026 Example Campaign",
    "creatures": [
        {
            "name": "Vale Stalker",
            "team": "monsters",
            "ac": 14,
            "max_hp": 22,
            "speed": 40,
            "abilities": {"strength": 14, "dexterity": 16, "constitution": 12},
            "attacks": [
                {
                    "name": "Claw",
                    "attack_bonus": 5,
                    "damage": "1d6+3",
                    "damage_type": "slashing",
                    "kind": "melee",
                    "reach": 5,
                }
            ],
            "provenance": "Original content",
        }
    ],
    "spells": [
        {
            "name": "Crimson Bolt",
            "level": 1,
            "school": "evocation",
            "requires_attack_roll": True,
            "damage": "2d8",
            "damage_type": "necrotic",
            "range_feet": 60,
            "provenance": "Original content",
        }
    ],
    "conditions": [
        {
            "name": "vale-cursed",
            "description": "The vale's hunger gnaws; every strike comes harder.",
            "effects": {"own_attacks_have_disadvantage": True},
            "provenance": "Original content",
        }
    ],
    "items": [
        {
            "name": "Vale Draught",
            "description": "Bitter, and it works.",
            "use": {"heal": "2d4+2"},
            "provenance": "Original content",
        },
        {
            "name": "Cursed Needle",
            "use": {"condition": "vale-cursed"},
            "provenance": "Original content",
        },
    ],
}


def write_pack(directory: Path, name: str, payload: dict[str, Any]) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def problems(diagnostics: list[Any], severity: Severity = Severity.ERROR) -> list[str]:
    return [d.problem for d in diagnostics if d.severity is severity]


def fields(diagnostics: list[Any], severity: Severity = Severity.ERROR) -> list[str]:
    """The record keys the diagnostics blame.

    ``problem`` carries only the complaint ("must be text, got int"); the key it is
    about lives in ``Diagnostic.field``. A test that wants to prove the *right* key
    was caught has to look here.
    """
    return [d.field for d in diagnostics if d.severity is severity]


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    return write_pack(tmp_path, "crimson-vale.json", CAMPAIGN)


class TestLoading:
    def test_every_category_loads(self, pack: Path) -> None:
        registry = load_packs([pack], include_environment=False)
        assert "Vale Stalker" in registry.creatures
        assert "Crimson Bolt" in registry.spells
        assert "vale-cursed" in registry.condition_effects
        assert "Vale Draught" in registry.items

    def test_builtins_are_included_by_default(self, pack: Path) -> None:
        registry = load_packs([pack], include_environment=False)
        assert "Goblin Warrior" in registry.creatures
        assert "Fireball" in registry.spells
        assert registry.builtin is BuiltinMode.INCLUDE

    def test_a_directory_is_scanned_for_json(self, tmp_path: Path) -> None:
        write_pack(tmp_path / "packs", "a.json", CAMPAIGN)
        write_pack(
            tmp_path / "packs", "b.json",
            {"pack": "b", "provenance": "test", "items": [
                {"name": "Rope", "use": {"heal": "1d1"}, "provenance": "test"}
            ]},
        )
        (tmp_path / "packs" / "notes.txt").write_text("ignored", encoding="utf-8")
        registry = load_packs([tmp_path / "packs"], include_environment=False)
        assert "Vale Stalker" in registry.creatures
        assert "Rope" in registry.items

    def test_each_entry_records_which_pack_it_came_from(self, pack: Path) -> None:
        registry = load_packs([pack], include_environment=False)
        assert registry.source_of("creatures", "Vale Stalker") == str(pack)
        assert registry.source_of("creatures", "Goblin Warrior") == "bundled:monsters.json"

    def test_a_creature_can_be_built_from_a_pack_record(self, pack: Path) -> None:
        registry = load_packs([pack], include_environment=False)
        stalker = make_creature("Vale Stalker", registry=registry, label="Stalker A")
        assert stalker.name == "Stalker A"
        assert stalker.max_hp == 22
        assert stalker.attacks[0].name == "Claw"


class TestExcludeMode:
    def test_bundled_content_is_absent(self, pack: Path) -> None:
        registry = load_packs([pack], builtin="exclude", include_environment=False)
        assert "Goblin Warrior" not in registry.creatures
        assert "Fireball" not in registry.spells
        assert "Vale Stalker" in registry.creatures

    def test_asking_for_a_bundled_name_says_the_builtins_are_excluded(
        self, pack: Path
    ) -> None:
        registry = load_packs([pack], builtin="exclude", include_environment=False)
        from fivee_sim.content import DataError

        with pytest.raises(DataError, match="excluded"):
            make_creature("Goblin Warrior", registry=registry)

    def test_the_conditions_the_stepper_applies_itself_are_retained(
        self, pack: Path
    ) -> None:
        # Dropping to 0 hit points applies Unconscious and Prone. That is engine
        # machinery, not content, so exclude mode cannot remove it without making a
        # creature falling over crash the fight.
        registry = load_packs([pack], builtin="exclude", include_environment=False)
        assert set(registry.retained_conditions) == {"unconscious", "prone"}
        assert "blinded" not in registry.condition_effects
        # And they must appear in the catalogue, not only in the effects table. A
        # condition the stepper applies but `lookup_rule` does not list would leave the
        # catalogue contradicting `encounter_state`, which is precisely the drift this
        # engine exists to remove.
        listed = registry.names()["conditions"]
        assert "unconscious" in listed and "prone" in listed
        assert registry.summary()["counts"]["conditions"] == len(listed)
        assert registry.source_of("conditions", "prone") == "engine"

    def test_a_fight_in_exclude_mode_can_still_drop_a_creature(self, pack: Path) -> None:
        registry = load_packs([pack], builtin="exclude", include_environment=False)
        attacker = make_creature("Vale Stalker", registry=registry, label="A", team="a")
        victim = make_creature("Vale Stalker", registry=registry, label="B", team="b")
        victim.max_hp = 1
        victim.hp = 1
        rng = Random(3)
        encounter = Encounter(
            [attacker, victim], rng,
            spellbook=registry.spells,
            items=registry.items,
            condition_effects=registry.condition_effects,
        )
        victim.take_damage(1)
        assert victim.dying
        # The state view must render too, which is where a stray `.value` would show.
        rendered = next(
            entry for entry in encounter.state()["combatants"] if entry["name"] == "B"
        )
        assert "unconscious" in rendered["conditions"]
        assert "prone" in rendered["conditions"]
        encounter.advance(rng)


class TestDuplicatesAndOverrides:
    def test_two_packs_claiming_one_name_fail_and_name_both(self, tmp_path: Path) -> None:
        first = write_pack(tmp_path / "a", "one.json", CAMPAIGN)
        second = write_pack(tmp_path / "b", "two.json", CAMPAIGN)
        with pytest.raises(ContentError) as caught:
            load_packs([first, second], include_environment=False)
        message = str(caught.value)
        assert "Vale Stalker" in message
        assert str(first) in message and str(second) in message

    def test_colliding_with_a_builtin_fails_unless_declared(self, tmp_path: Path) -> None:
        payload = {
            "pack": "shadow", "provenance": "test",
            "creatures": [
                {"name": "Goblin Warrior", "ac": 99, "max_hp": 99, "provenance": "test"}
            ],
        }
        path = write_pack(tmp_path, "shadow.json", payload)
        with pytest.raises(ContentError, match="overrides"):
            load_packs([path], include_environment=False)

    def test_an_explicit_override_replaces_the_builtin(self, tmp_path: Path) -> None:
        payload = {
            "pack": "shadow", "provenance": "test",
            "creatures": [
                {
                    "name": "Goblin Warrior", "ac": 99, "max_hp": 99,
                    "overrides": True, "provenance": "test",
                }
            ],
        }
        path = write_pack(tmp_path, "shadow.json", payload)
        registry = load_packs([path], include_environment=False)
        assert registry.creatures["Goblin Warrior"]["ac"] == 99
        assert registry.source_of("creatures", "Goblin Warrior") == str(path)

    def test_two_overrides_at_the_same_level_are_still_ambiguous(
        self, tmp_path: Path
    ) -> None:
        # Packs at one level load in path order, so "which override wins" would be an
        # accident of filenames rather than a decision anyone made.
        payload = {
            "pack": "shadow", "provenance": "test",
            "creatures": [
                {
                    "name": "Goblin Warrior", "ac": 99, "max_hp": 99,
                    "overrides": True, "provenance": "test",
                }
            ],
        }
        first = write_pack(tmp_path, "a.json", payload)
        second = write_pack(tmp_path, "b.json", payload)
        with pytest.raises(ContentError, match="same level"):
            load_packs([first, second], include_environment=False)

    def test_an_override_of_nothing_is_a_warning_not_an_error(
        self, tmp_path: Path
    ) -> None:
        # In exclude mode this is the normal case, so it must not fail; but a typo is
        # worth surfacing.
        payload = {
            "pack": "shadow", "provenance": "test",
            "creatures": [
                {
                    "name": "Gobln Warrior", "ac": 9, "max_hp": 9,
                    "overrides": True, "provenance": "test",
                }
            ],
        }
        path = write_pack(tmp_path, "typo.json", payload)
        registry = load_packs([path], include_environment=False)
        assert any("declares an override" in w.problem for w in registry.warnings)

    def test_one_file_named_twice_loads_once(self, pack: Path) -> None:
        # Naming a path that the environment already loads must not report every record
        # in it as colliding with itself.
        registry = load_packs([pack], env={"FIVEE_SIM_CONTENT": str(pack)})
        assert "Vale Stalker" in registry.creatures
        assert [info.label for info in registry.packs].count(str(pack)) == 1

    def test_the_same_path_given_twice_in_one_call_loads_once(self, pack: Path) -> None:
        registry = load_packs([pack, pack], include_environment=False)
        assert "Vale Stalker" in registry.creatures

    def test_a_directory_and_a_file_inside_it_do_not_collide(self, tmp_path: Path) -> None:
        inner = write_pack(tmp_path / "packs", "vale.json", CAMPAIGN)
        registry = load_packs([tmp_path / "packs", inner], include_environment=False)
        assert "Vale Stalker" in registry.creatures

    def test_a_name_defined_twice_in_one_pack_is_reported(self, tmp_path: Path) -> None:
        payload = {
            "pack": "twice", "provenance": "test",
            "items": [
                {"name": "Rope", "use": {"heal": "1d1"}, "provenance": "test"},
                {"name": "Rope", "use": {"heal": "2d2"}, "provenance": "test"},
            ],
        }
        path = write_pack(tmp_path, "twice.json", payload)
        assert any(
            "defined twice in the same pack" in p
            for p in problems(validate([path], include_environment=False))
        )


class TestDiagnostics:
    def check(self, tmp_path: Path, payload: dict[str, Any]) -> list[str]:
        path = write_pack(tmp_path, "bad.json", payload)
        return problems(validate([path], include_environment=False))

    def test_a_pack_without_provenance_is_refused(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {"pack": "x", "creatures": []})
        assert any("where its content came from" in p for p in found)

    def test_a_record_without_provenance_is_refused(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {
            "pack": "x", "provenance": "test",
            "spells": [{"name": "Bolt", "level": 1}],
        })
        assert any("required" in p for p in found)

    def test_a_bad_dice_expression_names_the_expression(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {
            "pack": "x", "provenance": "test",
            "spells": [{
                "name": "Bolt", "level": 1, "damage": "2x8",
                "damage_type": "fire", "provenance": "test",
            }],
        })
        assert any("not a dice expression" in p for p in found)

    def test_a_bad_damage_type_lists_the_valid_ones(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {
            "pack": "x", "provenance": "test",
            "spells": [{
                "name": "Bolt", "level": 1, "damage": "2d8",
                "damage_type": "sonic", "provenance": "test",
            }],
        })
        assert any("'sonic' is not valid" in p and "thunder" in p for p in found)

    def test_an_unknown_record_key_is_refused_rather_than_dropped(
        self, tmp_path: Path
    ) -> None:
        # This is the whole reason validation is strict: a mistyped key would produce a
        # creature that fights wrongly and looks entirely fine.
        found = self.check(tmp_path, {
            "pack": "x", "provenance": "test",
            "creatures": [{
                "name": "Thing", "ac": 10, "max_hp": 10,
                "attack_bonuses": 5, "provenance": "test",
            }],
        })
        assert any("unknown key" in p for p in found)

    def test_an_unknown_top_level_section_is_refused(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {
            "pack": "x", "provenance": "test", "monsters": [],
        })
        assert any("unknown top-level key" in p for p in found)

    def test_an_unknown_condition_flag_lists_the_valid_flags(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {
            "pack": "x", "provenance": "test",
            "conditions": [{
                "name": "hexed", "provenance": "test",
                "effects": {"deals_double_damage": True},
            }],
        })
        assert any("not an effect this engine can apply" in p for p in found)
        assert any("own_attacks_have_disadvantage" in p for p in found)

    def test_a_condition_no_pack_defines_is_caught_across_the_merged_set(
        self, tmp_path: Path
    ) -> None:
        found = self.check(tmp_path, {
            "pack": "x", "provenance": "test",
            "spells": [{
                "name": "Hex", "level": 1, "condition": "vale-cursed",
                "save_ability": "wisdom", "provenance": "test",
            }],
        })
        assert any("which no loaded pack defines" in p for p in found)

    def test_a_condition_defined_in_a_sibling_pack_resolves(self, tmp_path: Path) -> None:
        # The same reference is fine once the merge can see the definition, which is
        # why validation merges before cross-checking.
        conditions = write_pack(tmp_path, "conditions.json", {
            "pack": "c", "provenance": "test",
            "conditions": [{
                "name": "vale-cursed", "provenance": "test",
                "effects": {"own_attacks_have_disadvantage": True},
            }],
        })
        spells = write_pack(tmp_path, "spells.json", {
            "pack": "s", "provenance": "test",
            "spells": [{
                "name": "Hex", "level": 1, "condition": "vale-cursed",
                "save_ability": "wisdom", "provenance": "test",
            }],
        })
        assert not problems(validate([conditions, spells], include_environment=False))

    def test_an_item_that_does_nothing_is_refused(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {
            "pack": "x", "provenance": "test",
            "items": [{"name": "Pebble", "use": {}, "provenance": "test"}],
        })
        assert any("heal, deal damage, or apply a condition" in p for p in found)

    def test_several_problems_in_one_record_are_all_reported(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {
            "pack": "x", "provenance": "test",
            "spells": [{
                "name": "Bolt", "level": 1, "damage": "nonsense",
                "damage_type": "sonic", "wibble": 1, "provenance": "test",
            }],
        })
        assert len(found) >= 3, found

    def test_a_creature_without_provenance_is_refused(self, tmp_path: Path) -> None:
        # Creatures were the one section that skipped this check, which is the section
        # where the licence boundary actually bites.
        found = self.check(tmp_path, {
            "pack": "x", "provenance": "test",
            "creatures": [{"name": "Thing", "ac": 10, "max_hp": 10}],
        })
        assert any("required" in p for p in found)

    def test_hp_and_position_are_not_creature_record_keys(self, tmp_path: Path) -> None:
        # make_creature always starts a creature at full hit points and takes position
        # as a per-instance argument, so accepting them in a record would accept a key
        # and silently drop it.
        found = self.check(tmp_path, {
            "pack": "x", "provenance": "test",
            "creatures": [{
                "name": "Thing", "ac": 10, "max_hp": 10, "provenance": "test",
                "hp": 4, "position": 15,
            }],
        })
        assert sum("unknown key" in p for p in found) == 2, found

    @pytest.mark.parametrize(
        "key,wrong",
        [
            ("team", 5), ("ac", "high"), ("max_hp", "lots"), ("hit_dice", 3),
            ("speed", "fast"), ("abilities", []), ("save_bonuses", []),
            ("attacks", "none"), ("attacks_per_action", "two"), ("spells", "Fireball"),
            ("spell_slots", []), ("spell_save_dc", "hard"),
            ("spell_attack_bonus", "high"), ("items", []), ("conditions", "prone"),
            ("immunities", "fire"), ("resistances", "cold"), ("vulnerabilities", "acid"),
            ("provenance", 1), ("unmodelled", "a trait"), ("overrides", "yes"),
        ],
    )
    def test_no_allowed_creature_key_escapes_validation(
        self, tmp_path: Path, key: str, wrong: Any
    ) -> None:
        """Every key we accept must be checked, or the loader's promise is false.

        ``data/__init__.py`` states that records reaching ``make_creature`` are already
        validated, so construction does not re-check them. A key that is accepted and
        unvalidated breaks that: it passes ``content_validate`` and then raises a bare
        ``ValueError`` part-way into building an encounter. Parametrised over the
        allowed set so adding a key without validating it fails here.

        The diagnostic has to *name* the key. Asserting only that something failed
        would pass on a validator that rejected the record for an unrelated reason —
        and would still pass if the key under test were silently ignored while some
        other field complained.
        """
        record: dict[str, Any] = {
            "name": "Thing", "ac": 10, "max_hp": 10, "provenance": "test",
        }
        record[key] = wrong
        path = write_pack(tmp_path, "bad.json", {
            "pack": "x", "provenance": "test", "creatures": [record],
        })
        blamed = fields(validate([path], include_environment=False))
        assert key in blamed, (
            f"{key}={wrong!r} passed validation unchecked; "
            f"diagnostics blamed {blamed or 'nothing'}"
        )

    def test_a_creature_naming_an_undefined_spell_or_item_warns(
        self, tmp_path: Path
    ) -> None:
        path = write_pack(tmp_path, "x.json", {
            "pack": "x", "provenance": "test",
            "creatures": [{
                "name": "Thing", "ac": 10, "max_hp": 10, "provenance": "test",
                "spells": ["Nonexistent Bolt"], "items": {"Nonexistent Flask": 1},
            }],
        })
        diagnostics = validate([path], include_environment=False)
        assert not problems(diagnostics), "these are warnings, not load failures"
        warned = problems(diagnostics, Severity.WARNING)
        assert any("Nonexistent Bolt" in p for p in warned)
        assert any("Nonexistent Flask" in p for p in warned)

    def test_the_bundled_packs_validate_clean(self) -> None:
        # The built-in slice goes through this same parser, so a malformed row could
        # never ship unnoticed.
        assert not validate(include_environment=False)


class TestAttackRiderValidation:
    """The rider keys an attack record may carry, and the pairings they enforce."""

    def check(self, tmp_path: Path, attack: dict[str, Any]) -> list[str]:
        payload = {
            "pack": "x", "provenance": "test",
            "creatures": [{
                "name": "Thing", "ac": 10, "max_hp": 10, "provenance": "test",
                "attacks": [{
                    "name": "Claw", "attack_bonus": 4, "damage": "1d6",
                    "damage_type": "slashing", **attack,
                }],
            }],
        }
        path = write_pack(tmp_path, "riders.json", payload)
        return problems(validate([path], include_environment=False))

    def test_bonus_damage_without_its_type_is_refused(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {"bonus_damage": "1d4"})
        assert any("defended against its own type" in p for p in found)

    def test_a_bonus_type_without_bonus_damage_is_refused(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {"bonus_damage_type": "fire"})
        assert any("names a type for no damage" in p for p in found)

    def test_an_unknown_on_hit_condition_is_caught_across_the_merged_set(
        self, tmp_path: Path
    ) -> None:
        found = self.check(tmp_path, {"on_hit_condition": "vale-toxin"})
        assert any("which no loaded pack defines" in p for p in found)

    def test_an_invalid_expiry_lists_the_valid_ones(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {
            "on_hit_condition": "poisoned", "on_hit_expiry": "next_tuesday",
        })
        assert any(
            "'next_tuesday' is not valid" in p
            and "start_of_attacker_next_turn" in p
            for p in found
        )

    def test_a_save_ability_without_a_dc_is_refused(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {
            "on_hit_condition": "poisoned", "on_hit_save_ability": "constitution",
        })
        assert any("required when on_hit_save_ability is present" in p for p in found)

    def test_a_save_dc_without_an_ability_is_refused(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {
            "on_hit_condition": "poisoned", "on_hit_save_dc": 11,
        })
        assert any("required when on_hit_save_dc is present" in p for p in found)

    def test_a_save_or_expiry_without_a_condition_is_refused(
        self, tmp_path: Path
    ) -> None:
        found = self.check(tmp_path, {"on_hit_expiry": "end_of_target_next_turn"})
        assert any("no condition to ride the hit" in p for p in found)

    def test_a_mistyped_rider_key_is_still_refused(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {"advantage_bonus_dmg": "1d4"})
        assert any("unknown key" in p for p in found)

    def test_a_full_rider_attack_round_trips_through_make_creature(
        self, tmp_path: Path
    ) -> None:
        from fivee_sim.kernel.actions import RiderExpiry
        from fivee_sim.kernel.dice import Dice
        from fivee_sim.kernel.rules import Ability, DamageType

        path = write_pack(tmp_path, "riders.json", {
            "pack": "x", "provenance": "test",
            "creatures": [{
                "name": "Thing", "ac": 10, "max_hp": 10, "provenance": "test",
                "attacks": [{
                    "name": "Claw", "attack_bonus": 4, "damage": "1d6",
                    "damage_type": "slashing",
                    "bonus_damage": "1d4", "bonus_damage_type": "fire",
                    "advantage_bonus_damage": "1d4",
                    "on_hit_condition": "poisoned",
                    "on_hit_save_ability": "constitution", "on_hit_save_dc": 11,
                    "on_hit_expiry": "start_of_attacker_next_turn",
                }],
            }],
        })
        registry = load_packs([path], include_environment=False)
        thing = make_creature("Thing", registry=registry)
        option = thing.attacks[0]
        assert option.bonus_damage == Dice(1, 4)
        assert option.bonus_damage_type is DamageType.FIRE
        assert option.advantage_bonus_damage == Dice(1, 4)
        assert option.on_hit_condition == "poisoned"
        assert option.on_hit_save_ability is Ability.CONSTITUTION
        assert option.on_hit_save_dc == 11
        assert option.on_hit_expiry is RiderExpiry.START_OF_ATTACKER_NEXT_TURN


class TestConstructionSeam:
    """``Creature.from_record`` takes what a registry would otherwise be asked for.

    Construction lives in ``model`` because ``model`` owns creatures, but a
    ``ContentRegistry`` is ``content``'s concept and importing it there would
    invert the layering. So the two values construction cannot derive from the
    record alone — the condition table the creature reads its conditions
    against, and the provenance to fall back on — arrive as arguments.
    ``make_creature`` is the caller that pulls both off a registry.

    These pin the seam itself. Field-by-field mapping is covered above through
    ``make_creature``, which is the same code path.
    """

    def record(self, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "name": "Vale Stalker", "ac": 14, "max_hp": 22, "provenance": "test",
        }
        base.update(overrides)
        return base

    def test_the_record_provenance_wins_when_it_has_one(self) -> None:
        creature = Creature.from_record(
            self.record(provenance="Original content"),
            condition_effects=EFFECTS,
            source="bundled:monsters.json",
        )
        assert creature.provenance == "Original content"

    def test_the_source_is_the_fallback_when_the_record_omits_provenance(self) -> None:
        # Content validation makes provenance required, so this only fires for a
        # record built in code. It still has to name where the creature came
        # from: the licence boundary turns on every creature carrying one.
        bare = self.record()
        del bare["provenance"]
        creature = Creature.from_record(
            bare, condition_effects=EFFECTS, source="bundled:monsters.json"
        )
        assert creature.provenance == "bundled:monsters.json"

    def test_the_given_condition_table_is_what_the_creature_reads(self) -> None:
        # A pack-defined condition is a plain str with no entry in EFFECTS. If
        # construction consulted a module-level table instead of this argument,
        # the creature would be carrying a condition nothing could resolve.
        table = {**EFFECTS, "vale-cursed": ConditionEffect(incapacitated=True)}
        creature = Creature.from_record(
            self.record(conditions=["vale-cursed"]),
            condition_effects=table,
            source="test",
        )
        assert creature.conditions == {"vale-cursed"}
        assert not creature.active

    def test_label_and_team_rename_the_instance(self) -> None:
        # Two of a kind in one fight need distinct names: combatant names are
        # how the encounter identifies them.
        creature = Creature.from_record(
            self.record(), condition_effects=EFFECTS, source="test",
            label="Stalker A", team="party",
        )
        assert creature.name == "Stalker A"
        assert creature.team == "party"
        # And the record's own values stand when nothing overrides them.
        default = Creature.from_record(
            self.record(), condition_effects=EFFECTS, source="test"
        )
        assert default.name == "Vale Stalker"
        assert default.team == "monsters"


class TestAreaDeclaration:
    """``shape`` and ``radius`` are one declaration, and they have to agree.

    Only ``radius`` is load-bearing — ``model.encounter`` and ``analytics.montecarlo``
    both decide "is this an area?" from it alone, and nothing anywhere reads ``shape``.
    A pack author has no way to know that: ``shape`` is the field that *looks* like the
    one declaring an area, and the docs tell them to set both. So a record giving one
    without the other is a mistake the loader has to name, or the spell quietly does
    something other than what the record says.
    """

    def check(self, tmp_path: Path, spell: dict[str, Any]) -> list[Any]:
        path = write_pack(tmp_path, "vale.json", {
            "pack": "x", "provenance": "test", "spells": [spell],
        })
        return validate([path], include_environment=False)

    def blast(self, **overrides: Any) -> dict[str, Any]:
        spell: dict[str, Any] = {
            "name": "Vale Blast", "level": 3, "save_ability": "dexterity",
            "damage": "6d6", "damage_type": "fire", "range_feet": 120,
            "provenance": "test",
        }
        spell.update(overrides)
        return spell

    def test_an_area_shape_without_a_radius_is_refused(self, tmp_path: Path) -> None:
        # The defect this class exists for. The author declared a sphere and got a
        # spell that hits exactly one creature, with nothing said about it.
        found = problems(self.check(tmp_path, self.blast(shape="sphere")))
        assert any("radius" in p for p in found), found
        assert any("sphere" in p for p in found), found

    def test_declaring_single_target_alongside_a_radius_is_refused(
        self, tmp_path: Path
    ) -> None:
        # The mirror image, and silent the other way round: the radius wins, so the
        # spell sweeps an area while the record claims one target. Matched on wording
        # unique to this branch — "radius" and "single" both appear in the sphere
        # message too, so a looser assertion would not pin which branch fired, and
        # branch fallthrough in this block is a mistake already made once.
        found = problems(self.check(tmp_path, self.blast(shape="single", radius=20)))
        assert any("decides who is caught" in p for p in found), found
        assert any("drop the radius" in p for p in found), found

    def test_a_radius_without_a_shape_warns_and_still_loads(self, tmp_path: Path) -> None:
        # Incomplete, not wrong: radius alone already resolves as an area. So this
        # stays a warning, and the message must not claim a consequence that does not
        # happen — the old one said "it will be treated as single-target", which was
        # simply false.
        path = write_pack(tmp_path, "vale.json", {
            "pack": "x", "provenance": "test",
            "spells": [self.blast(radius=20)],
        })
        diagnostics = validate([path], include_environment=False)
        assert not problems(diagnostics), "an area that works must not fail to load"
        warned = problems(diagnostics, Severity.WARNING)
        assert any("shape" in p for p in warned), warned
        assert not any("single-target" in p for p in warned), warned
        registry = load_packs([path], include_environment=False)
        assert registry.spells["Vale Blast"].radius == 20

    def test_a_shape_and_radius_that_agree_load_clean(self, tmp_path: Path) -> None:
        # The regression guard: the check must not cost a correct pack anything.
        path = write_pack(tmp_path, "vale.json", {
            "pack": "x", "provenance": "test",
            "spells": [self.blast(shape="sphere", radius=20)],
        })
        assert not validate([path], include_environment=False)
        assert load_packs([path], include_environment=False).spells["Vale Blast"].radius == 20

    def test_a_single_target_spell_declaring_neither_is_clean(self, tmp_path: Path) -> None:
        assert not self.check(tmp_path, self.blast())

    def test_an_unparseable_shape_is_reported_once_and_not_second_guessed(
        self, tmp_path: Path
    ) -> None:
        # "ring" is not a shape this engine knows. That is the enum's error to report;
        # the agreement check must not also announce what the record "declares",
        # because it does not know.
        found = problems(self.check(tmp_path, self.blast(shape="ring", radius=20)))
        assert found == [
            "'ring' is not valid; must be one of: single, sphere, cone, line, cube"
        ], found


class TestPathSafety:
    def test_a_missing_path_is_reported(self, tmp_path: Path) -> None:
        found = problems(validate([tmp_path / "nope.json"], include_environment=False))
        assert any("does not exist" in p for p in found)

    def test_a_non_json_file_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "pack.yaml"
        path.write_text("pack: x", encoding="utf-8")
        found = problems(validate([path], include_environment=False))
        assert any(".json" in p for p in found)

    def test_malformed_json_names_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        found = problems(validate([path], include_environment=False))
        assert any("not valid JSON" in p for p in found)

    def test_a_file_over_the_size_cap_fails_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(content_module, "MAX_PACK_BYTES", 10)
        path = write_pack(tmp_path, "big.json", CAMPAIGN)
        found = problems(validate([path], include_environment=False))
        assert any("over the" in p and "byte limit" in p for p in found)

    def test_a_symlink_out_of_a_scanned_directory_is_not_read(
        self, tmp_path: Path
    ) -> None:
        outside = write_pack(tmp_path / "outside", "secret.json", CAMPAIGN)
        packs = tmp_path / "packs"
        packs.mkdir()
        link = packs / "escape.json"
        try:
            link.symlink_to(outside)
        except OSError:  # pragma: no cover - platform without symlinks
            pytest.skip("symlinks unavailable")
        found = problems(validate([packs], include_environment=False))
        assert any("outside the content directory" in p for p in found)
        # Refused loudly rather than skipped: a file the author put in their content
        # directory that silently did not load would be worse than a failed load.
        with pytest.raises(ContentError, match="outside the content directory"):
            load_packs([packs], include_environment=False)


class TestEnvironment:
    def test_the_content_variable_is_read(
        self, tmp_path: Path, pack: Path
    ) -> None:
        registry = load_packs(builtin="include", env={"FIVEE_SIM_CONTENT": str(pack)})
        assert "Vale Stalker" in registry.creatures

    def test_several_entries_are_separated_by_the_path_separator(
        self, tmp_path: Path
    ) -> None:
        first = write_pack(tmp_path / "a", "one.json", CAMPAIGN)
        second = write_pack(tmp_path / "b", "two.json", {
            "pack": "b", "provenance": "test",
            "items": [{"name": "Rope", "use": {"heal": "1d1"}, "provenance": "test"}],
        })
        joined = os.pathsep.join([str(first), str(second)])
        registry = load_packs(env={"FIVEE_SIM_CONTENT": joined})
        assert "Vale Stalker" in registry.creatures
        assert "Rope" in registry.items

    def test_the_project_directory_is_used_when_the_variable_is_unset(
        self, tmp_path: Path
    ) -> None:
        write_pack(tmp_path / ".fivee-sim" / "content", "vale.json", CAMPAIGN)
        registry = load_packs(env={"CLAUDE_PROJECT_DIR": str(tmp_path)})
        assert "Vale Stalker" in registry.creatures

    def test_the_variable_wins_over_the_project_directory(self, tmp_path: Path) -> None:
        # Someone who exported the variable should not silently also load whatever sits
        # in the repository they happen to be standing in.
        write_pack(tmp_path / ".fivee-sim" / "content", "vale.json", CAMPAIGN)
        other = write_pack(tmp_path / "elsewhere", "rope.json", {
            "pack": "b", "provenance": "test",
            "items": [{"name": "Rope", "use": {"heal": "1d1"}, "provenance": "test"}],
        })
        registry = load_packs(env={
            "FIVEE_SIM_CONTENT": str(other),
            "CLAUDE_PROJECT_DIR": str(tmp_path),
        })
        assert "Rope" in registry.items
        assert "Vale Stalker" not in registry.creatures


class TestCustomConditions:
    """A pack's condition is a plain string, not a ``Condition`` member.

    These are the tests that catch a leftover ``.value``: the built-in suite cannot,
    because every SRD condition is a ``StrEnum`` member and answers to both.
    """

    def registry(self, pack: Path) -> Any:
        return load_packs([pack], include_environment=False)

    def test_a_custom_condition_changes_an_attack_roll(self, pack: Path) -> None:
        registry = self.registry(pack)
        assert compute_attack_advantage(
            attacker_conditions=["vale-cursed"],
            target_conditions=[],
            distance=5,
            condition_effects=registry.condition_effects,
        ) is Advantage.DISADVANTAGE

    def test_an_unknown_condition_is_refused_with_the_available_names(
        self, pack: Path
    ) -> None:
        from fivee_sim.kernel.conditions import UnknownCondition

        registry = self.registry(pack)
        with pytest.raises(UnknownCondition, match="vale-cursed"):
            compute_attack_advantage(
                attacker_conditions=["vale-blessed"],
                target_conditions=[],
                distance=5,
                condition_effects=registry.condition_effects,
            )

    def test_a_custom_condition_survives_narration_and_state(self, pack: Path) -> None:
        registry = self.registry(pack)
        hero = make_creature("Vale Stalker", registry=registry, label="A", team="a")
        villain = make_creature("Vale Stalker", registry=registry, label="B", team="b")
        villain.position = 5
        villain.items = {"Cursed Needle": 1}
        rng = Random(5)
        encounter = Encounter(
            [villain, hero], rng,
            spellbook=registry.spells,
            items=registry.items,
            condition_effects=registry.condition_effects,
        )
        advance_to(encounter, "B", rng)
        events = encounter.act(
            Action(kind=ActionKind.USE_ITEM, item="Cursed Needle", target="A"), rng
        )
        # The event detail is what Claude narrates from, so it must render.
        assert any("vale-cursed" in event.detail for event in events)
        assert "vale-cursed" in hero.conditions
        state = encounter.state()
        held = next(c for c in state["combatants"] if c["name"] == "A")
        assert "vale-cursed" in held["conditions"]

    def test_a_custom_condition_reaches_the_incapacitated_check(
        self, tmp_path: Path
    ) -> None:
        payload = {
            "pack": "x", "provenance": "test",
            "conditions": [{
                "name": "vale-frozen", "provenance": "test",
                "effects": {"incapacitated": True, "speed_zero": True},
            }],
        }
        path = write_pack(tmp_path, "frozen.json", payload)
        registry = load_packs([path], include_environment=False)
        hero = make_creature("Goblin Warrior", registry=registry, label="A", team="a")
        hero.add_condition("vale-frozen")
        assert hero.active is False

    def test_a_pack_concentration_spell_releases_its_own_condition(
        self, tmp_path: Path
    ) -> None:
        """The release mechanism is driven by pack data, not by the SRD spell list.

        Nothing here is an SRD name: the spell, the condition and the caster are the
        pack's. If the release were special-cased on Hold Person or on the
        ``Condition`` enum, this is the test that would fail.
        """
        payload = {
            "pack": "x", "provenance": "test",
            "conditions": [{
                "name": "vale-frozen", "provenance": "test",
                "effects": {"incapacitated": True, "speed_zero": True},
            }],
            "spells": [{
                "name": "Vale Binding", "level": 1, "provenance": "test",
                "save_ability": "wisdom", "condition": "vale-frozen",
                "concentration": True, "range_feet": 60,
            }],
        }
        path = write_pack(tmp_path, "binding.json", payload)
        registry = load_packs([path], include_environment=False)
        binder = make_creature("Goblin Warrior", registry=registry, label="A", team="a")
        binder.spells = ("Vale Binding",)
        binder.spell_slots = {1: 1}
        binder.spell_save_dc = 20
        victim = make_creature("Goblin Warrior", registry=registry, label="B", team="b")
        victim.position = 10
        # A third combatant keeps the fight running once the binder goes down, so
        # the encounter still takes turns and the release is observable.
        ally = make_creature("Goblin Warrior", registry=registry, label="C", team="a")
        ally.position = 5
        rng = Random(5)
        encounter = Encounter(
            [binder, victim, ally], rng,
            spellbook=registry.spells,
            items=registry.items,
            condition_effects=registry.condition_effects,
        )
        advance_to(encounter, "A", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Vale Binding", target="B"), Random(3)
        )
        assert "vale-frozen" in victim.conditions
        assert binder.concentrating_on == "Vale Binding"

        binder.take_damage(binder.hp)
        encounter.advance(rng)
        assert binder.concentrating_on is None
        assert "vale-frozen" not in victim.conditions


class TestCustomTerrain:
    """A pack's terrain kind is a plain string, resolved through the registry's table.

    The :class:`TestCustomConditions` analogue: the built-in suite exercises only
    the engine's own ``TERRAIN`` dict, so these are the tests that prove a
    pack-defined kind flows through the loader into ``terrain_effect_of`` — and
    that unknown kinds and unknown effect fields are refused with lists the
    author can act on.
    """

    THORNS: dict[str, Any] = {
        "pack": "crimson-vale-terrain",
        "provenance": "Original content, (c) 2026 Example Campaign",
        "terrain": [{
            "name": "vale-thornfield",
            "description": "Knee-high thorns: slow going, and something to duck behind.",
            "effects": {"move_cost_multiplier": 2, "cover": 1},
            "provenance": "Original content",
        }],
    }

    def test_a_pack_terrain_kind_loads_and_resolves(self, tmp_path: Path) -> None:
        from fivee_sim.kernel.grid import TERRAIN, terrain_effect_of

        path = write_pack(tmp_path, "thorns.json", self.THORNS)
        registry = load_packs([path], include_environment=False)
        assert "vale-thornfield" not in TERRAIN
        effect = terrain_effect_of("vale-thornfield", registry.terrain_effects)
        assert effect.move_cost_multiplier == 2
        assert effect.cover == 1
        assert effect.passable and not effect.opaque
        assert registry.source_of("terrain", "vale-thornfield") == str(path)

    def test_the_builtin_kinds_arrive_through_the_same_pack_path(
        self, tmp_path: Path
    ) -> None:
        from fivee_sim.kernel.grid import TERRAIN, terrain_effect_of

        path = write_pack(tmp_path, "thorns.json", self.THORNS)
        registry = load_packs([path], include_environment=False)
        assert terrain_effect_of("difficult", registry.terrain_effects) == (
            TERRAIN["difficult"]
        )
        assert registry.source_of("terrain", "difficult") == "bundled:terrain"
        # And exclude mode removes them, exactly like every other bundled record.
        alone = load_packs([path], builtin="exclude", include_environment=False)
        assert "difficult" not in alone.terrain_effects
        assert "vale-thornfield" in alone.terrain_effects

    def test_a_pack_terrain_kind_works_end_to_end_on_a_battle_map(
        self, tmp_path: Path
    ) -> None:
        # The deferred assertion from the terrain content step: a pack-defined
        # kind, on a real map, actually slows movement and actually screens.
        from fivee_sim.kernel.grid import CoverGrade
        from fivee_sim.model.battlemap import BattleMap

        path = write_pack(tmp_path, "thorns.json", self.THORNS)
        registry = load_packs([path], include_environment=False)
        battle_map = BattleMap.flat(
            name="thornfield", width=5, height=1,
            terrain={(2, 0): "vale-thornfield"},
            provenance="test fixture",
        )
        hero = make_creature("Goblin Warrior", registry=registry, label="A", team="a")
        villain = make_creature(
            "Goblin Warrior", registry=registry, label="B", team="b",
            position=(20, 0),
        )
        rng = Random(5)
        encounter = Encounter(
            [hero, villain], rng,
            condition_effects=registry.condition_effects,
            battle_map=battle_map,
            terrain_effects=registry.terrain_effects,
        )
        # cover: 1 — the thorns screen whoever stands behind them.
        assert encounter.cover_between("A", "B") is CoverGrade.HALF
        advance_to(encounter, "A", rng)
        # move_cost_multiplier: 2 — three squares, the thorny one at double.
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=(15, 0)), rng)
        move = next(event for event in events if event.kind == "move")
        assert move.data["cost"] == 20

    def test_an_unknown_kind_is_refused_with_the_loaded_kinds(
        self, tmp_path: Path
    ) -> None:
        from fivee_sim.kernel.grid import UnknownTerrain, terrain_effect_of

        path = write_pack(tmp_path, "thorns.json", self.THORNS)
        registry = load_packs([path], include_environment=False)
        with pytest.raises(UnknownTerrain, match="vale-thornfield"):
            terrain_effect_of("vale-swamp", registry.terrain_effects)

    def test_an_unknown_effect_field_is_refused_listing_the_valid_ones(
        self, tmp_path: Path
    ) -> None:
        payload = {
            "pack": "x", "provenance": "test",
            "terrain": [{
                "name": "vale-mire", "provenance": "test",
                "effects": {"levitation": True},
            }],
        }
        path = write_pack(tmp_path, "mire.json", payload)
        with pytest.raises(ContentError) as caught:
            load_packs([path], include_environment=False)
        message = str(caught.value)
        assert "levitation" in message
        assert "new terrain kinds but not new kinds of effect" in message
        assert "move_cost_multiplier" in message and "opaque" in message

    def test_effect_values_are_type_checked(self, tmp_path: Path) -> None:
        payload = {
            "pack": "x", "provenance": "test",
            "terrain": [{
                "name": "vale-mire", "provenance": "test",
                "effects": {"opaque": 1, "cover": 9, "move_cost_multiplier": 0},
            }],
        }
        path = write_pack(tmp_path, "mire.json", payload)
        with pytest.raises(ContentError) as caught:
            load_packs([path], include_environment=False)
        message = str(caught.value)
        assert "opaque must be true or false" in message
        assert "cover must be a whole number from 0 (none) to 3 (total)" in message
        assert "move_cost_multiplier must be a whole number of 1 or more" in message


class TestSpellShapeSchema:
    """The shape fields pair with their measurements, checked at load."""

    def spell_pack(self, tmp_path: Path, record: dict[str, Any]) -> Path:
        payload = {
            "pack": "shapes", "provenance": "test",
            "spells": [{
                "name": "Test Spell", "provenance": "test", "level": 1,
                "save_ability": "dexterity", "damage": "3d6",
                "damage_type": "fire", **record,
            }],
        }
        return write_pack(tmp_path, "shapes.json", payload)

    def test_a_cone_without_a_length_is_refused(self, tmp_path: Path) -> None:
        path = self.spell_pack(tmp_path, {"shape": "cone"})
        with pytest.raises(ContentError, match="a cone needs a length"):
            load_packs([path], include_environment=False)

    def test_a_cube_without_a_size_is_refused(self, tmp_path: Path) -> None:
        path = self.spell_pack(tmp_path, {"shape": "cube"})
        with pytest.raises(ContentError, match="a cube needs a size"):
            load_packs([path], include_environment=False)

    def test_a_sphere_without_a_radius_is_refused(self, tmp_path: Path) -> None:
        path = self.spell_pack(tmp_path, {"shape": "sphere"})
        with pytest.raises(ContentError, match="a sphere needs a radius"):
            load_packs([path], include_environment=False)

    @pytest.mark.parametrize(
        ("record", "checks"),
        [
            ({"shape": "cone", "length": 15},
             {"shape": SpellShape.CONE, "length": 15}),
            ({"shape": "line", "length": 30, "width": 5},
             {"shape": SpellShape.LINE, "length": 30}),
            ({"shape": "cube", "size": 10},
             {"shape": SpellShape.CUBE, "size": 10}),
        ],
        ids=["cone-has-a-length", "line-has-a-length", "cube-has-a-size"],
    )
    def test_each_shape_loads_with_its_measurement(
        self, tmp_path: Path, record: dict[str, Any], checks: dict[str, Any]
    ) -> None:
        path = self.spell_pack(tmp_path, record)
        spell = load_packs([path], include_environment=False).spells["Test Spell"]
        assert spell.is_area
        for field_name, expected in checks.items():
            assert getattr(spell, field_name) == expected

    def test_a_legacy_radius_without_a_shape_still_resolves_as_a_sphere(
        self, tmp_path: Path
    ) -> None:
        from fivee_sim.content import validate
        from fivee_sim.kernel.spells import SpellShape

        path = self.spell_pack(tmp_path, {"radius": 20, "range_feet": 120})
        registry = load_packs([path], include_environment=False)
        spell = registry.spells["Test Spell"]
        assert spell.is_area
        assert spell.effective_shape is SpellShape.SPHERE
        # And it still warns, so the author is told the encoding is legacy.
        diagnostics = validate([path], include_environment=False)
        assert any("resolves as a sphere" in d.problem for d in diagnostics)


class TestDeterminism:
    def test_the_same_seed_gives_the_same_fight_with_packs_loaded(
        self, pack: Path
    ) -> None:
        registry = load_packs([pack], include_environment=False)

        def transcript(seed: int) -> list[tuple[str, str, str, str]]:
            rng = Random(seed)
            combatants = [
                make_creature("Vale Stalker", registry=registry, label="A", team="a"),
                make_creature("Goblin Warrior", registry=registry, label="B", team="b"),
            ]
            combatants[1].position = 5
            encounter = Encounter(
                combatants, rng,
                spellbook=registry.spells,
                items=registry.items,
                condition_effects=registry.condition_effects,
            )
            from fivee_sim.analytics.montecarlo import run_encounter

            run_encounter(encounter, rng, max_rounds=20)
            return [
                (e.kind, e.actor, e.target, e.detail) for e in encounter.log
            ]

        assert transcript(19) == transcript(19)


class TestContentTools:
    def test_status_reports_the_bundled_slice_by_default(self) -> None:
        status = api.content_status()
        assert status["builtin"] == "include"
        # Derived, not transcribed: the tool's job is to report what is actually
        # bundled, so the expectation is read off the same data rather than from a
        # literal that a new monster or condition would quietly falsify.
        assert status["counts"]["creatures"] == BUNDLED_CREATURES
        assert status["counts"]["conditions"] == BUNDLED_CONDITIONS

    def test_validate_reports_problems_without_loading_them(self, tmp_path: Path) -> None:
        path = write_pack(tmp_path, "bad.json", {"pack": "x", "creatures": []})
        result = api.content_validate([str(path)])
        assert result["ok"] is False
        assert result["errors"]
        # And nothing was adopted.
        assert api.content_status()["counts"]["creatures"] == BUNDLED_CREATURES

    def test_validate_passes_a_good_pack(self, pack: Path) -> None:
        result = api.content_validate([str(pack)])
        assert result["ok"] is True
        assert result["summary"] == "no problems found"

    def test_configure_loads_a_pack_and_bumps_the_generation(self, pack: Path) -> None:
        before = api.content_status()["generation"]
        status = api.content_configure([str(pack)])
        assert status["changed"] is True
        assert status["generation"] == before + 1
        assert "Vale Stalker" in api.lookup_rule()["creatures"]

    def test_configure_can_switch_to_exclude(self, pack: Path) -> None:
        api.content_configure([str(pack)], builtin="exclude")
        listing = api.lookup_rule()
        assert listing["builtin"] == "exclude"
        assert "Goblin Warrior" not in listing["creatures"]
        assert "Vale Stalker" in listing["creatures"]

    def test_configure_adds_rather_than_replaces_when_asked(self, tmp_path: Path) -> None:
        first = write_pack(tmp_path / "a", "one.json", CAMPAIGN)
        second = write_pack(tmp_path / "b", "two.json", {
            "pack": "b", "provenance": "test",
            "items": [{"name": "Rope", "use": {"heal": "1d1"}, "provenance": "test"}],
        })
        api.content_configure([str(first)])
        status = api.content_configure([str(second)], add=True)
        assert len(status["configured_paths"]) == 2
        assert "Rope" in api.lookup_rule()["items"]

    def test_a_failed_configure_leaves_the_working_content_alone(
        self, tmp_path: Path
    ) -> None:
        broken = write_pack(tmp_path, "broken.json", {"pack": "x", "creatures": []})
        before = api.content_status()
        with pytest.raises(api.ToolError, match="content not changed"):
            api.content_configure([str(broken)])
        after = api.content_status()
        assert after["generation"] == before["generation"]
        assert after["counts"] == before["counts"]

    def test_configure_with_nothing_to_change_is_refused(self) -> None:
        with pytest.raises(api.ToolError, match="nothing to change"):
            api.content_configure()

    def test_a_bad_mode_lists_the_valid_ones(self) -> None:
        with pytest.raises(api.ToolError, match="include, exclude"):
            api.content_configure(builtin="maybe")

    def test_lookup_reports_the_pack_a_custom_entry_came_from(self, pack: Path) -> None:
        api.content_configure([str(pack)])
        entry = api.lookup_rule("Vale Stalker")
        assert entry["kind"] == "creature"
        assert entry["source"] == str(pack)
        assert entry["unmodelled"] == [], "present but empty, so the skill's check holds"

    def test_lookup_finds_a_custom_condition(self, pack: Path) -> None:
        api.content_configure([str(pack)])
        entry = api.lookup_rule("vale-cursed")
        assert entry["kind"] == "condition"
        assert entry["effects"]["own_attacks_have_disadvantage"] is True
        assert "hunger" in entry["description"]

    def test_lookup_finds_a_custom_item(self, pack: Path) -> None:
        api.content_configure([str(pack)])
        entry = api.lookup_rule("Vale Draught")
        assert entry["kind"] == "item"
        assert entry["use"]["heal"] == "2d4+2"


class TestReconfigurationAndLiveFights:
    def test_a_fight_in_progress_keeps_the_content_it_started_with(
        self, pack: Path
    ) -> None:
        api.content_configure([str(pack)])
        created = api.encounter_create(
            [
                {"creature": "Vale Stalker", "label": "Stalker", "team": "monsters"},
                {"creature": "Goblin Warrior", "label": "Goblin", "team": "heroes",
                 "position": 5},
            ],
            seed=4,
        )
        encounter_id = str(created["encounter_id"])
        # Switching to exclude would have stripped the Goblin now taking its turn.
        api.content_configure([str(pack)], builtin="exclude")
        state = api.encounter_state(encounter_id)
        assert {c["name"] for c in state["combatants"]} == {"Stalker", "Goblin"}
        # The fight still steps, under the content it was built with.
        api.encounter_advance(encounter_id)

    def test_status_flags_encounters_running_on_older_content(self, pack: Path) -> None:
        created = api.encounter_create(
            [
                {"creature": "Goblin Warrior", "label": "A", "team": "a"},
                {"creature": "Goblin Warrior", "label": "B", "team": "b", "position": 5},
            ],
            seed=4,
        )
        api.content_configure([str(pack)])
        status = api.content_status()
        stale = status["encounters_on_older_content"]
        assert [entry["encounter_id"] for entry in stale] == [created["encounter_id"]]

    def test_a_new_fight_uses_the_new_content(self, pack: Path) -> None:
        api.content_configure([str(pack)])
        created = api.encounter_create(
            [
                {"creature": "Vale Stalker", "label": "A", "team": "a"},
                {"creature": "Vale Stalker", "label": "B", "team": "b", "position": 5},
            ],
            seed=4,
        )
        assert created["content_generation"] == api.content_status()["generation"]

    def test_analytics_stay_reproducible_across_a_reconfiguration(
        self, pack: Path
    ) -> None:
        # The batch binds its registry once. If it resolved content per iteration, a
        # reconfiguration landing mid-run would silently change the answer.
        combatants: list[dict[str, Any]] = [
            {"creature": "Goblin Warrior", "label": "A", "team": "a"},
            {"creature": "Goblin Warrior", "label": "B", "team": "b", "position": 5},
        ]
        first = api.simulate_rounds(combatants, iterations=20, seed=8)
        api.content_configure([str(pack)])
        second = api.simulate_rounds(combatants, iterations=20, seed=8)
        assert first["wins"] == second["wins"]
        assert first["rounds"] == second["rounds"]

    def test_items_from_a_pack_can_be_used_through_the_action_path(
        self, pack: Path
    ) -> None:
        api.content_configure([str(pack)])
        created = api.encounter_create(
            [
                {
                    "name": "Thora", "team": "heroes", "ac": 16, "max_hp": 30, "hp": 5,
                    "items": {"Vale Draught": 1},
                    "abilities": {"dexterity": 14},
                },
                {"creature": "Vale Stalker", "label": "Stalker", "team": "monsters",
                 "position": 60},
            ],
            seed=6,
        )
        encounter_id = str(created["encounter_id"])
        for _ in range(4):
            if api.encounter_state(encounter_id)["turn"] == "Thora":
                break
            api.encounter_advance(encounter_id)
        else:
            raise AssertionError("Thora never got a turn")
        result = api.encounter_act(encounter_id, "use_item", item="Vale Draught")
        thora = next(
            c for c in result["state"]["combatants"] if c["name"] == "Thora"
        )
        assert thora["hp"] > 5
        assert thora["items"] == {"Vale Draught": 0}
