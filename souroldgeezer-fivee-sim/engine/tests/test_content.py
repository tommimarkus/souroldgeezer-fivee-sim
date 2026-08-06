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

import dataclasses
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
from fivee_sim.kernel.actions import AttackKind, compute_attack_advantage
from fivee_sim.kernel.conditions import (
    EFFECTS,
    Condition,
    ConditionEffect,
    compute_ability_check_advantage,
)
from fivee_sim.kernel.dice import Advantage
from fivee_sim.kernel.rules import Ability, Size
from fivee_sim.kernel.spells import SpellShape
from fivee_sim.model.creature import Creature, DeathRule
from fivee_sim.model.encounter import Action, ActionKind, Encounter
from fivee_sim.service.errors import NotFoundError, RequestError

from . import api
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
        assert registry.source_of("creatures", "Goblin Warrior") == (
            "bundled:catalog-15-monsters-a-z.json"
        )

    def test_a_creature_can_be_built_from_a_pack_record(self, pack: Path) -> None:
        registry = load_packs([pack], include_environment=False)
        stalker = make_creature("Vale Stalker", registry=registry, label="Stalker A")
        assert stalker.name == "Stalker A"
        assert stalker.max_hp == 22
        assert stalker.attacks[0].name == "Claw"

    def test_hit_dice_is_accepted_and_validated_but_never_reaches_the_creature(
        self, tmp_path: Path
    ) -> None:
        """hit_dice is a deliberately unconsumed key, not an oversight.

        It is transcribed straight from the SRD stat block, validated as a string,
        and then dropped: the engine rolls no hit points and models no rest, so
        nothing downstream has a use for it. This pins that decision two ways —
        loading a record carrying it raises nothing, and ``Creature`` is a
        ``slots=True`` dataclass with no ``hit_dice`` field, so a build that wired
        the value up (or dropped the key from the allowlist and this record
        started failing) would show up here: the attribute would either appear or
        the load would raise, either of which breaks this test.
        """
        path = write_pack(tmp_path, "hit-dice.json", {
            "pack": "x", "provenance": "test",
            "creatures": [{
                "name": "Dice Bearer", "ac": 10, "max_hp": 10, "provenance": "test",
                "hit_dice": "3d6",
            }],
        })
        registry = load_packs([path], include_environment=False)
        creature = make_creature("Dice Bearer", registry=registry, label="Bearer A")
        assert creature.max_hp == 10
        assert not hasattr(creature, "hit_dice")
        assert "hit_dice" not in {f.name for f in dataclasses.fields(creature)}


