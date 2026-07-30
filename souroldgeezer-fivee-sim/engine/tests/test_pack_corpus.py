"""The fifty-pack corpus under ``tests/packs/``, taken into use.

The corpus shipped as data with nothing reading it. This module reads it, and it
earns its place for one reason the bundled slice cannot serve.

**Plain-string conditions, at scale.** ``content`` promises that a pack's condition
is an ordinary ``str``, and CLAUDE.md is explicit that a green suite does not prove
it: every bundled condition is a :class:`Condition` enum member, so it answers to a
``.value`` access and to a lookup in the module-level table just as happily as to
neither. ``test_content.py::TestCustomConditions`` makes the point with one
condition setting one flag. Here it is made with every condition the corpus defines,
covering every effect flag the engine can apply — including the two directional ones
no single-condition test reaches.

**The commit message becomes assertions.** The corpus was described in prose:
self-contained packs, unique names, exactly seven expected warnings under
``exclude``, a list of edge shapes covered once each. None of it was enforced, so a
pack could be edited or deleted and the coverage would degrade silently. Each claim
below is now a test.

Every loader call passes ``include_environment=False``. The default reads
``FIVEE_SIM_CONTENT`` and ``CLAUDE_PROJECT_DIR``, which would make results depend on
whether the developer running the suite happens to have a campaign exported.
"""

from __future__ import annotations

import json
from pathlib import Path
from random import Random
from typing import Any

import pytest

from fivee_sim.content import (
    ContentRegistry,
    Diagnostic,
    Severity,
    load_packs,
    validate,
)
from fivee_sim.data import make_creature
from fivee_sim.kernel.actions import (
    MELEE_THRESHOLD,
    AttackKind,
    compute_attack_advantage,
    melee_hit_is_critical,
)
from fivee_sim.kernel.conditions import (
    EFFECT_FLAGS,
    Condition,
    ConditionEffect,
    effect_of,
    is_incapacitated,
    speed_is_zero,
)
from fivee_sim.kernel.dice import Advantage
from fivee_sim.model.encounter import Action, ActionKind, Encounter

CORPUS = Path(__file__).parent / "packs"
PACK_FILES = tuple(sorted(CORPUS.glob("*.json")))

#: The corpus as committed. These are deliberately hard-coded rather than counted
#: from the files: a test that recomputes its own expectation cannot notice a pack
#: going missing. Changing the corpus means changing these numbers on purpose.
PACK_COUNT = 50
RECORD_COUNTS = {"creatures": 210, "spells": 152, "conditions": 111, "items": 176}

#: The one pack that reuses bundled names, every record declaring ``overrides``.
HOUSE_RULES = "44-house-rules-variants.json"
OVERRIDE_WARNING = "declares an override but nothing of that name is loaded"
OVERRIDDEN = {
    ("creatures", "Goblin Warrior"),
    ("creatures", "Wolf"),
    ("creatures", "Ogre"),
    ("spells", "Fireball"),
    ("spells", "Hold Person"),
    ("conditions", "paralyzed"),
    ("conditions", "frightened"),
}


def problems(
    diagnostics: list[Diagnostic], severity: Severity = Severity.ERROR
) -> list[str]:
    return [d.problem for d in diagnostics if d.severity is severity]


def from_corpus(registry: ContentRegistry, section: str) -> dict[str, Any]:
    """The records in ``section`` that a corpus pack defined.

    Filtering by source matters under ``exclude``: the loader retains the structural
    conditions (``prone``, ``unconscious``) whatever the packs say, and ``prone`` is
    the one bundled row that sets the directional flags. Counting it as corpus
    coverage would let the corpus claim a flag it never exercises.
    """
    records = registry.records_for(section)
    return {
        name: record
        for name, record in records.items()
        if registry.source_of(section, name).startswith(str(CORPUS))
    }


