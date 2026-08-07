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

**A declared ``example`` is what makes an object-valued argument callable.** A
schema that says ``"type": "object"`` says nothing a caller can act on, and the
client's help used to synthesise its example from that alone — so
``encounter.create`` advertised ``--json '{"combatants": []}'`` and the shape of
a combatant appeared nowhere on any surface an agent reads. Each such route now
declares one whole body that *runs*, carried into the OpenAPI document's media
type and read back by ``fivee help``. Two rules keep them worth having:
``tests/test_web_http.py::TestDeclaredExamples`` sends every one of them to the
route that declared it and fails on a refusal, and it derives which routes need
one from the schemas rather than from a list, so an object-valued argument added
tomorrow is covered today.

**An ``enum`` here is a set some other module is the authority on.** It is
therefore derived from that module wherever the authority is a public constant —
``ActionKind``, ``MovementMode``, ``DiagonalRule``, ``BuiltinMode`` — because a
second copy of a closed set is a copy that drifts in silence. The two the map
service keeps as private module constants are written out and held against it by
``TestDeclaredEnums`` instead, which buys the same guarantee at the cost of a
test. Declaring the set here does more than document it: the dispatcher enforces
``enum``, so the refusal an agent gets for guessing wrong names every legal
value, and it names them before the operation is reached.

Those imports are the only ones this module has, and they are the reason it
still holds no code: ``kernel``, ``model`` and ``content`` are pure, downward,
and drag in no socket server, which is the property the ``handler`` name below
exists to protect.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

from ..content import BuiltinMode
from ..kernel.grid import DiagonalRule, MovementMode
from ..kernel.rules import Ability
from ..model.encounter import ActionKind, EncounterMode

