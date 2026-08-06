"""Every D20 Test site, accounted for.

SRD 5.2.1 p.180: "D20 Tests encompass the four main d20 rolls of the game:
ability checks, attack rolls, and saving throws. If something in the game
affects D20 Tests, it affects all three." A condition's
``d20_test_penalty_per_level`` has to reach every one of them, and T1b
(``6156d70``) is the standing proof that "reaches every ability check" is
easy to get subtly wrong: an Initiative rider set on the general ability-check
flag leaked into every map-fixture check as well, green throughout, because
nothing enumerated the call sites the fix had to reach.

So this reads the whole ``src/fivee_sim`` tree with :mod:`ast`, the same
derivation :func:`tests.test_player_brief.emitted_data_keys` uses for
``Encounter._emit`` call sites, and finds every call to the primitives a D20
Test is actually rolled through: :func:`~fivee_sim.kernel.rules.make_d20_test`,
:func:`~fivee_sim.kernel.rules.resolve_attack_roll`,
:func:`~fivee_sim.kernel.dice.roll_d20`, and the two expectation helpers
analytics values a proposed action by
(:func:`~fivee_sim.analytics.expectation.attack_damage_expectation`,
:func:`~fivee_sim.analytics.expectation.save_damage_expectation`). Every site
found has to be named in :data:`REGISTRY` below, with a reason. A thirteenth
site added later — a new spell resolution path, a new analytics valuation —
fails here until someone decides which bucket it belongs in, rather than
silently rolling under an unpenalised modifier.
"""

from __future__ import annotations

import ast
from pathlib import Path

import fivee_sim

_D20_TEST_CALLS = frozenset(
    {
        "make_d20_test",
        "resolve_attack_roll",
        "roll_d20",
        "attack_damage_expectation",
        "save_damage_expectation",
    }
)