def expected_advantage(
    effect: ConditionEffect, *, as_attacker: bool, in_melee: bool
) -> Advantage:
    """What one condition, alone, should do to a d20 roll.

    Deliberately an independent restatement of the rule rather than a call into
    ``compute_attack_advantage``: a test that asks the implementation what it expects
    agrees with every regression. It is small enough to be worth the duplication, and
    the directional pair is exactly the part a mix-up would get wrong.
    """
    up = down = 0
    if as_attacker:
        up += effect.own_attacks_have_advantage
        down += effect.own_attacks_have_disadvantage
    else:
        up += effect.attacked_with_advantage
        down += effect.attacked_with_disadvantage
        up += effect.attacked_with_advantage_in_melee and in_melee
        down += effect.attacked_with_disadvantage_at_range and not in_melee
    if up and down:
        return Advantage.NONE  # 2024: both cancel, however many of each
    if up:
        return Advantage.ADVANTAGE
    if down:
        return Advantage.DISADVANTAGE
    return Advantage.NONE


@pytest.fixture(scope="module")
def corpus() -> ContentRegistry:
    """The whole corpus over the bundled slice — the way a campaign would load it."""
    return load_packs([CORPUS], include_environment=False)


@pytest.fixture(scope="module")
def corpus_alone() -> ContentRegistry:
    """The corpus with the bundled slice excluded."""
    return load_packs([CORPUS], builtin="exclude", include_environment=False)


class TestTheCorpusIsWhereItSaysItIs:
    def test_the_files_are_found(self) -> None:
        # Without this, an empty glob would silently parametrize every per-file test
        # below into zero cases and the module would pass having checked nothing.
        assert len(PACK_FILES) == PACK_COUNT


class TestEveryPackStandsAlone:
    """Each file loads on its own, in either builtin mode.

    This is the claim that a spell or item applying a condition sits in the same file
    as that condition. It is what lets an author copy one pack out of the corpus and
    have it work, and it is not implied by the whole directory validating.
    """

    @pytest.mark.parametrize("mode", ["include", "exclude"])
    @pytest.mark.parametrize("path", PACK_FILES, ids=lambda p: p.stem)
    def test_a_pack_validates_alone(self, path: Path, mode: str) -> None:
        found = problems(validate([path], builtin=mode, include_environment=False))
        assert not found, f"{path.name} under {mode}: {found}"

    @pytest.mark.parametrize("path", PACK_FILES, ids=lambda p: p.stem)
    def test_only_the_house_rules_pack_warns_and_only_under_exclude(
        self, path: Path
    ) -> None:
        under_include = validate([path], include_environment=False)
        assert not problems(under_include, Severity.WARNING)

        under_exclude = validate([path], builtin="exclude", include_environment=False)
        warned = problems(under_exclude, Severity.WARNING)
        if path.name == HOUSE_RULES:
            assert len(warned) == len(OVERRIDDEN)
        else:
            assert not warned


class TestTheCorpusLoadsAsOneLevel:
    """Fifty files, one registry, no collisions."""

    def test_every_pack_is_registered(self, corpus: ContentRegistry) -> None:
        labels = {Path(info.label).name for info in corpus.packs}
        assert {path.name for path in PACK_FILES} <= labels

    @pytest.mark.parametrize("section", sorted(RECORD_COUNTS))
    def test_the_record_count_is_what_was_committed(
        self, corpus: ContentRegistry, section: str
    ) -> None:
        assert len(from_corpus(corpus, section)) == RECORD_COUNTS[section]

    def test_the_corpus_holds_the_record_total(self, corpus: ContentRegistry) -> None:
        total = sum(len(from_corpus(corpus, section)) for section in RECORD_COUNTS)
        assert total == sum(RECORD_COUNTS.values())

    def test_no_name_is_claimed_by_two_packs(self) -> None:
        # The loader fails a genuine collision, so the directory loading at all is most
        # of the proof. This adds the part the loader cannot check: that a name is not
        # reused across *sections*, which is legal but would make the corpus confusing
        # to read and to write assertions against.
        seen: dict[str, str] = {}
        for path in PACK_FILES:
            payload = json.loads(path.read_text(encoding="utf-8"))
            for section in RECORD_COUNTS:
                for record in payload.get(section, []):
                    key = f"{section}:{record['name']}"
                    assert key not in seen, f"{key} in both {seen.get(key)} and {path.name}"
                    seen[key] = path.name

    def test_every_record_is_traceable_to_its_file(
        self, corpus: ContentRegistry
    ) -> None:
        for section in RECORD_COUNTS:
            for name in from_corpus(corpus, section):
                source = Path(corpus.source_of(section, name))
                assert source.parent == CORPUS
                assert source.exists()


