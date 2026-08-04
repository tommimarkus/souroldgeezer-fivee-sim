"""MCP stdio server: a thin adapter over the engine.

Every tool validates its input, calls the kernel or the encounter model, and
serialises the result. No rules logic belongs in this file — if a behaviour needs
deciding, it is decided in ``kernel`` or ``model`` where the tests can reach it.

Two conventions worth knowing:

* Every tool that consumes randomness accepts an optional ``seed`` and **always
  reports the seed it used**. Omitting one does not make a result irreproducible;
  it makes the engine choose a seed and tell you, so any roll can be replayed.
* Encounter state lives in this process, keyed by an id. ``encounter_state`` is
  the authoritative view — narration should follow it rather than memory.

Anything written to stdout other than protocol traffic corrupts the stream, so
diagnostics go to stderr.

What is left here is exactly two things: the tool docstrings, which are the
schemas a client reads, and the translation between this transport's vocabulary
and the service layer's. Every body below is one call into ``service/``, where
the same work is reachable without a live MCP process — the browser editor
already goes in by that door, and :data:`_STATE` is the single object this
process owns and threads down.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer

from .. import __version__
from ..content import ContentRegistry
from ..editor.cli import read_state, state_file_for
from ..editor.http_server import TOKEN_HEADER
from ..service import analytics as _analytics
from ..service import catalog as _catalog_service
from ..service import content_ops as _content_ops
from ..service import encounters as _encounters
from ..service import map_ops as _map_ops
from ..service import maps as _map_service
from ..service import primitives as _primitives
from ..service import replay as _replay_service
from ..service import sessions as _sessions
from ..service.common import slugify
from ..service.errors import RequestError

INSTRUCTIONS = """\
A 5E-compatible combat engine. The engine owns the fight: hit points, initiative
order, conditions, and dice are computed here, so read encounter_state as
authoritative and narrate from it rather than tracking state yourself.

Content is configurable. The bundled SRD 5.2.1 slice loads by default, and a campaign
may add its own creatures, spells, conditions, terrain, and items as content packs —
or run on its own material alone. Call content_status to see what is actually loaded before
telling anyone what is available.

Bundled rules content comes from SRD 5.2.1 under CC-BY-4.0; see the plugin's NOTICE.
"""

server: MCPServer = MCPServer(
    name="souroldgeezer-fivee-sim",
    version=__version__,
    instructions=INSTRUCTIONS,
)

#: Every fight, map and registry this process holds. One object, constructed
#: here and passed into each service call — the composition edge for this
#: adapter, and the reason the same tool bodies run under a test that builds
#: its own.
_STATE = _sessions.EngineState()


class ToolError(ValueError):
    """Bad tool input, reported to the caller rather than crashing the server."""


def _call(tool: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Run a service call, translating its refusal into this transport's error.

    The service layer raises :class:`~fivee_sim.service.errors.RequestError` and
    its siblings because it may not know what a ``ToolError`` is; this is the
    one place that knows both. Anything that is not a refusal — a defect —
    propagates untouched, since dressing a bug as bad input is how a bug hides.
    """
    try:
        return tool(*args, **kwargs)
    except RequestError as error:
        raise ToolError(str(error)) from error


def _registry() -> ContentRegistry:
    """The content this process is running on, loaded on first use."""
    return _sessions.active_registry(_STATE)


@server.tool()
def scenario_timing(
    distance_feet: int,
    speed_feet: int,
    dash: bool = False,
    start_delay_rounds: int = 0,
    response_after_rounds: int | None = None,
) -> dict[str, Any]:
    """Measure route arrival and, optionally, its lead over a timed response.

    This is scenario evidence rather than combat state: supply the authored route
    distance, movement speed, and response delay.  ``dash`` means the traveller
    spends its action to move twice its speed every round.
    """
    return _call(
        _primitives.scenario_timing,
        distance_feet,
        speed_feet,
        dash,
        start_delay_rounds,
        response_after_rounds,
    )