class TestPlaytestFieldsSchema:
    def test_environment_lifecycle_and_healing_fields_round_trip(
        self, tmp_path: Path
    ) -> None:
        from fivee_sim.kernel.dice import Dice
        from fivee_sim.kernel.items import ActionCost

        path = write_pack(tmp_path, "playtest.json", {
            "pack": "playtest", "provenance": "test",
            "creatures": [{
                "name": "Skirmisher", "ac": 13, "max_hp": 9,
                "speed": 10, "climb_speed": 20, "swim_speed": 30,
                "fly_speed": 40, "terrain_cost_overrides": ["grain"],
                "darkvision": 60, "blindsight": 10,
                "death_rule": "instant",
                "bonus_actions": ["dash", "disengage"],
                "surrender_when_last": True,
                "redirect_attack": True,
                "attacks": [{
                    "name": "Proboscis", "attack_bonus": 5,
                    "damage": "1d4", "damage_type": "piercing",
                    "advantage_bonus_damage": "1d6",
                    "advantage_bonus_with_adjacent_ally": True,
                    "on_hit_attach": True, "attached_damage": "2d4",
                    "attached_damage_type": "necrotic",
                    "detach_after_damage": 5,
                }],
                "provenance": "test",
            }],
            "spells": [{
                "name": "Restore", "level": 1, "heal": "1d8+3",
                "upcast_heal": "1d8", "temp_hp": "1d4+1", "upcast_temp_hp": "1d4",
                "range_feet": 5,
                "action_cost": "bonus_action",
                "provenance": "test",
            }],
            "terrain": [{
                "name": "deep-water", "effects": {
                    "move_cost_multiplier": 2, "underwater": True,
                }, "provenance": "test",
            }],
            "items": [{
                "name": "Second Wind", "use": {
                    "heal": "1d10+1", "temp_hp": "1d6", "action_cost": "bonus_action",
                }, "provenance": "test",
            }],
        })

        registry = load_packs([path], builtin="exclude", include_environment=False)
        creature = make_creature("Skirmisher", registry=registry)
        attack = creature.attacks[0]

        assert (creature.speed, creature.climb_speed, creature.swim_speed) == (10, 20, 30)
        assert creature.fly_speed == 40
        assert creature.terrain_cost_overrides == frozenset({"grain"})
        assert (creature.darkvision, creature.blindsight) == (60, 10)
        assert creature.death_rule is DeathRule.INSTANT
        assert creature.bonus_actions == frozenset({"dash", "disengage"})
        assert creature.surrender_when_last is True
        assert creature.redirect_attack is True
        assert attack.advantage_bonus_damage == Dice(1, 6)
        assert attack.advantage_bonus_with_adjacent_ally is True
        assert attack.on_hit_attach is True
        assert attack.attached_damage == Dice(2, 4)
        assert attack.detach_after_damage == 5
        assert registry.spells["Restore"].heal == Dice(1, 8, 3)
        assert registry.spells["Restore"].upcast_heal == Dice(1, 8)
        assert registry.spells["Restore"].temp_hp == Dice(1, 4, 1)
        assert registry.spells["Restore"].upcast_temp_hp == Dice(1, 4)
        assert registry.spells["Restore"].action_cost is ActionCost.BONUS_ACTION
        assert registry.terrain_effects["deep-water"].underwater is True
        assert registry.items["Second Wind"].action_cost is ActionCost.BONUS_ACTION
        assert registry.items["Second Wind"].temp_hp == Dice(1, 6)

    def test_an_unknown_bonus_action_is_refused_by_name(self, tmp_path: Path) -> None:
        path = write_pack(tmp_path, "bad-bonus.json", {
            "pack": "x", "provenance": "test", "creatures": [{
                "name": "Thing", "ac": 10, "max_hp": 10,
                "bonus_actions": ["teleport"], "provenance": "test",
            }],
        })

        found = problems(validate([path], builtin="exclude", include_environment=False))

        assert any("Valid values: dash, disengage" in problem for problem in found)


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
        victim.death_rule = DeathRule.DEATH_SAVES
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
                "save_ability": "wisdom", "range_feet": 30, "provenance": "test",
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
                "save_ability": "wisdom", "range_feet": 30, "provenance": "test",
            }],
        })
        assert not problems(validate([conditions, spells], include_environment=False))

    def test_an_item_that_does_nothing_is_refused(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {
            "pack": "x", "provenance": "test",
            "items": [{"name": "Pebble", "use": {}, "provenance": "test"}],
        })
        assert any(
            "heal, deal damage, grant temporary hit points, or apply a condition" in p
            for p in found
        )

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

    def test_an_items_entry_named_by_the_creatures_own_attack_does_not_warn(
        self, tmp_path: Path
    ) -> None:
        # "Arrows" is definitionally not an item: ``ItemEffect.__post_init__``
        # refuses a use that does nothing, and ammunition has no ``use`` block. So
        # the old warning — "refers to X, which no loaded pack defines; the engine
        # will refuse it when the creature tries to use it" — is simply false for a
        # name the creature's own attack declares as ``ammunition``: the engine
        # spends it, it never refuses it.
        path = write_pack(tmp_path, "x.json", {
            "pack": "x", "provenance": "test",
            "creatures": [{
                "name": "Archer", "ac": 10, "max_hp": 10, "provenance": "test",
                "items": {"Arrows": 20},
                "attacks": [{
                    "name": "Shortbow", "attack_bonus": 4, "damage": "1d6",
                    "damage_type": "piercing", "kind": "ranged",
                    "normal_range": 80, "long_range": 320,
                    "ammunition": "Arrows",
                }],
            }],
        })
        diagnostics = validate([path], include_environment=False)
        assert not problems(diagnostics)
        warned = problems(diagnostics, Severity.WARNING)
        assert not any("Arrows" in p for p in warned)

    def test_an_attack_naming_ammunition_the_creature_does_not_carry_warns(
        self, tmp_path: Path
    ) -> None:
        # An authoring mistake worth catching now rather than at the first shot:
        # the attack names ammunition the creature's own ``items`` never stocks, so
        # the very first attempt to fire it refuses at use time.
        path = write_pack(tmp_path, "x.json", {
            "pack": "x", "provenance": "test",
            "creatures": [{
                "name": "Archer", "ac": 10, "max_hp": 10, "provenance": "test",
                "attacks": [{
                    "name": "Shortbow", "attack_bonus": 4, "damage": "1d6",
                    "damage_type": "piercing", "kind": "ranged",
                    "normal_range": 80, "long_range": 320,
                    "ammunition": "Arrows",
                }],
            }],
        })
        diagnostics = validate([path], include_environment=False)
        assert not problems(diagnostics), "still a warning, not a load failure"
        warned = problems(diagnostics, Severity.WARNING)
        assert any(
            "Arrows" in p and "does not carry" in p for p in warned
        )

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

    def test_ammunition_on_a_melee_attack_is_refused(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {"ammunition": "Arrow"})
        assert any("needs kind ranged" in p for p in found), found

    def test_loading_on_a_melee_attack_is_refused(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {"loading": True})
        assert any("needs kind ranged" in p for p in found), found

    def test_a_blank_ammunition_name_is_refused(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {
            "kind": "ranged", "normal_range": 150, "long_range": 600,
            "ammunition": "   ",
        })
        assert any("must not be blank" in p for p in found), found

    def test_thrown_on_a_melee_attack_is_refused(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {"thrown": True})
        assert any("needs kind ranged" in p for p in found), found

    def test_a_thrown_attack_with_no_range_is_refused(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {"kind": "ranged", "thrown": True})
        assert any("needs a normal_range or long_range" in p for p in found), found

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

    def test_ammunition_and_loading_reach_the_attack_option_through_from_record(
        self, tmp_path: Path
    ) -> None:
        path = write_pack(tmp_path, "riders.json", {
            "pack": "x", "provenance": "test",
            "creatures": [{
                "name": "Archer", "ac": 10, "max_hp": 10, "provenance": "test",
                "attacks": [{
                    "name": "Longbow", "attack_bonus": 4, "damage": "1d8",
                    "damage_type": "piercing", "kind": "ranged",
                    "normal_range": 150, "long_range": 600,
                    "ammunition": "Arrow", "loading": True,
                }],
            }],
        })
        registry = load_packs([path], include_environment=False)
        archer = make_creature("Archer", registry=registry)
        option = archer.attacks[0]
        assert option.ammunition == "Arrow"
        assert option.loading is True

    def test_thrown_reaches_the_attack_option_through_from_record(
        self, tmp_path: Path
    ) -> None:
        # The pack-authoring surface for the SRD's "Melee or Ranged Attack
        # Roll" line, checked end to end: the key has to survive validation
        # *and* the record reader, because a key that validates and is then
        # dropped is the exact shape of the ``range`` defect.
        path = write_pack(tmp_path, "thrown.json", {
            "pack": "x", "provenance": "test",
            "creatures": [{
                "name": "Skirmisher", "ac": 10, "max_hp": 10, "provenance": "test",
                "attacks": [{
                    "name": "Javelin", "attack_bonus": 4, "damage": "1d6",
                    "damage_type": "piercing", "kind": "ranged", "reach": 5,
                    "normal_range": 30, "long_range": 120, "thrown": True,
                }],
            }],
        })
        registry = load_packs([path], include_environment=False)
        option = make_creature("Skirmisher", registry=registry).attacks[0]
        assert option.thrown is True
        assert option.resolves_as_melee(5) is True
        assert option.resolves_as_melee(10) is False