#: Every call site to one of the four D20 Test primitives, keyed by
#: ``relative/path.py:enclosing_function:call_name`` — stable under line-number
#: churn, because a moved line is not a new site. The value is why the penalty
#: does or does not need to be applied *at* that site.
REGISTRY: dict[str, str] = {
    # --- model/encounter.py: the model-layer roll-assembly sites ----------
    "model/encounter.py:__init__:roll_d20": (
        "Initiative — a Dexterity ability check that does not route through "
        "check_modifier, so the penalty is subtracted explicitly beside the "
        "printed-bonus/Dexterity-modifier choice (T1b's inverse: this is the "
        "one ability check *not* covered by the general fold)."
    ),
    "model/encounter.py:_death_save:roll_d20": (
        "Death saving throw — SRD 5.2.1 p.17: not tied to an ability score, so "
        "it does not route through save_modifier either; the penalty is "
        "subtracted explicitly into the total the DC is compared against, "
        "while the natural-20/natural-1 face rulings keep reading the raw die."
    ),
    "model/encounter.py:_apply_attack_rider:make_d20_test": (
        "An attack rider's saving throw — modifier=target.save_modifier(...), "
        "which already folds the penalty."
    ),
    "model/encounter.py:_apply_damage:make_d20_test": (
        "Concentration and Undead Fortitude saves — both pass "
        "modifier=target.save_modifier(...)."
    ),
    "model/encounter.py:_undead_fortitude_save:make_d20_test": (
        "Undead Fortitude's own save — modifier=target.save_modifier(...)."
    ),
    "model/encounter.py:_do_interact:make_d20_test": (
        "A map fixture's ability check — modifier=actor.check_modifier(...)."
    ),
    # --- analytics/montecarlo.py: the auto-play policy's own expectations -
    "analytics/montecarlo.py:_attack_options:attack_damage_expectation": (
        "The weapon-attack valuation — attack_bonus=actor.attack_modifier(...)."
    ),
    "analytics/montecarlo.py:_spell_options:attack_damage_expectation": (
        "The attack-roll spell valuation — "
        "attack_bonus=actor.attack_modifier(actor.spell_attack_bonus)."
    ),
    "analytics/montecarlo.py:_save_expectation:save_damage_expectation": (
        "The save-based spell valuation — save_modifier=target.save_modifier(...)."
    ),
    # --- kernel/: pure primitives, which never hold a Creature -------------
    # CLAUDE.md: "kernel/ holds the primitives ... and knows nothing about
    # creatures; callers pass the handful of values a roll depends on." A
    # kernel function cannot call Creature.attack_modifier/save_modifier/
    # check_modifier — it has no creature to call them on — so every site
    # below receives a modifier its caller (always in model/ or analytics/,
    # every one registered above) has already folded the penalty into.
    "kernel/actions.py:resolve_attack:resolve_attack_roll": (
        "resolve_attack forwards the attack_bonus its model-layer caller "
        "computed (already folded through Creature.attack_modifier)."
    ),
    "kernel/items.py:resolve_item_use:make_d20_test": (
        "resolve_item_use forwards the save modifier its model-layer caller "
        "computed (already folded through Creature.save_modifier)."
    ),
    "kernel/spells.py:resolve_spell:resolve_attack_roll": (
        "resolve_spell forwards the spell attack bonus its model-layer caller "
        "computed (already folded through Creature.attack_modifier)."
    ),
    "kernel/spells.py:resolve_spell:make_d20_test": (
        "resolve_spell forwards each target's save modifier its model-layer "
        "caller computed (already folded through Creature.save_modifier)."
    ),
    "kernel/rules.py:make_d20_test:roll_d20": (
        "make_d20_test's own definition — the modifier it receives is added "
        "after this roll, by every caller above."
    ),
    "kernel/rules.py:resolve_attack_roll:roll_d20": (
        "resolve_attack_roll's own definition — same standing as make_d20_test."
    ),
    # --- service/primitives.py: bare, caller-supplied rolls ----------------
    "service/primitives.py:check:make_d20_test": (
        "primitives.check takes a caller-supplied modifier and no combatant — "
        "there is no Creature here for a condition to be held on."
    ),
    "service/primitives.py:save:make_d20_test": (
        "primitives.save takes a caller-supplied modifier and no combatant — "
        "same standing as primitives.check."
    ),
    "service/primitives.py:roll:roll_d20": (
        "primitives.roll is a bare die — no modifier, no combatant, no "
        "condition to apply."
    ),
}


def _call_sites() -> set[str]:
    root = Path(str(fivee_sim.__file__)).parent
    sites: set[str] = set()
    total = 0
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        holder: dict[ast.AST, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for child in ast.walk(node):
                    holder.setdefault(child, node)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id not in _D20_TEST_CALLS:
                continue
            total += 1
            enclosing = holder.get(node)
            key = (
                f"{path.relative_to(root)}:"
                f"{enclosing.name if enclosing is not None else '<module>'}:"
                f"{node.func.id}"
            )
            sites.add(key)
    assert total >= 17, (
        f"only {total} D20 Test call sites were found; the derivation has "
        f"stopped reading the source rather than the source having stopped "
        f"rolling D20 Tests"
    )
    return sites


class TestEveryD20TestSiteIsAccountedFor:
    def test_every_call_site_is_registered(self) -> None:
        found = _call_sites()
        missing = sorted(found - set(REGISTRY))
        assert not missing, (
            "these D20 Test call sites exist and REGISTRY names none of them, "
            "so nobody has decided whether the condition penalty reaches this "
            "roll — add each to REGISTRY with a reason: " + ", ".join(missing)
        )

    def test_no_registered_site_has_gone_stale(self) -> None:
        found = _call_sites()
        stale = sorted(set(REGISTRY) - found)
        assert not stale, (
            "REGISTRY names these sites but the source no longer calls a D20 "
            "Test primitive there — the site moved, was renamed, or was "
            "deleted; update REGISTRY to match: " + ", ".join(stale)
        )
