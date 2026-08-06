"""The suite's in-process door to the engine: one state, and the tool bodies.

Most of this suite is about *behaviour* — what a fight does, what a journal
records, what a replay bundle contains — and reaching that behaviour needs an
:class:`~fivee_sim.service.sessions.EngineState` and a call into ``service/``.
The functions here are that and nothing more: each is one service call with the
process-wide :data:`STATE` threaded in, so a test says
``api.encounter_create(...)`` rather than repeating the state argument two
hundred times.

Three things this deliberately is not:

* **Not an adapter.** It translates no errors. A refusal arrives as the service
  layer's own :class:`~fivee_sim.service.errors.RequestError` family, which is
  what ``tests/test_assertion_discipline.py`` enforces a ``match=`` on. The MCP
  server used to wrap all of them into one ``ToolError``, and that flattening is
  exactly what made a ``NotFoundError`` indistinguishable from a bad argument.
* **Not the contract.** ``/api/v1`` is the engine's published surface and
  ``tests/test_web_http.py`` is what pins it. Nothing here is shipped, exported,
  or reachable by a user; a behaviour that matters to a caller has to be
  demonstrated over HTTP as well, not only through this door.
* **Not a second implementation.** Every body below is one call. If one of these
  ever grows a branch, the branch belongs in ``service/``.
"""

from __future__ import annotations

from typing import Any

from fivee_sim.content import ContentRegistry
from fivee_sim.service import adventures as _adventures
from fivee_sim.service import analytics as _analytics
from fivee_sim.service import catalog as _catalog
from fivee_sim.service import content_ops as _content_ops
from fivee_sim.service import encounters as _encounters
from fivee_sim.service import map_ops as _map_ops
from fivee_sim.service import primitives as _primitives
from fivee_sim.service import rulings as _rulings
from fivee_sim.service import scenes as _scenes
from fivee_sim.service import sessions as _sessions

#: Every fight, map and registry this process holds, as one object. Saved and
#: restored around each test by ``conftest._isolate_server_state``.
STATE = _sessions.EngineState()


def _registry() -> ContentRegistry:
    return _sessions.active_registry(STATE)


# --- primitives --------------------------------------------------------------
def roll(
    expression: str,
    advantage: str = "none",
    seed: int | None = None,
    encounter_id: str | None = None,
    request_id: str | None = None,
    label: str | None = None,
    natural: int | list[int] | None = None,
) -> dict[str, Any]:
    return _primitives.roll(
        STATE, expression, advantage, seed, encounter_id, request_id, label, natural
    )


def check(
    modifier: int,
    dc: int,
    advantage: str = "none",
    seed: int | None = None,
    encounter_id: str | None = None,
    request_id: str | None = None,
    ability: str | None = None,
    skill: str | None = None,
    natural: int | list[int] | None = None,
) -> dict[str, Any]:
    return _primitives.check(
        STATE, modifier, dc, advantage, seed, encounter_id, request_id, ability, skill,
        natural,
    )


def save(
    modifier: int,
    dc: int,
    advantage: str = "none",
    auto_fail: bool = False,
    seed: int | None = None,
    encounter_id: str | None = None,
    request_id: str | None = None,
    ability: str | None = None,
    natural: int | list[int] | None = None,
) -> dict[str, Any]:
    return _primitives.save(
        STATE, modifier, dc, advantage, auto_fail, seed, encounter_id, request_id, ability,
        natural,
    )


def rules_rulings(code: str = "", kind: str = "") -> dict[str, Any]:
    return _rulings.listing(code=code, kind=kind)


def lookup_rule(topic: str = "") -> dict[str, Any]:
    return _primitives.lookup_rule(STATE, topic)


# --- catalog -----------------------------------------------------------------
def catalog_search(
    query: str,
    kind: str | None = None,
    simulation: str | None = None,
    since: int = 0,
    limit: int = 10,
) -> dict[str, Any]:
    return _catalog.search(_registry(), query, kind, simulation, since=since, limit=limit)