class TestSpellAttackKindSchema:
    def pack(self, tmp_path: Path, **spell_fields: Any) -> Path:
        spell = {
            "name": "Vale Arc",
            "level": 1,
            "requires_attack_roll": True,
            "damage": "1d8",
            "damage_type": "force",
            "range_feet": 60,
            "provenance": "test",
            **spell_fields,
        }
        return write_pack(tmp_path, "arc.json", {
            "pack": "x", "provenance": "test", "spells": [spell],
        })

    def test_a_melee_spell_attack_kind_loads(self, tmp_path: Path) -> None:
        registry = load_packs(
            [self.pack(tmp_path, attack_kind="melee")],
            builtin="exclude",
            include_environment=False,
        )
        assert registry.spells["Vale Arc"].attack_kind is AttackKind.MELEE

    def test_an_omitted_attack_kind_defaults_to_ranged(self, tmp_path: Path) -> None:
        registry = load_packs(
            [self.pack(tmp_path)], builtin="exclude", include_environment=False
        )
        assert registry.spells["Vale Arc"].attack_kind is AttackKind.RANGED

    def test_an_invalid_attack_kind_names_the_field(self, tmp_path: Path) -> None:
        diagnostics = validate(
            [self.pack(tmp_path, attack_kind="eldritch")],
            builtin="exclude",
            include_environment=False,
        )
        assert "attack_kind" in fields(diagnostics)


class TestCreatureSizeSchema:
    """``size`` on a creature and ``on_hit_max_size`` on its attack.

    Both are closed enums, unlike ``conditions`` and ``terrain``: ``SECTIONS``
    lets a pack *define* those two and nothing else, so a size can be referenced
    but never invented. These pin that a pack goes through the same enum path
    every other closed taxonomy uses.
    """

    def creature_pack(self, tmp_path: Path, **record: Any) -> Path:
        payload = {
            "pack": "x", "provenance": "test",
            "creatures": [{
                "name": "Thing", "ac": 10, "max_hp": 10, "provenance": "test", **record,
            }],
        }
        return write_pack(tmp_path, "sizes.json", payload)

    def test_a_declared_size_reaches_the_creature(self, tmp_path: Path) -> None:
        path = self.creature_pack(tmp_path, size="large")
        registry = load_packs([path], include_environment=False)
        thing = make_creature("Thing", registry=registry)
        assert thing.size is Size.LARGE

    def test_an_omitted_size_defaults_to_medium(self, tmp_path: Path) -> None:
        # Every bundled record predates this field; the default is what keeps
        # them, and any pack written before it, loading unchanged.
        path = self.creature_pack(tmp_path)
        registry = load_packs([path], include_environment=False)
        assert make_creature("Thing", registry=registry).size is Size.MEDIUM

    def test_an_unknown_size_is_refused_by_name(self, tmp_path: Path) -> None:
        path = self.creature_pack(tmp_path, size="colossal")
        found = problems(validate([path], include_environment=False))
        assert found == [
            "'colossal' is not valid; must be one of: "
            "tiny, small, medium, large, huge, gargantuan"
        ], found

    def test_an_unknown_size_blames_the_size_key(self, tmp_path: Path) -> None:
        path = self.creature_pack(tmp_path, size="colossal")
        assert fields(validate([path], include_environment=False)) == ["size"]

    def test_a_riders_size_gate_reaches_the_attack_option(self, tmp_path: Path) -> None:
        path = self.creature_pack(tmp_path, attacks=[{
            "name": "Bite", "attack_bonus": 4, "damage": "1d6", "damage_type": "piercing",
            "on_hit_condition": "prone", "on_hit_max_size": "medium",
        }])
        registry = load_packs([path], include_environment=False)
        bite = make_creature("Thing", registry=registry).attacks[0]
        assert bite.on_hit_max_size is Size.MEDIUM

    def test_a_rider_without_a_gate_leaves_it_unset(self, tmp_path: Path) -> None:
        path = self.creature_pack(tmp_path, attacks=[{
            "name": "Bite", "attack_bonus": 4, "damage": "1d6", "damage_type": "piercing",
            "on_hit_condition": "prone",
        }])
        registry = load_packs([path], include_environment=False)
        assert make_creature("Thing", registry=registry).attacks[0].on_hit_max_size is None

    def test_a_size_gate_with_no_condition_to_ride_is_refused(self, tmp_path: Path) -> None:
        # Same pairing rule the other rider keys follow: a gate on nothing is a
        # record whose author meant something the engine cannot guess.
        path = self.creature_pack(tmp_path, attacks=[{
            "name": "Bite", "attack_bonus": 4, "damage": "1d6", "damage_type": "piercing",
            "on_hit_max_size": "medium",
        }])
        found = problems(validate([path], include_environment=False))
        assert any("there is no condition to ride the hit" in p for p in found), found


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
            source="bundled:catalog-15-monsters-a-z.json",
        )
        assert creature.provenance == "Original content"

    def test_the_source_is_the_fallback_when_the_record_omits_provenance(self) -> None:
        # Content validation makes provenance required, so this only fires for a
        # record built in code. It still has to name where the creature came
        # from: the licence boundary turns on every creature carrying one.
        bare = self.record()
        del bare["provenance"]
        creature = Creature.from_record(
            bare,
            condition_effects=EFFECTS,
            source="bundled:catalog-15-monsters-a-z.json",
        )
        assert creature.provenance == "bundled:catalog-15-monsters-a-z.json"

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
            "'ring' is not valid; must be one of: single, sphere, cone, line, cube, "
            "emanation, cylinder"
        ], found