class TestOverridesAndExcludeMode:
    """The house-rules pack, which is the corpus's test of declared replacement."""

    def test_exactly_the_expected_warnings_appear_under_exclude(self) -> None:
        diagnostics = validate([CORPUS], builtin="exclude", include_environment=False)
        assert not problems(diagnostics)
        warned = [d for d in diagnostics if d.severity is Severity.WARNING]
        # Membership, not just the count: were these to migrate to another pack the
        # count alone would still read as correct.
        assert len(warned) == len(OVERRIDDEN)
        assert all(Path(d.source).name == HOUSE_RULES for d in warned)
        assert all(OVERRIDE_WARNING in d.problem for d in warned)

    def test_the_corpus_loads_clean_over_the_bundled_slice(self) -> None:
        assert not validate([CORPUS], include_environment=False)

    @pytest.mark.parametrize("section,name", sorted(OVERRIDDEN))
    def test_a_declared_override_replaces_the_bundled_record(
        self, corpus: ContentRegistry, section: str, name: str
    ) -> None:
        assert Path(corpus.source_of(section, name)).name == HOUSE_RULES

    def test_hold_person_still_resolves_with_the_builtins_excluded(
        self, corpus_alone: ContentRegistry
    ) -> None:
        # The reason the house-rules pack carries `paralyzed` at all. Without that row
        # its Hold Person would name a condition no loaded pack defines, which is an
        # error rather than a warning.
        assert "Hold Person" in corpus_alone.spells
        assert "paralyzed" in corpus_alone.condition_effects


class TestEdgeShapes:
    """The shapes the corpus set out to cover once each, so none can quietly go."""

    def test_every_effect_flag_is_exercised(
        self, corpus_alone: ContentRegistry
    ) -> None:
        effects = from_corpus(corpus_alone, "conditions")
        set_somewhere = {
            flag
            for record in effects.values()
            for flag, value in (record.get("effects") or {}).items()
            if value
        }
        missing = [flag for flag in EFFECT_FLAGS if flag not in set_somewhere]
        assert not missing, f"no corpus condition sets: {missing}"

    def test_a_condition_may_carry_no_flags_at_all(
        self, corpus_alone: ContentRegistry
    ) -> None:
        names = [
            name
            for name, effect in from_corpus(corpus_alone, "conditions").items()
            if not (effect.get("effects") or {})
        ]
        assert names, "the corpus no longer covers a condition with no mechanics"
        # It must still resolve rather than being treated as unknown.
        assert effect_of(names[0], corpus_alone.condition_effects) is not None

    @pytest.mark.parametrize("level", [0, 9])
    def test_the_spell_level_range_is_covered(
        self, corpus_alone: ContentRegistry, level: int
    ) -> None:
        records = from_corpus(corpus_alone, "spells")
        assert any(record.get("level") == level for record in records.values())

    def test_a_spell_may_carry_no_mechanics_if_it_says_why(
        self, corpus_alone: ContentRegistry
    ) -> None:
        mechanics = {
            "requires_attack_roll", "save_ability", "damage", "damage_type",
            "half_on_save", "upcast_damage", "shape", "radius", "max_targets",
            "condition", "concentration",
        }
        inert = [
            record
            for record in from_corpus(corpus_alone, "spells").values()
            if not mechanics & set(record)
        ]
        assert inert, "the corpus no longer covers a spell with nothing modelled"
        # Silently dropping the printed effect is the failure mode this guards.
        assert all(record.get("unmodelled") for record in inert)

    def test_a_spell_may_decline_half_damage_on_a_save(
        self, corpus_alone: ContentRegistry
    ) -> None:
        records = from_corpus(corpus_alone, "spells")
        assert any(record.get("half_on_save") is False for record in records.values())

    def test_a_spell_may_cap_its_targets(self, corpus_alone: ContentRegistry) -> None:
        records = from_corpus(corpus_alone, "spells")
        assert any(record.get("max_targets") for record in records.values())

    def test_a_creature_may_have_no_speed(self, corpus_alone: ContentRegistry) -> None:
        records = from_corpus(corpus_alone, "creatures")
        assert any(record.get("speed") == 0 for record in records.values())

    def test_a_creature_may_swing_more_than_once(
        self, corpus_alone: ContentRegistry
    ) -> None:
        records = from_corpus(corpus_alone, "creatures")
        assert any(
            (record.get("attacks_per_action") or 1) > 1 for record in records.values()
        )

    def test_reach_extends_past_a_weapon_arm(
        self, corpus_alone: ContentRegistry
    ) -> None:
        reaches = {
            attack.get("reach", 5)
            for record in from_corpus(corpus_alone, "creatures").values()
            for attack in record.get("attacks", [])
        }
        assert max(reaches) >= 20

    def test_the_minimum_creature_is_loadable(
        self, corpus_alone: ContentRegistry
    ) -> None:
        mechanical = {"team", "speed", "abilities", "attacks", "spells", "items"}
        bare = [
            name
            for name, record in from_corpus(corpus_alone, "creatures").items()
            if not mechanical & set(record)
        ]
        assert bare, "the corpus no longer covers the minimum legal creature"
        creature = make_creature(
            bare[0], registry=corpus_alone, label="A", team="a"
        )
        assert creature.max_hp > 0