@server.tool()
def roll(
    expression: str,
    advantage: str = "none",
    seed: int | None = None,
    encounter_id: str | None = None,
    request_id: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Roll a dice expression such as "2d6+3" or "d20", optionally with advantage.

    Advantage and disadvantage apply only to a single d20; they are ignored for
    other expressions because the rules attach them to d20 tests.
    """
    return _call(
        _primitives.roll,
        _STATE,
        expression,
        advantage,
        seed,
        encounter_id,
        request_id,
        label,
    )


@server.tool()
def check(
    modifier: int,
    dc: int,
    advantage: str = "none",
    seed: int | None = None,
    encounter_id: str | None = None,
    request_id: str | None = None,
    ability: str | None = None,
    skill: str | None = None,
) -> dict[str, Any]:
    """Make an ability or skill check, optionally attached to an encounter."""
    return _call(
        _primitives.check,
        _STATE,
        modifier,
        dc,
        advantage,
        seed,
        encounter_id,
        request_id,
        ability,
        skill,
    )


@server.tool()
def save(
    modifier: int,
    dc: int,
    advantage: str = "none",
    auto_fail: bool = False,
    seed: int | None = None,
    encounter_id: str | None = None,
    request_id: str | None = None,
    ability: str | None = None,
) -> dict[str, Any]:
    """Make a saving throw. ``auto_fail`` covers conditions that forfeit the save."""
    return _call(
        _primitives.save,
        _STATE,
        modifier,
        dc,
        advantage,
        auto_fail,
        seed,
        encounter_id,
        request_id,
        ability,
    )


@server.tool()
def lookup_rule(topic: str = "") -> dict[str, Any]:
    """Look up a loaded condition, spell, creature, item, or terrain kind.
    Omit ``topic`` for compact loaded-content counts and catalog search guidance.

    Searches whatever content is loaded, bundled or not, and every entry names the
    pack it came from in ``source``. A miss means the subject is not loaded — check
    content_status before concluding it does not exist.
    """
    return _call(_primitives.lookup_rule, _STATE, topic)


@server.tool()
def catalog_search(
    query: str,
    kind: str | None = None,
    simulation: str | None = None,
    since: int = 0,
    limit: int = 10,
) -> dict[str, Any]:
    """Search structured catalog identities and loaded custom content.

    Results use stable exact/prefix/substring ranking. ``kind`` and ``simulation``
    (``reference_only``, ``partial``, or ``executable``) are optional filters;
    ``since`` and ``limit`` page through at most 25 compact results at a time.
    """
    try:
        return _catalog_service.search(
            _registry(), query, kind, simulation, since=since, limit=limit
        )
    except ValueError as error:
        raise ToolError(str(error)) from error


@server.tool()
def catalog_get(id: str) -> dict[str, Any]:
    """Return one structured catalog record by stable ID.

    Catalog and executable provenance are reported separately, so a campaign
    override of SRD execution data never disguises the catalog source.
    """
    try:
        return _catalog_service.get_record(_registry(), id)
    except ValueError as error:
        raise ToolError(str(error)) from error


@server.tool()
def catalog_table(id: str, since: int = 0, limit: int = 20) -> dict[str, Any]:
    """Return a structured printed table in a window of at most 25 rows."""
    try:
        return _catalog_service.get_table(_registry(), id, since=since, limit=limit)
    except ValueError as error:
        raise ToolError(str(error)) from error


@server.tool()
def encounter_create(
    combatants: list[dict[str, Any]],
    seed: int | None = None,
    movement_rule: str = "5-5-5",
    map: dict[str, Any] | None = None,
    map_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Start an encounter and roll initiative, optionally on a battle map.

    Each combatant is either ``{"monster": "Goblin Warrior", "label": "Goblin A",
    "team": "monsters", "position": [15, 0]}`` for a bundled stat block, or an
    explicit description with at least name, team, ac, and max_hp. A key the spec
    does not define is refused rather than ignored, so a misspelling or an
    unsupported field is reported instead of silently dropped; the two forms
    take different keys, and the refusal lists the ones that would have worked.
    ``arrival_round`` is per-instance reinforcement timing: a combatant scheduled
    after round 1 is absent, untargetable, and unable to act until that round
    begins. Names must be
    unique — they identify combatants in every later call. A position is ``[x, y]``
    in feet on a flat plane; a bare number is accepted and means feet along the
    x-axis. ``movement_rule`` is how diagonals are measured: "5-5-5" (the default)
    or "5-10-5" (every second diagonal costs double).

    ``map`` puts the fight on a grid of 5-foot squares: ``{"width", "height"}``
    plus either ``"rows"`` (a list of strings, one per row, top row first) with a
    ``"legend"`` mapping each character to a terrain kind, or a ``"terrain"``
    list of ``{"kind", "squares": [[x, y], ...]}`` overrides on
    ``"default_terrain"``. ``"features"`` lists doors and the like:
    ``{"name", "square", "kind"?, "initially_open"?}``. Ground height is
    optional: ``"default_elevation"`` in feet plus an ``"elevation"`` list of
    ``[x, y, feet]`` for the squares that differ. With a map, terrain costs
    movement, walls block sight and routes, cover adjusts AC, and starting
    positions must be on-map, passable, and unoccupied; positions snap to their
    square. Without one, the plane is open and featureless.

    Height reaches movement and nothing else: a slope costs difficult terrain, a
    cliff costs a climb at an extra foot per foot, and climbing down costs what
    climbing up costs. Sight, cover, and area templates are measured flat, so
    high ground confers no advantage beyond the movement it costs to reach.

    ``map_id`` fights on a loaded map session (see map_generate and map_load)
    instead of an inline spec — one or the other, never both. The fight captures
    the document by value: a later map_edit does not reach into it, and the
    ``map_source`` field here and in encounter_state reports the captured
    generation and whether the live map has since moved on.
    """
    return _call(
        _encounters.create,
        _STATE,
        combatants,
        seed,
        movement_rule,
        map,
        map_id,
        request_id,
    )


@server.tool()
def encounter_state(encounter_id: str) -> dict[str, Any]:
    """The authoritative state of an encounter. Narrate from this, not from memory.

    Each combatant's ``position`` is ``[x, y]`` in feet on the plane. For a
    fight created from a ``map_id``, ``map_source`` reports the map generation
    it captured and whether the live map has been edited since (``stale``).
    A map fixture reports its live state and optional trigger definition; an
    automatic transition is an ordinary interact event with an empty actor,
    ``automatic: true``, and ``triggered_by``.
    """
    return _call(_encounters.state_of, _STATE, encounter_id)


@server.tool()
def encounter_note(
    encounter_id: str,
    text: str,
    category: str = "note",
    request_id: str | None = None,
) -> dict[str, Any]:
    """Attach a durable narrative or adjudication note to an encounter."""
    return _call(_encounters.note, _STATE, encounter_id, text, category, request_id)


@server.tool()
def encounter_log(
    encounter_id: str,
    since: int = 0,
    limit: int = 500,
    include_actions: bool = True,
) -> dict[str, Any]:
    """The full event history of an encounter, paged, with the actions that made it.

    Events come back from ``since`` (a ``seq`` value) in pages of at most ``limit``;
    ``next`` is the ``since`` for the following page, or null on the last one.
    ``actions`` lists every successful act and advance in order — applied against
    the reported seed and the same combatants, they reproduce the log exactly.
    ``encounter_state`` stays the view of now; this is the record of how the fight
    got there.
    """
    return _call(
        _encounters.event_log, _STATE, encounter_id, since, limit, include_actions
    )


@server.tool()
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
    request_id: str | None = None,
) -> dict[str, Any]:
    """Take the current creature's action and durably audit success or refusal.

    ``request_id`` makes retries idempotent. The action fields have the same
    meanings documented by encounter_state and encounter_log: attacks name a
    target, movement names a destination/path/storey, and spells name their aim.
    """
    return _call(
        _encounters.act,
        _STATE,
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
        request_id,
    )


