"""The view one combatant is entitled to, rather than the whole fight.

``encounter.state`` reports every creature's hit points, AC, slots and items, so
handing it to a player is handing them the other side's sheet. Until now the only
answer was for a game master to redact in prose, which is a discipline rather than
a guarantee — and a discipline that has to be re-applied every single turn.

``encounter.brief`` is that redaction moved into the engine, and the same
projection narrows the four operations that *write* when they are given a chair.
What is asserted here is mostly *absence*, which is the hard thing to test well,
so nothing below is checked against a field list written into this file: a
literal set would pin the projection against itself, and both sides would be
edited in the same commit.

**Every classification is derived, and each from the thing it classifies.** The
creature, map and fixture buckets are held against real payloads. The event
buckets are read out of ``model/encounter.py`` with ``ast`` — every ``_emit``
call site, following a ``**splat`` into the function that built it — because a
sampled set is only whatever the fixture happened to make happen: a roster with
no undead never emits ``undead_fortitude``, so its ``dc`` would sit unclassified
with the suite green. The derivation asserts it found something, so it cannot
pass by matching nothing.

**The brief's top level is deliberately not classified**, and that is a
difference from the payloads that are. It is a payload the model *constructs* —
every key of it is written out by hand in :meth:`Encounter.brief_of` — so the
failure two frozen sets exist to prevent, a field added to the model defaulting
into the answer, cannot happen there. The sets guard exactly the four places the
brief passes a payload through by *filtering* it: an opponent's entry, the map
block, each fixture summary, and an event.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest

from fivee_sim.model import encounter as model
from fivee_sim.service.errors import NotFoundError

from . import api
from .conftest import advance_encounter_to

FIXTURE = "synthetic test fixture, not SRD content"

#: The seat every case below looks through.
VIEWER = "Thora"

#: Values chosen to be unmistakable in a byte search: no other field of this
#: fixture — position, initiative, distance, elevation, round — can produce
#: them, so finding one in a response means it came from the creature that owns
#: it and from nowhere else.
FOE_MAX_HP = 4400
FOE_HP = 3300
FOE_AC = 4093
FOE_ATTACK = "Rustcleaver"
FOE_SPELL = "Wraithbolt"
FOE_ITEM = "Emberflask"

#: Same *ratio* as the foe above, a hundredth of the scale. Its band must match,
#: which is what makes the band a bracket rather than a lossy encoding of hp.
TWIN_MAX_HP = 44
TWIN_HP = 33

#: The ambusher. Rolled into initiative like anyone else and reported by
#: ``encounter.state``, but not on the battlefield until round 9.
AMBUSHER = "Zzaxil"

#: A seed at which the ambusher wins initiative outright, so that ending the
#: last turn of a round stamps ``round 2 begins`` with a creature the viewer
#: cannot see. One case needs that and no other does; the case asserts the
#: property rather than trusting the number, so a dice-stream change fails it
#: with a sentence instead of passing vacuously.
AMBUSHER_LEADS_SEED = 20260810

ROOM: dict[str, Any] = {
    "name": "muster hall",
    "width": 10,
    "height": 6,
    "rows": [
        "##########",
        "#........#",
        "#........#",
        "#........#",
        "#........#",
        "##########",
    ],
    "legend": {"#": "wall", ".": "normal"},
}


def hero_spec() -> dict[str, Any]:
    """The viewer, carrying one of everything the withheld half covers.

    Slots, items, attacks, spells, a condition, a facing and a bonus action are
    all populated deliberately: an empty own-sheet field cannot show whether it
    was projected or dropped.
    """
    return {
        "name": VIEWER,
        "team": "party",
        "ac": 16,
        "max_hp": 30,
        "hp": 22,
        "speed": 30,
        "darkvision": 60,
        "facing": "north",
        "conditions": ["prone"],
        "bonus_actions": ["dash"],
        "spells": ["Sacred Flame"],
        "spell_slots": {"1": 3},
        "items": {"Potion of Healing": 2},
        "attacks": [
            {
                "name": "Longsword",
                "attack_bonus": 5,
                "damage": "1d8+3",
                "damage_type": "slashing",
                "kind": "melee",
                "provenance": FIXTURE,
            }
        ],
        "position": [5, 5],
        "provenance": FIXTURE,
    }


def ally_spec() -> dict[str, Any]:
    """A second seat on the viewer's side, so ``allies`` is never empty."""
    return {**hero_spec(), "name": "Kesh", "position": [10, 5], "conditions": []}


def foe_spec() -> dict[str, Any]:
    return {
        "name": "Grelk",
        "team": "monsters",
        "ac": FOE_AC,
        "max_hp": FOE_MAX_HP,
        "hp": FOE_HP,
        "spells": [FOE_SPELL],
        "spell_slots": {"3": 2},
        "items": {FOE_ITEM: 5},
        "attacks": [
            {
                "name": FOE_ATTACK,
                "attack_bonus": 6,
                "damage": "2d6+4",
                "damage_type": "slashing",
                "kind": "melee",
                "provenance": FIXTURE,
            }
        ],
        "position": [40, 5],
        "provenance": FIXTURE,
    }


def twin_spec() -> dict[str, Any]:
    return {
        "name": "Grelka",
        "team": "monsters",
        "ac": 13,
        "max_hp": TWIN_MAX_HP,
        "hp": TWIN_HP,
        "position": [40, 20],
        "provenance": FIXTURE,
    }


def ambusher_spec() -> dict[str, Any]:
    return {
        "name": AMBUSHER,
        "team": "monsters",
        "ac": 12,
        "max_hp": 9,
        "position": [20, 20],
        "arrival_round": 9,
        "provenance": FIXTURE,
    }


def fight(seed: int = 20260805, on_a_map: bool = True) -> str:
    """The shared roster: a viewer, an ally, two visible foes, one who is late."""
    created = api.encounter_create(
        [hero_spec(), ally_spec(), foe_spec(), twin_spec(), ambusher_spec()],
        seed=seed,
        map=ROOM if on_a_map else None,
    )
    return str(created["encounter_id"])


#: A saved map document rather than the inline spec, because only the document
#: format carries the two things this fixture exists for: a fixture whose
#: ability check has a DC, and the wiring that says what it governs.
VAULT_DC = 21
VAULT_DOOR = "vault-door"
VAULT_LEVER = "sluice-lever"

#: The lever's own DC, and the reason it has one: it is the fixture the viewer
#: can actually reach and actually work, so it is the only one whose check ends
#: up in an event *the viewer owns*. Distinctive on purpose — a d20 and this
#: roster's modifiers cannot reach 29, so finding it in a payload is proof of
#: where it came from.
LEVER_DC = 29