class TestConditionsAreOrdinaryStrings:
    """The centerpiece: 111 plain-``str`` conditions through the paths that read them.

    A leftover ``.value``, or a lookup against the module-level ``EFFECTS`` rather
    than the passed table, survives the rest of the suite because every bundled
    condition is a ``StrEnum`` member. None of these names is.
    """

    def condition_names(self, registry: ContentRegistry) -> list[str]:
        return sorted(from_corpus(registry, "conditions"))

    def test_there_are_enough_of_them_to_matter(
        self, corpus_alone: ContentRegistry
    ) -> None:
        assert len(self.condition_names(corpus_alone)) == RECORD_COUNTS["conditions"]

    def test_none_of_them_is_secretly_an_enum_member(
        self, corpus_alone: ContentRegistry
    ) -> None:
        # The premise of everything below. Were these Condition members they would
        # answer to the module-level table too, and the sweep would prove nothing.
        for name in self.condition_names(corpus_alone):
            assert not isinstance(name, Condition)

    def test_every_condition_resolves_through_every_query_path(
        self, corpus_alone: ContentRegistry
    ) -> None:
        table = corpus_alone.condition_effects
        for name in self.condition_names(corpus_alone):
            effect = effect_of(name, table)
            # Consistency between the flag and the helper that reads it: a helper
            # short-circuiting on an enum member would diverge here.
            assert is_incapacitated([name], table) is effect.incapacitated
            assert speed_is_zero([name], table) is effect.speed_zero
            for kind, distance in ((AttackKind.MELEE, 5), (AttackKind.RANGED, 60)):
                in_melee = kind is AttackKind.MELEE
                # The automatic critical is scoped by distance alone, so its oracle
                # reads the distance. The two cases above vary kind and distance
                # together, so an oracle keyed on ``in_melee`` agreed here by
                # coincidence and would have hidden a ranged attack from inside 5 ft.
                within_5_feet = distance <= MELEE_THRESHOLD
                assert melee_hit_is_critical(
                    target_conditions=[name], distance=distance,
                    condition_effects=table,
                ) is (effect.melee_hits_are_critical and within_5_feet)
                for as_attacker in (True, False):
                    got = compute_attack_advantage(
                        attacker_conditions=[name] if as_attacker else [],
                        target_conditions=[] if as_attacker else [name],
                        kind=kind,
                        distance=distance,
                        condition_effects=table,
                    )
                    want = expected_advantage(
                        effect, as_attacker=as_attacker, in_melee=in_melee
                    )
                    role = "attacker" if as_attacker else "target"
                    assert got is want, f"{name} as {role}, {kind} at {distance} ft"

    def test_a_custom_condition_can_be_directional(
        self, corpus_alone: ContentRegistry
    ) -> None:
        # The prone shape, which no bundled-condition test can reach through a pack:
        # easier to hit up close, harder to hit from across the scree.
        table = corpus_alone.condition_effects
        pinned = ["shatterhorn-scree-pinned"]
        assert compute_attack_advantage(
            attacker_conditions=[], target_conditions=pinned,
            kind=AttackKind.MELEE, distance=5, condition_effects=table,
        ) is Advantage.ADVANTAGE
        assert compute_attack_advantage(
            attacker_conditions=[], target_conditions=pinned,
            kind=AttackKind.RANGED, distance=60, condition_effects=table,
        ) is Advantage.DISADVANTAGE

    def test_a_custom_condition_takes_a_creature_out_of_the_fight(
        self, corpus_alone: ContentRegistry
    ) -> None:
        table = corpus_alone.condition_effects
        name = next(
            n for n in self.condition_names(corpus_alone) if effect_of(n, table).incapacitated
        )
        creature = make_creature(
            "Shatterhorn Ram", registry=corpus_alone, label="A", team="a"
        )
        creature.add_condition(name)
        assert creature.active is False

    def test_a_custom_condition_survives_an_encounter_round_trip(
        self, corpus_alone: ContentRegistry
    ) -> None:
        # Drives the narration and state paths, and exercises the condition table
        # Encounter.__init__ injects into every combatant.
        attacker = make_creature(
            "Shatterhorn Scree-Hawk", registry=corpus_alone, label="A", team="a"
        )
        target = make_creature(
            "Shatterhorn Ram", registry=corpus_alone, label="B", team="b"
        )
        attacker.items = {"Hawk-Feather Charm": 1}
        target.position = 5
        rng = Random(7)
        encounter = Encounter(
            [attacker, target], rng,
            spellbook=corpus_alone.spells,
            items=corpus_alone.items,
            condition_effects=corpus_alone.condition_effects,
        )
        for _ in range(4):
            if encounter.current_name == "A":
                break
            encounter.advance(rng)
        # Named here rather than surfacing as an EncounterError out of act(): if a
        # future initiative change stops the seed reaching A, the cause should say so.
        assert encounter.current_name == "A"
        events = encounter.act(
            Action(kind=ActionKind.USE_ITEM, item="Hawk-Feather Charm", target="A"), rng
        )
        assert any("shatterhorn-hawk-eyed" in event.detail for event in events)
        assert "shatterhorn-hawk-eyed" in attacker.conditions
        state = encounter.state()
        shown = next(c for c in state["combatants"] if c["name"] == "A")
        assert "shatterhorn-hawk-eyed" in shown["conditions"]

    def test_a_fight_under_corpus_content_is_reproducible(
        self, corpus_alone: ContentRegistry
    ) -> None:
        from fivee_sim.analytics.montecarlo import run_encounter

        def transcript(seed: int) -> list[tuple[str, str, str, str]]:
            rng = Random(seed)
            combatants = [
                make_creature(
                    "Shatterhorn Crag-Stalker", registry=corpus_alone,
                    label="A", team="a",
                ),
                make_creature(
                    "Shatterhorn Storm-Goat", registry=corpus_alone,
                    label="B", team="b",
                ),
            ]
            combatants[1].position = 5
            encounter = Encounter(
                combatants, rng,
                spellbook=corpus_alone.spells,
                items=corpus_alone.items,
                condition_effects=corpus_alone.condition_effects,
            )
            run_encounter(encounter, rng, max_rounds=20)
            return [(e.kind, e.actor, e.target, e.detail) for e in encounter.log]

        assert transcript(11) == transcript(11)