class TestSpellActionCostSchema:
    """A spell's casting time defaults to an action, as SRD 5.2.1 prints most.

    Healing Word and Mass Healing Word are the two most-cast exceptions: SRD
    5.2.1 prints both as "Casting Time: Bonus Action". Mirrors
    ``ItemEffect.action_cost`` (``kernel/items.py``), which solved the same
    problem for items first.
    """

    def test_an_omitted_action_cost_defaults_to_action(self, tmp_path: Path) -> None:
        from fivee_sim.kernel.items import ActionCost

        path = write_pack(tmp_path, "vale.json", {
            "pack": "x", "provenance": "test",
            "spells": [{
                "name": "Vale Bolt", "level": 1, "damage": "1d10",
                "damage_type": "force", "range_feet": 60, "provenance": "test",
            }],
        })
        registry = load_packs([path], include_environment=False)
        assert registry.spells["Vale Bolt"].action_cost is ActionCost.ACTION


class TestSpellDurationRoundsSchema:
    """A spell's printed duration cap is stored in rounds, and defaults to none.

    SRD 5.2.1 prints durations in minutes or hours; this engine counts rounds,
    at 10 rounds per SRD minute (1 round = 6 seconds). ``0`` means "no cap" —
    the same reading ``range_feet`` gives its own default — so a record
    carrying only ``name``, ``level`` and ``provenance`` keeps loading exactly
    as it does today.
    """

    def test_an_omitted_duration_rounds_defaults_to_no_cap(self, tmp_path: Path) -> None:
        path = write_pack(tmp_path, "vale.json", {
            "pack": "x", "provenance": "test",
            "spells": [{
                "name": "Vale Bolt", "level": 1, "damage": "1d10",
                "damage_type": "force", "range_feet": 60, "provenance": "test",
            }],
        })
        registry = load_packs([path], include_environment=False)
        assert registry.spells["Vale Bolt"].duration_rounds == 0

    def test_duration_rounds_is_read_from_the_record(self, tmp_path: Path) -> None:
        path = write_pack(tmp_path, "vale.json", {
            "pack": "x", "provenance": "test",
            "spells": [{
                "name": "Vale Hold", "level": 2, "condition": "paralyzed",
                "save_ability": "wisdom", "range_feet": 60, "concentration": True,
                "duration_rounds": 10, "provenance": "test",
            }],
        })
        registry = load_packs([path], include_environment=False)
        assert registry.spells["Vale Hold"].duration_rounds == 10


class TestCatalogContentRefMustResolve:
    """A catalog row's ``content_ref`` has to name a record that exists.

    The field is what makes `catalog.get` say "the engine can run this", so a
    dangling one is a broken promise rather than a cosmetic slip: the payload
    still carries the ref while `sources.executable` comes back null, so a caller
    is pointed at an executable record and finds nothing there. Only the merged
    set can answer whether the target exists, which is why it belongs beside the
    condition checks in ``_cross_reference`` rather than in ``_parse_catalog_record``.
    """

    def check(self, tmp_path: Path, ref: dict[str, Any]) -> list[str]:
        path = write_pack(tmp_path, "linked.json", {
            "pack": "x", "provenance": "test",
            "spells": [{
                "name": "Real Spell", "level": 1, "range_feet": 30,
                "provenance": "test",
            }],
            "catalog": [{
                "id": "row-1", "kind": "spell", "name": "Row", "source_ids": ["row-1"],
                "pages": [42], "fact_status": "complete", "facts": {},
                "provenance": "test", "content_ref": ref,
            }],
        })
        return problems(validate([path], include_environment=False))

    def test_a_content_ref_naming_no_such_record_is_refused(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {"section": "spells", "name": "Ghost Spell"})
        assert any("no loaded pack defines" in p for p in found), found
        assert any("Ghost Spell" in p for p in found), found

    def test_a_content_ref_naming_a_real_record_is_accepted(self, tmp_path: Path) -> None:
        assert not self.check(tmp_path, {"section": "spells", "name": "Real Spell"})

    def test_the_section_is_where_the_record_is_looked_for(self, tmp_path: Path) -> None:
        # The name exists — as a *spell* — but the ref says `items`, so it must not
        # resolve. This is the case that made Goodberry a judgement call: nothing
        # ties a row's `kind` to the section it points at, so the section has to be
        # taken literally as the place to look.
        found = self.check(tmp_path, {"section": "items", "name": "Real Spell"})
        assert any("no loaded pack defines" in p for p in found), found