@server.tool()
def encounter_advance(
    encounter_id: str, request_id: str | None = None
) -> dict[str, Any]:
    """End this turn, begin the next, and durably record the transition."""
    return _call(_encounters.advance, _STATE, encounter_id, request_id)


@server.tool()
def encounter_resume(encounter_id: str) -> dict[str, Any]:
    """Load an encounter from its verified journal, repairing a partial crash tail."""
    return _call(_encounters.resume, _STATE, encounter_id)


@server.tool()
def encounter_list(status: str = "active") -> dict[str, Any]:
    """Discover durable encounters without loading them into process memory."""
    return _call(_encounters.list_encounters, _STATE, status)


@server.tool()
def encounter_finalize(encounter_id: str) -> dict[str, Any]:
    """Atomically export replay v2 and mark the durable encounter finalized."""
    return _call(_encounters.finalize, _STATE, encounter_id, _viewer_link)


@server.tool()
def replay_validate(bundle: dict[str, Any]) -> dict[str, Any]:
    """Validate a v1 or v2 replay and verify every v2 integrity hash."""
    return _call(_map_ops.replay_validate, bundle)


@server.tool()
def replay_export(
    encounter_id: str,
    path: str | None = None,
    embed: bool = False,
    format_version: int = _replay_service.LATEST_FORMAT_VERSION,
) -> dict[str, Any]:
    """Export a fight's replay: a bundle file, or a standalone viewer page.

    Version 2 is the default: a self-contained, validated audit record with
    normalized combatants, captured content and map, actions, attempts,
    timestamped events, authoritative state checkpoints, and integrity hashes.
    Pass ``format_version=1`` for the legacy viewer contract.

    Plain export: a small bundle is returned inline as ``bundle``; a large
    one — or any call with ``path`` — is written to disk (default
    ``<replays root>/<name>-<seed>.json``, a sibling of the maps directory)
    and answered with ``path``, ``bytes``, and ``sha256``. With ``embed`` true
    the bundle is baked into the replay viewer page instead, producing one
    self-contained ``.html`` the user opens directly in a browser — no server,
    hand the file over. An existing file at the target is replaced: the export
    is derived from the session, not an original.

    Two ways to show it, and they answer different asks. If
    ``map_editor_serve`` is already running, a written bundle comes back with
    a ``viewer_url`` that plays it in that browser tab — best when the user is
    at the machine. ``embed`` is for handing the fight to someone who is not.
    """
    return _call(
        _map_ops.replay_export,
        _STATE,
        encounter_id,
        path,
        embed,
        format_version,
        _viewer_link,
    )