def catalog_get(id: str) -> dict[str, Any]:
    return _catalog.get_record(_registry(), id)


def catalog_table(id: str, since: int = 0, limit: int = 20) -> dict[str, Any]:
    return _catalog.get_table(_registry(), id, since=since, limit=limit)


# --- encounters ---------------------------------------------------------------
def encounter_create(
    combatants: list[dict[str, Any]],
    seed: int | None = None,
    movement_rule: str = "5-5-5",
    map: dict[str, Any] | None = None,
    map_id: str | None = None,
    request_id: str | None = None,
    viewer: str | None = None,
    mode: str = "combat",
) -> dict[str, Any]:
    return _encounters.create(
        STATE, combatants, seed, movement_rule, map, map_id, request_id, viewer,
        mode=mode,
    )


def encounter_state(encounter_id: str) -> dict[str, Any]:
    return _encounters.state_of(STATE, encounter_id)


def encounter_condition(
    encounter_id: str,
    target: str,
    condition: str,
    applied: bool = True,
    levels: int = 1,
    request_id: str | None = None,
) -> dict[str, Any]:
    return _encounters.condition(
        STATE, encounter_id, target, condition, applied, levels, request_id
    )


def encounter_note(
    encounter_id: str,
    text: str,
    category: str = "note",
    request_id: str | None = None,
    speaker: str | None = None,
) -> dict[str, Any]:
    return _encounters.note(STATE, encounter_id, text, category, request_id, speaker)


def encounter_log(
    encounter_id: str,
    since: int = 0,
    limit: int = 500,
    include_actions: bool = True,
) -> dict[str, Any]:
    return _encounters.event_log(STATE, encounter_id, since, limit, include_actions)