class TestSpellRangeIsRequired:
    """``range_feet`` is *warned* about, not required, when a named-target spell omits it.

    ``0`` already means "resolve with no range check at all" (see
    ``Encounter._require_in_range``), so a record that simply omits the field is
    indistinguishable from one deliberately declaring an unlimited range — Cure
    Wounds and Regenerate are both Range: Touch, and the honest transcription of
    "Touch" is to omit the field, which produces exactly that trap.

    A warning rather than a refusal, because refusing is a breaking change to the
    pack format that this suite already forbids:
    ``test_existing_packs_remain_compatible_and_new_sections_are_optional`` loads a
    minimal legacy spell carrying only a name, level and provenance. A campaign's
    own packs are outside this repo by design, so the fact that no *bundled* or
    *test-corpus* record omits the field says nothing about the packs a user
    already has on disk — and that is the population the promise is to.

    An area spell is exempt: its range is measured from its point of origin or
    poured out of the caster, so the field means something different there.
    """

    def check(self, tmp_path: Path, spell: dict[str, Any]) -> list[Any]:
        path = write_pack(tmp_path, "vale.json", {
            "pack": "x", "provenance": "test", "spells": [spell],
        })
        return validate([path], include_environment=False)

    def test_a_single_target_spell_omitting_range_feet_is_warned_about(
        self, tmp_path: Path
    ) -> None:
        spell = {
            "name": "Vale Touch", "level": 1, "heal": "1d8+3", "provenance": "test",
        }
        diagnostics = self.check(tmp_path, spell)
        # A warning, so the severity has to be named: `fields`/`problems` filter on
        # ERROR by default, and reading the default here would have asserted the
        # absence of an error rather than the presence of the advice.
        assert fields(diagnostics, Severity.WARNING) == ["range_feet"], diagnostics
        assert not problems(diagnostics), "advice must not be an error"
        found = problems(diagnostics, Severity.WARNING)
        assert any("no range check" in p for p in found), found
        assert any("5 for Touch" in p for p in found), found
        assert any("0 for Self" in p for p in found), found

    def test_the_warning_does_not_stop_the_pack_loading(self, tmp_path: Path) -> None:
        # The compatibility half of the rule above, asserted rather than implied:
        # the diagnostic is advice, and a pack written before the advice existed
        # still resolves — with the unlimited range that earned the warning.
        path = write_pack(tmp_path, "vale.json", {
            "pack": "x", "provenance": "test",
            "spells": [{
                "name": "Vale Touch", "level": 1, "heal": "1d8+3",
                "provenance": "test",
            }],
        })
        registry = load_packs([path], include_environment=False)
        assert registry.spells["Vale Touch"].range_feet == 0

    def test_range_feet_zero_is_accepted_as_a_deliberate_no_check(
        self, tmp_path: Path
    ) -> None:
        path = write_pack(tmp_path, "vale.json", {
            "pack": "x", "provenance": "test",
            "spells": [{
                "name": "Vale Ward", "level": 1, "heal": "1d8+3",
                "range_feet": 0, "provenance": "test",
            }],
        })
        assert not problems(validate([path], include_environment=False))
        registry = load_packs([path], include_environment=False)
        assert registry.spells["Vale Ward"].range_feet == 0

    def test_an_area_spell_omitting_range_feet_is_exempt(self, tmp_path: Path) -> None:
        found = self.check(tmp_path, {
            "name": "Vale Burst", "level": 3, "save_ability": "dexterity",
            "damage": "6d6", "damage_type": "fire", "shape": "sphere", "radius": 20,
            "provenance": "test",
        })
        assert not found, found


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

    def test_the_host_neutral_project_directory_is_used(
        self, tmp_path: Path
    ) -> None:
        write_pack(tmp_path / ".fivee-sim" / "content", "vale.json", CAMPAIGN)
        registry = load_packs(env={"FIVEE_SIM_PROJECT_DIR": str(tmp_path)})
        assert "Vale Stalker" in registry.creatures

    def test_the_host_neutral_project_directory_wins_over_the_claude_fallback(
        self, tmp_path: Path
    ) -> None:
        neutral = tmp_path / "neutral"
        claude = tmp_path / "claude"
        write_pack(neutral / ".fivee-sim" / "content", "vale.json", CAMPAIGN)
        write_pack(claude / ".fivee-sim" / "content", "rope.json", {
            "pack": "b", "provenance": "test",
            "items": [{"name": "Rope", "use": {"heal": "1d1"}, "provenance": "test"}],
        })
        registry = load_packs(env={
            "FIVEE_SIM_PROJECT_DIR": str(neutral),
            "CLAUDE_PROJECT_DIR": str(claude),
        })
        assert "Vale Stalker" in registry.creatures
        assert "Rope" not in registry.items

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
            "FIVEE_SIM_PROJECT_DIR": str(tmp_path),
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

    def test_a_custom_condition_changes_an_ability_check(self, tmp_path: Path) -> None:
        payload = {
            "pack": "x", "provenance": "test",
            "conditions": [{
                "name": "vale-addled", "provenance": "test",
                "effects": {"own_ability_checks_have_disadvantage": True},
            }],
        }
        path = write_pack(tmp_path, "addled.json", payload)
        registry = load_packs(
            [path], builtin="exclude", include_environment=False
        )
        assert compute_ability_check_advantage(
            conditions=["vale-addled"],
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
        # The event detail is what the assistant narrates from, so it must render.
        assert any("vale-cursed" in event.detail for event in events)
        assert "vale-cursed" in hero.conditions
        state = encounter.state()
        held = next(c for c in state["combatants"] if c["name"] == "A")
        assert "vale-cursed" in held["conditions"]

    def test_a_pack_condition_that_conceals_reads_exactly_like_the_SRD_one(
        self, tmp_path: Path
    ) -> None:
        """``unseen`` is the whole of Invisible's "Attacks Affected" clause.

        SRD 5.2.1, Invisible: "Attack rolls against you have Disadvantage, and
        your attack rolls have Advantage. If a creature can somehow see you, you
        don't gain this benefit against that creature." The withdrawal is a
        relationship the kernel table cannot state, so the model derives both
        halves from sight — and a pack that hides a creature by declaring
        ``unseen`` must therefore get the same four answers the bundled
        condition gets, withdrawal included. A pack forced to also set
        ``attacked_with_disadvantage`` to be hidden would be getting the
        unconditional half back.
        """
        payload = {
            "pack": "x", "provenance": "test",
            "conditions": [{
                "name": "vale-shrouded", "provenance": "test",
                "effects": {"unseen": True},
            }],
        }
        path = write_pack(tmp_path, "shrouded.json", payload)
        registry = load_packs([path], include_environment=False)
        seer = make_creature("Goblin Warrior", registry=registry, label="Seer", team="a")
        seer.blindsight = 60
        sighted = make_creature(
            "Goblin Warrior", registry=registry, label="Sighted", team="a"
        )
        sighted.position = 5
        for pack_hidden in (True, False):
            ghost = make_creature(
                "Goblin Warrior", registry=registry, label="Ghost", team="b"
            )
            ghost.add_condition("vale-shrouded" if pack_hidden else Condition.INVISIBLE)
            encounter = Encounter(
                [ghost, seer, sighted], Random(5),
                condition_effects=registry.condition_effects,
            )
            swing = seer.attacks[0]
            note = "pack" if pack_hidden else "SRD"
            assert encounter.attack_advantage(
                seer, ghost, swing
            ) is Advantage.NONE, note
            assert encounter.attack_advantage(
                sighted, ghost, swing
            ) is Advantage.DISADVANTAGE, note
            assert encounter.attack_advantage(
                ghost, seer, ghost.attacks[0]
            ) is Advantage.NONE, note
            assert encounter.attack_advantage(
                ghost, sighted, ghost.attacks[0]
            ) is Advantage.ADVANTAGE, note

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

    def test_an_emanation_without_a_radius_is_refused(self, tmp_path: Path) -> None:
        path = self.spell_pack(tmp_path, {"shape": "emanation"})
        with pytest.raises(ContentError, match="an emanation needs a radius"):
            load_packs([path], include_environment=False)

    def test_a_cylinder_without_a_radius_is_refused(self, tmp_path: Path) -> None:
        path = self.spell_pack(tmp_path, {"shape": "cylinder", "height": 20})
        with pytest.raises(ContentError, match="a cylinder needs a radius"):
            load_packs([path], include_environment=False)

    def test_a_cylinder_without_a_height_is_refused(self, tmp_path: Path) -> None:
        path = self.spell_pack(tmp_path, {"shape": "cylinder", "radius": 10})
        with pytest.raises(ContentError, match="a cylinder needs a height"):
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
            ({"shape": "emanation", "radius": 10},
             {"shape": SpellShape.EMANATION, "radius": 10}),
            ({"shape": "cylinder", "radius": 10, "height": 40},
             {"shape": SpellShape.CYLINDER, "radius": 10, "height": 40}),
        ],
        ids=[
            "cone-has-a-length", "line-has-a-length", "cube-has-a-size",
            "emanation-has-a-radius", "cylinder-has-a-radius-and-height",
        ],
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