@server.tool()
def map_generate(
    kind: str,
    params: dict[str, Any] | None = None,
    seed: int | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Generate a battle map — "dungeon", "caves", or "overland" — under a seed.

    The seed is always reported; the same seed, kind, and params reproduce the
    map exactly. ``params`` overrides the kind's defaults (call with an unknown
    key to be told the valid ones), and the result's ``params`` comes back
    fully resolved. The map is held in this session under ``map_id`` for
    map_render, map_edit, map_query, map_save, and encounter_create; small maps
    include an inline render, larger ones return ``render: null`` and a note —
    use map_render with a viewport or downsample.
    """
    return _call(_map_ops.generate, _STATE, kind, params, seed, name)


@server.tool()
def map_load(
    path: str | None = None,
    document: dict[str, Any] | None = None,
    replace: str | None = None,
) -> dict[str, Any]:
    """Load a map document into the session — from a file, or given inline.

    Exactly one of ``path`` and ``document``. Validation is strict and a
    failure reports every diagnostic; warnings ride along with success.
    ``replace`` rebinds an existing map_id to the loaded document (bumping its
    generation) instead of minting a new id — the way to re-read a file after
    an external editor saved it. ``sha256`` is the canonical document hash.
    """
    return _call(_map_ops.load, _STATE, path, document, replace)


@server.tool()
def map_save(
    map_id: str,
    path: str | None = None,
    overwrite: bool = False,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Write a loaded map to disk as canonical JSON, refusing silent overwrites.

    ``path`` defaults to ``<maps root>/<slug-of-name>.json`` under the
    project's ``.fivee-sim/maps`` (or ``FIVEE_SIM_MAPS``). An existing file is
    only replaced when ``overwrite`` is true. Returns the written path, byte
    count, and sha256 — the hash to quote when handing the file elsewhere.

    ``expected_sha256`` is the hash you last read. The write is refused if
    anything else — another session, or the open editor — has changed the file
    since; ``overwrite`` alone guards against clobbering a file you did not know
    about, not a version you did not read.

    You rarely need to pass it. When this session already loaded or wrote the
    target, it supplies its own last-seen hash, so a concurrent change is
    refused by default rather than only when the caller remembered to ask. Pass
    ``"*"`` to write regardless, which is the deliberate way to take a file over.
    """
    return _call(_map_ops.save, _STATE, map_id, path, overwrite, expected_sha256)


