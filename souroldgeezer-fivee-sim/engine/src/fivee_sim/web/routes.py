"""One declaration per operation, read by three consumers.

Every operation this engine exposes over HTTP is declared exactly once, here,
as a :class:`Route`. Three things read the table and nothing else declares an
endpoint:

1. :mod:`fivee_sim.web.http_server` dispatches from it — the path templates
   compile to regexes, the ``405`` and its ``Allow`` header come from the table
   rather than a hand-kept branch, and query and body validation are driven by
   the same ``params`` and ``body_schema`` the contract publishes.
2. :mod:`fivee_sim.web.openapi` renders the OpenAPI 3.1 document from it.
3. ``GET /api/v1/operations`` renders the compact agent index from it.

So "an added endpoint has no contract entry" cannot happen: an endpoint that is
not in this table is not routed at all, and a test asserts every contract route
appears in both the OpenAPI document and the operations index.

``operation`` is ``group.verb`` — ``encounter.act``, ``map.render``. That is the
name the operations index reports and the name a client command takes; it maps
to an OpenAPI ``operationId`` by camel-casing, so one name serves three
surfaces.

**Ordering is load-bearing.** The table is scanned in order, so a literal
sub-resource (``/maps/render``) must precede the templated sibling that would
also match it (``/maps/{id}``). The consequence is deliberate and small: a saved
map whose id happens to be ``render`` is still readable at ``GET
/maps/render`` — only the ``POST`` is claimed by the sub-resource.

**Two groups are function-style, and are named as such.** ``dice.*`` and
``analytics.*`` POST to a collection that returns a *result* resource rather
than storing one. That is the documented RPC carve-out, not an accident of
naming, and the OpenAPI description says so.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

__all__ = [
    "API_PREFIX",
    "ERROR_TYPES",
    "PAGES",
    "ROUTES",
    "Param",
    "Route",
    "allowed_methods",
    "api_routes",
    "compile_path",
    "error_type",
    "find",
    "operation_id",
]

#: Everything this contract answers lives under one version prefix. The served
#: pages take it as ``apiBase`` from their injected configuration, so the
#: browser side of the version is one string.
API_PREFIX = "/api/v1"

#: The error ``type`` registry: status to the kebab name inside
#: ``urn:fivee-sim:error:``. A URN rather than an ``https://`` base because this
#: project publishes no domain and no repository URL in any manifest — a URL
#: would be a claim on a name we neither own nor serve, while a URN is a URI,
#: satisfies RFC 9457, is stable forever, and lies about nothing.
ERROR_TYPES: Mapping[int, str] = {
    400: "invalid-parameter",
    401: "unauthorized",
    403: "forbidden-host",
    404: "not-found",
    405: "method-not-allowed",
    409: "stale-write",
    413: "payload-too-large",
    422: "invalid-entity",
    428: "precondition-required",
    500: "internal",
}


def error_type(status: int) -> str:
    """The ``urn:fivee-sim:error:*`` URI for a status.

    A status with no registered name still gets a URN rather than
    ``about:blank``, derived from its own reason phrase, so the field is never
    the placeholder RFC 9457 offers for problems that have nothing to say.
    """
    name = ERROR_TYPES.get(status)
    if name is None:
        name = HTTPStatus(status).phrase.casefold().replace(" ", "-")
    return f"urn:fivee-sim:error:{name}"


@dataclass(frozen=True, eq=False)
class Param:
    """One declared input that is not the request body.

    ``location`` is ``path``, ``query`` or ``header``. ``schema`` is the JSON
    Schema the OpenAPI document publishes *and* the coercion the dispatcher
    applies, so a query parameter cannot be documented as an integer and read
    as a string.
    """

    name: str
    location: str
    schema: Mapping[str, Any] = field(default_factory=dict)
    required: bool = False
    description: str = ""


@dataclass(frozen=True, eq=False)
class Route:
    """One operation: how it is reached, what it takes, and who answers it.

    ``handler`` names the method on the request handler that answers this
    route. It is a name rather than a reference because this module must stay
    importable by the OpenAPI renderer without dragging in a socket server;
    :mod:`fivee_sim.web.http_server` holds the one registry that resolves it,
    and a test fails if the two ever disagree.

    ``body_schema`` is three-valued on purpose. ``None`` means the request
    carries no body and none is read. A schema with ``properties`` is enforced
    key by key — an unknown key is refused rather than ignored, which is how a
    misspelling becomes a message instead of a silently dropped argument. An
    empty schema means "any JSON": the map documents and replay bundles that
    arrive whole, whose validation belongs to the service layer that owns the
    format.
    """

    method: str
    path: str
    operation: str
    summary: str
    params: Sequence[Param] = ()
    body_schema: Mapping[str, Any] | None = None
    handler: str = ""
    #: The status a successful call answers with.
    success: int = 200
    #: Statuses this route can refuse with beyond the ones every route shares.
    errors: Sequence[int] = ()
    #: False for the served browser pages: routed like anything else, but not
    #: part of the versioned JSON contract the OpenAPI document describes.
    contract: bool = True

    @property
    def operation_id(self) -> str:
        return operation_id(self.operation)


def operation_id(operation: str) -> str:
    """``encounter.act`` -> ``encounterAct``; ``map.uvtt`` -> ``mapUvtt``.

    Stable by construction: the operation name is the only input, so an
    ``operationId`` cannot drift from the operation it identifies.
    """
    words = [word for word in re.split(r"[.\-_]", operation) if word]
    if not words:
        return ""
    return words[0] + "".join(word[:1].upper() + word[1:] for word in words[1:])


_TEMPLATE = re.compile(r"\{([a-z_]+)\}")


def compile_path(path: str) -> re.Pattern[str]:
    """A path template as an anchored regex with named groups for its ``{}``s."""
    pattern = "".join(
        f"(?P<{part[1:-1]}>[^/]+)" if _TEMPLATE.fullmatch(part) else re.escape(part)
        for part in re.split(r"(\{[a-z_]+\})", path)
        if part
    )
    return re.compile(pattern)


# --- shared parameter and schema fragments ---------------------------------
_ID = Param("id", "path", {"type": "string"}, required=True, description="the resource id")
_IF_MATCH = Param(
    "If-Match",
    "header",
    {"type": "string"},
    description=(
        "the version you read: a map's sha256 ETag, an encounter's journal-head "
        "ETag, or * to write regardless"
    ),
)
_IDEMPOTENCY = Param(
    "Idempotency-Key",
    "header",
    {"type": "string"},
    description="replay key: a retry under the same key returns the first result",
)
_SINCE = Param("since", "query", {"type": "integer", "default": 0}, description="page offset")


def _limit(default: int) -> Param:
    return Param(
        "limit", "query", {"type": "integer", "default": default}, description="page size"
    )


_SEED: Mapping[str, Any] = {"type": ["integer", "null"], "default": None}
_ADVANTAGE: Mapping[str, Any] = {
    "type": "string", "enum": ["none", "advantage", "disadvantage"], "default": "none"
}
_COMBATANTS: Mapping[str, Any] = {"type": "array", "items": {"type": "object"}}
_POINT: Mapping[str, Any] = {"type": ["array", "integer", "null"], "default": None}
_MAP_SUBJECT: Mapping[str, Any] = {
    "map_id": {"type": ["string", "null"], "default": None},
    "document": {"type": ["object", "null"], "default": None},
}


ROUTES: tuple[Route, ...] = (
    # --- the server itself --------------------------------------------------
    Route(
        "GET", f"{API_PREFIX}/ping", "server.ping",
        "Liveness, engine version, and the directories this launch serves.",
        handler="ping",
    ),
    Route(
        "GET", f"{API_PREFIX}/operations", "server.operations",
        "Every operation this server exposes, as a compact index.",
        handler="operations",
    ),
    Route(
        "GET", f"{API_PREFIX}/openapi.json", "server.openapi",
        "The OpenAPI 3.1 description of this contract.",
        handler="openapi",
    ),
    Route(
        "POST", f"{API_PREFIX}/shutdown", "server.shutdown",
        "Stop this server after answering.",
        body_schema={"type": "object", "properties": {}},
        handler="shutdown", success=202,
    ),
    # --- dice: a result resource, returned rather than stored ---------------
    Route(
        "POST", f"{API_PREFIX}/dice/rolls", "dice.roll",
        "Roll a dice expression, optionally with advantage and against a fight.",
        params=(_IDEMPOTENCY,),
        body_schema={
            "type": "object",
            "properties": {
                "expression": {"type": "string"},
                "advantage": _ADVANTAGE,
                "seed": _SEED,
                "encounter_id": {"type": ["string", "null"], "default": None},
                "label": {"type": ["string", "null"], "default": None},
            },
            "required": ["expression"],
        },
        handler="dice_roll",
    ),
    Route(
        "POST", f"{API_PREFIX}/dice/checks", "dice.check",
        "Make an ability or skill check.",
        params=(_IDEMPOTENCY,),
        body_schema={
            "type": "object",
            "properties": {
                "modifier": {"type": "integer"},
                "dc": {"type": "integer"},
                "advantage": _ADVANTAGE,
                "seed": _SEED,
                "encounter_id": {"type": ["string", "null"], "default": None},
                "ability": {"type": ["string", "null"], "default": None},
                "skill": {"type": ["string", "null"], "default": None},
            },
            "required": ["modifier", "dc"],
        },
        handler="dice_check",
    ),
    Route(
        "POST", f"{API_PREFIX}/dice/saves", "dice.save",
        "Make a saving throw; auto_fail covers conditions that forfeit it.",
        params=(_IDEMPOTENCY,),
        body_schema={
            "type": "object",
            "properties": {
                "modifier": {"type": "integer"},
                "dc": {"type": "integer"},
                "advantage": _ADVANTAGE,
                "auto_fail": {"type": "boolean", "default": False},
                "seed": _SEED,
                "encounter_id": {"type": ["string", "null"], "default": None},
                "ability": {"type": ["string", "null"], "default": None},
            },
            "required": ["modifier", "dc"],
        },
        handler="dice_save",
    ),
    # --- rules and catalog --------------------------------------------------
    Route(
        "GET", f"{API_PREFIX}/rules", "rules.lookup",
        "Look up a loaded condition, spell, creature, item or terrain kind.",
        params=(
            Param(
                "topic", "query", {"type": "string", "default": ""},
                description="an exact loaded content name; omit for loaded counts",
            ),
        ),
        handler="rules_lookup",
    ),
    Route(
        "GET", f"{API_PREFIX}/catalog/search", "catalog.search",
        "Search catalog identities and loaded custom content, ranked and paged.",
        params=(
            Param("query", "query", {"type": "string"}, required=True),
            Param("kind", "query", {"type": ["string", "null"], "default": None}),
            Param("simulation", "query", {"type": ["string", "null"], "default": None}),
            _SINCE,
            _limit(10),
        ),
        handler="catalog_search",
    ),
    Route(
        "GET", f"{API_PREFIX}/catalog/records/{{id}}", "catalog.get",
        "One structured catalog record by stable id.",
        params=(_ID,),
        handler="catalog_get",
    ),
    Route(
        "GET", f"{API_PREFIX}/catalog/tables/{{id}}", "catalog.table",
        "One printed catalog table, in a bounded row window.",
        params=(_ID, _SINCE, _limit(20)),
        handler="catalog_table",
    ),
    # --- content ------------------------------------------------------------
    Route(
        "GET", f"{API_PREFIX}/content", "content.status",
        "What content is loaded, from where, and which fights predate it.",
        handler="content_status",
    ),
    Route(
        "POST", f"{API_PREFIX}/content/validations", "content.validate",
        "Report problems with content packs without loading them.",
        body_schema={
            "type": "object",
            "properties": {
                "paths": {"type": ["array", "null"], "items": {"type": "string"},
                          "default": None},
                "builtin": {"type": ["string", "null"], "default": None},
            },
        },
        handler="content_validate",
    ),
    Route(
        "POST", f"{API_PREFIX}/content/configuration", "content.configure",
        "Load content packs and/or switch whether the bundled slice is included.",
        body_schema={
            "type": "object",
            "properties": {
                "paths": {"type": ["array", "null"], "items": {"type": "string"},
                          "default": None},
                "builtin": {"type": ["string", "null"], "default": None},
                "add": {"type": "boolean", "default": False},
            },
        },
        handler="content_configure",
    ),
    # --- analytics: also a result resource ----------------------------------
    Route(
        "POST", f"{API_PREFIX}/analytics/rounds", "analytics.rounds",
        "Auto-play one encounter many times; report win rates and length.",
        body_schema={
            "type": "object",
            "properties": {
                "combatants": _COMBATANTS,
                "iterations": {"type": "integer", "default": 500},
                "seed": {"type": "integer", "default": 0},
                "max_rounds": {"type": "integer", "default": 20},
                "movement_rule": {"type": "string", "default": "5-5-5"},
                "map": {"type": ["object", "null"], "default": None},
                "map_id": {"type": ["string", "null"], "default": None},
            },
            "required": ["combatants"],
        },
        handler="analytics_rounds",
    ),
    Route(
        "POST", f"{API_PREFIX}/analytics/dpr", "analytics.dpr",
        "Measure the damage a build lands over several rounds against an AC.",
        body_schema={
            "type": "object",
            "properties": {
                "build": {"type": "object"},
                "target_ac": {"type": "integer"},
                "rounds": {"type": "integer", "default": 3},
                "iterations": {"type": "integer", "default": 1000},
                "seed": {"type": "integer", "default": 0},
                "distance": {"type": "integer", "default": 5},
            },
            "required": ["build", "target_ac"],
        },
        handler="analytics_dpr",
    ),
    Route(
        "POST", f"{API_PREFIX}/analytics/scenario-timing", "analytics.scenario-timing",
        "Measure route arrival and its lead over a timed response.",
        body_schema={
            "type": "object",
            "properties": {
                "distance_feet": {"type": "integer"},
                "speed_feet": {"type": "integer"},
                "dash": {"type": "boolean", "default": False},
                "start_delay_rounds": {"type": "integer", "default": 0},
                "response_after_rounds": {"type": ["integer", "null"], "default": None},
            },
            "required": ["distance_feet", "speed_feet"],
        },
        handler="analytics_scenario_timing",
    ),
    # --- encounters ---------------------------------------------------------
    Route(
        "GET", f"{API_PREFIX}/encounters", "encounter.list",
        "Durable encounters, without loading them into memory.",
        params=(
            Param(
                "status", "query",
                {"type": "string", "enum": ["active", "finalized", "all"],
                 "default": "active"},
            ),
        ),
        handler="encounter_list",
    ),
    Route(
        "POST", f"{API_PREFIX}/encounters", "encounter.create",
        "Start an encounter and roll initiative, optionally on a battle map.",
        params=(_IDEMPOTENCY,),
        body_schema={
            "type": "object",
            "properties": {
                "combatants": _COMBATANTS,
                "seed": _SEED,
                "movement_rule": {"type": "string", "default": "5-5-5"},
                "map": {"type": ["object", "null"], "default": None},
                "map_id": {"type": ["string", "null"], "default": None},
            },
            "required": ["combatants"],
        },
        handler="encounter_create", success=201,
    ),
    Route(
        "GET", f"{API_PREFIX}/encounters/{{id}}", "encounter.state",
        "The authoritative state of one encounter. Narrate from this.",
        params=(_ID,),
        handler="encounter_state",
    ),
    Route(
        "GET", f"{API_PREFIX}/encounters/{{id}}/log", "encounter.log",
        "The paged event history of an encounter, with the actions that made it.",
        params=(
            _ID, _SINCE, _limit(500),
            Param("include_actions", "query", {"type": "boolean", "default": True}),
        ),
        handler="encounter_log",
    ),
    Route(
        "POST", f"{API_PREFIX}/encounters/{{id}}/actions", "encounter.act",
        "Take the current creature's action and durably audit it.",
        params=(_ID, _IF_MATCH, _IDEMPOTENCY),
        body_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "target": {"type": ["string", "null"], "default": None},
                "attack": {"type": ["string", "null"], "default": None},
                "item": {"type": ["string", "null"], "default": None},
                "spell": {"type": ["string", "null"], "default": None},
                "slot_level": {"type": ["integer", "null"], "default": None},
                "to_position": _POINT,
                "targets": {"type": ["array", "null"], "items": {"type": "string"},
                            "default": None},
                "center": _POINT,
                "direction": {"type": ["array", "null"], "default": None},
                "toward": {"type": ["string", "array", "null"], "default": None},
                "path": {"type": ["array", "null"], "default": None},
                "feature": {"type": ["string", "null"], "default": None},
                "set_open": {"type": ["boolean", "null"], "default": None},
                "to_level": {"type": ["integer", "null"], "default": None},
                "movement_mode": {"type": ["string", "null"], "default": None},
                "as_bonus_action": {"type": "boolean", "default": False},
                "facing": {"type": ["string", "null"], "default": None},
            },
            "required": ["kind"],
        },
        handler="encounter_act", errors=(409,),
    ),
    Route(
        "POST", f"{API_PREFIX}/encounters/{{id}}/advance", "encounter.advance",
        "End this turn, begin the next, and record the transition.",
        params=(_ID, _IF_MATCH, _IDEMPOTENCY),
        body_schema={"type": "object", "properties": {}},
        handler="encounter_advance", errors=(409,),
    ),
    Route(
        "POST", f"{API_PREFIX}/encounters/{{id}}/notes", "encounter.note",
        "Attach a durable narrative or adjudication note to an encounter.",
        params=(_ID, _IF_MATCH, _IDEMPOTENCY),
        body_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "category": {"type": "string", "default": "note"},
            },
            "required": ["text"],
        },
        handler="encounter_note", success=201, errors=(409,),
    ),
    Route(
        "POST", f"{API_PREFIX}/encounters/{{id}}/resume", "encounter.resume",
        "Load an encounter from its verified journal, repairing a partial tail.",
        params=(_ID,),
        body_schema={"type": "object", "properties": {}},
        handler="encounter_resume",
    ),
    Route(
        "POST", f"{API_PREFIX}/encounters/{{id}}/finalize", "encounter.finalize",
        "Export replay v2 and mark the durable encounter finalized.",
        params=(_ID, _IF_MATCH),
        body_schema={"type": "object", "properties": {}},
        handler="encounter_finalize", errors=(409,),
    ),
    Route(
        "POST", f"{API_PREFIX}/encounters/{{id}}/replay", "encounter.replay",
        "Export a fight's replay: a bundle inline or on disk, or a viewer page.",
        params=(_ID,),
        body_schema={
            "type": "object",
            "properties": {
                "path": {"type": ["string", "null"], "default": None},
                "embed": {"type": "boolean", "default": False},
                "format_version": {"type": "integer", "default": 2},
            },
        },
        handler="encounter_replay",
    ),
    # --- maps: files, addressed by id ---------------------------------------
    Route(
        "GET", f"{API_PREFIX}/maps", "map.list",
        "Every saved map under the maps directory, keyed by id.",
        handler="map_list",
    ),
    Route(
        "POST", f"{API_PREFIX}/maps/generate", "map.generate",
        "Generate a map under a seed; save_as also writes it under that id.",
        body_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "params": {"type": ["object", "null"], "default": None},
                "seed": _SEED,
                "name": {"type": ["string", "null"], "default": None},
                "save_as": {"type": ["string", "null"], "default": None},
            },
            "required": ["kind"],
        },
        handler="map_generate",
    ),
    Route(
        "POST", f"{API_PREFIX}/maps/render", "map.render",
        "Render a viewport of a saved or inline map as rows of glyphs.",
        body_schema={
            "type": "object",
            "properties": {
                **_MAP_SUBJECT,
                "x": {"type": "integer", "default": 0},
                "y": {"type": "integer", "default": 0},
                "width": {"type": ["integer", "null"], "default": None},
                "height": {"type": ["integer", "null"], "default": None},
                "downsample": {"type": "integer", "default": 1},
                "show_features": {"type": "boolean", "default": True},
                "show_elevation": {"type": "boolean", "default": False},
                "level": {"type": "integer", "default": 0},
                "encounter_id": {"type": ["string", "null"], "default": None},
            },
        },
        handler="map_render", errors=(422,),
    ),
    Route(
        "POST", f"{API_PREFIX}/maps/query", "map.query",
        "Geometry over a saved or inline map: distance, line_of_sight, or path.",
        body_schema={
            "type": "object",
            "properties": {
                **_MAP_SUBJECT,
                "query": {"type": "string"},
                "frm": {"type": ["array", "null"], "default": None},
                "to": {"type": ["array", "null"], "default": None},
                "level": {"type": "integer", "default": 0},
            },
            "required": ["query"],
        },
        handler="map_query", errors=(422,),
    ),
    Route(
        "POST", f"{API_PREFIX}/maps/uvtt", "map.uvtt",
        "Export a saved or inline map as a Universal VTT file on disk.",
        body_schema={
            "type": "object",
            "properties": {
                **_MAP_SUBJECT,
                "path": {"type": ["string", "null"], "default": None},
                "pixels_per_grid": {"type": "integer", "default": 32},
                "include_image": {"type": "boolean", "default": True},
                "level": {"type": "integer", "default": 0},
                "open_features": {"type": ["array", "null"], "items": {"type": "string"},
                                  "default": None},
            },
        },
        handler="map_uvtt", errors=(422,),
    ),
    Route(
        "POST", f"{API_PREFIX}/maps/validate", "map.validate",
        "Report a map document's errors and warnings without saving it.",
        body_schema={},
        handler="map_validate",
    ),
    Route(
        "GET", f"{API_PREFIX}/maps/{{id}}", "map.get",
        "One saved map document, with its sha256 as an ETag.",
        params=(_ID,),
        handler="map_get", errors=(422,),
    ),
    Route(
        "PUT", f"{API_PREFIX}/maps/{{id}}", "map.put",
        "Write a map document under an id, guarded by If-Match.",
        params=(_ID, Param(
            "If-Match", "header", {"type": "string"}, required=True,
            description="the sha256 ETag from the last GET, or * to create",
        )),
        body_schema={},
        handler="map_put", success=200, errors=(409, 422, 428),
    ),
    Route(
        "POST", f"{API_PREFIX}/maps/{{id}}/edits", "map.edit",
        "Apply edit operations to a saved map atomically: all, or none.",
        params=(_ID,),
        body_schema={
            "type": "object",
            "properties": {"operations": {"type": "array", "items": {"type": "object"}}},
            "required": ["operations"],
        },
        handler="map_edit", errors=(409, 422),
    ),
    # --- replays: read-only, and that asymmetry is the contract -------------
    Route(
        "GET", f"{API_PREFIX}/replays", "replay.list",
        "Every replay bundle under the replays directory, keyed by id.",
        handler="replay_list",
    ),
    Route(
        "POST", f"{API_PREFIX}/replays/validate", "replay.validate",
        "Validate a v1 or v2 replay and verify every v2 integrity hash.",
        body_schema={
            "type": "object",
            "properties": {"bundle": {"type": "object"}},
            "required": ["bundle"],
        },
        handler="replay_validate",
    ),
    Route(
        "GET", f"{API_PREFIX}/replays/{{id}}", "replay.get",
        "One replay bundle, whole, with its sha256 as an ETag.",
        params=(_ID,),
        handler="replay_get", errors=(422,),
    ),
    # --- the served pages ---------------------------------------------------
    Route(
        "GET", "/", "page.home", "The landing page: what this launch serves.",
        handler="page", contract=False,
    ),
    Route(
        "GET", "/editor", "page.editor", "The interactive map editor page.",
        handler="page", contract=False,
    ),
    Route(
        "GET", "/viewer", "page.viewer", "The replay viewer page.",
        handler="page", contract=False,
    ),
    Route(
        "GET", "/assets/renderer.js", "page.renderer", "The shared canvas renderer.",
        handler="page", contract=False,
    ),
)

#: path -> (file under ``static/``, content type, inject the launch config).
PAGES: Mapping[str, tuple[str, str, bool]] = {
    "/": ("home.html", "text/html; charset=utf-8", True),
    "/editor": ("editor.html", "text/html; charset=utf-8", True),
    "/viewer": ("viewer.html", "text/html; charset=utf-8", True),
    "/assets/renderer.js": ("renderer.js", "text/javascript; charset=utf-8", False),
}

_COMPILED: tuple[tuple[Route, re.Pattern[str]], ...] = tuple(
    (route, compile_path(route.path)) for route in ROUTES
)


def find(method: str, path: str) -> tuple[Route, Mapping[str, str]] | None:
    """The first route matching both, with its path parameters, or ``None``."""
    for route, pattern in _COMPILED:
        match = pattern.fullmatch(path)
        if match is not None and route.method == method:
            return route, match.groupdict()
    return None


def allowed_methods(path: str) -> tuple[str, ...]:
    """Every method any route answers on this path — the ``Allow`` header.

    Empty when no route's template matches at all, which is what separates a
    ``404`` from a ``405``.
    """
    found = {route.method for route, pattern in _COMPILED if pattern.fullmatch(path)}
    return tuple(sorted(found))


def api_routes() -> tuple[Route, ...]:
    """The versioned JSON contract: every route but the served pages."""
    return tuple(route for route in ROUTES if route.contract)
