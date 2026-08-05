"""What a player-chair client is allowed to see, checked as an allowlist.

``encounter.state`` is the GM's view and reports every enemy's hit points, AC,
slots, items and attacks. ``encounter.view`` is the other one — the battlefield
brief the ``game-master`` agent describes: positions and distances, cover and
terrain, plain-language health, conditions, whose turn it is, and *your own*
side of the sheet in full.

The security claim these tests exist to keep is narrow and byte-level: **an
opposing creature's own-sheet fields never appear in the serialised response,
and an undetected creature does not appear at all**. That claim is only as good
as the projection being an allowlist, so the first case here is the one that
matters most — it derives the field set from a real ``_creature_state`` payload
and fails when a model field lands in no bucket. A field added tomorrow is
therefore refused a decision rather than defaulted into the view.

Nothing here spells the expected field list out. A literal set written into this
file would pin the projection against itself: both sides would be edited in the
same commit and the test could never fail.
"""

from __future__ import annotations

import ast
import json
from copy import deepcopy
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest

from fivee_sim.model import encounter as encounter_module
from fivee_sim.service import player_view
from fivee_sim.service.errors import NotFoundError

from . import api
from .conftest import advance_encounter_to

FIXTURE = "synthetic test fixture, not SRD content"

#: The seat every case below looks through.
VIEWER = "Thora"

#: Values chosen to be unmistakable in a byte search: no other field of this
#: fixture — position, initiative, distance, elevation, round — can produce
#: them, so finding one in the response means it came from the creature that
#: owns it and from nowhere else.
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
    """The viewer, carrying one of everything the ``OWN`` bucket covers.

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
    """The shared roster: a viewer, two visible foes and one who has not arrived."""
    created = api.encounter_create(
        [hero_spec(), foe_spec(), twin_spec(), ambusher_spec()],
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
            {"id": VAULT_LEVER, "kind": "lever", "at": [1, 1], "state": "closed"},
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


def swing(encounter_id: str) -> dict[str, Any]:
    """The viewer's own attack on the foe beside them, as the GM is answered."""
    advance_encounter_to(encounter_id, VIEWER)
    return api.encounter_act(
        encounter_id, "attack", target=DUMMY, attack="Longsword"
    )


def counterswing(encounter_id: str) -> dict[str, Any]:
    """The foe's attack on the viewer — the other side of every ``own`` decision."""
    advance_encounter_to(encounter_id, DUMMY)
    return api.encounter_act(
        encounter_id, "attack", target=VIEWER, attack=DUMMY_ATTACK
    )


def kinds_in(result: dict[str, Any], key: str = "events") -> list[str]:
    return [str(one["kind"]) for one in result[key]]


def event_named(result: dict[str, Any], kind: str, key: str = "events") -> dict[str, Any]:
    found = [one for one in result[key] if one["kind"] == kind]
    assert found, f"no {kind} event in {kinds_in(result, key)}"
    entry: dict[str, Any] = found[0]
    return entry


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
    source = Path(str(encounter_module.__file__)).read_text(encoding="utf-8")
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


def entry_named(view: dict[str, Any], name: str) -> dict[str, Any]:
    found = [one for one in view["combatants"] if one["name"] == name]
    assert len(found) == 1, f"{name} appears {len(found)} times in the view"
    result: dict[str, Any] = found[0]
    return result


def serialised(view: dict[str, Any]) -> bytes:
    """The response as a client receives it — bytes, not a parsed dict.

    A nested occurrence of a withheld value is invisible to ``key in entry``
    and plain in the wire form, which is why every confidentiality assertion
    below is made here.
    """
    return json.dumps(view, sort_keys=True, default=str).encode("utf-8")


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