@server.tool()
def map_render(
    map_id: str,
    x: int = 0,
    y: int = 0,
    width: int | None = None,
    height: int | None = None,
    downsample: int = 1,
    show_features: bool = True,
    show_elevation: bool = False,
    level: int = 0,
    encounter_id: str | None = None,
) -> dict[str, Any]:
    """Render a viewport of a loaded map as rows of glyphs.

    The viewport (``x``, ``y``, ``width``, ``height``, in squares) is clamped
    to the map; ``downsample=k`` renders each k-by-k block as its majority
    terrain. A render over 10000 cells is refused — narrow the viewport or
    raise the downsample. Overlays: ``+`` closed door, ``/`` open door, ``<``
    and ``>`` stairs, ``@`` spawn. With ``encounter_id``, conscious combatants
    overlay as letters in initiative order (``tokens`` maps letter to name)
    and downed ones as ``x`` — positions come from that encounter's state, so
    render after acting, not before.

    ``encounter_id`` also shows the map *that fight is on* rather than the map
    as authored: a fixture the fight has opened draws open, floods whatever its
    overlay governs, and drops that ground with it. A terrain kind a fixture
    introduces that the document's legend has no glyph for borrows one, and
    ``legend`` names what it borrowed like any other glyph. Without an
    ``encounter_id`` the render is the file on disk, fixtures included.

    ``show_elevation`` adds ``elevation_rows`` and ``elevation_legend`` beside
    the terrain rows: one glyph per square, lettered from the lowest ground in
    view upward, with the legend giving each its height in feet.
    """
    return _call(
        _map_ops.render,
        _STATE,
        map_id,
        x,
        y,
        width,
        height,
        downsample,
        show_features,
        show_elevation,
        level,
        encounter_id,
    )