def vault_document() -> dict[str, Any]:
    return {
        "format": "fivee-sim-map",
        "format_version": 1,
        "name": "vault",
        "grid": {"width": 6, "height": 4, "cell_feet": 5},
        "legend": {".": "floor", "#": "wall"},
        "tiles": ["######", "#....#", "#....#", "######"],
        "features": [
            {
                "id": VAULT_DOOR,
                "kind": "door",
                "at": [5, 2],
                "orientation": "vertical",
                "state": "closed",
                "check": {"ability": "strength", "dc": VAULT_DC},
                "requires": [VAULT_LEVER],
            },
            {
                "id": VAULT_LEVER,
                "kind": "lever",
                "at": [1, 1],
                "state": "closed",
                "check": {"ability": "strength", "dc": LEVER_DC},
            },
        ],
        "provenance": {
            "generator": "hand",
            "seed": 7,
            "params": {"width": 6, "height": 4},
            "edited": False,
            "source": "Authored for the test suite; 5E-compatible original content",
        },
    }


def fight_in_the_vault(seed: int = 20260805) -> str:
    """A fight on a map whose fixtures have a DC and a dependency.

    Positions are respelled rather than reused: the vault is six squares wide
    and the shared roster stands in a ten-wide hall, so its feet would put two
    combatants through a wall.
    """
    api.map_save("vault", vault_document(), "*")
    hero = {**hero_spec(), "position": [10, 5]}
    foe = {**foe_spec(), "position": [20, 10]}
    ambusher = {**ambusher_spec(), "position": [15, 10]}
    created = api.encounter_create([hero, foe, ambusher], seed=seed, map_id="vault")
    return str(created["encounter_id"])


#: The creature the viewer can actually reach and actually hurt, and the numbers
#: that say where a leaked byte came from. ``foe_spec``'s AC is 4093 and its
#: position 35 feet off — both deliberate, both right for a brief, and both
#: useless for an event: a swing that can never be made and could never land
#: emits no ``damage``, so every claim about one would pass with the projection
#: deleted. This one stands next to the viewer, has an AC a prone commoner
#: clears, and carries a bite so it can swing back.
DUMMY = "Skrit"
DUMMY_MAX_HP = 7700
DUMMY_HP = 6600
DUMMY_ATTACK = "Serrated bite"


def dummy_spec() -> dict[str, Any]:
    return {
        "name": DUMMY,
        "team": "monsters",
        "ac": 5,
        "max_hp": DUMMY_MAX_HP,
        "hp": DUMMY_HP,
        "position": [10, 5],
        "attacks": [
            {
                "name": DUMMY_ATTACK,
                "attack_bonus": 9,
                "damage": "1d4",
                "damage_type": "piercing",
                "kind": "melee",
                "provenance": FIXTURE,
            }
        ],
        "provenance": FIXTURE,
    }


def brawl(seed: int = 20260805) -> str:
    """The shared roster plus a foe within reach, so real events are emitted.

    The viewer is stood up: ``hero_spec`` is Prone, which is disadvantage on
    every swing, and a fixture that can only miss is a fixture whose ``damage``
    assertions never run.
    """
    created = api.encounter_create(
        [
            {**hero_spec(), "conditions": []},
            dummy_spec(),
            foe_spec(),
            twin_spec(),
            ambusher_spec(),
        ],
        seed=seed,
        map=ROOM,
    )
    return str(created["encounter_id"])


def swing(encounter_id: str, viewer: str | None = None) -> dict[str, Any]:
    """The viewer's own attack on the foe beside them, answered to ``viewer``."""
    advance_encounter_to(encounter_id, VIEWER)
    return api.encounter_act(
        encounter_id, "attack", target=DUMMY, attack="Longsword", viewer=viewer
    )


def counterswing(encounter_id: str, viewer: str | None = None) -> dict[str, Any]:
    """The foe's attack on the viewer — the other side of every ``own`` decision."""
    advance_encounter_to(encounter_id, DUMMY)
    return api.encounter_act(
        encounter_id, "attack", target=VIEWER, attack=DUMMY_ATTACK, viewer=viewer
    )


def kinds_in(result: dict[str, Any], key: str = "events") -> list[str]:
    return [str(one["kind"]) for one in result[key]]


def event_named(result: dict[str, Any], kind: str, key: str = "events") -> dict[str, Any]:
    found = [one for one in result[key] if one["kind"] == kind]
    assert found, f"no {kind} event in {kinds_in(result, key)}"
    entry: dict[str, Any] = found[0]
    return entry


def enemy_named(brief: dict[str, Any], name: str) -> dict[str, Any]:
    found = [one for one in brief["enemies"] if one["name"] == name]
    assert len(found) == 1, f"{name} appears {len(found)} times among the enemies"
    entry: dict[str, Any] = found[0]
    return entry


def ally_named(brief: dict[str, Any], name: str) -> dict[str, Any]:
    found = [one for one in brief["allies"] if one["name"] == name]
    assert len(found) == 1, f"{name} appears {len(found)} times among the allies"
    entry: dict[str, Any] = found[0]
    return entry


def serialised(payload: dict[str, Any]) -> bytes:
    """The response as a client receives it — bytes, not a parsed dict.

    A nested occurrence of a withheld value is invisible to ``key in entry``
    and plain in the wire form, which is why every confidentiality assertion
    below is made here.
    """
    return json.dumps(payload, sort_keys=True, default=str).encode("utf-8")


def floats_in(value: Any) -> list[float]:
    """Every float anywhere inside a payload, however deeply nested."""
    if isinstance(value, bool):
        return []
    if isinstance(value, float):
        return [value]
    if isinstance(value, dict):
        return [found for one in value.values() for found in floats_in(one)]
    if isinstance(value, (list, tuple)):
        return [found for one in value for found in floats_in(one)]
    return []


#: Keyword names :meth:`Encounter._emit` takes as the event's own fields rather
#: than into ``**data``. Read off the signature below so the two cannot drift.
_STAMPED = ("kind", "actor", "target", "detail")


def _literal_keys(node: ast.expr, name: str) -> set[str]:
    """Every key of a dict literal, refusing anything it cannot read whole."""
    if isinstance(node, ast.IfExp):
        return _literal_keys(node.body, name) | _literal_keys(node.orelse, name)
    assert isinstance(node, ast.Dict), (
        f"{name} at line {node.lineno} is assigned something this derivation "
        f"cannot read; it is splatted into an event and its keys have to be "
        f"classified, so widen the reader rather than letting them pass"
    )
    keys = set()
    for key in node.keys:
        assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
            f"{name} at line {node.lineno} builds a key that is not a literal"
        )
        keys.add(key.value)
    return keys


