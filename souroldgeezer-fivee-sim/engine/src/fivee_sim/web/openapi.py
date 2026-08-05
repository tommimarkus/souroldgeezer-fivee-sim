"""The OpenAPI 3.1 document, rendered from the route table and nothing else.

Every path, parameter, request body and ``operationId`` here is read off
:data:`fivee_sim.web.routes.ROUTES`. Nothing is written twice, so the published
contract cannot describe an endpoint the dispatcher does not route, or miss one
it does — a test asserts the correspondence in both directions.

Two things this document says out loud rather than leaving to be inferred:

* **The RPC carve-out.** ``dice.*`` and ``analytics.*`` POST to a collection
  that returns a *result* resource rather than storing one. That is a
  deliberate exception to the resource shape everything else follows, so the
  description names it instead of letting a reader discover it.
* **The error type registry.** Every problem carries a ``urn:fivee-sim:error:*``
  ``type``, enumerated in ``components.schemas.ErrorType``. A URN rather than an
  ``https://`` base because this project publishes no domain and no repository
  URL — a URL would be a claim on a name we neither own nor serve.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Any

from .. import __version__
from .routes import API_PREFIX, ERROR_TYPES, Route, api_routes, error_type

__all__ = ["OPENAPI_VERSION", "document", "operations_index"]

OPENAPI_VERSION = "3.1.1"

DESCRIPTION = """\
The 5E-compatible simulation engine's whole operation surface, over HTTP.

This is a single-user localhost tool: the server binds 127.0.0.1 on an
ephemeral port, is started by the user's own agent, and serves that user's own
game state on their own machine. Authorization is the per-launch bearer token
in the X-Fivee-Editor-Token header, plus a Host check — a deliberate carve-out
from OAuth 2.0 for one trusted caller on one machine with no network path in.
No CORS headers are emitted at all.

Two operation groups are function-style rather than resource-style, and are so
by design: POST /dice/rolls|checks|saves and
POST /analytics/rounds|dpr|scenario-timing create a result that is returned
rather than stored. Everything else is a resource, and a state transition is a
POST to a sub-resource (.../actions, .../advance, .../finalize).

Errors are RFC 9457 problem+json. Every problem carries an instance (the
request target) and a urn:fivee-sim:error:* type.\
"""

_PROBLEM_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "description": "An RFC 9457 problem detail.",
    "properties": {
        "type": {"$ref": "#/components/schemas/ErrorType"},
        "title": {"type": "string", "description": "the status's reason phrase"},
        "status": {"type": "integer"},
        "detail": {
            "type": "string",
            "description": "what this branch refused, in the words the engine uses",
        },
        "instance": {
            "type": "string",
            "description": "the request target this problem describes",
        },
        "diagnostics": {
            "type": "array",
            "description": "per-field validation detail, where the failure has any",
            "items": {"type": "object"},
        },
    },
    "required": ["type", "title", "status", "detail", "instance"],
}

#: Statuses any route can answer with, whatever it takes.
_UNIVERSAL: tuple[int, ...] = (400, 401, 403, 404, 405, 500)


def _problem_response(status: int) -> dict[str, Any]:
    return {
        "description": HTTPStatus(status).phrase,
        "content": {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/Problem"}
            }
        },
    }


def _statuses(route: Route) -> tuple[int, ...]:
    """Every status this route can answer with, success first."""
    found = set(_UNIVERSAL) | set(route.errors)
    if route.body_schema is not None:
        found.add(413)
    for param in route.params:
        if param.name == "If-Match":
            found.update((409, 428) if param.required else (409,))
    return (route.success, *sorted(found))


def _parameters(route: Route) -> list[dict[str, Any]]:
    return [
        {
            "name": param.name,
            "in": param.location,
            "required": param.required or param.location == "path",
            "schema": dict(param.schema),
            **({"description": param.description} if param.description else {}),
            **({"example": param.example} if param.example is not None else {}),
        }
        for param in route.params
    ]


def _operation(route: Route) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if route.body_schema is not None:
        # OpenAPI 3.1 puts a body example on the Media Type Object and a
        # parameter's on the Parameter Object, so a declared example lands
        # where any reader of this document already looks for one — and, more
        # to the point, where ``fivee help`` reads it back from.
        media: dict[str, Any] = {"schema": dict(route.body_schema)}
        if route.example is not None:
            media["example"] = dict(route.example)
        body = {
            "requestBody": {
                "required": bool(route.body_schema.get("required")),
                "content": {"application/json": media},
            }
        }
    responses: dict[str, Any] = {}
    for status in _statuses(route):
        if status == route.success:
            responses[str(status)] = {
                "description": route.summary,
                "content": {"application/json": {"schema": {"type": "object"}}},
            }
        else:
            responses[str(status)] = _problem_response(status)
    return {
        "operationId": route.operation_id,
        "summary": route.summary,
        "tags": [route.operation.split(".", 1)[0]],
        "parameters": _parameters(route),
        **body,
        "responses": responses,
    }


def document(routes: Sequence[Route] | None = None) -> dict[str, Any]:
    """The whole contract, as an OpenAPI 3.1 object ready to serialise."""
    selected = tuple(routes) if routes is not None else api_routes()
    paths: dict[str, dict[str, Any]] = {}
    for route in selected:
        paths.setdefault(route.path, {})[route.method.lower()] = _operation(route)
    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": "5E-compatible simulation engine",
            "version": __version__,
            "description": DESCRIPTION,
        },
        "servers": [{"url": "/", "description": "this launch, on 127.0.0.1"}],
        "security": [{"launchToken": []}],
        "tags": sorted(
            ({"name": route.operation.split(".", 1)[0]} for route in selected),
            key=lambda tag: str(tag["name"]),
        ),
        "paths": paths,
        "components": {
            "securitySchemes": {
                "launchToken": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Fivee-Editor-Token",
                    "description": (
                        "the per-launch token, injected into the served pages and "
                        "written to this launch's state file; never in a URL"
                    ),
                }
            },
            "schemas": {
                "Problem": dict(_PROBLEM_SCHEMA),
                "ErrorType": {
                    "type": "string",
                    "description": "the registry of problem type URIs this engine emits",
                    "enum": [error_type(status) for status in sorted(ERROR_TYPES)],
                },
            },
        },
    }


def operations_index(routes: Sequence[Route] | None = None) -> dict[str, Any]:
    """The compact agent-facing index behind ``GET /api/v1/operations``.

    The same table the OpenAPI document is rendered from, reduced to what a
    client needs to *call* one: the operation name it is known by, the request
    line, and what it takes. A client that wants the schema fetches
    ``openapi.json``; this is the help text.
    """
    selected = tuple(routes) if routes is not None else api_routes()
    return {
        "version": __version__,
        "base": API_PREFIX,
        "openapi": f"{API_PREFIX}/openapi.json",
        "count": len(selected),
        "operations": [
            {
                "operation": route.operation,
                "operation_id": route.operation_id,
                "method": route.method,
                "path": route.path,
                "summary": route.summary,
                "parameters": [
                    {"name": param.name, "in": param.location, "required": param.required}
                    for param in route.params
                ],
                "body": sorted(route.body_schema.get("properties", {}))
                if route.body_schema
                else [],
                "required": sorted(route.body_schema.get("required", []))
                if route.body_schema
                else [],
            }
            for route in selected
        ],
    }