@server.tool()
def map_edit(map_id: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Edit a loaded map atomically: all operations apply, or none do.

    Each operation is an object with an ``op`` key: ``set_terrain`` {rect:
    [x, y, w, h], terrain}, ``paint`` {cells: [[x, y], ...], terrain},
    ``line`` {from, to, terrain}, ``carve_corridor`` {from, to, terrain?,
    horizontal_first?}, ``add_feature`` {feature}, ``set_feature`` {feature} to
    edit one in place by the id in its record — it keeps the feature's position
    in the array and the storey it stands on, and **writes the record whole**, so
    a key left out is a key removed rather than kept — ``remove_feature`` {id},
    ``toggle_door`` {at}, ``resize`` {width, height, anchor?, fill?},
    ``set_legend`` {glyph, terrain}, ``set_name`` {name},
    ``set_palette`` {terrain, color} to color a terrain kind in this document —
    one hex color, a {light, dark} pair of them, or null to drop back to the
    color the renderers compute —
    ``set_elevation`` {rect | cells, feet} or {default} to move the height every
    unnamed square sits at, ``adjust_elevation`` {rect | cells, by} to raise or
    lower what is already there. Heights are feet and may be negative.

    The ``feature`` both feature ops take is {id, kind, at, orientation?,
    hinge?, swing?, state?, linked_to?, team?, to_level?} plus, for a fixture,
    terrain, elevation, affects, requires, trigger, costs_action and check. A
    trigger is {when: {fixture_id: open|closed, ...}, set: open|closed,
    mode: edge|maintained}; its predicate is AND and dependencies must be
    acyclic. A door's
    hinge and swing use the cardinal directions valid for its orientation.
    ``linked_to`` must name one reciprocal adjacent door with the same state and
    interaction contract; toggling either leaf toggles both. ``to_level`` makes the feature a
    connector — the square a creature may step between storeys on, which is what
    turns a drawn stairway into a walkable one.

    A bad operation is refused with its index and changes nothing. A successful edit bumps the
    map's generation, marks it edited, and returns a render covering what
    changed. Fights already created from this map keep the version they
    captured — their encounter_state reports ``stale`` instead.
    """
    return _call(_map_ops.edit, _STATE, map_id, operations)


@server.tool()
def map_query(
    map_id: str,
    query: str,
    frm: list[int] | None = None,
    to: list[int] | None = None,
    level: int = 0,
) -> dict[str, Any]:
    """Geometry over a loaded map: "distance", "line_of_sight", or "path".

    ``frm`` and ``to`` are ``[x, y]`` square indices (``frm`` because ``from``
    is a reserved word in the implementation language). Doors count in their
    recorded default state and nothing is occupied — for questions inside a
    fight, use the encounter tools, which see live doors and creatures.
    ``distance`` answers in feet; ``line_of_sight`` is a boolean; ``path``
    returns the squares and cost in feet, or ``reachable: false``. Ground height
    is charged to a ``path`` — a slope is difficult terrain and a cliff is a
    climb, and the result names both ends' elevation so a large cost is
    explainable — but ``distance`` and ``line_of_sight`` are measured flat.
    """
    return _call(_map_ops.query_map, _STATE, map_id, query, frm, to, level)


@server.tool()
def uvtt_export(
    map_id: str,
    path: str | None = None,
    pixels_per_grid: int = 32,
    include_image: bool = True,
    level: int = 0,
    open_features: list[str] | None = None,
) -> dict[str, Any]:
    """Export a loaded map as a Universal VTT file another virtual tabletop can import.

    The payload carries wall polylines derived from the terrain, one portal per
    door feature (with its recorded default open/closed state), and — unless
    ``include_image`` is false — a rendered PNG of the map, which some
    importers require. Lights, object line-of-sight, and elevation are
    deliberately absent: the engine does not model them. The format has one
    plane, so ``level`` picks the storey to export and a map with floors takes
    one call per floor. The image side is
    capped at 4096 pixels; lower ``pixels_per_grid`` for large maps.

    ``open_features`` names the fixtures to export as open — a fight's live
    set, which ``encounter_state``'s map block reports. Given it, the walls,
    the image and the portals all show the map *that fight is on*: a raised
    portcullis stops being a wall and a sluice's flooded room exports as water.
    Omit it and the export is the map as the file has it. A door's own square is
    the one thing that does not change either way: a door travels as a portal
    here, and a portal in solid wall is a door the importer cannot open.

    The result is always written to disk — default
    ``<maps root>/uvtt/<slug-of-name>.uvtt`` — never inlined, because the
    payload embeds a base64 image. An existing file at the target is
    replaced: the export is derived from the session's map, not an original.
    """
    return _call(
        _map_ops.uvtt_export,
        _STATE,
        map_id,
        path,
        pixels_per_grid,
        include_image,
        level,
        open_features,
    )


# --- the interactive editor ------------------------------------------------
# The one surface that stays in the adapter. Spawning a process and speaking
# HTTP to it is transport, not policy, and ``service/`` may import neither.
#: How long map_editor_serve waits for a spawned editor to bind and report.
_EDITOR_SPAWN_TIMEOUT = 5.0
#: Spawned editor processes, kept so the parent can reap them once stopped.
_EDITOR_PROCESSES: list[subprocess.Popen[bytes]] = []


def _editor_maps_dir(maps_dir: str | None) -> Path:
    return Path(maps_dir).expanduser() if maps_dir is not None else _map_service.maps_root()


def _editor_ping(port: int, token: str) -> dict[str, Any] | None:
    """The editor's ``/api/ping`` answer, or ``None`` when nothing answers."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/ping", headers={TOKEN_HEADER: token}
    )
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _viewer_link(target: Path) -> str | None:
    """A URL into a **running** viewer for a just-written bundle, or ``None``.

    Offered only when all three hold, because a link that fails is worse than
    no link — the user clicks it, gets a refused connection, and blames the
    export rather than the absent server:

    * a server for the default maps root answers its own ``/api/ping``;
    * it reports a ``replays_dir``, which only a server new enough to serve
      ``/api/replays`` does;
    * the file just written is actually inside that directory, so that server
      can see it. An export aimed somewhere else is not its to play.

    The id is the ``slugify`` of the stem, which is how the server's own
    replay index names it — the same rule on both sides, not a guess.
    """
    live = _live_editor_state(state_file_for(_map_service.maps_root()))
    if live is None:
        return None
    served = live.get("replays_dir")
    if not isinstance(served, str):
        return None
    try:
        written = target.resolve()
        root = Path(served).expanduser().resolve()
    except OSError:
        return None
    if not written.is_relative_to(root):
        return None
    return (
        f"http://127.0.0.1:{live['port']}/viewer"
        f"?replay={quote(slugify(written.stem), safe='')}"
    )


