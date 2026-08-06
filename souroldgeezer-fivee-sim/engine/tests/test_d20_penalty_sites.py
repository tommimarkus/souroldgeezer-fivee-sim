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
from enum import StrEnum
from pathlib import Path

import fivee_sim


class Verdict(StrEnum):
    """Why the penalty does or does not need applying *at* a given site.

    A closed set rather than free prose, because prose is unfalsifiable: a
    registry whose only requirement is "say something" is satisfied by the
    string ``trust me``, and an audit demonstrated exactly that against the
    first version of this file — registering a genuinely unfolded new site
    with a bogus reason passed. Each verdict below is corroborated against the
    source by
    :meth:`TestEveryD20TestSiteIsAccountedFor.test_every_verdict_is_corroborated_by_the_source`,
    so a wrong one fails rather than reads plausibly.
    """

    #: The modifier handed to this call comes from an accessor that already
    #: subtracts the penalty. Corroborated: the call's own keyword arguments
    #: must name one of the three accessors.
    FOLDED = "folded"
    #: This roll does not route through any accessor, so the enclosing
    #: function subtracts the penalty itself. Corroborated: that function must
    #: reference ``d20_test_penalty``.
    EXPLICIT = "explicit"
    #: A ``kernel/`` primitive, which by CLAUDE.md's layer rule never holds a
    #: ``Creature`` and can only forward what its caller folded. Corroborated:
    #: the site must actually live under ``kernel/``.
    KERNEL_FORWARDS = "kernel_forwards"
    #: A bare operation taking a caller-supplied modifier and no combatant, so
    #: there is no creature for a condition to be held on. Corroborated: the
    #: site must live in ``service/primitives.py``.
    NO_COMBATANT = "no_combatant"


#: The accessors that fold the penalty in. A ``FOLDED`` site must name one.
_FOLDING_ACCESSORS = frozenset({"save_modifier", "check_modifier", "attack_modifier"})

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
REGISTRY: dict[str, tuple[Verdict, str]] = {
    # --- model/encounter.py: the model-layer roll-assembly sites ----------
    "model/encounter.py:__init__:roll_d20": (
        Verdict.EXPLICIT,
        "Initiative — a Dexterity ability check that does not route through "
        "check_modifier, so the penalty is subtracted explicitly beside the "
        "printed-bonus/Dexterity-modifier choice (T1b's inverse: this is the "
        "one ability check *not* covered by the general fold)."
    ),
    "model/encounter.py:_death_save:roll_d20": (
        Verdict.EXPLICIT,
        "Death saving throw — SRD 5.2.1 p.17: not tied to an ability score, so "
        "it does not route through save_modifier either; the penalty is "
        "subtracted explicitly into the total the DC is compared against, "
        "while the natural-20/natural-1 face rulings keep reading the raw die."
    ),
    "model/encounter.py:_apply_attack_rider:make_d20_test": (
        Verdict.FOLDED,
        "An attack rider's saving throw — modifier=target.save_modifier(...), "
        "which already folds the penalty."
    ),
    "model/encounter.py:_apply_damage:make_d20_test": (
        Verdict.FOLDED,
        "Concentration and Undead Fortitude saves — both pass "
        "modifier=target.save_modifier(...)."
    ),
    "model/encounter.py:_undead_fortitude_save:make_d20_test": (
        Verdict.FOLDED,
        "Undead Fortitude's own save — modifier=target.save_modifier(...)."
    ),
    "model/encounter.py:_do_interact:make_d20_test": (
        Verdict.FOLDED,
        "A map fixture's ability check — modifier=actor.check_modifier(...)."
    ),
    # --- analytics/montecarlo.py: the auto-play policy's own expectations -
    "analytics/montecarlo.py:_attack_options:attack_damage_expectation": (
        Verdict.FOLDED,
        "The weapon-attack valuation — attack_bonus=actor.attack_modifier(...)."
    ),
    "analytics/montecarlo.py:_spell_options:attack_damage_expectation": (
        Verdict.FOLDED,
        "The attack-roll spell valuation — "
        "attack_bonus=actor.attack_modifier(actor.spell_attack_bonus)."
    ),
    "analytics/montecarlo.py:_save_expectation:save_damage_expectation": (
        Verdict.FOLDED,
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
        Verdict.KERNEL_FORWARDS,
        "resolve_attack forwards the attack_bonus its model-layer caller "
        "computed (already folded through Creature.attack_modifier)."
    ),
    "kernel/items.py:resolve_item_use:make_d20_test": (
        Verdict.KERNEL_FORWARDS,
        "resolve_item_use forwards the save modifier its model-layer caller "
        "computed (already folded through Creature.save_modifier)."
    ),
    "kernel/spells.py:resolve_spell:resolve_attack_roll": (
        Verdict.KERNEL_FORWARDS,
        "resolve_spell forwards the spell attack bonus its model-layer caller "
        "computed (already folded through Creature.attack_modifier)."
    ),
    "kernel/spells.py:resolve_spell:make_d20_test": (
        Verdict.KERNEL_FORWARDS,
        "resolve_spell forwards each target's save modifier its model-layer "
        "caller computed (already folded through Creature.save_modifier)."
    ),
    "kernel/rules.py:make_d20_test:roll_d20": (
        Verdict.KERNEL_FORWARDS,
        "make_d20_test's own definition — the modifier it receives is added "
        "after this roll, by every caller above."
    ),
    "kernel/rules.py:resolve_attack_roll:roll_d20": (
        Verdict.KERNEL_FORWARDS,
        "resolve_attack_roll's own definition — same standing as make_d20_test."
    ),
    # --- service/primitives.py: bare, caller-supplied rolls ----------------
    "service/primitives.py:check:make_d20_test": (
        Verdict.NO_COMBATANT,
        "primitives.check takes a caller-supplied modifier and no combatant — "
        "there is no Creature here for a condition to be held on."
    ),
    "service/primitives.py:save:make_d20_test": (
        Verdict.NO_COMBATANT,
        "primitives.save takes a caller-supplied modifier and no combatant — "
        "same standing as primitives.check."
    ),
    "service/primitives.py:roll:roll_d20": (
        Verdict.NO_COMBATANT,
        "primitives.roll is a bare die — no modifier, no combatant, no "
        "condition to apply."
    ),
}