def encounter_act(
    encounter_id: str,
    kind: str,
    target: str | None = None,
    attack: str | None = None,
    item: str | None = None,
    spell: str | None = None,
    slot_level: int | None = None,
    to_position: int | list[int] | None = None,
    targets: list[str] | None = None,
    center: int | list[int] | None = None,
    direction: list[int] | None = None,
    toward: str | list[int] | None = None,
    path: list[list[int]] | None = None,
    feature: str | None = None,
    set_open: bool | None = None,
    to_level: int | None = None,
    movement_mode: str | None = None,
    as_bonus_action: bool = False,
    facing: str | None = None,
    natural: int | list[int] | None = None,
    request_id: str | None = None,
    viewer: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    return _encounters.act(
        STATE,
        encounter_id,
        kind,
        target,
        attack,
        item,
        spell,
        slot_level,
        to_position,
        targets,
        center,
        direction,
        toward,
        path,
        feature,
        set_open,
        to_level,
        movement_mode,
        as_bonus_action,
        facing,
        natural,
        request_id,
        viewer,
        actor=actor,
    )


def encounter_advance(
    encounter_id: str,
    natural: int | list[int] | None = None,
    request_id: str | None = None,
    viewer: str | None = None,
) -> dict[str, Any]:
    return _encounters.advance(STATE, encounter_id, natural, request_id, viewer)


def encounter_brief(encounter_id: str, as_name: str) -> dict[str, Any]:
    return _encounters.brief_for(STATE, encounter_id, as_name)


def encounter_resume(
    encounter_id: str, viewer: str | None = None
) -> dict[str, Any]:
    return _encounters.resume(STATE, encounter_id, viewer)


def encounter_list(status: str = "active") -> dict[str, Any]:
    return _encounters.list_encounters(STATE, status)


def encounter_finalize(encounter_id: str) -> dict[str, Any]:
    return _encounters.finalize(STATE, encounter_id)


# --- adventures ---------------------------------------------------------------
def adventure_create(name: str, request_id: str | None = None) -> dict[str, Any]:
    return _adventures.create(name, request_id)


def adventure_state(adventure_id: str) -> dict[str, Any]:
    return _adventures.state_of(adventure_id)


def adventure_list(status: str = "active") -> dict[str, Any]:
    return _adventures.list_adventures(status)


def adventure_encounter(
    adventure_id: str,
    combatants: list[dict[str, Any]] | None = None,
    carry: list[str] | None = None,
    recovery: dict[str, Any] | None = None,
    seed: int | None = None,
    movement_rule: str = "5-5-5",
    map: dict[str, Any] | None = None,
    map_id: str | None = None,
    request_id: str | None = None,
    expected_version: str | None = None,
    mode: str = "combat",
    carry_map: bool = False,
) -> dict[str, Any]:
    return _adventures.link_encounter(
        STATE,
        adventure_id,
        combatants,
        carry,
        recovery,
        seed,
        movement_rule,
        map,
        map_id,
        request_id,
        expected_version,
        mode=mode,
        carry_map=carry_map,
    )


def adventure_finalize(
    adventure_id: str, expected_version: str | None = None
) -> dict[str, Any]:
    return _adventures.finalize(adventure_id, expected_version)


def adventure_replay(adventure_id: str, path: str | None = None) -> dict[str, Any]:
    return _adventures.compose_replay(adventure_id, path)


# --- maps and replays ---------------------------------------------------------
def map_generate(
    kind: str,
    params: dict[str, Any] | None = None,
    seed: int | None = None,
    name: str | None = None,
    save_as: str | None = None,
) -> dict[str, Any]:
    return _map_ops.generate(STATE, kind, params, seed, name, save_as)


def map_save(
    map_id: str,
    document: dict[str, Any],
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    return _map_ops.save_map(STATE, map_id, document, expected_sha256)


def map_edit(
    map_id: str,
    operations: list[dict[str, Any]],
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    return _map_ops.edit(STATE, map_id, operations, expected_sha256)


def replay_validate(bundle: dict[str, Any]) -> dict[str, Any]:
    return _map_ops.replay_validate(bundle)


# --- scenes -------------------------------------------------------------------
def scene_list() -> dict[str, Any]:
    return _scenes.list_scenes()


def scene_get(scene_id: str) -> dict[str, Any]:
    return _scenes.load(scene_id)


def scene_save(
    scene_id: str,
    document: dict[str, Any],
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    return _scenes.save(scene_id, document, expected_sha256=expected_sha256)


def scene_validate(
    document: dict[str, Any], map_ids: list[str] | None = None
) -> dict[str, Any]:
    return _scenes.validate(document, map_ids=map_ids)


def replay_export(
    encounter_id: str,
    path: str | None = None,
    embed: bool = False,
    format_version: int = 2,
) -> dict[str, Any]:
    """Export a replay with no viewer link.

    The link is a property of a *running* server — it names the port serving the
    replays directory the bundle landed in — so there is none to offer from a
    call that started no server. ``test_web_http`` pins the link itself, against
    the server that would answer it.
    """
    return _map_ops.replay_export(STATE, encounter_id, path, embed, format_version)


# --- content ------------------------------------------------------------------
def content_status() -> dict[str, Any]:
    return _content_ops.status(STATE)


def content_validate(
    paths: list[str] | None = None, builtin: str | None = None
) -> dict[str, Any]:
    return _content_ops.validate(STATE, paths, builtin)


def content_configure(
    paths: list[str] | None = None,
    builtin: str | None = None,
    add: bool = False,
) -> dict[str, Any]:
    return _content_ops.configure(STATE, paths, builtin, add)


# --- analytics ----------------------------------------------------------------
def simulate_rounds(
    combatants: list[dict[str, Any]],
    iterations: int = 500,
    seed: int = 0,
    max_rounds: int = 20,
    movement_rule: str = "5-5-5",
    map: dict[str, Any] | None = None,
    map_id: str | None = None,
) -> dict[str, Any]:
    return _analytics.simulate_rounds(
        STATE, combatants, iterations, seed, max_rounds, movement_rule, map, map_id
    )