def _live_editor_state(state_path: Path) -> dict[str, Any] | None:
    """The state file's record, but only when the server it names still answers."""
    state = read_state(state_path)
    if state is None:
        return None
    port, token = state.get("port"), state.get("token")
    if not isinstance(port, int) or not isinstance(token, str):
        return None
    if _editor_ping(port, token) is None:
        return None
    return state


@server.tool()
def map_editor_serve(port: int | None = None, maps_dir: str | None = None) -> dict[str, Any]:
    """Start the browser map editor and replay viewer — or find them already up.

    One localhost-only process serves both pages: ``url`` is the map editor,
    ``viewer_url`` the replay viewer, which plays any bundle under the
    ``replays_dir`` it reports. Hand the user whichever fits what they asked
    for; each page links to the other, and each configures its own access
    token, so the URL alone is enough. Calling
    this again while the editor runs returns the same URL with
    ``already_running`` true rather than starting a second one. After the user
    saves in the editor, the file is the truth — ``map_load`` (with
    ``replace``) re-reads it into the session. ``maps_dir`` defaults to the
    configured maps root; ``port`` defaults to an ephemeral one.
    """
    root = _editor_maps_dir(maps_dir)
    state_path = state_file_for(root)
    live = _live_editor_state(state_path)
    if live is not None:
        return {
            "url": f"http://127.0.0.1:{live['port']}/",
            "viewer_url": f"http://127.0.0.1:{live['port']}/viewer",
            "port": live["port"],
            "maps_dir": str(live.get("maps_dir", root)),
            "replays_dir": str(
                live.get("replays_dir", _replay_service.replays_root())
            ),
            "already_running": True,
        }
    # A state file nobody answers for describes a dead server; clear it so the
    # fresh spawn's record is the only one anybody can read.
    state_path.unlink(missing_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = state_path.parent / "editor.log"
    arguments = [
        sys.executable, "-m", "fivee_sim.editor",
        "--maps-dir", str(root),
        "--state-file", str(state_path),
    ]
    if port is not None:
        arguments += ["--port", str(port)]
    # stdout must be the logfile, never inherited: this process's stdout is
    # the JSON-RPC channel, and one stray line on it breaks the protocol.
    with open(log_path, "ab") as log_file:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    _EDITOR_PROCESSES.append(process)
    deadline = time.monotonic() + _EDITOR_SPAWN_TIMEOUT
    state: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        candidate = read_state(state_path)
        if candidate is not None and isinstance(candidate.get("port"), int):
            state = candidate
            break
        if process.poll() is not None:
            raise ToolError(
                f"the editor process exited with status {process.returncode} before "
                f"binding; see the log at {log_path}"
            )
        time.sleep(0.05)
    if state is None:
        process.terminate()
        raise ToolError(
            f"the editor did not report a bound port within "
            f"{_EDITOR_SPAWN_TIMEOUT:.0f}s; see the log at {log_path}"
        )
    return {
        "url": f"http://127.0.0.1:{state['port']}/",
        "viewer_url": f"http://127.0.0.1:{state['port']}/viewer",
        "port": state["port"],
        "maps_dir": str(root),
        "replays_dir": str(state.get("replays_dir", _replay_service.replays_root())),
        "already_running": False,
        "log": str(log_path),
    }


@server.tool()
def map_editor_stop(maps_dir: str | None = None) -> dict[str, Any]:
    """Stop the browser map editor for a maps directory, if one is running.

    Asks it to shut down gracefully over its own API, falls back to SIGTERM
    at the recorded pid, and clears the state file either way. ``was_running``
    reports whether anything was there to stop.
    """
    root = _editor_maps_dir(maps_dir)
    state_path = state_file_for(root)
    state = read_state(state_path)
    if state is None:
        return {"stopped": False, "was_running": False}
    port, token, pid = state.get("port"), state.get("token"), state.get("pid")
    stopped = False
    if isinstance(port, int) and isinstance(token, str):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/shutdown",
            method="POST",
            headers={TOKEN_HEADER: token},
            data=b"",
        )
        try:
            with urllib.request.urlopen(request, timeout=2.0):
                stopped = True
        except (OSError, ValueError):
            stopped = False
    if not stopped and isinstance(pid, int):
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except OSError:
            stopped = False
    if stopped:
        # The exiting server removes its own state file; give it a moment so
        # the record disappears with the process rather than being yanked
        # from under it.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and state_path.exists():
            time.sleep(0.05)
    state_path.unlink(missing_ok=True)
    for process in _EDITOR_PROCESSES:
        if process.pid == pid and process.poll() is None:
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                pass
    # A state file existed, so something was there to stop — even when both
    # shutdown paths failed because the recorded process is already dead.
    return {"stopped": stopped, "was_running": True}