class TestHalfDamageOnSaveIsOptedInto:
    """A damage spell halves on a successful save only when its record says so.

    SRD 5.2.1 states the half-damage clause per spell — Fireball has "half as much
    damage on a successful save", Sacred Flame (p. 159) has no such clause and
    deals nothing. So the *absence* of the field has to mean all-or-nothing: a
    default of half makes every faithfully-transcribed cantrip quietly generous,
    and the record looks correct while playing wrong. This is the same reasoning
    the loader already applies to an unknown key.
    """

    def spell_pack(self, tmp_path: Path, record: dict[str, Any]) -> Path:
        return write_pack(tmp_path, "flames.json", {
            "pack": "flames", "provenance": "test",
            "spells": [{
                "name": "Test Flame", "provenance": "test", "level": 0,
                "save_ability": "dexterity", "damage": "1d8", "range_feet": 60,
                "damage_type": "radiant", **record,
            }],
        })

    def test_a_spell_that_says_nothing_does_not_halve(self, tmp_path: Path) -> None:
        registry = load_packs(
            [self.spell_pack(tmp_path, {})], include_environment=False
        )
        assert registry.spells["Test Flame"].half_on_save is False

    def test_a_spell_may_still_opt_in(self, tmp_path: Path) -> None:
        registry = load_packs(
            [self.spell_pack(tmp_path, {"half_on_save": True})],
            include_environment=False,
        )
        assert registry.spells["Test Flame"].half_on_save is True

    def test_an_item_use_follows_the_same_default(self, tmp_path: Path) -> None:
        path = write_pack(tmp_path, "flask.json", {
            "pack": "flask", "provenance": "test",
            "items": [{
                "name": "Blast Flask", "provenance": "test",
                "use": {
                    "damage": "2d6", "damage_type": "fire",
                    "save_ability": "dexterity", "save_dc": 13,
                },
            }],
        })
        registry = load_packs([path], include_environment=False)
        assert registry.items["Blast Flask"].half_on_save is False

    def test_every_bundled_spell_that_halves_declares_it(self) -> None:
        """The bundled slice must not be relying on the default in either direction.

        Guards the flip itself: were a bundled record leaning on the old default,
        this change would silently halve or unhalve it.
        """
        from fivee_sim.content import load_packs as _load
        registry = _load([], include_environment=False)
        halving = {
            name for name, spell in registry.spells.items() if spell.half_on_save
        }
        assert halving == {"Fireball", "Shatter"}


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
        assert api.lookup_rule("Vale Stalker")["name"] == "Vale Stalker"

    def test_configure_can_switch_to_exclude(self, pack: Path) -> None:
        api.content_configure([str(pack)], builtin="exclude")
        listing = api.lookup_rule()
        assert listing["builtin"] == "exclude"
        assert listing["counts"]["creatures"] == 1
        with pytest.raises(NotFoundError, match="nothing loaded"):
            api.lookup_rule("Goblin Warrior")
        assert api.lookup_rule("Vale Stalker")["name"] == "Vale Stalker"

    def test_configure_adds_rather_than_replaces_when_asked(self, tmp_path: Path) -> None:
        first = write_pack(tmp_path / "a", "one.json", CAMPAIGN)
        second = write_pack(tmp_path / "b", "two.json", {
            "pack": "b", "provenance": "test",
            "items": [{"name": "Rope", "use": {"heal": "1d1"}, "provenance": "test"}],
        })
        api.content_configure([str(first)])
        status = api.content_configure([str(second)], add=True)
        assert len(status["configured_paths"]) == 2
        assert api.lookup_rule("Rope")["name"] == "Rope"

    def test_a_failed_configure_leaves_the_working_content_alone(
        self, tmp_path: Path
    ) -> None:
        broken = write_pack(tmp_path, "broken.json", {"pack": "x", "creatures": []})
        before = api.content_status()
        with pytest.raises(RequestError, match="content not changed"):
            api.content_configure([str(broken)])
        after = api.content_status()
        assert after["generation"] == before["generation"]
        assert after["counts"] == before["counts"]

    def test_configure_with_nothing_to_change_is_refused(self) -> None:
        with pytest.raises(RequestError, match="nothing to change"):
            api.content_configure()

    def test_a_bad_mode_lists_the_valid_ones(self) -> None:
        with pytest.raises(RequestError, match="include, exclude"):
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