def _call_sites() -> dict[str, tuple[ast.Call, ast.FunctionDef | ast.AsyncFunctionDef | None]]:
    """Every D20 Test call site, keyed as in :data:`REGISTRY`.

    Returns the call node and its enclosing function as well as the key,
    because corroborating a verdict means reading what the site actually does
    — the keyword arguments it passes, and what its enclosing function
    references — not just that somebody wrote a sentence about it.
    """
    root = Path(str(fivee_sim.__file__)).parent
    sites: dict[str, tuple[ast.Call, ast.FunctionDef | ast.AsyncFunctionDef | None]] = {}
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
            sites[key] = (node, enclosing)
    assert total >= 17, (
        f"only {total} D20 Test call sites were found; the derivation has "
        f"stopped reading the source rather than the source having stopped "
        f"rolling D20 Tests"
    )
    return sites


def _names_used(node: ast.AST) -> set[str]:
    """Every bare name and attribute name appearing anywhere under ``node``."""
    used: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            used.add(child.id)
        elif isinstance(child, ast.Attribute):
            used.add(child.attr)
    return used


class TestEveryD20TestSiteIsAccountedFor:
    def test_every_call_site_is_registered(self) -> None:
        found = set(_call_sites())
        missing = sorted(found - set(REGISTRY))
        assert not missing, (
            "these D20 Test call sites exist and REGISTRY names none of them, "
            "so nobody has decided whether the condition penalty reaches this "
            "roll — add each to REGISTRY with a reason: " + ", ".join(missing)
        )

    def test_no_registered_site_has_gone_stale(self) -> None:
        found = set(_call_sites())
        stale = sorted(set(REGISTRY) - found)
        assert not stale, (
            "REGISTRY names these sites but the source no longer calls a D20 "
            "Test primitive there — the site moved, was renamed, or was "
            "deleted; update REGISTRY to match: " + ", ".join(stale)
        )

    def test_every_verdict_is_corroborated_by_the_source(self) -> None:
        """A registration has to be *true*, not merely present.

        The two tests above prove the registry is complete and current, which
        is drift detection: nobody can add a D20 Test roll without deciding
        about it. They do not prove the decision was right, and an audit made
        that concrete by registering a genuinely unfolded site with the reason
        ``trust me`` — both tests passed.

        So each :class:`Verdict` names a claim about the source, and this
        checks the source still bears it out. What it deliberately cannot
        check is whether an accessor a ``FOLDED`` site names actually
        subtracts the penalty *correctly* — that is what the behavioural pins
        in ``test_encounter.py::TestD20TestPenalty`` are for, and the two
        controls are complementary rather than redundant.
        """
        sites = _call_sites()
        for key, (verdict, reason) in sorted(REGISTRY.items()):
            assert len(reason.split()) >= 5, (
                f"{key}: a one-word reason explains nothing — say why"
            )
            call, enclosing = sites[key]
            if verdict is Verdict.FOLDED:
                named = _names_used(call) & _FOLDING_ACCESSORS
                assert named, (
                    f"{key} claims its modifier is already folded, but the "
                    f"call names none of {sorted(_FOLDING_ACCESSORS)}. Either "
                    f"the fold is missing or the verdict is wrong."
                )
            elif verdict is Verdict.EXPLICIT:
                assert enclosing is not None, f"{key}: no enclosing function"
                assert "d20_test_penalty" in _names_used(enclosing), (
                    f"{key} claims it subtracts the penalty itself, but "
                    f"{enclosing.name} never references d20_test_penalty."
                )
            elif verdict is Verdict.KERNEL_FORWARDS:
                assert key.startswith("kernel/"), (
                    f"{key} claims kernel standing — that a function holds no "
                    f"Creature — but it does not live under kernel/."
                )
            else:
                assert key.startswith("service/primitives.py:"), (
                    f"{key} claims it takes a caller-supplied modifier and no "
                    f"combatant, which is only true of service/primitives.py."
                )