class TestTheProjectionIsAnAllowlist:
    """A field nobody classified must not reach a player, and must not pass silently.

    ``Encounter._creature_state`` is an open, growing set — ``facing`` was added
    to it recently — and several of the fields already there carry capability:
    ``spell_slots``, ``items``, ``attacks``, ``spells``, ``death_saves``,
    ``concentrating_on``. A denylist projection leaks every field added after it
    was written. These cases are what make the projection the other kind.
    """

    def test_every_creature_field_the_model_emits_is_classified_exactly_once(
        self,
    ) -> None:
        snapshot = api.encounter_state(fight())
        emitted = {key for creature in snapshot["combatants"] for key in creature}

        buckets = {
            "CREATURE_SHARED": player_view.CREATURE_SHARED,
            "CREATURE_OWN": player_view.CREATURE_OWN,
            "CREATURE_NEVER": player_view.CREATURE_NEVER,
        }
        unclassified = sorted(emitted - set().union(*buckets.values()))
        assert not unclassified, (
            "Encounter._creature_state emits these and player_view classifies "
            "none of them, so nobody has decided whether a player may see them. "
            "Put each in exactly one of SHARED (everyone), OWN (your own side) "
            "or NEVER (absent from a player view): " + ", ".join(unclassified)
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

        ``facing`` appears only when something is tracking it and ``level`` and
        ``elevation`` only on a battle map. A roster without them would let the
        case above pass while three real fields stayed undecided.
        """
        snapshot = api.encounter_state(fight())
        emitted = {key for creature in snapshot["combatants"] for key in creature}

        assert {"facing", "level", "elevation"} <= emitted

    def test_every_top_level_state_field_is_classified_exactly_once(self) -> None:
        snapshot = api.encounter_state(fight())

        buckets = {
            "STATE_SHARED": player_view.STATE_SHARED,
            "STATE_OWN": player_view.STATE_OWN,
            "STATE_NEVER": player_view.STATE_NEVER,
        }
        unclassified = sorted(set(snapshot) - set().union(*buckets.values()))
        assert not unclassified, (
            "the encounter snapshot carries these at the top level and "
            "player_view classifies none of them: " + ", ".join(unclassified)
        )
        for (left, one), (right, other) in combinations(buckets.items(), 2):
            assert not one & other, (
                f"{left} and {right} both claim {', '.join(sorted(one & other))}"
            )

    def test_every_map_and_fixture_field_is_classified_exactly_once(self) -> None:
        """The level the first draft missed, and the audit found.

        ``map`` was passed through whole, so every fixture's ability-check DC —
        the first entry on ``game-master.md``'s Withhold list — arrived in the
        player's brief. An allowlist that stops one level above the payload is
        a denylist by another name, so the map block and each fixture summary
        are classified here exactly as the creature payload is.
        """
        snapshot = api.encounter_state(fight_in_the_vault())
        block = snapshot["map"]
        fixtures = {key for one in block["features"].values() for key in one}

        for label, emitted, buckets in (
            ("map", set(block), (player_view.MAP_SHARED, player_view.MAP_NEVER)),
            (
                "fixture",
                fixtures,
                (player_view.FEATURE_SHARED, player_view.FEATURE_NEVER),
            ),
        ):
            unclassified = sorted(emitted - set().union(*buckets))
            assert not unclassified, (
                f"the {label} payload carries these and player_view classifies "
                f"none of them: {', '.join(unclassified)}"
            )
            assert not buckets[0] & buckets[1], f"{label} double-classifies"

    def test_the_view_carries_no_key_outside_the_allowlist(self) -> None:
        """Derived from the buckets, so the projection cannot grow a field quietly."""
        view = api.encounter_view(fight(), VIEWER)

        permitted = (
            player_view.STATE_SHARED
            | player_view.STATE_OWN
            | player_view.STATE_DERIVED
        )
        assert not set(view) - permitted
        per_creature = (
            player_view.CREATURE_SHARED
            | player_view.CREATURE_OWN
            | player_view.CREATURE_DERIVED
        )
        for creature in view["combatants"]:
            assert not set(creature) - per_creature, creature["name"]


class TestAnOpponentsSheetNeverCrossesTheWire:
    """The NFR, stated as bytes: confidentiality of the other side's capability.

    Asserted on the serialised response rather than the parsed dictionary
    because the failure being guarded against is a *nested* one — an own-sheet
    field surviving inside a summary, an effect record or a token — which a
    top-level key check cannot see.
    """

    def test_no_own_field_of_an_opposing_creature_appears_in_the_response(
        self,
    ) -> None:
        encounter_id = fight()
        raw = api.encounter_state(encounter_id)

        view = api.encounter_view(encounter_id, VIEWER)

        body = serialised(view)
        for secret in (
            str(FOE_HP), str(FOE_MAX_HP), str(FOE_AC), FOE_ATTACK, FOE_SPELL, FOE_ITEM
        ):
            assert secret.encode("utf-8") not in body, (
                f"{secret!r} is the opposing side's own sheet and it reached the "
                f"player's response"
            )
        # And the fixture really did put every one of them in the GM's view, so
        # this cannot pass on a roster where the foe simply had none of them.
        gm = serialised(raw)
        for secret in (
            str(FOE_HP), str(FOE_MAX_HP), str(FOE_AC), FOE_ATTACK, FOE_SPELL, FOE_ITEM
        ):
            assert secret.encode("utf-8") in gm

    def test_no_opposing_entry_carries_an_own_bucket_key_at_all(self) -> None:
        """The same claim per creature, derived from the bucket rather than listed."""
        encounter_id = fight()
        raw_foe = next(
            one for one in api.encounter_state(encounter_id)["combatants"]
            if one["name"] == "Grelk"
        )
        assert player_view.CREATURE_OWN & set(raw_foe), (
            "the foe carries no own-sheet field at all, so the assertion below "
            "would hold against a projection that redacted nothing"
        )

        view = api.encounter_view(encounter_id, VIEWER)

        side = entry_named(view, VIEWER)["team"]
        for creature in view["combatants"]:
            if creature["team"] == side:
                continue
            leaked = sorted(set(creature) & player_view.CREATURE_OWN)
            assert not leaked, f"{creature['name']} exposes {', '.join(leaked)}"
            for key in player_view.CREATURE_OWN:
                assert f'"{key}"' not in json.dumps(creature)

    def test_the_turn_budget_of_someone_elses_turn_is_not_reported(self) -> None:
        """Whose turn it is, is public. What they have left of it is not."""
        encounter_id = fight()
        advance_encounter_to(encounter_id, "Grelk")

        view = api.encounter_view(encounter_id, VIEWER)

        assert view["turn"] == "Grelk"
        assert "turn_state" not in view


class TestTheMapShowsTheRoomAndNotTheModule:
    """The leak a security audit found in the first draft of this projection.

    ``map`` was classified ``SHARED`` and handed over whole, which published
    every fixture's ability-check DC — ``game-master.md`` withholds "DCs before
    a roll" in its first sentence — along with the dependencies that say which
    lever opens which door.
    """

    def test_a_fixtures_dc_does_not_reach_the_table(self) -> None:
        encounter_id = fight_in_the_vault()
        gm = api.encounter_state(encounter_id)["map"]["features"][VAULT_DOOR]
        assert gm["check"]["dc"] == VAULT_DC, "the fixture carries no DC to withhold"

        view = api.encounter_view(encounter_id, VIEWER)

        assert str(VAULT_DC).encode("utf-8") not in serialised(view)
        assert "check" not in view["map"]["features"][VAULT_DOOR]

    def test_the_wiring_behind_a_fixture_stays_behind_it(self) -> None:
        encounter_id = fight_in_the_vault()
        gm = api.encounter_state(encounter_id)["map"]["features"][VAULT_DOOR]
        assert set(gm) & player_view.FEATURE_NEVER, "no wiring to withhold"

        view = api.encounter_view(encounter_id, VIEWER)

        for name, fixture in view["map"]["features"].items():
            assert not set(fixture) & player_view.FEATURE_NEVER, name

    def test_the_room_itself_still_arrives(self) -> None:
        """Redaction that emptied the map would pass every case above."""
        view = api.encounter_view(fight_in_the_vault(), VIEWER)

        block = view["map"]
        assert block["name"] == "vault"
        assert (block["width"], block["height"]) == (6, 4)
        assert set(block["features"]) == {VAULT_DOOR, VAULT_LEVER}
        assert block["features"][VAULT_DOOR]["open"] is False
        assert block["features"][VAULT_DOOR]["square"] == [5, 2]

    def test_a_fight_on_the_open_plane_reports_no_map_rather_than_an_empty_one(
        self,
    ) -> None:
        view = api.encounter_view(fight(on_a_map=False), VIEWER)

        assert view["map"] is None


class TestHealthIsABandAndNotANumber:
    def test_the_band_is_plain_language(self) -> None:
        view = api.encounter_view(fight(), VIEWER)

        band = entry_named(view, "Grelk")["health_band"]
        assert band in player_view.HEALTH_BANDS
        assert isinstance(band, str)

    def test_no_arithmetic_on_the_entry_recovers_the_hit_points(self) -> None:
        """Neither the ratio, the band's bounds, nor ``max_hp`` may be emitted."""
        view = api.encounter_view(fight(), VIEWER)

        foe = entry_named(view, "Grelk")
        assert foe["health_band"], "there is no band to be lossy about"
        assert FOE_HP not in foe.values() and FOE_MAX_HP not in foe.values()
        assert not set(foe) & player_view.CREATURE_OWN
        assert not floats_in(foe), (
            "a float in an opponent's entry is a ratio by another name: "
            f"{floats_in(foe)}"
        )
        body = serialised(view)
        for bound in ("0.25", "0.5", "0.75", "1.0"):
            assert bound.encode("utf-8") not in body

    def test_two_foes_a_hundredfold_apart_at_one_ratio_report_one_band(self) -> None:
        """The band brackets the ratio, so scale is exactly what it does not carry."""
        view = api.encounter_view(fight(), VIEWER)

        assert FOE_MAX_HP == TWIN_MAX_HP * 100
        assert FOE_HP == TWIN_HP * 100
        assert (
            entry_named(view, "Grelk")["health_band"]
            == entry_named(view, "Grelka")["health_band"]
        )


class TestAnUndetectedCreatureIsAbsentFromEveryField:
    """Omitted, not merely unlabelled. A blank entry in the order is a reveal."""

    def test_a_creature_who_has_not_arrived_appears_nowhere_in_the_response(
        self,
    ) -> None:
        encounter_id = fight()
        raw = api.encounter_state(encounter_id)
        assert AMBUSHER in raw["order"], "the fixture never rolled the ambusher in"

        view = api.encounter_view(encounter_id, VIEWER)

        assert AMBUSHER.encode("utf-8") not in serialised(view)
        assert AMBUSHER not in {one["name"] for one in view["combatants"]}
        assert AMBUSHER not in view["order"]

    def test_an_unarrived_creature_holding_the_turn_leaves_the_turn_unnamed(
        self,
    ) -> None:
        """The sharpest leak: ``turn`` names a creature the player cannot see."""
        encounter_id = fight()
        advance_encounter_to(encounter_id, AMBUSHER)

        view = api.encounter_view(encounter_id, VIEWER)

        assert api.encounter_state(encounter_id)["turn"] == AMBUSHER
        assert view["turn"] is None
        assert AMBUSHER.encode("utf-8") not in serialised(view)


class TestYourOwnSeatIsReportedInFull:
    """Withholding a player's own sheet does not create tension; it makes them guess."""

    def test_on_your_turn_your_own_sheet_is_whole(self) -> None:
        encounter_id = fight()
        advance_encounter_to(encounter_id, VIEWER)
        raw = api.encounter_state(encounter_id)

        view = api.encounter_view(encounter_id, VIEWER)

        assert view["turn"] == VIEWER
        # Movement left, and whether the action and bonus action are in hand.
        assert view["turn_state"] == raw["turn_state"]
        assert view["turn_state"]["movement_left"] == 30
        assert view["turn_state"]["action_used"] is False
        assert view["turn_state"]["bonus_action_used"] is False
        seat = entry_named(view, VIEWER)
        assert seat["spell_slots"] == {1: 3}          # slots by level
        assert seat["items"] == {"Potion of Healing": 2}  # item charges
        assert seat["conditions"] == ["prone"]        # conditions on them
        assert seat["speeds"]["walk"] == 30
        assert seat["hp"] == 22 and seat["max_hp"] == 30 and seat["ac"] == 16
        assert seat["attacks"] == ["Longsword"]
        assert seat["spells"] == ["Sacred Flame"]

    def test_your_own_entry_keeps_every_field_the_model_gave_it(self) -> None:
        """Derived from the buckets: nothing on your own side is quietly dropped."""
        encounter_id = fight()
        raw_seat = next(
            one for one in api.encounter_state(encounter_id)["combatants"]
            if one["name"] == VIEWER
        )

        view = api.encounter_view(encounter_id, VIEWER)

        expected = (
            set(raw_seat) & (player_view.CREATURE_SHARED | player_view.CREATURE_OWN)
        ) | player_view.CREATURE_DERIVED
        assert set(entry_named(view, VIEWER)) == expected

    def test_the_brief_carries_the_distance_to_everyone_still_standing(self) -> None:
        view = api.encounter_view(fight(), VIEWER)

        assert entry_named(view, VIEWER)["distance"] == 0
        assert entry_named(view, "Grelk")["distance"] == 35


class TestTheEventsAreAnAllowlistToo:
    """The same standard as the creature projection, over the other payload.

    ``encounter.view`` serves no events, so for a release the four operations
    that *do* answered a seat with the fight's own account of what had just
    happened, unredacted. The brief said the ogre was "hurt" and the ``damage``
    event beside it said ``{"hp": 3291, "max_hp": 4400}``. These cases are what
    make an event key the same kind of decision a creature field is.
    """

    def test_every_event_data_key_the_model_emits_is_classified_exactly_once(
        self,
    ) -> None:
        emitted = emitted_data_keys()

        buckets = {
            "EVENT_SHARED": player_view.EVENT_SHARED,
            "EVENT_OWN": player_view.EVENT_OWN,
            "EVENT_NEVER": player_view.EVENT_NEVER,
        }
        unclassified = sorted(emitted - set().union(*buckets.values()))
        assert not unclassified, (
            "Encounter._emit is called with these in its data payload and "
            "player_view classifies none of them, so nobody has decided whether "
            "a player may see them. Put each in exactly one of EVENT_SHARED "
            "(what the table watched), EVENT_OWN (your own side's rolls and "
            "resources) or EVENT_NEVER: " + ", ".join(unclassified)
        )
        for (left, one), (right, other) in combinations(buckets.items(), 2):
            assert not one & other, (
                f"{left} and {right} both claim "
                f"{', '.join(sorted(one & other))}; a key has one answer"
            )

    def test_no_bucket_names_a_key_the_model_never_emits(self) -> None:
        """The other direction, which keeps the buckets from becoming a wish list.

        A key classified here and emitted nowhere is a decision about nothing —
        and, worse, cover: it makes the set above look complete while the key
        that replaced it goes unclassified.
        """
        emitted = emitted_data_keys()
        classified = (
            player_view.EVENT_SHARED | player_view.EVENT_OWN | player_view.EVENT_NEVER
        )

        stale = sorted(classified - emitted)
        assert not stale, (
            "player_view classifies these and no _emit call site sends them: "
            + ", ".join(stale)
        )

    def test_the_derivation_reaches_keys_no_fixture_in_this_file_produces(
        self,
    ) -> None:
        """The reason the set above is read off the source and not off a fight.

        Nothing here fights a zombie, works a fixture with a lock on it, or
        drains anybody, so a derivation that sampled real events would never see
        ``dc``, ``check`` or ``total_drained`` — and each would sit unclassified
        with the suite green.
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
            "EVENT_ENVELOPE_SHARED": player_view.EVENT_ENVELOPE_SHARED,
            "EVENT_ENVELOPE_NEVER": player_view.EVENT_ENVELOPE_NEVER,
        }
        unclassified = sorted(emitted - set().union(*buckets.values()))
        assert not unclassified, (
            "Event.as_dict emits these and player_view classifies none of them: "
            + ", ".join(unclassified)
        )
        assert not (
            player_view.EVENT_ENVELOPE_SHARED & player_view.EVENT_ENVELOPE_NEVER
        )

    def test_a_projected_event_carries_no_key_outside_the_allowlist(self) -> None:
        """Derived from the buckets, so an event cannot grow a field quietly."""
        encounter_id = brawl()
        whole = swing(encounter_id)

        brief = player_view.briefed(whole, VIEWER)

        assert kinds_in(brief) == ["attack", "damage"], kinds_in(brief)
        permitted = (
            player_view.EVENT_SHARED | player_view.EVENT_OWN | player_view.EVENT_NEVER
        )
        for event in brief["events"]:
            assert not set(event) - player_view.EVENT_ENVELOPE_SHARED, event["kind"]
            assert not set(event["data"]) - permitted, event["kind"]


class TestTheGmsSentenceIsNotServedToAChair:
    """``detail`` is omitted, and the distinction from an empty one is the point."""

    def test_the_detail_of_every_event_is_absent_rather_than_blank(self) -> None:
        encounter_id = brawl()
        whole = swing(encounter_id)
        assert all(one["detail"] for one in whole["events"]), (
            "the fixture emitted an event with no detail, so the assertion "
            "below would hold against a projection that passed detail through"
        )

        brief = player_view.briefed(whole, VIEWER)

        for event in brief["events"]:
            assert "detail" not in event, event

    def test_the_sentence_spells_out_what_every_bucket_withheld(self) -> None:
        """Why prose is omitted rather than classified: it restates the payload.

        One ``detail`` here reads ``6 damage, 6594/7700 hit points left`` and the
        one above it names the AC the swing was rolled against. Neither is a key
        anything could be decided about — which is the whole argument for
        omitting the field rather than trying to serve part of it.
        """
        encounter_id = brawl()
        whole = swing(encounter_id)
        sentences = " | ".join(one["detail"] for one in whole["events"])
        assert "vs AC" in sentences, sentences
        assert f"/{DUMMY_MAX_HP}" in sentences, sentences

        brief = player_view.briefed(whole, VIEWER)

        body = serialised(brief)
        assert b"vs AC" not in body
        assert str(DUMMY_MAX_HP).encode("utf-8") not in body


class TestAnEventNeverCarriesTheOtherSidesSheet:
    """The finding, as bytes: ``state`` said "hurt" and the event said 6594/7700."""

    def test_a_foes_exact_hit_points_do_not_ride_in_on_a_damage_event(self) -> None:
        encounter_id = brawl()
        whole = swing(encounter_id)
        wound = event_named(whole, "damage")
        assert wound["target"] == DUMMY
        assert wound["data"]["max_hp"] == DUMMY_MAX_HP, wound
        left = int(wound["data"]["hp"])

        brief = player_view.briefed(whole, VIEWER)

        body = serialised(brief)
        for secret in (str(DUMMY_MAX_HP), str(left)):
            assert secret.encode("utf-8") not in body, (
                f"{secret} is the opposing side's own sheet and it reached the "
                f"player's response through the events"
            )
        assert entry_named(brief["state"], DUMMY)["health_band"] in (
            player_view.HEALTH_BANDS
        )

    def test_what_the_table_watched_land_still_arrives(self) -> None:
        """Redaction that emptied the payload would pass every case above."""
        encounter_id = brawl()
        whole = swing(encounter_id)

        brief = player_view.briefed(whole, VIEWER)

        wound = event_named(brief, "damage")
        assert wound["data"]["amount"] == event_named(whole, "damage")["data"]["amount"]
        assert wound["data"]["amount"] > 0
        assert event_named(brief, "attack")["data"]["hit"] is True

    def test_your_own_sides_hit_points_are_reported_on_a_damage_event(self) -> None:
        """The other half of ``own``: your ally's wound is your side's to see."""
        encounter_id = brawl()
        whole = counterswing(encounter_id)
        wound = event_named(whole, "damage")
        assert wound["target"] == VIEWER

        brief = player_view.briefed(whole, VIEWER)

        seen = event_named(brief, "damage")
        assert seen["data"]["hp"] == wound["data"]["hp"]
        assert seen["data"]["max_hp"] == wound["data"]["max_hp"]

    def test_your_own_swing_reports_the_roll_and_a_foes_swing_does_not(self) -> None:
        """``total`` bounds an AC and ``natural`` with it is the roller's bonus.

        A table says "nineteen hits" and the player who rolled it may do that
        arithmetic; the GM does not read their own dice out. So the same three
        keys are answered differently by whose swing it was.
        """
        mine = player_view.briefed(swing(brawl()), VIEWER)
        theirs = player_view.briefed(counterswing(brawl()), VIEWER)

        own = event_named(mine, "attack")["data"]
        other = event_named(theirs, "attack")["data"]
        assert {"total", "natural", "attack", "advantage"} <= set(own), own
        assert not {"total", "natural", "attack", "advantage"} & set(other), other
        # And what a table watches is answered the same way on both.
        assert own["hit"] is True and other["hit"] is True


class TestAnEventNamingSomeoneUnseenIsNotReported:
    """``combatants`` and ``order`` omit an unarrived creature; so does this."""

    def test_an_unarrived_creatures_turn_is_absent_from_the_events(self) -> None:
        encounter_id = brawl()
        advance_encounter_to(encounter_id, VIEWER)
        whole = api.encounter_advance(encounter_id)
        assert AMBUSHER in {one["actor"] for one in whole["events"]}, (
            "the fixture never handed the ambusher a turn, so the assertion "
            "below would hold against a projection that reported it"
        )

        brief = player_view.briefed(whole, VIEWER)

        assert AMBUSHER.encode("utf-8") not in serialised(brief)
        assert "turn_start" not in kinds_in(brief)
        # Dropped rather than blanked, which is the same answer `combatants`
        # gives; the gap it leaves in `seq` is the residual the module names.
        assert [one["seq"] for one in brief["events"]] != [
            one["seq"] for one in whole["events"]
        ]

    def test_the_round_still_turns_over_when_an_unseen_creature_is_next(self) -> None:
        """The reason ``turn`` is nulled rather than dropped for.

        ``round 2 begins`` is stamped with whoever is about to act, so an event
        dropped for naming them would take the most public thing in the fight
        with it.
        """
        encounter_id = fight()
        advance_encounter_to(encounter_id, "Grelk")
        whole = api.encounter_advance(encounter_id)
        turned = event_named(whole, "round")
        assert turned["turn"] == AMBUSHER, turned

        brief = player_view.briefed(whole, VIEWER)

        seen = event_named(brief, "round")
        assert seen["data"]["round"] == turned["data"]["round"]
        assert seen["turn"] is None
        assert AMBUSHER.encode("utf-8") not in serialised(brief)

    def test_an_arrival_needs_no_exemption_because_they_have_arrived(self) -> None:
        """Named because it is the one event that looks like it should be dropped."""
        encounter_id = fight()
        for _ in range(40):
            whole = api.encounter_advance(encounter_id)
            if "arrival" in kinds_in(whole):
                break
        else:  # pragma: no cover - the fixture arrives in round 9
            raise AssertionError("the ambusher never arrived")

        brief = player_view.briefed(whole, VIEWER)

        landed = event_named(brief, "arrival")
        assert landed["actor"] == AMBUSHER
        assert AMBUSHER in {one["name"] for one in brief["state"]["combatants"]}
        # The round they were scheduled for is the module's schedule, and stays
        # NEVER exactly as it is on the creature.
        assert "arrival_round" not in landed["data"]
        assert landed["data"]["position"] == whole["events"][
            kinds_in(whole).index("arrival")
        ]["data"]["position"]


class TestTheFightsOwnRecordIsNotRewritten:
    """``briefed`` rebuilds because the dictionary it is handed belongs elsewhere."""

    def test_briefing_a_result_leaves_the_result_it_was_handed_alone(self) -> None:
        encounter_id = brawl()
        whole = swing(encounter_id)
        before = deepcopy(whole)

        player_view.briefed(whole, VIEWER)

        assert whole == before, (
            "briefed edited the dictionary it was given; that dictionary is the "
            "one the journal recorded and the one an idempotent retry replays"
        )

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
        advance_encounter_to(encounter_id, VIEWER)

        briefed_result = player_view.briefed(
            api.encounter_act(
                encounter_id, "attack", target=DUMMY, attack="Longsword"
            ),
            VIEWER,
        )

        assert str(DUMMY_MAX_HP).encode("utf-8") not in serialised(briefed_result)
        records, _ = encounter_journal.read(encounter_id)
        written = json.dumps(records)
        assert str(DUMMY_MAX_HP) in written, (
            "the journal lost the fight's own numbers, so briefing rewrote the "
            "audit record rather than rebuilding an answer beside it"
        )
        assert '"detail"' in written and "vs AC" in written


class TestAViewerTheFightDoesNotHold:
    def test_an_unknown_seat_is_refused_rather_than_projected_empty(self) -> None:
        """An all-hidden view reads like a working one, which is why this refuses.

        A projection keyed on team membership answers a name nobody holds with
        a brief in which every creature is an opponent — well formed, plausible,
        and a lie about who asked. The refusal is the difference.
        """
        encounter_id = fight()

        with pytest.raises(NotFoundError, match="Nobody Here"):
            api.encounter_view(encounter_id, "Nobody Here")

    def test_an_unknown_encounter_is_refused_by_the_session_lookup(self) -> None:
        with pytest.raises(NotFoundError, match="enc-does-not-exist"):
            api.encounter_view("enc-does-not-exist", VIEWER)