class TestSpellcastingModifierSchema:
    """``add_spellcasting_modifier`` on a spell, ``spellcasting_ability`` on its caster.

    Two halves of one rule, and each is useless alone: a spell that opts in with
    nobody declaring an ability heals its flat dice, and an ability nobody's
    spell asks for changes nothing. They are validated separately because a pack
    may legitimately ship either half — a caster who knows only damaging spells
    still has a spellcasting ability.
    """

    def spell_pack(self, tmp_path: Path, **fields: Any) -> Path:
        return write_pack(tmp_path, "balm.json", {
            "pack": "x", "provenance": "test",
            "spells": [{
                "name": "Vale Balm", "level": 1, "heal": "2d4",
                "range_feet": 30, "provenance": "test", **fields,
            }],
        })

    def creature_pack(self, tmp_path: Path, **fields: Any) -> Path:
        return write_pack(tmp_path, "acolyte.json", {
            "pack": "x", "provenance": "test",
            "creatures": [{
                "name": "Vale Acolyte", "ac": 13, "max_hp": 20,
                "abilities": {"wisdom": 16}, "spells": ["Vale Balm"],
                "spell_slots": {"1": 2}, "provenance": "test", **fields,
            }],
        })

    def test_a_spell_opts_into_the_modifier(self, tmp_path: Path) -> None:
        registry = load_packs(
            [self.spell_pack(tmp_path, add_spellcasting_modifier=True)],
            builtin="exclude", include_environment=False,
        )
        assert registry.spells["Vale Balm"].add_spellcasting_modifier

    def test_omitting_it_leaves_the_healing_flat(self, tmp_path: Path) -> None:
        registry = load_packs(
            [self.spell_pack(tmp_path)], builtin="exclude", include_environment=False
        )
        assert not registry.spells["Vale Balm"].add_spellcasting_modifier

    def test_a_non_boolean_names_the_field(self, tmp_path: Path) -> None:
        diagnostics = validate(
            [self.spell_pack(tmp_path, add_spellcasting_modifier="wisdom")],
            builtin="exclude", include_environment=False,
        )
        assert "add_spellcasting_modifier" in fields(diagnostics)
        # The field name alone does not identify the branch: an unregistered key
        # is refused by name too, so a rename would satisfy a name-only check.
        assert any("must be true or false" in problem for problem in problems(diagnostics))

    def test_a_creature_declares_which_ability_it_casts_with(
        self, tmp_path: Path
    ) -> None:
        registry = load_packs(
            [self.creature_pack(tmp_path, spellcasting_ability="wisdom")],
            builtin="exclude", include_environment=False,
        )
        acolyte = Creature.from_record(
            registry.creatures["Vale Acolyte"],
            condition_effects=registry.condition_effects,
            source="test",
        )

        assert acolyte.spellcasting_ability is Ability.WISDOM
        assert acolyte.spellcasting_modifier == 3

    def test_a_creature_without_one_contributes_no_modifier(
        self, tmp_path: Path
    ) -> None:
        # Not a default of zero on the *ability*: a creature whose sheet never
        # said adds nothing, which is what keeps every pack written before this
        # field resolving exactly as it did.
        registry = load_packs(
            [self.creature_pack(tmp_path)], builtin="exclude", include_environment=False
        )
        acolyte = Creature.from_record(
            registry.creatures["Vale Acolyte"],
            condition_effects=registry.condition_effects,
            source="test",
        )

        assert acolyte.spellcasting_ability is None
        assert acolyte.spellcasting_modifier == 0

    def test_an_ability_that_is_not_one_names_the_field(self, tmp_path: Path) -> None:
        diagnostics = validate(
            [self.creature_pack(tmp_path, spellcasting_ability="moxie")],
            builtin="exclude", include_environment=False,
        )
        assert "spellcasting_ability" in fields(diagnostics)
        assert any("is not valid; must be one of" in p for p in problems(diagnostics))