__all__ = [
    "API_PREFIX",
    "ERROR_TYPES",
    "ERROR_TYPE_NAMES",
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
    "problem_type",
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

#: Stable problem names are a registry independent of status. Most statuses
#: have one general type, while 409 distinguishes a stale durable write from a
#: replay key whose request identity changed.
ERROR_TYPE_NAMES: frozenset[str] = frozenset(
    {*ERROR_TYPES.values(), "idempotency-conflict"}
)


def problem_type(name: str) -> str:
    """The registered RFC 9457 URI for one named problem family."""
    if name not in ERROR_TYPE_NAMES:
        raise ValueError(f"unregistered problem type {name!r}")
    return f"urn:fivee-sim:error:{name}"


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
    return problem_type(name)


@dataclass(frozen=True, eq=False)
class Param:
    """One declared input that is not the request body.

    ``location`` is ``path``, ``query`` or ``header``. ``schema`` is the JSON
    Schema the OpenAPI document publishes *and* the coercion the dispatcher
    applies, so a query parameter cannot be documented as an integer and read
    as a string.

    ``example`` is a value that makes the call *work*, for the parameters where
    one exists to be given — ``map.put``'s ``If-Match: *`` is the case it was
    added for. A path id has no such value and takes none: the help keeps
    printing a placeholder there, because substituting one is the caller's part
    and pretending otherwise would be the dishonest kind of example.
    """

    name: str
    location: str
    schema: Mapping[str, Any] = field(default_factory=dict)
    required: bool = False
    description: str = ""
    example: str | None = None


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

    ``example`` is one whole request body that this operation answers, declared
    wherever the schema alone cannot show the shape: an object, an array of
    objects, or a free body. It is not a fragment and not a sketch — it is sent
    verbatim by a test, so a stale one fails rather than misleads.
    """

    method: str
    path: str
    operation: str
    summary: str
    params: Sequence[Param] = ()
    body_schema: Mapping[str, Any] | None = None
    example: Mapping[str, Any] | None = None
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
_IF_NONE_MATCH = Param(
    "If-None-Match",
    "header",
    {"type": "string"},
    description="the ETag from the last live brief; a match answers 304",
)
#: An adventure's precondition, required rather than optional — the map's rule
#: rather than the encounter's, because an adventure is a *document* rewritten
#: whole. An unguarded link would let two callers each be told they linked and
#: leave one encounter missing from a run that acknowledged it. ``*`` is the
#: documented escape and means "against whatever version is current".
_ADVENTURE_IF_MATCH = Param(
    "If-Match",
    "header",
    {"type": "string"},
    required=True,
    description="the version from the last GET of this adventure, or * for the current one",
    example="*",
)
_IDEMPOTENCY = Param(
    "Idempotency-Key",
    "header",
    {"type": "string"},
    description="replay key: a retry under the same key returns the first result",
)
_SINCE = Param("since", "query", {"type": "integer", "default": 0}, description="page offset")
#: The chair a *write* answers to, and one vocabulary across the surface: it is
#: spelled exactly as ``encounter.brief`` spells it and refused exactly as
#: ``encounter.brief`` refuses it. Optional here where that one is required, and
#: the option is the point — omitted, the operation answers the state it always
#: answered, which is what the CLI and both skills read. Given, its ``state``
#: becomes that seat's brief and its events are narrowed with it, so a player's
#: own action is no longer answered with every opponent's sheet.
#:
#: No example, for the reason the required one takes none: the value is the
#: caller's own seat, and inventing one would read as a name this engine knows.
_AS_SEAT = Param(
    "as",
    "query",
    {"type": ["string", "null"], "default": None},
    description="the combatant whose chair this is; omit for the whole fight, as the GM reads it",
)


#: How much of the fight a write answers with. The fourth enum written out here
#: rather than imported, for the same reason as the three above:
#: ``service/views.py``'s ``VIEWS`` is the authority and ``TestDeclaredEnums``
#: holds this against it, but ``web/`` may not reach into ``service/``.
_VIEWS: tuple[str, ...] = ("delta", "live", "full")


def _view(default: str) -> Param:
    """The ``view`` parameter, whose *default* differs by operation.

    A function rather than a constant because that difference is the whole
    design: ``encounter.act`` and ``.advance`` default to ``delta``, while
    ``encounter.create`` and ``.resume`` default to ``full`` because they are
    what a delta would have to be against. Declared here as well as in
    ``service/views.py`` so the OpenAPI document, ``GET /operations`` and the
    CLI's ``--view`` flag all show the right default per operation without
    anybody consulting the service layer to find out.
    """
    return Param(
        "view",
        "query",
        {"type": "string", "enum": list(_VIEWS), "default": default},
        description=(
            "how much of the fight to answer with: delta (what changed since "
            "this seat's last payload), live (every combatant, sheets replaced "
            "by a digest), or full (the whole state)"
        ),
    )


def _limit(default: int) -> Param:
    return Param(
        "limit", "query", {"type": "integer", "default": default}, description="page size"
    )


_SEED: Mapping[str, Any] = {"type": ["integer", "null"], "default": None}
_ADVANTAGE: Mapping[str, Any] = {
    "type": "string", "enum": ["none", "advantage", "disadvantage"], "default": "none"
}
_ABILITY: Mapping[str, Any] = {
    "type": ["string", "null"],
    "enum": [*(ability.value for ability in Ability), None],
    "default": None,
}
_COMBATANTS: Mapping[str, Any] = {"type": "array", "items": {"type": "object"}}
_POINT: Mapping[str, Any] = {"type": ["array", "integer", "null"], "default": None}
_MAP_SUBJECT: Mapping[str, Any] = {
    "map_id": {"type": ["string", "null"], "default": None},
    "document": {"type": ["object", "null"], "default": None},
}
#: The map a fight is on when it does not name a saved one. Two shapes go here,
#: and the object says which it is: a whole ``fivee-sim-map`` document declares
#: itself in ``format``, and anything else is read as the hand-authored
#: battle-map spec. Written once and shared by the three operations that take
#: it, because a caller who learns the rule from one of them has learnt it from
#: all three — and because the editor's Play button posts a document to
#: ``encounter.create`` while a hand-written fight posts a spec.
_INLINE_MAP: Mapping[str, Any] = {
    "type": ["object", "null"],
    "default": None,
    "description": (
        "the map to fight on, inline: either a fivee-sim-map document (the shape "
        "map.put stores and the editor edits, recognised by its 'format' key) or a "
        "battle-map spec of width, height, rows and legend. Give this or 'map_id', "
        "never both; omit both for theatre of the mind"
    ),
}

#: Bounds on caller-supplied strings, enforced by the dispatcher before the
#: operation is reached — which is the only place they can do their job. An
#: audited encounter operation journals its arguments *before* the operation
#: body validates them, so a length check inside the body has already let the
#: payload onto the disk; refusing here is what keeps it off.
#:
#: A name is an identifier that has to match a loaded creature, condition,
#: attack, spell, item, or fixture, so the bound only has to sit past anything
#: a content pack would plausibly name. ``MAX_NOTE_TEXT`` is the note rule the
#: service already owned; ``tests/test_web_http.py::TestDeclaredBounds`` holds
#: the two against each other, the same trade ``TestDeclaredEnums`` makes for a
#: set the map service keeps private.
MAX_NAME_TEXT = 100
MAX_NOTE_TEXT = 4000
#: The correction rule the service already owns, mirrored the same way
#: ``MAX_NOTE_TEXT`` is: ``TestDeclaredBounds`` holds the two equal.
MAX_REASON_TEXT = 4000
#: A filesystem path the caller names. Not journalled, so not part of the rule
#: above — bounded because an unbounded one reaches path handling regardless,
#: and no real path is longer than the kernel's own limit.
MAX_PATH_TEXT = 4096

_NAME_OR_NULL: Mapping[str, Any] = {
    "type": ["string", "null"], "default": None, "maxLength": MAX_NAME_TEXT
}

# --- closed sets, derived from whatever module is the authority on them ------
# A nullable one carries ``None`` in its own enum, because the dispatcher
# checks the enum on any value actually sent and an explicit null is a value.
_ACTION_KIND: Mapping[str, Any] = {
    "type": "string", "enum": [kind.value for kind in ActionKind]
}
_MOVEMENT_MODE: Mapping[str, Any] = {
    "type": ["string", "null"],
    "enum": [*(mode.value for mode in MovementMode), None],
    "default": None,
}
_MOVEMENT_RULE: Mapping[str, Any] = {
    "type": "string",
    "enum": [rule.value for rule in DiagonalRule],
    "default": DiagonalRule.FIVE_FIVE_FIVE.value,
}
#: Which kind of chapter to start. Derived from the model's own declaration
#: rather than written out, for the reason ``_ACTION_KIND`` is: this is a closed
#: set with an owner, and a second spelling of it here is a pair of declarations
#: that must agree with nothing holding them together.
_ENCOUNTER_MODE: Mapping[str, Any] = {
    "type": "string",
    "enum": [mode.value for mode in EncounterMode],
    "default": EncounterMode.COMBAT.value,
    "description": (
        "combat for a fight — initiative, rounds, and an end when one side is "
        "left standing; exploration for an interlude, where each act names its "
        "own actor and the chapter ends when it is finalized"
    ),
}
_BUILTIN_MODE: Mapping[str, Any] = {
    "type": ["string", "null"],
    "enum": [*(mode.value for mode in BuiltinMode), None],
    "default": None,
}
#: The two sets whose authority is a private constant of ``service/maps.py``.
#: Written here and held against it by ``TestDeclaredEnums`` rather than
#: imported, because reaching through a module's underscore for a documentation
#: string is a worse coupling than the test that closes the same gap.
_MAP_KIND: Mapping[str, Any] = {
    "type": "string", "enum": ["caves", "dungeon", "overland"]
}
_MAP_QUERY: Mapping[str, Any] = {
    "type": "string", "enum": ["distance", "line_of_sight", "path"]
}
#: Who is taking this act, in a chapter where nothing decided an order. Bounded
#: like every other journalled name, and nullable because a fight refuses it —
#: initiative has already answered the question this asks.
_ACTOR: Mapping[str, Any] = {
    "type": ["string", "null"],
    "default": None,
    "maxLength": MAX_NAME_TEXT,
    "description": (
        "the combatant taking this act; required in an exploration interlude, "
        "refused in combat, where initiative decides"
    ),
}
#: The third of that kind: ``service/adventures.py``'s ``LIST_STATUSES``, which
#: is public there but still unreachable from here.
_ADVENTURE_STATUS: Mapping[str, Any] = {
    "type": "string", "enum": ["active", "finalized", "all"], "default": "active"
}


# --- example bodies ----------------------------------------------------------
# One hand-built creature and one drawn from loaded content, because those are
# the two ways a combatant is ever written and a caller shown only the second
# would think the first impossible.
_COMBATANTS_EXAMPLE: list[Mapping[str, Any]] = [
    {
        "name": "Thora",
        "team": "party",
        "ac": 16,
        "max_hp": 30,
        "position": [0, 0],
        "attacks": [
            {
                "name": "Longsword",
                "attack_bonus": 5,
                "damage": "1d8+3",
                "damage_type": "slashing",
                "kind": "melee",
            }
        ],
    },
    {"monster": "Goblin Warrior", "team": "monsters", "position": [15, 0]},
]

#: The smallest document that validates clean: a walled chamber with a floor.
#: Small on purpose — it is printed on one line by ``fivee help``, five times.
_MAP_EXAMPLE: Mapping[str, Any] = {
    "format": "fivee-sim-map",
    "format_version": 1,
    "name": "chamber",
    "grid": {"width": 5, "height": 4, "cell_feet": 5},
    "legend": {".": "floor", "#": "wall"},
    "tiles": ["#####", "#...#", "#...#", "#####"],
    "provenance": {
        "generator": "hand",
        "seed": 1,
        "params": {},
        "edited": False,
        "source": "hand-authored example; 5E-compatible original content",
    },
}

#: A scene is a saved ``encounter.create`` body, so its example *is* that
#: operation's — the same roster, the same seed, deliberately not paraphrased.
#: A reader who sees the two agree has learned the whole idea of a scene, and
#: has also learned what Play does with one: post it.
_SCENE_EXAMPLE: Mapping[str, Any] = {
    "name": "Ambush at the ford",
    "combatants": _COMBATANTS_EXAMPLE,
    "seed": 20260805,
}

#: A replay is normally megabytes and written by the exporter, so the example
#: is the smallest *valid* one instead: a v1 bundle with one creature and one
#: event, which is what the validator's own required fields add up to.
_REPLAY_EXAMPLE: Mapping[str, Any] = {
    "format": "fivee-sim-replay",
    "format_version": 1,
    "seed": 7,
    "initial": {
        "creatures": [{"name": "Thora", "position": [0, 0], "hp": 30, "max_hp": 30}],
        "map_open_features": [],
    },
    "map": None,
    "events": [{"kind": "round"}],
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
                "ability": _ABILITY,
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
                "ability": _ABILITY,
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
        "GET", f"{API_PREFIX}/rulings", "rules.rulings",
        "Where the SRD does not decide, what this engine decided, and what would change it.",
        params=(
            Param(
                "code", "query", {"type": "string", "default": ""},
                description="one ruling code; omit for the whole register",
            ),
            Param(
                "kind", "query", {"type": "string", "default": ""},
                description="srd_silent, approximation, schema_ceiling, out_of_scope, superseded",
            ),
        ),
        handler="rules_rulings",
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
                "builtin": _BUILTIN_MODE,
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
                "builtin": _BUILTIN_MODE,
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
                "movement_rule": _MOVEMENT_RULE,
                "map": _INLINE_MAP,
                "map_id": {"type": ["string", "null"], "default": None},
            },
            "required": ["combatants"],
        },
        example={"combatants": _COMBATANTS_EXAMPLE, "iterations": 100},
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
        # One combatant, measured rather than fought: the same spec shape the
        # fight operations take, which is the point worth showing.
        example={
            "build": _COMBATANTS_EXAMPLE[0],
            "target_ac": 15,
            "iterations": 100,
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
        params=(_IDEMPOTENCY, _AS_SEAT, _view("full")),
        body_schema={
            "type": "object",
            "properties": {
                "combatants": _COMBATANTS,
                "seed": _SEED,
                "mode": _ENCOUNTER_MODE,
                "movement_rule": _MOVEMENT_RULE,
                "map": _INLINE_MAP,
                "map_id": {"type": ["string", "null"], "default": None},
            },
            "required": ["combatants"],
        },
        example={"combatants": _COMBATANTS_EXAMPLE, "seed": 20260805},
        handler="encounter_create", success=201,
    ),
    # Declared above the ``{id}`` routes on purpose: ``find`` takes the first
    # template that matches, so a literal segment that could also read as an id
    # has to come first to stay unambiguous.
    Route(
        "POST", f"{API_PREFIX}/encounters/prune", "encounter.prune",
        "Reclaim ids claimed by a creation that never wrote a fight. Lists by default.",
        body_schema={
            "type": "object",
            "properties": {"apply": {"type": "boolean", "default": False}},
        },
        handler="encounter_prune",
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
        "GET", f"{API_PREFIX}/encounters/{{id}}/brief", "encounter.brief",
        "One combatant's own view: their sheet whole, the other side redacted.",
        params=(
            _ID,
            Param(
                # No example, for the reason a path id takes none: the value is
                # the caller's own seat, and inventing one would read as a name
                # this engine knows — including one this projection exists to
                # withhold.
                "as", "query", {"type": "string"}, required=True,
                description="the combatant whose chair this is",
            ),
        ),
        handler="encounter_brief",
    ),
    Route(
        "POST", f"{API_PREFIX}/encounters/{{id}}/actions", "encounter.act",
        "Take the current creature's action and durably audit it.",
        params=(_ID, _IF_MATCH, _IDEMPOTENCY, _AS_SEAT, _view("delta")),
        body_schema={
            "type": "object",
            "properties": {
                "kind": _ACTION_KIND,
                "target": _NAME_OR_NULL,
                "attack": _NAME_OR_NULL,
                "item": _NAME_OR_NULL,
                "spell": _NAME_OR_NULL,
                "slot_level": {"type": ["integer", "null"], "default": None},
                "to_position": _POINT,
                "targets": {"type": ["array", "null"],
                            "items": {"type": "string", "maxLength": MAX_NAME_TEXT},
                            "default": None},
                "center": _POINT,
                "direction": {"type": ["array", "null"], "default": None},
                "toward": {"type": ["string", "array", "null"], "default": None,
                           "maxLength": MAX_NAME_TEXT},
                "path": {"type": ["array", "null"], "default": None},
                "feature": _NAME_OR_NULL,
                "set_open": {"type": ["boolean", "null"], "default": None},
                "to_level": {"type": ["integer", "null"], "default": None},
                "movement_mode": _MOVEMENT_MODE,
                "as_bonus_action": {"type": "boolean", "default": False},
                "facing": _NAME_OR_NULL,
                "actor": _ACTOR,
            },
            "required": ["kind"],
        },
        # Deliberately the action that needs nothing else. An attack would read
        # better and would not run: it needs a target that is in reach of
        # whichever creature's turn it happens to be, which no declared example
        # can know. The ten kinds above are what teach the rest.
        example={"kind": "dodge"},
        handler="encounter_act", errors=(409,),
    ),
    Route(
        "POST", f"{API_PREFIX}/encounters/{{id}}/advance", "encounter.advance",
        "End this turn, begin the next, and record the transition.",
        params=(_ID, _IF_MATCH, _IDEMPOTENCY, _AS_SEAT, _view("delta")),
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
                "text": {"type": "string", "maxLength": MAX_NOTE_TEXT},
                "category": {"type": "string", "default": "note",
                             "maxLength": MAX_NAME_TEXT},
                "speaker": _NAME_OR_NULL,
            },
            "required": ["text"],
        },
        handler="encounter_note", success=201, errors=(409,),
    ),
    Route(
        "POST", f"{API_PREFIX}/encounters/{{id}}/conditions", "encounter.condition",
        "Impose or lift a condition on one combatant by the table's ruling.",
        params=(_ID, _IF_MATCH, _IDEMPOTENCY),
        body_schema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "maxLength": MAX_NAME_TEXT},
                "condition": {"type": "string", "maxLength": MAX_NAME_TEXT},
                "applied": {"type": "boolean", "default": True},
                "levels": {"type": "integer", "default": 1},
            },
            "required": ["target", "condition"],
        },
        handler="encounter_condition", errors=(409,),
    ),
    Route(
        "POST", f"{API_PREFIX}/encounters/{{id}}/corrections", "encounter.correct",
        "Overwrite a live combatant's state when the simulation got it wrong.",
        params=(_ID, _IF_MATCH, _IDEMPOTENCY),
        body_schema={
            "type": "object",
            "properties": {
                "state": {"type": "object"},
                "reason": {"type": "string", "maxLength": MAX_REASON_TEXT},
            },
            "required": ["state", "reason"],
        },
        # "Thora" is the hand-built combatant on ``encounter.create``'s own
        # example roster, so the example that proves this route also proves it
        # against a fight the create example actually starts.
        example={"state": {"Thora": {"ac": 17}}, "reason": "the stat block was mistyped"},
        handler="encounter_correct", errors=(409,),
    ),
    Route(
        "POST", f"{API_PREFIX}/encounters/{{id}}/resume", "encounter.resume",
        "Load an encounter from its verified journal, repairing a partial tail.",
        params=(_ID, _AS_SEAT, _view("full")),
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
                "path": {"type": ["string", "null"], "default": None,
                         "maxLength": MAX_PATH_TEXT},
                "embed": {"type": "boolean", "default": False},
                "format_version": {"type": "integer", "default": 3},
            },
        },
        handler="encounter_replay",
    ),
    # --- adventures: ordered runs of encounters, carrying the party ---------
    Route(
        "GET", f"{API_PREFIX}/adventures", "adventure.list",
        "Durable adventures, without loading the encounters they name.",
        params=(
            # Written out rather than imported: this module may not reach into
            # ``service/``. ``adventures.LIST_STATUSES`` is the authority, and
            # ``TestDeclaredEnums`` holds this against it.
            Param("status", "query", _ADVENTURE_STATUS),
        ),
        handler="adventure_list",
    ),
    Route(
        "POST", f"{API_PREFIX}/adventures", "adventure.create",
        "Start an adventure: an ordered run of encounters sharing a party.",
        params=(_IDEMPOTENCY,),
        body_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        handler="adventure_create", success=201,
    ),
    Route(
        "GET", f"{API_PREFIX}/adventures/{{id}}", "adventure.state",
        "One adventure: its encounters in order, and the version a write must match.",
        params=(_ID,),
        handler="adventure_state",
    ),
    Route(
        "GET", f"{API_PREFIX}/adventures/{{id}}/brief", "adventure.brief",
        "The current chapter as one combatant may see it, conditionally pollable.",
        params=(
            _ID,
            Param(
                "as", "query", {"type": "string"}, required=True,
                description="the combatant whose chair this is",
            ),
            _IF_NONE_MATCH,
        ),
        handler="adventure_brief",
        errors=(409,),
    ),
    Route(
        "POST", f"{API_PREFIX}/adventures/{{id}}/encounters", "adventure.encounter",
        "Start the next encounter, carrying the last one's cast as it left them.",
        params=(_ID, _ADVENTURE_IF_MATCH, _IDEMPOTENCY),
        body_schema={
            "type": "object",
            "properties": {
                # Defaulted rather than required, unlike ``encounter.create``:
                # a later encounter may be entirely the party the last one left
                # behind, with nobody new to describe.
                "combatants": {**_COMBATANTS, "default": []},
                "carry": {"type": ["array", "null"], "items": {"type": "string"},
                          "default": None},
                "recovery": {"type": ["object", "null"], "default": None},
                "recovery_note": {
                    "type": ["string", "null"],
                    "default": None,
                    "maxLength": MAX_NOTE_TEXT,
                    "description": (
                        "caller-stated label for the recovery boundary, such as "
                        "'Long rest at the abbey'; requires recovery and is not "
                        "interpreted as a rules input"
                    ),
                },
                "seed": _SEED,
                "movement_rule": _MOVEMENT_RULE,
                # The same declaration ``encounter.create`` takes, because this
                # is the same argument: a chapter of a run is an encounter, and
                # a run is fights and interludes in the order they happened.
                "mode": _ENCOUNTER_MODE,
                "map": _INLINE_MAP,
                "map_id": {"type": ["string", "null"], "default": None},
                "carry_map": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "put this chapter on the map the previous one was on; "
                        "refused alongside 'map' or 'map_id', and refused when "
                        "that chapter had no saved map to name"
                    ),
                },
            },
        },
        # The first encounter of a run, which is the one a reader meets first
        # and the only one whose whole roster has to be written out.
        example={"combatants": _COMBATANTS_EXAMPLE, "seed": 20260805},
        handler="adventure_encounter", success=201, errors=(409, 428),
    ),
    Route(
        "POST", f"{API_PREFIX}/adventures/{{id}}/finalize", "adventure.finalize",
        "Close an adventure so no further encounter can be linked to it.",
        params=(_ID, _ADVENTURE_IF_MATCH),
        body_schema={"type": "object", "properties": {}},
        handler="adventure_finalize", errors=(409, 428),
    ),
    # No ``If-Match``: this composes a *new file* out of frozen ones and does
    # not rewrite the adventure document, so there is no version to guard.
    Route(
        "POST", f"{API_PREFIX}/adventures/{{id}}/replay", "adventure.replay",
        "Compose the run's finalized encounters into one replay bundle on disk.",
        params=(_ID,),
        body_schema={
            "type": "object",
            "properties": {"path": {"type": ["string", "null"], "default": None}},
        },
        handler="adventure_replay", errors=(422,),
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
                "kind": _MAP_KIND,
                "params": {"type": ["object", "null"], "default": None},
                "seed": _SEED,
                "name": {"type": ["string", "null"], "default": None},
                "save_as": {"type": ["string", "null"], "default": None},
            },
            "required": ["kind"],
        },
        # ``width`` and ``height`` are the two knobs all three generators share;
        # each kind's own set is named in the refusal an unknown one earns.
        example={
            "kind": "dungeon",
            "params": {"width": 40, "height": 24},
            "seed": 20260805,
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
        # Inline rather than by ``map_id``, here and in the two below: a saved
        # id is a placeholder a reader must already have, while the document is
        # the thing the schema calls "an object" and shows nothing of.
        example={"document": _MAP_EXAMPLE},
        handler="map_render", errors=(422,),
    ),
    Route(
        "POST", f"{API_PREFIX}/maps/query", "map.query",
        "Geometry over a saved or inline map: distance, line_of_sight, or path.",
        body_schema={
            "type": "object",
            "properties": {
                **_MAP_SUBJECT,
                "query": _MAP_QUERY,
                "frm": {"type": ["array", "null"], "default": None},
                "to": {"type": ["array", "null"], "default": None},
                "level": {"type": "integer", "default": 0},
            },
            "required": ["query"],
        },
        example={
            "document": _MAP_EXAMPLE,
            "query": "distance",
            "frm": [1, 1],
            "to": [3, 2],
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
        # No ``path``: left out, the export names itself under the maps
        # directory, so the example writes somewhere that exists on every host
        # rather than somewhere a reader has to invent.
        example={"document": _MAP_EXAMPLE},
        handler="map_uvtt", errors=(422,),
    ),
    Route(
        "POST", f"{API_PREFIX}/maps/validate", "map.validate",
        "Report a map document's errors and warnings without saving it.",
        body_schema={},
        example=_MAP_EXAMPLE,
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
            example="*",
        )),
        body_schema={},
        example=_MAP_EXAMPLE,
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
        # Every op is an object with an ``op`` key and that op's own arguments;
        # the fifteen names and what each takes come back in the refusal an
        # unknown one earns, which is what makes one worked case enough here.
        example={
            "operations": [
                {"op": "paint", "cells": [[2, 2]], "terrain": "wall"},
            ]
        },
        handler="map_edit", errors=(409, 422),
    ),
    # --- scenes: a saved encounter.create body, addressed by id -------------
    # Shaped like the map routes above because a scene is the same kind of
    # thing: one document, rewritten whole, that two servers can both hold.
    # There is deliberately no ``scene.play``: Play posts the stored body to
    # ``encounter.create``, so exactly one code path starts a fight.
    Route(
        "GET", f"{API_PREFIX}/scenes", "scene.list",
        "Every saved scene under the scenes directory, keyed by id.",
        handler="scene_list",
    ),
    Route(
        "POST", f"{API_PREFIX}/scenes/validate", "scene.validate",
        "Report a scene envelope's errors and warnings without saving it.",
        body_schema={},
        example=_SCENE_EXAMPLE,
        handler="scene_validate",
    ),
    Route(
        "GET", f"{API_PREFIX}/scenes/{{id}}", "scene.get",
        "One saved scene, with its sha256 as an ETag.",
        params=(_ID,),
        handler="scene_get",
    ),
    Route(
        "PUT", f"{API_PREFIX}/scenes/{{id}}", "scene.put",
        "Write a scene under an id, guarded by If-Match.",
        params=(_ID, Param(
            "If-Match", "header", {"type": "string"}, required=True,
            description="the sha256 ETag from the last GET, or * to create",
            example="*",
        )),
        body_schema={},
        example=_SCENE_EXAMPLE,
        handler="scene_put", success=200, errors=(409, 428),
    ),
    # --- replays: read-only, and that asymmetry is the contract -------------
    Route(
        "GET", f"{API_PREFIX}/replays", "replay.list",
        "Every replay bundle under the replays directory, keyed by id.",
        handler="replay_list",
    ),
    Route(
        "POST", f"{API_PREFIX}/replays/validate", "replay.validate",
        "Validate a v1 or v2 replay, or an adventure's replay, hashes included.",
        body_schema={
            "type": "object",
            "properties": {"bundle": {"type": "object"}},
            "required": ["bundle"],
        },
        example={"bundle": _REPLAY_EXAMPLE},
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
        params=(
            Param(
                "replay", "query", {"type": "string"},
                description="the served replay to open",
            ),
            Param(
                "adventure", "query", {"type": "string"},
                description="the live adventure to follow",
            ),
            Param(
                "as", "query", {"type": "string"},
                description="the live adventure's player seat",
            ),
        ),
        handler="page", contract=False,
    ),
    Route(
        "GET", "/assets/renderer.js", "page.renderer", "The shared canvas renderer.",
        handler="page", contract=False,
    ),
    Route(
        "GET", "/assets/play.js", "page.play", "The extracted Play-mode driver.",
        handler="page", contract=False,
    ),
)

#: path -> (file under ``static/``, content type, inject the launch config).
PAGES: Mapping[str, tuple[str, str, bool]] = {
    "/": ("home.html", "text/html; charset=utf-8", True),
    "/editor": ("editor.html", "text/html; charset=utf-8", True),
    "/viewer": ("viewer.html", "text/html; charset=utf-8", True),
    "/assets/renderer.js": ("renderer.js", "text/javascript; charset=utf-8", False),
    "/assets/play.js": ("play.js", "text/javascript; charset=utf-8", False),
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