def _splatted_keys(function: ast.FunctionDef, name: str) -> set[str]:
    """The keys a ``**name`` splat can carry, read off the function that builds it.

    Four spellings appear at the call sites and all four are read: ``name = {...}``,
    the annotated form, ``name["key"] = ...``, and ``name.update({...})``. Anything
    else fails the assertion above rather than quietly contributing nothing.
    """
    keys: set[str] = set()
    built = False
    for node in ast.walk(function):
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                assert node.value is not None, f"{name} is declared without a value"
                keys |= _literal_keys(node.value, name)
                built = True
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    keys |= _literal_keys(node.value, name)
                    built = True
                elif (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == name
                ):
                    index = target.slice
                    assert isinstance(index, ast.Constant) and isinstance(
                        index.value, str
                    ), f"{name} is subscripted with something that is not a literal"
                    keys.add(index.value)
                    built = True
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == name
        ):
            for argument in node.args:
                keys |= _literal_keys(argument, name)
                built = True
    assert built, f"nothing in {function.name} builds {name}"
    return keys


def emitted_data_keys() -> set[str]:
    """Every ``data`` key ``Encounter._emit`` can be called with, off the source.

    Read statically rather than sampled from a fight, because a sampled set is
    whatever the fixture happened to make happen: a roster with no undead never
    emits ``undead_fortitude``, so its ``dc`` — a difficulty class, the first
    entry on the Withhold list — would sit unclassified and the suite would
    stay green. Every call site is enumerable, so every call site is enumerated.

    A ``**splat`` is followed into the function that built it, so the three that
    carry a rider's bonus damage, a fixture's check and a spell's cover are not
    silently missing.
    """
    source = Path(str(model.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    holder: dict[ast.AST, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for child in ast.walk(node):
                holder.setdefault(child, node)

    keys: set[str] = set()
    sites = 0
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_emit"
        ):
            continue
        sites += 1
        for keyword in node.keywords:
            if keyword.arg is None:
                assert isinstance(keyword.value, ast.Name), (
                    f"the splat at line {node.lineno} is not a plain name, so its "
                    f"keys cannot be read; widen the reader"
                )
                keys |= _splatted_keys(holder[node], keyword.value.id)
            elif keyword.arg not in _STAMPED:
                keys.add(keyword.arg)
    assert sites > 40, (
        f"only {sites} _emit call sites were found; the derivation has stopped "
        f"reading the model rather than the model having stopped emitting"
    )
    return keys


class TestTheClassificationIsTotal:
    """A field nobody classified must not reach a player, and must not pass silently.

    Two frozen sets rather than one filter, everywhere below. A single allowlist
    answers "is this shown?" but not "has anybody looked at this?" — so a field
    added to a payload would default to withheld and simply go missing, which
    reads exactly like a field that was considered and refused. Requiring a
    deliberate classification is what makes a new secret impossible to add by
    accident.
    """

    def test_every_creature_field_the_model_emits_is_classified_exactly_once(
        self,
    ) -> None:
        snapshot = api.encounter_state(fight())
        emitted = {key for creature in snapshot["combatants"] for key in creature}

        buckets = {
            "ENEMY_VISIBLE_KEYS": model.ENEMY_VISIBLE_KEYS,
            "ENEMY_WITHHELD_KEYS": model.ENEMY_WITHHELD_KEYS,
        }
        unclassified = sorted(emitted - set().union(*buckets.values()))
        assert not unclassified, (
            "Encounter._creature_state emits these and the brief classifies none "
            "of them, so nobody has decided whether an opponent may see them. "
            "Put each in exactly one of ENEMY_VISIBLE_KEYS (what the table can "
            "see) or ENEMY_WITHHELD_KEYS (what the stat block says): "
            + ", ".join(unclassified)
        )
        for (left, one), (right, other) in combinations(buckets.items(), 2):
            assert not one & other, (
                f"{left} and {right} both claim "
                f"{', '.join(sorted(one & other))}; a field has one answer"
            )

    def test_the_fixture_reaches_the_fields_the_model_only_sometimes_emits(
        self,
    ) -> None:
        """Three keys are conditional, so a thin fixture would skip classifying them.

        ``facing`` appears only when something is tracking it, and ``level`` and
        ``elevation`` only on a battle map. A mapless roster would let the case
        above pass while three real fields stayed undecided — which is exactly
        how ``elevation`` sat unclassified through a release.
        """
        snapshot = api.encounter_state(fight())
        emitted = {key for creature in snapshot["combatants"] for key in creature}

        assert {"facing", "level", "elevation"} <= emitted

    def test_every_map_and_fixture_field_is_classified_exactly_once(self) -> None:
        """The level the first draft missed.

        ``map`` was passed through whole, so every fixture's ability-check DC —
        the first entry on ``game-master.md``'s Withhold list — arrived in the
        player's brief. An allowlist that stops one level above the payload is a
        denylist by another name, so the map block and each fixture summary are
        classified here exactly as the creature payload is.
        """
        snapshot = api.encounter_state(fight_in_the_vault())
        block = snapshot["map"]
        fixtures = {key for one in block["features"].values() for key in one}

        for label, emitted, buckets in (
            ("map", set(block), (model.MAP_VISIBLE_KEYS, model.MAP_WITHHELD_KEYS)),
            (
                "fixture",
                fixtures,
                (model.FEATURE_VISIBLE_KEYS, model.FEATURE_WITHHELD_KEYS),
            ),
        ):
            unclassified = sorted(emitted - set().union(*buckets))
            assert not unclassified, (
                f"the {label} payload carries these and the brief classifies "
                f"none of them: {', '.join(unclassified)}"
            )
            assert not buckets[0] & buckets[1], f"{label} double-classifies"

    def test_the_vault_reaches_every_fixture_field_that_is_worth_withholding(
        self,
    ) -> None:
        """A plain door carries four keys, and none of them is a secret.

        ``_feature_summary`` omits everything at its default, so a fixture with
        no check, no dependency and no wiring would let the case above pass over
        the very keys the withheld half exists for.
        """
        block = api.encounter_state(fight_in_the_vault())["map"]
        fixtures = {key for one in block["features"].values() for key in one}

        assert model.FEATURE_WITHHELD_KEYS & fixtures, (
            "the vault carries no wiring at all, so the classification case "
            "above would pass over an empty half"
        )

    def test_every_event_data_key_the_model_emits_is_classified_exactly_once(
        self,
    ) -> None:
        emitted = emitted_data_keys()

        buckets = {
            "EVENT_VISIBLE_KEYS": model.EVENT_VISIBLE_KEYS,
            "EVENT_WITHHELD_KEYS": model.EVENT_WITHHELD_KEYS,
        }
        unclassified = sorted(emitted - set().union(*buckets.values()))
        assert not unclassified, (
            "Encounter._emit is called with these in its data payload and the "
            "brief classifies none of them, so nobody has decided whether a "
            "player may see them. Put each in exactly one of EVENT_VISIBLE_KEYS "
            "(what the table watched) or EVENT_WITHHELD_KEYS (the owning side's "
            "sheet): " + ", ".join(unclassified)
        )
        for (left, one), (right, other) in combinations(buckets.items(), 2):
            assert not one & other, (
                f"{left} and {right} both claim "
                f"{', '.join(sorted(one & other))}; a key has one answer"
            )

    def test_the_keys_no_seat_is_served_are_a_line_inside_the_withheld_half(
        self,
    ) -> None:
        """``EVENT_NEVER_KEYS`` refines the classification; it does not escape it.

        A key served to nobody is still a key somebody decided about, so it has
        to be inside the total pair rather than beside it — otherwise the case
        above would pass over it and nobody would have looked.
        """
        stray = sorted(model.EVENT_NEVER_KEYS - model.EVENT_WITHHELD_KEYS)
        assert not stray, (
            "these are served to no seat and are not in EVENT_WITHHELD_KEYS, so "
            "the totality check above never sees them: " + ", ".join(stray)
        )

    def test_no_bucket_names_a_key_the_model_never_emits(self) -> None:
        """The other direction, which keeps the buckets from becoming a wish list.

        A key classified here and emitted nowhere is a decision about nothing —
        and, worse, cover: it makes the set above look complete while the key
        that replaced it goes unclassified.
        """
        emitted = emitted_data_keys()
        classified = model.EVENT_VISIBLE_KEYS | model.EVENT_WITHHELD_KEYS

        stale = sorted(classified - emitted)
        assert not stale, (
            "the brief classifies these event keys and no _emit call site sends "
            "them: " + ", ".join(stale)
        )

    def test_the_derivation_reaches_keys_no_fixture_in_this_file_produces(
        self,
    ) -> None:
        """The reason the set above is read off the source and not off a fight.

        Nothing here fights a zombie or drains anybody, so a derivation that
        sampled real events would never see ``dc`` or ``total_drained`` — and
        each would sit unclassified with the suite green.
        """
        emitted = emitted_data_keys()

        assert {"dc", "check", "total_drained", "triggered_by"} <= emitted

    def test_every_event_field_the_model_emits_is_classified_exactly_once(self) -> None:
        """The envelope, derived from a real event rather than from ``Event``."""
        created = api.encounter_create(
            [hero_spec(), foe_spec()], seed=20260805, map=ROOM
        )
        emitted = {key for event in created["log"] for key in event}
        assert emitted, "the fixture produced no events to classify"

        buckets = {
            "EVENT_ENVELOPE_VISIBLE_KEYS": model.EVENT_ENVELOPE_VISIBLE_KEYS,
            "EVENT_ENVELOPE_WITHHELD_KEYS": model.EVENT_ENVELOPE_WITHHELD_KEYS,
        }
        unclassified = sorted(emitted - set().union(*buckets.values()))
        assert not unclassified, (
            "Event.as_dict emits these and the brief classifies none of them: "
            + ", ".join(unclassified)
        )
        assert not (
            model.EVENT_ENVELOPE_VISIBLE_KEYS & model.EVENT_ENVELOPE_WITHHELD_KEYS
        )

    def test_every_band_the_table_can_be_told_is_one_the_vocabulary_names(
        self,
    ) -> None:
        """Both halves derived: the ceilings' words, and the two that are not bands."""
        assert {described for _, described in model._HEALTH_BANDS} <= set(
            model.HEALTH_BANDS
        )
        assert {"dead", "down"} <= set(model.HEALTH_BANDS)


class TestWhatTheAskerSees:
    def test_their_own_sheet_comes_back_whole(self) -> None:
        """Withholding a player's own sheet does not create tension; it makes
        them guess."""
        brief = api.encounter_brief(fight(), VIEWER)

        seat = brief["you"]
        assert seat["hp"] == 22 and seat["max_hp"] == 30 and seat["ac"] == 16
        assert seat["spell_slots"] == {1: 3}
        assert seat["items"] == {"Potion of Healing": 2}
        assert seat["conditions"] == ["prone"]
        assert seat["speeds"]["walk"] == 30
        assert seat["attacks"] == ["Longsword"]
        assert seat["spells"] == ["Sacred Flame"]

    def test_your_own_entry_keeps_every_field_the_model_gave_it(self) -> None:
        """Derived from the buckets: nothing on your own side is quietly dropped."""
        encounter_id = fight()
        raw_seat = next(
            one for one in api.encounter_state(encounter_id)["combatants"]
            if one["name"] == VIEWER
        )

        brief = api.encounter_brief(encounter_id, VIEWER)

        assert set(brief["you"]) == set(raw_seat)

    def test_an_ally_is_not_redacted(self) -> None:
        # Party members share numbers at a real table; the boundary is the
        # *other side*, not other people.
        brief = api.encounter_brief(fight(), VIEWER)

        kesh = ally_named(brief, "Kesh")
        assert kesh["hp"] == 22 and kesh["max_hp"] == 30 and kesh["ac"] == 16
        assert kesh["attacks"] == ["Longsword"]

    def test_the_distance_to_everyone_still_standing_is_reported(self) -> None:
        # Without this a player cannot decide their own movement, which is the
        # whole reason the brief exists rather than a prose summary.
        brief = api.encounter_brief(fight(), VIEWER)

        assert ally_named(brief, "Kesh")["distance"] == 5
        assert enemy_named(brief, "Grelk")["distance"] == 35

    def test_it_is_the_asker_s_side_that_is_privileged_not_the_party(self) -> None:
        # The mirror case. A brief that always showed "party" in full would pass
        # every other test in this class and be wrong.
        brief = api.encounter_brief(fight(), "Grelk")

        assert {one["name"] for one in brief["allies"]} == {"Grelka"}
        assert {one["name"] for one in brief["enemies"]} == {VIEWER, "Kesh"}
        assert "hp" not in enemy_named(brief, VIEWER)

    def test_on_your_turn_your_own_budget_arrives_and_no_one_else_s_does(
        self,
    ) -> None:
        """Whose turn it is, is public. What they have left of it is not."""
        encounter_id = fight()
        advance_encounter_to(encounter_id, VIEWER)
        raw = api.encounter_state(encounter_id)

        mine = api.encounter_brief(encounter_id, VIEWER)
        theirs = api.encounter_brief(encounter_id, "Grelk")

        assert mine["your_turn"] is True
        assert mine["turn_state"] == raw["turn_state"]
        assert mine["turn_state"]["movement_left"] == 30
        assert theirs["turn"] == VIEWER
        assert theirs["your_turn"] is False
        assert "turn_state" not in theirs


class TestWhatIsWithheld:
    @pytest.mark.parametrize(
        "secret", ["hp", "max_hp", "ac", "spell_slots", "items", "attacks", "spells"]
    )
    def test_an_enemy_never_carries_a_number_the_table_would_not_say(
        self, secret: str
    ) -> None:
        assert secret not in enemy_named(api.encounter_brief(fight(), VIEWER), "Grelk")

    def test_no_opposing_entry_carries_a_withheld_key_at_all(self) -> None:
        """The same claim per creature, derived from the bucket rather than listed."""
        encounter_id = fight()
        raw_foe = next(
            one for one in api.encounter_state(encounter_id)["combatants"]
            if one["name"] == "Grelk"
        )
        assert model.ENEMY_WITHHELD_KEYS & set(raw_foe), (
            "the foe carries no own-sheet field at all, so the assertion below "
            "would hold against a projection that redacted nothing"
        )

        brief = api.encounter_brief(encounter_id, VIEWER)

        for creature in brief["enemies"]:
            leaked = sorted(set(creature) & model.ENEMY_WITHHELD_KEYS)
            assert not leaked, f"{creature['name']} exposes {', '.join(leaked)}"
            for key in model.ENEMY_WITHHELD_KEYS:
                assert f'"{key}"' not in json.dumps(creature)

    def test_no_own_field_of_an_opposing_creature_appears_in_the_response(
        self,
    ) -> None:
        """The NFR, stated as bytes: confidentiality of the other side's capability.

        Asserted on the serialised response rather than the parsed dictionary
        because the failure being guarded against is a *nested* one — an
        own-sheet field surviving inside a summary, an effect record or a token
        — which a top-level key check cannot see.
        """
        encounter_id = fight()
        raw = api.encounter_state(encounter_id)

        brief = api.encounter_brief(encounter_id, VIEWER)

        body = serialised(brief)
        secrets = (
            str(FOE_HP), str(FOE_MAX_HP), str(FOE_AC), FOE_ATTACK, FOE_SPELL, FOE_ITEM
        )
        for secret in secrets:
            assert secret.encode("utf-8") not in body, (
                f"{secret!r} is the opposing side's own sheet and it reached the "
                f"player's response"
            )
        # And the fixture really did put every one of them in the GM's view, so
        # this cannot pass on a roster where the foe simply had none of them.
        gm = serialised(raw)
        for secret in secrets:
            assert secret.encode("utf-8") in gm

    def test_the_brief_carries_no_key_outside_the_allowlist(self) -> None:
        """Derived from the buckets, so an opponent's entry cannot grow one."""
        brief = api.encounter_brief(fight(), VIEWER)

        permitted = model.ENEMY_VISIBLE_KEYS | model.ENEMY_DERIVED_KEYS
        for creature in brief["enemies"]:
            assert not set(creature) - permitted, creature["name"]


class TestHealthIsABandAndNotANumber:
    def test_the_band_is_plain_language(self) -> None:
        brief = api.encounter_brief(fight(), VIEWER)

        band = enemy_named(brief, "Grelk")["health"]
        assert band in model.HEALTH_BANDS
        assert isinstance(band, str)

    def test_no_arithmetic_on_the_entry_recovers_the_hit_points(self) -> None:
        """Neither the ratio, the band's bounds, nor ``max_hp`` may be emitted."""
        brief = api.encounter_brief(fight(), VIEWER)

        foe = enemy_named(brief, "Grelk")
        assert foe["health"], "there is no band to be lossy about"
        assert FOE_HP not in foe.values() and FOE_MAX_HP not in foe.values()
        assert not set(foe) & model.ENEMY_WITHHELD_KEYS
        assert not floats_in(foe), (
            "a float in an opponent's entry is a ratio by another name: "
            f"{floats_in(foe)}"
        )
        body = serialised(brief)
        for bound in ("0.25", "0.5", "0.75", "1.0"):
            assert bound.encode("utf-8") not in body

    def test_two_foes_a_hundredfold_apart_at_one_ratio_report_one_band(self) -> None:
        """The band brackets the ratio, so scale is exactly what it does not carry."""
        brief = api.encounter_brief(fight(), VIEWER)

        assert FOE_MAX_HP == TWIN_MAX_HP * 100
        assert FOE_HP == TWIN_HP * 100
        assert (
            enemy_named(brief, "Grelk")["health"]
            == enemy_named(brief, "Grelka")["health"]
        )


class TestTheMapShowsTheRoomAndNotTheModule:
    """The leak an audit found in the released brief.

    ``map`` was handed over whole, which published every fixture's
    ability-check DC — ``game-master.md`` withholds "DCs before a roll" in its
    first sentence — along with the dependencies that say which lever opens
    which door.
    """

    def test_a_fixtures_dc_does_not_reach_the_table(self) -> None:
        encounter_id = fight_in_the_vault()
        gm = api.encounter_state(encounter_id)["map"]["features"][VAULT_DOOR]
        assert gm["check"]["dc"] == VAULT_DC, "the fixture carries no DC to withhold"

        brief = api.encounter_brief(encounter_id, VIEWER)

        assert str(VAULT_DC).encode("utf-8") not in serialised(brief)
        assert "check" not in brief["map"]["features"][VAULT_DOOR]

    def test_the_wiring_behind_a_fixture_stays_behind_it(self) -> None:
        encounter_id = fight_in_the_vault()
        gm = api.encounter_state(encounter_id)["map"]["features"][VAULT_DOOR]
        assert set(gm) & model.FEATURE_WITHHELD_KEYS, "no wiring to withhold"

        brief = api.encounter_brief(encounter_id, VIEWER)

        for name, fixture in brief["map"]["features"].items():
            assert not set(fixture) & model.FEATURE_WITHHELD_KEYS, name

    def test_the_room_itself_still_arrives(self) -> None:
        """Redaction that emptied the map would pass every case above."""
        brief = api.encounter_brief(fight_in_the_vault(), VIEWER)

        block = brief["map"]
        assert block["name"] == "vault"
        assert (block["width"], block["height"]) == (6, 4)
        assert set(block["features"]) == {VAULT_DOOR, VAULT_LEVER}
        assert block["features"][VAULT_DOOR]["open"] is False
        assert block["features"][VAULT_DOOR]["square"] == [5, 2]

    def test_a_fight_on_the_open_plane_reports_no_map_at_all(self) -> None:
        brief = api.encounter_brief(fight(on_a_map=False), VIEWER)

        assert "map" not in brief


class TestSight:
    def test_a_creature_behind_total_cover_is_not_listed_at_all(self) -> None:
        # Not merely redacted — absent. Telling a player "there is something you
        # cannot see, at this distance" is itself information they do not have.
        # Total cover means *sealed*, not merely screened: a wall between two
        # creatures leaves three-quarters cover at most, because corner-to-corner
        # lines get around it. This is the box from
        # `test_grid.py::test_a_sealed_target_has_total_cover`, with the foe
        # walled in at square (5, 5) and Thora out at (0, 0).
        walled = api.encounter_create(
            [
                {**hero_spec(), "position": [0, 0]},
                {**foe_spec(), "position": [25, 25]},
            ],
            seed=41,
            map={
                "width": 7, "height": 7,
                "rows": [
                    ".......",
                    ".......",
                    ".......",
                    ".......",
                    "....###",
                    "....#.#",
                    "....###",
                ],
                "legend": {".": "normal", "#": "wall"},
            },
        )["encounter_id"]

        brief = api.encounter_brief(str(walled), VIEWER)

        assert brief["enemies"] == []
        assert b"Grelk" not in serialised(brief)


class TestAnUndetectedCreatureIsAbsentFromEveryField:
    """Omitted, not merely unlabelled. A blank entry in the order is a reveal."""

    def test_a_creature_who_has_not_arrived_appears_nowhere_in_the_response(
        self,
    ) -> None:
        encounter_id = fight()
        raw = api.encounter_state(encounter_id)
        assert AMBUSHER in raw["order"], "the fixture never rolled the ambusher in"

        brief = api.encounter_brief(encounter_id, VIEWER)

        assert AMBUSHER.encode("utf-8") not in serialised(brief)

    def test_an_unarrived_creature_holding_the_turn_leaves_the_turn_unnamed(
        self,
    ) -> None:
        """The sharpest leak: ``turn`` names a creature the player cannot see."""
        encounter_id = fight()
        advance_encounter_to(encounter_id, AMBUSHER)

        brief = api.encounter_brief(encounter_id, VIEWER)

        assert api.encounter_state(encounter_id)["turn"] == AMBUSHER
        assert brief["turn"] is None
        assert brief["your_turn"] is False
        assert AMBUSHER.encode("utf-8") not in serialised(brief)


class TestTheEventsAreClassifiedToo:
    """The same standard as the creature projection, over the other payload.

    ``encounter.brief`` serves no events, so for a release the four operations
    that *do* answered a seat with the fight's own account of what had just
    happened, unredacted. The brief said a foe was "hurt" and the ``damage``
    event beside it said 6594/7700. These cases are what make an event key the
    same kind of decision a creature field is.
    """

    def test_a_projected_event_carries_no_key_outside_the_allowlist(self) -> None:
        """Derived from the buckets, so an event cannot grow a field quietly."""
        brief = swing(brawl(), viewer=VIEWER)

        assert kinds_in(brief) == ["attack", "damage"], kinds_in(brief)
        permitted = model.EVENT_VISIBLE_KEYS | model.EVENT_WITHHELD_KEYS
        for event in brief["events"]:
            assert not set(event) - model.EVENT_ENVELOPE_VISIBLE_KEYS, event["kind"]
            assert not set(event["data"]) - permitted, event["kind"]
            assert not set(event["data"]) & model.EVENT_NEVER_KEYS, event["kind"]

    def test_the_detail_of_every_event_is_absent_rather_than_blank(self) -> None:
        """``detail`` is omitted, and the distinction from an empty one is the point."""
        whole = swing(brawl())
        assert all(one["detail"] for one in whole["events"]), (
            "the fixture emitted an event with no detail, so the assertion "
            "below would hold against a projection that passed detail through"
        )

        brief = swing(brawl(), viewer=VIEWER)

        for event in brief["events"]:
            assert "detail" not in event, event

    def test_the_sentence_spells_out_what_every_bucket_withheld(self) -> None:
        """Why prose is omitted rather than classified: it restates the payload.

        One ``detail`` here reads ``6 damage, 6594/7700 hit points left`` and the
        one above it names the AC the swing was rolled against. Neither is a key
        anything could be decided about — which is the whole argument for
        omitting the field rather than trying to serve part of it.
        """
        whole = swing(brawl())
        sentences = " | ".join(one["detail"] for one in whole["events"])
        assert "vs AC" in sentences, sentences
        assert f"/{DUMMY_MAX_HP}" in sentences, sentences

        brief = swing(brawl(), viewer=VIEWER)

        body = serialised(brief)
        assert b"vs AC" not in body
        assert str(DUMMY_MAX_HP).encode("utf-8") not in body

    def test_a_foes_exact_hit_points_do_not_ride_in_on_a_damage_event(self) -> None:
        """The finding, as bytes: ``state`` said "hurt" and the event said 6594/7700."""
        whole = swing(brawl())
        wound = event_named(whole, "damage")
        assert wound["target"] == DUMMY
        assert wound["data"]["max_hp"] == DUMMY_MAX_HP, wound
        left = int(wound["data"]["hp"])

        brief = swing(brawl(), viewer=VIEWER)

        body = serialised(brief)
        for secret in (str(DUMMY_MAX_HP), str(left)):
            assert secret.encode("utf-8") not in body, (
                f"{secret} is the opposing side's own sheet and it reached the "
                f"player's response through the events"
            )
        assert enemy_named(brief["state"], DUMMY)["health"] in model.HEALTH_BANDS

    def test_a_dc_the_viewer_rolled_against_is_withheld_from_the_viewer_too(
        self,
    ) -> None:
        """The line inside the withheld half, exercised where it actually bites.

        Almost everything an own-side event carries is the roller's own sheet,
        which is theirs to have. A difficulty class is not: ``game-master.md``
        withholds "DCs before a roll", and a fixture worked once will be asked
        for again. ``check`` is that DC in prose — ``d20 [12] +0 = 12 vs DC 29
        -> failure`` — so an event served whole hands it to the creature that
        rolled it, and every other case in this class would stay green.
        """
        encounter_id = fight_in_the_vault()
        advance_encounter_to(encounter_id, VIEWER)
        whole = api.encounter_act(
            encounter_id, "interact", feature=VAULT_LEVER, set_open=True
        )
        worked = event_named(whole, "interact")
        assert worked["actor"] == VIEWER, worked
        assert str(LEVER_DC) in worked["data"]["check"], (
            "the fixture asked for no check, so the assertion below would hold "
            "against an event served whole"
        )

        second = fight_in_the_vault()
        advance_encounter_to(second, VIEWER)
        brief = api.encounter_act(
            second, "interact", feature=VAULT_LEVER, set_open=True, viewer=VIEWER
        )

        seen = event_named(brief, "interact")
        assert seen["actor"] == VIEWER, "this is not the viewer's own event"
        assert "check" not in seen["data"], seen
        assert "success" in seen["data"], "the result of their own roll is theirs"
        assert str(LEVER_DC).encode("utf-8") not in serialised(brief)

    def test_what_the_table_watched_land_still_arrives(self) -> None:
        """Redaction that emptied the payload would pass every case above."""
        whole = swing(brawl())

        brief = swing(brawl(), viewer=VIEWER)

        wound = event_named(brief, "damage")
        assert wound["data"]["amount"] == event_named(whole, "damage")["data"]["amount"]
        assert wound["data"]["amount"] > 0
        assert event_named(brief, "attack")["data"]["hit"] is True

    def test_your_own_sides_hit_points_are_reported_on_a_damage_event(self) -> None:
        """The other half of ``own``: your own wound is your side's to see."""
        whole = counterswing(brawl())
        wound = event_named(whole, "damage")
        assert wound["target"] == VIEWER

        brief = counterswing(brawl(), viewer=VIEWER)

        seen = event_named(brief, "damage")
        assert seen["data"]["hp"] == wound["data"]["hp"]
        assert seen["data"]["max_hp"] == wound["data"]["max_hp"]

    def test_your_own_swing_reports_the_roll_and_a_foes_swing_does_not(self) -> None:
        """``total`` bounds an AC and ``natural`` with it is the roller's bonus.

        A table says "nineteen hits" and the player who rolled it may do that
        arithmetic; the GM does not read their own dice out. So the same three
        keys are answered differently by whose swing it was.
        """
        mine = swing(brawl(), viewer=VIEWER)
        theirs = counterswing(brawl(), viewer=VIEWER)

        own = event_named(mine, "attack")["data"]
        other = event_named(theirs, "attack")["data"]
        assert {"total", "natural", "attack", "advantage"} <= set(own), own
        assert not {"total", "natural", "attack", "advantage"} & set(other), other
        # And what a table watches is answered the same way on both.
        assert own["hit"] is True and other["hit"] is True


class TestAnEventNamingSomeoneUnseenIsNotReported:
    """The brief omits an unarrived creature; so does every event beside it."""

    def test_an_unarrived_creatures_turn_is_absent_from_the_events(self) -> None:
        encounter_id = brawl()
        advance_encounter_to(encounter_id, VIEWER)
        whole = api.encounter_advance(encounter_id)
        assert AMBUSHER in {one["actor"] for one in whole["events"]}, (
            "the fixture never handed the ambusher a turn, so the assertion "
            "below would hold against a projection that reported it"
        )

        second = brawl()
        advance_encounter_to(second, VIEWER)
        brief = api.encounter_advance(second, viewer=VIEWER)

        assert AMBUSHER.encode("utf-8") not in serialised(brief)
        assert "turn_start" not in kinds_in(brief)
        # Dropped rather than blanked, which is the same answer `enemies` gives;
        # the gap it leaves in `seq` is existence without identity.
        assert [one["seq"] for one in brief["events"]] != [
            one["seq"] for one in whole["events"]
        ]

    def test_the_round_still_turns_over_when_an_unseen_creature_is_next(self) -> None:
        """The reason ``turn`` is nulled rather than dropped for.

        ``round 2 begins`` is stamped with whoever is about to act, so an event
        dropped for naming them would take the most public thing in the fight
        with it.
        """
        encounter_id = fight(seed=AMBUSHER_LEADS_SEED)
        order = list(api.encounter_state(encounter_id)["order"])
        assert order[0] == AMBUSHER, (
            f"the ambusher does not lead this order, so ending the last turn "
            f"stamps the round with somebody the viewer can see: {order}"
        )
        advance_encounter_to(encounter_id, order[-1])
        whole = api.encounter_advance(encounter_id)
        turned = event_named(whole, "round")
        assert turned["turn"] == AMBUSHER, turned

        second = fight(seed=AMBUSHER_LEADS_SEED)
        advance_encounter_to(second, order[-1])
        brief = api.encounter_advance(second, viewer=VIEWER)

        seen = event_named(brief, "round")
        assert seen["data"]["round"] == turned["data"]["round"]
        assert seen["turn"] is None
        assert AMBUSHER.encode("utf-8") not in serialised(brief)

    def test_an_arrival_needs_no_exemption_because_they_have_arrived(self) -> None:
        """Named because it is the one event that looks like it should be dropped."""
        encounter_id = fight()
        for _ in range(40):
            brief = api.encounter_advance(encounter_id, viewer=VIEWER)
            if "arrival" in kinds_in(brief):
                break
        else:  # pragma: no cover - the fixture arrives in round 9
            raise AssertionError("the ambusher never arrived")

        landed = event_named(brief, "arrival")
        assert landed["actor"] == AMBUSHER
        assert AMBUSHER in {one["name"] for one in brief["state"]["enemies"]}
        # The round they landed in is the round that has just happened, and it
        # is ENEMY_VISIBLE_KEYS on the creature for the same reason: one key,
        # one answer.
        assert landed["data"]["arrival_round"] == enemy_named(
            brief["state"], AMBUSHER
        )["arrival_round"]


class TestTheChairCarryingWritesAnswerOneShape:
    """``as=`` on the four operations that answer a caller with a fight's state.

    ``encounter.brief`` was this projection's only door for a release, and every
    *mutating* operation answered the seat that posted it with the GM's whole
    snapshot. The four now take the same seat the read does, and are answered
    the same brief — one projection, one shape, one classification.
    """

    def test_a_briefed_write_answers_the_brief_and_not_the_snapshot(self) -> None:
        briefed = swing(brawl(), viewer=VIEWER)

        state = briefed["state"]
        assert state["as"] == VIEWER
        assert set(state) >= {"as", "round", "turn", "you", "allies", "enemies"}
        # And it is the projection, not a relabelled snapshot.
        assert "combatants" not in state and "ongoing_effects" not in state

    def test_naming_no_seat_leaves_every_write_answering_the_fight_whole(self) -> None:
        # The additive promise, pinned rather than assumed: this is what the CLI
        # and both skills read, and the reconciliation must not have moved it.
        encounter_id = brawl()
        acted = swing(encounter_id)
        advanced = api.encounter_advance(encounter_id)
        resumed = api.encounter_resume(encounter_id)

        for whole in (acted, advanced, resumed):
            state = whole["state"]
            assert "as" not in state
            assert {one["name"] for one in state["combatants"]} >= {VIEWER, DUMMY}
        # Including the account of what happened, sentence and numbers both.
        assert "vs AC" in event_named(acted, "attack")["detail"]
        assert event_named(acted, "damage")["data"]["max_hp"] == DUMMY_MAX_HP

    def test_a_resumed_fight_never_hands_a_seat_the_host_s_filesystem(self) -> None:
        """``resume``'s snapshot is the one with ``map_source`` stapled on."""
        encounter_id = fight_in_the_vault()

        briefed = api.encounter_resume(encounter_id, viewer=VIEWER)
        whole = api.encounter_resume(encounter_id)

        assert whole["state"]["map_source"]["map_id"] == "vault"
        assert "map_source" not in briefed["state"]
        assert b"map_source" not in serialised(briefed)

    def test_a_retried_creation_narrows_the_log_it_replays(self) -> None:
        """The one whose events are not ``events``.

        ``create`` answers with ``log``, and an idempotent retry answers with
        the *whole* log of a fight already in progress rather than the opening
        pair a fresh one has. A projection that narrowed only ``events`` would
        hand a player every round of it.
        """
        roster = [
            {**hero_spec(), "conditions": []},
            dummy_spec(),
            foe_spec(),
            twin_spec(),
            ambusher_spec(),
        ]
        first = api.encounter_create(
            roster, seed=20260805, map=ROOM, request_id="the-same-fight"
        )
        encounter_id = str(first["encounter_id"])
        swing(encounter_id)

        retried = api.encounter_create(
            roster,
            seed=20260805,
            map=ROOM,
            request_id="the-same-fight",
            viewer=VIEWER,
        )

        assert retried["encounter_id"] == encounter_id
        assert len(retried["log"]) > 2, "the retry replayed no fight to narrow"
        assert str(DUMMY_MAX_HP).encode("utf-8") not in serialised(retried)
        assert AMBUSHER.encode("utf-8") not in serialised(retried)


class TestTheFightsOwnRecordIsNotRewritten:
    """The projection rebuilds, because the dictionary it is handed belongs elsewhere."""

    def test_a_briefed_action_leaves_the_recorded_result_whole(self) -> None:
        """The GM's retry replays the GM's answer, whoever briefed the first one.

        The result a chair is answered from is the one the journal recorded and
        the one an idempotent retry replays, so a projection applied in place
        would rewrite the fight's audit record into one player's brief and
        answer the next retry with it.
        """
        encounter_id = brawl()
        advance_encounter_to(encounter_id, VIEWER)

        briefed = api.encounter_act(
            encounter_id, "attack", target=DUMMY, attack="Longsword",
            request_id="one-swing", viewer=VIEWER,
        )
        replayed = api.encounter_act(
            encounter_id, "attack", target=DUMMY, attack="Longsword",
            request_id="one-swing",
        )

        assert str(DUMMY_MAX_HP).encode("utf-8") not in serialised(briefed)
        assert event_named(replayed, "damage")["data"]["max_hp"] == DUMMY_MAX_HP
        assert "vs AC" in event_named(replayed, "attack")["detail"]

    def test_the_journal_records_the_fight_whole_however_the_seat_was_answered(
        self,
    ) -> None:
        """The audit record is the GM's, whichever chair happened to post.

        Read off disk rather than out of memory: the journal is what a resume
        replays and what a replay bundle is composed from, so a projection that
        reached it would be invisible to every in-memory assertion.
        """
        from fivee_sim.service import encounter_journal

        encounter_id = brawl()

        briefed = swing(encounter_id, viewer=VIEWER)

        assert str(DUMMY_MAX_HP).encode("utf-8") not in serialised(briefed)
        records, _ = encounter_journal.read(encounter_id)
        written = json.dumps(records)
        assert str(DUMMY_MAX_HP) in written, (
            "the journal lost the fight's own numbers, so briefing rewrote the "
            "audit record rather than rebuilding an answer beside it"
        )
        assert '"detail"' in written and "vs AC" in written


class TestASeatTheFightDoesNotHold:
    def test_an_unknown_encounter_is_a_not_found(self) -> None:
        with pytest.raises(NotFoundError, match="enc-nope"):
            api.encounter_brief("enc-nope", VIEWER)

    def test_an_unknown_seat_is_refused_rather_than_projected_empty(self) -> None:
        """An all-hidden brief reads like a working one, which is why this refuses.

        A projection keyed on team membership answers a name nobody holds with
        a brief in which every creature is an opponent — well formed, plausible,
        and a lie about who asked. The refusal is the difference.
        """
        with pytest.raises(NotFoundError, match="Nobody Here"):
            api.encounter_brief(fight(), "Nobody Here")

    def test_the_refusal_does_not_name_the_cast_it_exists_to_withhold(self) -> None:
        """The refusal used to list every combatant, ambushers included.

        A sentence that answers "who is in this fight?" to anyone who guesses a
        wrong name is the leak wearing an error's clothes — and the one creature
        it discloses first is the one the projection works hardest to hide.
        """
        with pytest.raises(NotFoundError, match="no combatant named") as refused:
            api.encounter_brief(fight(), "Nobody Here")

        assert AMBUSHER not in str(refused.value)
        assert VIEWER not in str(refused.value)

    def test_every_write_refuses_the_same_seat_in_the_same_sentence(self) -> None:
        encounter_id = brawl()
        advance_encounter_to(encounter_id, VIEWER)

        calls: tuple[Callable[[], dict[str, Any]], ...] = (
            lambda: api.encounter_act(encounter_id, "dodge", viewer="Nobody Here"),
            lambda: api.encounter_advance(encounter_id, viewer="Nobody Here"),
            lambda: api.encounter_resume(encounter_id, viewer="Nobody Here"),
        )
        for call in calls:
            with pytest.raises(NotFoundError, match="Nobody Here") as refused:
                call()
            assert AMBUSHER not in str(refused.value)

    def test_creating_under_a_seat_the_roster_lacks_starts_no_fight(self) -> None:
        # The one refusal that has to land *before* the operation runs. Left to
        # the projection it would refuse after the fight had been created,
        # journaled and given an id — a 404 with an orphan encounter behind it.
        before = api.encounter_list("all")["encounters"]

        with pytest.raises(NotFoundError, match="Nobody Here"):
            api.encounter_create(
                [hero_spec(), foe_spec()], seed=20260805, viewer="Nobody Here"
            )

        assert api.encounter_list("all")["encounters"] == before