class TestInitiativeBonusSchema:
    """``initiative_bonus``: a printed Initiative score is the stat block's
    authority, not the Dexterity modifier — SRD 5.2.1, *Initiative*.
    """

    def creature_pack(self, tmp_path: Path, **fields: Any) -> Path:
        return write_pack(tmp_path, "loremaster.json", {
            "pack": "x", "provenance": "test",
            "creatures": [{
                "name": "Vale Loremaster", "ac": 13, "max_hp": 20,
                "abilities": {"dexterity": 8}, "provenance": "test", **fields,
            }],
        })

    def test_a_creature_carries_its_printed_initiative_bonus(self, tmp_path: Path) -> None:
        registry = load_packs(
            [self.creature_pack(tmp_path, initiative_bonus=7)],
            builtin="exclude", include_environment=False,
        )
        loremaster = Creature.from_record(
            registry.creatures["Vale Loremaster"],
            condition_effects=registry.condition_effects,
            source="test",
        )

        assert loremaster.initiative_bonus == 7

    def test_a_creature_without_one_falls_back_to_none(self, tmp_path: Path) -> None:
        registry = load_packs(
            [self.creature_pack(tmp_path)], builtin="exclude", include_environment=False
        )
        loremaster = Creature.from_record(
            registry.creatures["Vale Loremaster"],
            condition_effects=registry.condition_effects,
            source="test",
        )

        assert loremaster.initiative_bonus is None

    def test_a_non_integer_names_the_field(self, tmp_path: Path) -> None:
        diagnostics = validate(
            [self.creature_pack(tmp_path, initiative_bonus="seven")],
            builtin="exclude", include_environment=False,
        )
        assert "initiative_bonus" in fields(diagnostics)
        assert any("must be a whole number" in p for p in problems(diagnostics))


class TestSkillBonusesSchema:
    """``skill_bonuses``: a printed absolute modifier, not a proficiency to add.

    SRD stat blocks print totals ("Perception +5"), and the engine has no
    character level or proficiency bonus to derive one from — the value here
    *is* the printed total. Keys are plain strings, never a closed enum: the
    engine already treats a ``primitives.check`` skill label and a pack
    condition name the same way, and a skill this engine has never heard of is
    still a fact a stat block prints.
    """

    def creature_pack(self, tmp_path: Path, **fields: Any) -> Path:
        return write_pack(tmp_path, "sentry.json", {
            "pack": "x", "provenance": "test",
            "creatures": [{
                "name": "Vale Sentry", "ac": 13, "max_hp": 20,
                "abilities": {"wisdom": 12}, "provenance": "test", **fields,
            }],
        })

    def test_a_creature_carries_its_printed_skill_bonuses(self, tmp_path: Path) -> None:
        registry = load_packs(
            [self.creature_pack(tmp_path, skill_bonuses={"perception": 5, "stealth": 4})],
            builtin="exclude", include_environment=False,
        )
        sentry = Creature.from_record(
            registry.creatures["Vale Sentry"],
            condition_effects=registry.condition_effects,
            source="test",
        )

        assert sentry.skill_bonuses == {"perception": 5, "stealth": 4}

    def test_a_creature_without_any_falls_back_to_an_empty_mapping(
        self, tmp_path: Path
    ) -> None:
        registry = load_packs(
            [self.creature_pack(tmp_path)], builtin="exclude", include_environment=False
        )
        sentry = Creature.from_record(
            registry.creatures["Vale Sentry"],
            condition_effects=registry.condition_effects,
            source="test",
        )

        assert sentry.skill_bonuses == {}

    def test_a_non_integer_value_names_the_field(self, tmp_path: Path) -> None:
        diagnostics = validate(
            [self.creature_pack(tmp_path, skill_bonuses={"perception": "five"})],
            builtin="exclude", include_environment=False,
        )
        assert "skill_bonuses" in fields(diagnostics)
        assert any("must be a whole number" in p for p in problems(diagnostics))


class TestPassivePerceptionSchema:
    """``passive_perception``: transcription-only, following the
    ``initiative_bonus`` template exactly. A stat block's printed Passive
    Perception does not always equal ``10 + Wisdom modifier`` — the same
    reason ``initiative_bonus`` is carried rather than derived — and nothing
    in this engine reads it: there is no Hide, Search, Stealth, or Perception
    action here for it to reach. It is carried anyway, declared rather than
    silently dropped, per the ``hit_dice`` ruling.
    """

    def creature_pack(self, tmp_path: Path, **fields: Any) -> Path:
        return write_pack(tmp_path, "watcher.json", {
            "pack": "x", "provenance": "test",
            "creatures": [{
                "name": "Vale Watcher", "ac": 13, "max_hp": 20,
                "abilities": {"wisdom": 12}, "provenance": "test", **fields,
            }],
        })

    def test_a_creature_carries_its_printed_passive_perception(self, tmp_path: Path) -> None:
        registry = load_packs(
            [self.creature_pack(tmp_path, passive_perception=15)],
            builtin="exclude", include_environment=False,
        )
        watcher = Creature.from_record(
            registry.creatures["Vale Watcher"],
            condition_effects=registry.condition_effects,
            source="test",
        )

        assert watcher.passive_perception == 15

    def test_a_creature_without_one_falls_back_to_none(self, tmp_path: Path) -> None:
        registry = load_packs(
            [self.creature_pack(tmp_path)], builtin="exclude", include_environment=False
        )
        watcher = Creature.from_record(
            registry.creatures["Vale Watcher"],
            condition_effects=registry.condition_effects,
            source="test",
        )

        assert watcher.passive_perception is None

    def test_a_non_integer_names_the_field(self, tmp_path: Path) -> None:
        diagnostics = validate(
            [self.creature_pack(tmp_path, passive_perception="fifteen")],
            builtin="exclude", include_environment=False,
        )
        assert "passive_perception" in fields(diagnostics)
        assert any("must be a whole number" in p for p in problems(diagnostics))