# --- content ---------------------------------------------------------------
@server.tool()
def content_status() -> dict[str, Any]:
    """What content is loaded, from where, and under which mode.

    Use this before telling anyone what the engine supports: with packs loaded, or
    with the bundled slice excluded, the answer is whatever this reports and not what
    ships by default. It also names any encounter still running on older content.
    """
    return _call(_content_ops.status, _STATE)


@server.tool()
def content_validate(
    paths: list[str] | None = None,
    builtin: str | None = None,
) -> dict[str, Any]:
    """Report problems with content packs without loading them. The authoring aid.

    Give ``paths`` to check specific files or directories, or omit it to re-check what
    is currently configured. Every diagnostic names the pack, section, record, and
    field, and separates errors from warnings.
    """
    return _call(_content_ops.validate, _STATE, paths, builtin)


@server.tool()
def content_configure(
    paths: list[str] | None = None,
    builtin: str | None = None,
    add: bool = False,
) -> dict[str, Any]:
    """Load content packs and/or switch whether the bundled SRD slice is included.

    ``paths`` names files or directories of ``*.json`` packs and replaces the current
    set unless ``add`` is true. ``builtin`` is "include" or "exclude"; omit either
    argument to leave it as it is.

    Nothing changes unless the new content loads cleanly — a failed call reports every
    diagnostic and leaves the working content in place. Encounters already in progress
    keep resolving under the content they started with; only new ones use the result.
    """
    return _call(_content_ops.configure, _STATE, paths, builtin, add)


# --- analytics -------------------------------------------------------------
@server.tool()
def simulate_rounds(
    combatants: list[dict[str, Any]],
    iterations: int = 500,
    seed: int = 0,
    max_rounds: int = 20,
    movement_rule: str = "5-5-5",
    map: dict[str, Any] | None = None,
    map_id: str | None = None,
) -> dict[str, Any]:
    """Auto-play the same encounter many times and report win rates and length.

    Combatant specs match ``encounter_create``, as do ``movement_rule``, the
    inline ``map`` spec, and ``map_id`` (a loaded map session; one or the
    other, not both) — with a map, every iteration fights on it: terrain
    costs, cover, sight, and pathfinding all apply, the policy chooses authored
    Walk/Climb/Swim/Fly modes, and doors reset to their initial state between
    iterations. ``arrival_round`` schedules the same reinforcement in every
    iteration. Iteration ``i`` uses ``seed + i``, so one
    iteration reproduces a single hand-played encounter at that seed. With
    ``map_id`` the result's ``map_source`` records the exact map generation
    and hash the batch ran on.
    """
    return _call(
        _analytics.simulate_rounds,
        _STATE,
        combatants,
        iterations,
        seed,
        max_rounds,
        movement_rule,
        map,
        map_id,
    )


@server.tool()
def simulate_dpr(
    build: dict[str, Any],
    target_ac: int,
    rounds: int = 3,
    iterations: int = 1000,
    seed: int = 0,
    distance: int = 5,
) -> dict[str, Any]:
    """Measure the damage a build lands over several rounds against a given AC.

    The target is a passive dummy with enough hit points to absorb the whole run,
    driven through the real encounter stepper — so advantage, criticals, and
    resistances apply exactly as they would in play.

    ``distance`` is how far off the dummy stands, defaulting to melee reach. It must
    be greater than zero for an area caster: the policy refuses to catch the caster
    in its own blast, and a dummy standing on the attacker leaves no placement that
    catches one without the other. The ``actions`` field reports what the build
    actually did, so a spell the policy declined to cast is visible rather than
    silently absent from the damage figure.
    """
    return _call(
        _analytics.simulate_dpr,
        _STATE,
        build,
        target_ac,
        rounds,
        iterations,
        seed,
        distance,
    )


def main() -> None:
    """Entry point for the stdio server."""
    server.run("stdio")


if __name__ == "__main__":
    main()
