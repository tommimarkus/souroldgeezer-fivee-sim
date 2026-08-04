"""The localhost REST server: the engine's whole operation surface, over HTTP.

A stdlib :class:`~http.server.ThreadingHTTPServer` bound to ``127.0.0.1``,
speaking JSON under ``/api/v1`` and serving the two browser pages. It is an
adapter in exactly the sense the MCP server is one: every endpoint validates
input, calls a function in :mod:`fivee_sim.service`, and serialises the
result — no rules, map, or replay logic lives here, and error prose comes
verbatim from the service exception any other adapter would also report.

**Dispatch is table-driven.** :mod:`fivee_sim.web.routes` declares every
operation once; this module compiles the table's path templates, resolves each
route's ``handler`` name against :data:`_HANDLERS`, validates query and body
against the same schemas the OpenAPI document publishes, and answers ``405``
with an ``Allow`` header built from the table rather than from a hand-kept
branch. An endpoint that is not in the table is not routed at all, so the
published contract and the dispatch cannot drift apart.

**Maps are read-write here; replays are read-only, and that asymmetry is the
contract.** A map is a file the editor exists to change, so ``/maps/{id}``
takes PUT. A replay is the audit record of a fight the engine ran: the browser
plays one back and never writes one, so ``/replays`` answers GET and nothing
else. A route that quietly accepted a write would let a page overwrite the very
thing the bundle's integrity hashes exist to protect.

Security posture, for a single-user localhost tool:

* **Per-launch token.** Every ``/api/*`` request must carry the launch's
  random token in the ``X-Fivee-Editor-Token`` header or it is refused with
  401. The token reaches the browser only by being injected into the served
  pages — it is never logged and never put in a URL.
* **Host-header check.** A request whose ``Host`` is not ``127.0.0.1`` or
  ``localhost`` is refused with 403; a DNS-rebinding page can make a browser
  send the request, but not with a local ``Host``. No CORS headers are ever
  emitted, so cross-origin pages cannot read responses either.
* **Bounded bodies, contained paths.** Bodies over ``MAX_BODY_BYTES`` are
  refused with 413 before being read; ids resolve strictly to files under the
  maps or replays directory, so a traversal id is simply an unknown map.

**Concurrency preconditions travel as headers, because that is what they are.**
``If-Match`` on a map carries the document's sha256; on an encounter it carries
the journal chain head, and a mismatch is refused with the service layer's own
``StaleWriteError`` prose rather than merged. ``Idempotency-Key`` on a POST is
the replay key the encounter journal already keeps, so a retried turn returns
the first result instead of taking the turn twice.

Errors are RFC 9457 ``application/problem+json``: every problem carries
``instance`` (the request target) and a ``urn:fivee-sim:error:*`` ``type``.
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import parse_qsl, quote, unquote

from .. import __version__
from ..map_document import validate_document
from ..service import analytics as analytics_service
from ..service import catalog as catalog_service
from ..service import content_ops, map_ops, primitives, sessions
from ..service import encounters as encounter_service
from ..service import maps as map_service
from ..service import replay as replay_service
from ..service.common import slugify
from ..service.errors import (
    MapEditError,
    MapError,
    NotFoundError,
    ReplayError,
    RequestError,
    StaleWriteError,
)
from ..service.sessions import EngineState
from ..validation import Severity
from . import openapi, routes
from .routes import API_PREFIX, PAGES, Route

__all__ = [
    "API_PREFIX",
    "CONFIG_MARKER",
    "MAX_BODY_BYTES",
    "TOKEN_HEADER",
    "EngineServer",
]

#: The header every ``/api/*`` request must carry the per-launch token in. Named
#: for the editor that first needed it and deliberately left alone: it is the
#: contract the served pages code against, and the offline guarantee turns on
#: the config gate it belongs to.
TOKEN_HEADER = "X-Fivee-Editor-Token"
#: Request bodies above this are refused with 413 before being read.
MAX_BODY_BYTES = 8 * 1024 * 1024
#: The marker the served pages carry where the launch configuration goes. A
#: page is served with this replaced by ``window.__FIVEE_EDITOR__ = {...};``;
#: a page without the marker is served untouched.
CONFIG_MARKER = "/*__EDITOR_CONFIG__*/"

#: JSON type names to the phrase a refusal uses for them.
_TYPE_WORDS: Mapping[str, str] = {
    "string": "text",
    "integer": "a whole number",
    "number": "a number",
    "boolean": "true or false",
    "array": "a list",
    "object": "an object",
    "null": "null",
}


def _is_type(value: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    if name == "null":
        return value is None
    return True


def _type_names(schema: Mapping[str, Any]) -> tuple[str, ...]:
    declared = schema.get("type")
    if declared is None:
        return ()
    if isinstance(declared, str):
        return (declared,)
    return tuple(str(name) for name in declared)


def _type_phrase(names: Sequence[str]) -> str:
    words = [_TYPE_WORDS.get(name, name) for name in names]
    if len(words) <= 1:
        return words[0] if words else "valid"
    return f"{', '.join(words[:-1])} or {words[-1]}"


class _Problem(Exception):
    """One request failed; carries everything a problem+json response needs."""

    def __init__(
        self,
        status: HTTPStatus,
        detail: str,
        diagnostics: list[dict[str, Any]] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status = status
        self.detail = detail
        self.diagnostics = diagnostics
        self.headers = dict(headers or {})
        super().__init__(detail)


def _etag_of(sha256: str) -> str:
    return f'"{sha256}"'


def _etag_value(raw: str) -> str:
    """The bare hash inside an ``If-Match`` value, tolerant of ``W/`` and quotes."""
    value = raw.strip()
    if value.startswith("W/"):
        value = value[2:]
    return value.strip('"')


@dataclass(slots=True)
class _Request:
    """One validated request: which operation, and everything it was given."""

    route: Route
    path_params: Mapping[str, str]
    query: Mapping[str, Any]
    body: Any = None

    @property
    def id(self) -> str:
        return self.path_params["id"]


class _EngineHTTPServer(ThreadingHTTPServer):
    """The stdlib server with a back-pointer to its :class:`EngineServer`."""

    daemon_threads = True
    engine: EngineServer

    def handle_error(self, request: Any, client_address: Any) -> None:
        print(
            f"fivee-sim: error handling a request from {client_address}",
            file=self.engine.log,
        )
        traceback.print_exc(file=self.engine.log)


class EngineServer:
    """The engine's HTTP server: configuration, engine state, and lifecycle.

    Binds ``127.0.0.1`` only, on an ephemeral port by default — :attr:`port`
    reports the one actually bound. ``token`` defaults to a fresh
    ``secrets.token_urlsafe(16)`` per launch. ``log`` is where request lines
    and handler errors go; it defaults to ``sys.stderr`` and must never be
    stdout when the server is spawned by a process whose stdout is a protocol
    channel — the launcher passes a logfile there.

    :attr:`state` is the one :class:`~fivee_sim.service.sessions.EngineState`
    this process owns: every fight it is holding, and the content registry they
    resolve under. There is no separate terrain table any more, and there must
    not be — a content reconfiguration over this API has to move what map
    validation sees too, and a second copy is a second thing to forget.
    """

    def __init__(
        self,
        *,
        maps_dir: str | Path | None = None,
        replays_dir: str | Path | None = None,
        port: int = 0,
        token: str | None = None,
        log: TextIO | None = None,
    ) -> None:
        self.maps_dir = (
            Path(maps_dir).expanduser() if maps_dir is not None else map_service.maps_root()
        )
        # Replays are rooted independently of maps rather than derived from
        # them: a caller may point FIVEE_SIM_MAPS anywhere, and deriving the
        # replay root from that path would put fights in whatever directory
        # happened to be the maps one's neighbour.
        self.replays_dir = (
            Path(replays_dir).expanduser()
            if replays_dir is not None
            else replay_service.replays_root()
        )
        self.token = token if token else secrets.token_urlsafe(16)
        self.log = log if log is not None else sys.stderr
        self.state = EngineState(maps_dir=self.maps_dir)
        self._httpd = _EngineHTTPServer(("127.0.0.1", port), _Handler)
        self._httpd.engine = self

    @property
    def port(self) -> int:
        """The port actually bound — the answer when 0 (ephemeral) was asked for."""
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        """Serve until :meth:`shutdown`. Blocks; run it in a thread to embed.

        ``poll_interval`` is how often the loop checks whether it has been asked
        to stop, and so how long :meth:`shutdown` can block waiting for it. The
        stdlib default of half a second is right for a launch that shuts down
        once; a test harness starting and stopping a server per case wants it
        far smaller.
        """
        self._httpd.serve_forever(poll_interval)

    def shutdown(self) -> None:
        """Stop :meth:`serve_forever` gracefully. Safe to call more than once."""
        self._httpd.shutdown()

    def close(self) -> None:
        """Release the listening socket."""
        self._httpd.server_close()

    def replay_index(self) -> dict[str, dict[str, Any]]:
        """Every replay under the replays directory, keyed by id.

        The same id rule the maps index uses — the ``slugify`` of the file's
        stem, first in path order wins — so a URL naming a replay is built the
        same way a URL naming a map is, and neither surface needs a lookup
        table to explain itself.
        """
        index: dict[str, dict[str, Any]] = {}
        for entry in replay_service.list_replays([self.replays_dir]):
            replay_id = slugify(Path(str(entry["path"])).stem)
            if replay_id in index:
                continue
            index[replay_id] = {"id": replay_id, **entry}
        return index

    def viewer_link(self, target: Path) -> str | None:
        """A URL into *this* viewer for a just-written bundle, or ``None``.

        Offered only for a file inside the directory this launch serves
        replays from: a link to a bundle this server cannot see would fail in
        the user's browser and be blamed on the export rather than on the link.
        """
        try:
            written = target.resolve()
            root = self.replays_dir.resolve()
        except OSError:
            return None
        if not written.is_relative_to(root):
            return None
        return (
            f"http://127.0.0.1:{self.port}/viewer"
            f"?replay={quote(slugify(written.stem), safe='')}"
        )


class _Handler(BaseHTTPRequestHandler):
    """One request: host check, token check, route, validate, serialise."""

    protocol_version = "HTTP/1.1"
    server_version = "fivee-sim-server"
    server: _EngineHTTPServer

    # -- plumbing ------------------------------------------------------------
    @property
    def engine(self) -> EngineServer:
        return self.server.engine

    @property
    def state(self) -> EngineState:
        return self.server.engine.state

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        print(f"{self.address_string()} - {format % args}", file=self.engine.log)

    def do_GET(self) -> None:  # noqa: N802 - stdlib dispatch names
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        """Route one request, translating every service refusal exactly once.

        The service layer raises plain ``ValueError`` families because it may
        not know what an HTTP status is; this is the one place that knows both,
        which is why no handler below carries a ``try``. Anything that is not a
        refusal — a defect — becomes a 500 and is logged, since dressing a bug
        as bad input is how a bug hides.
        """
        try:
            self._route(method)
        except _Problem as problem:
            self._send_problem(problem)
        except NotFoundError as error:
            self._send_problem(_Problem(HTTPStatus.NOT_FOUND, str(error)))
        except StaleWriteError as error:
            self._send_problem(_Problem(HTTPStatus.CONFLICT, str(error)))
        except MapEditError as error:
            self._send_problem(_Problem(HTTPStatus.BAD_REQUEST, str(error)))
        except MapError as error:
            self._send_problem(
                _Problem(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    str(error),
                    diagnostics=[d.as_dict() for d in error.diagnostics],
                )
            )
        except ReplayError as error:
            self._send_problem(
                _Problem(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    str(error),
                    diagnostics=error.diagnostics,
                )
            )
        except RequestError as error:
            self._send_problem(_Problem(HTTPStatus.BAD_REQUEST, str(error)))
        except Exception as error:  # a handler bug must not kill the server
            print(f"fivee-sim: unhandled error serving {self.path}", file=self.engine.log)
            traceback.print_exc(file=self.engine.log)
            self._send_problem(
                _Problem(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(error).__name__}: {error}")
            )

    def _route(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        self._check_host()
        if path.startswith("/api/"):
            self._check_token()
        found = routes.find(method, path)
        if found is None:
            allowed = routes.allowed_methods(path)
            if allowed:
                raise _Problem(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    f"{method} is not supported on {path}",
                    headers={"Allow": ", ".join(allowed)},
                )
            raise _Problem(HTTPStatus.NOT_FOUND, f"no route for {path}")
        route, path_params = found
        request = _Request(
            route=route,
            path_params={name: unquote(value) for name, value in path_params.items()},
            query=self._parse_query(route),
            body=self._parse_body(route),
        )
        _HANDLERS[route.handler](self, request)

    def _check_host(self) -> None:
        host = self.headers.get("Host", "")
        name = host.rsplit(":", 1)[0] if ":" in host else host
        # RFC 9110 §7.2 inherits URI host semantics, where the host is
        # case-insensitive, so LOCALHOST is this server. Accepting it costs
        # nothing defensively: the guard is here to stop DNS rebinding, and an
        # attacker who can set Host arbitrarily would simply send lowercase.
        if name.lower() not in ("127.0.0.1", "localhost"):
            raise _Problem(
                HTTPStatus.FORBIDDEN,
                f"host {host!r} is not this server; it answers only as "
                f"127.0.0.1 or localhost",
            )

    def _check_token(self) -> None:
        given = self.headers.get(TOKEN_HEADER)
        if given is None or not secrets.compare_digest(given, self.engine.token):
            raise _Problem(
                HTTPStatus.UNAUTHORIZED,
                f"missing or invalid {TOKEN_HEADER} header; the served editor "
                f"page carries the token for this launch",
            )

    # -- reading the request -------------------------------------------------
    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b""
        try:
            length = int(raw_length)
        except ValueError:
            raise _Problem(
                HTTPStatus.BAD_REQUEST, f"Content-Length is not a number: {raw_length!r}"
            ) from None
        if length < 0:
            raise _Problem(HTTPStatus.BAD_REQUEST, f"Content-Length is negative: {length}")
        if length > MAX_BODY_BYTES:
            raise _Problem(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"request body is {length} bytes, over the {MAX_BODY_BYTES} byte limit",
            )
        return self.rfile.read(length)

    def _parse_body(self, route: Route) -> Any:
        """The request body, validated against the schema the contract publishes.

        Read even where the route declares none, because a body left in the
        socket desynchronises a kept-alive connection; validated only where a
        schema says what it should hold. A schema without ``properties`` means
        "any JSON": the map documents and replay bundles that arrive whole, and
        whose validation belongs to the layer that owns the format.
        """
        raw = self._read_body()
        schema = route.body_schema
        if schema is None:
            return None
        if not raw.strip():
            payload: Any = {}
        else:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as error:
                raise _Problem(
                    HTTPStatus.BAD_REQUEST, f"request body is not valid JSON: {error}"
                ) from None
        if "properties" not in schema:
            return payload
        return self._validated_object(schema, payload)

    def _validated_object(self, schema: Mapping[str, Any], payload: Any) -> dict[str, Any]:
        properties: Mapping[str, Any] = schema.get("properties", {})
        valid = ", ".join(sorted(properties)) or "none"
        if not isinstance(payload, dict):
            raise _Problem(
                HTTPStatus.BAD_REQUEST,
                f"request body must be a JSON object with keys from: {valid}",
            )
        unknown = sorted(set(payload) - set(properties))
        if unknown:
            raise _Problem(
                HTTPStatus.BAD_REQUEST,
                f"unknown key(s): {', '.join(repr(key) for key in unknown)}. "
                f"Valid keys: {valid}",
            )
        for name in schema.get("required", []):
            if name not in payload:
                raise _Problem(
                    HTTPStatus.BAD_REQUEST, f"'{name}' is required. Valid keys: {valid}"
                )
        checked: dict[str, Any] = {}
        for name, field_schema in properties.items():
            if name not in payload:
                if "default" in field_schema:
                    checked[name] = field_schema["default"]
                continue
            checked[name] = self._checked_value(name, field_schema, payload[name])
        return checked

    def _checked_value(self, name: str, schema: Mapping[str, Any], value: Any) -> Any:
        names = _type_names(schema)
        if names and not any(_is_type(value, one) for one in names):
            raise _Problem(HTTPStatus.BAD_REQUEST, f"'{name}' must be {_type_phrase(names)}")
        allowed = schema.get("enum")
        if allowed is not None and value not in allowed:
            raise _Problem(
                HTTPStatus.BAD_REQUEST,
                f"'{name}' must be one of: {', '.join(str(one) for one in allowed)}",
            )
        return value

    def _parse_query(self, route: Route) -> dict[str, Any]:
        """Query parameters, coerced by the very schema the contract publishes.

        An undeclared parameter is refused rather than ignored, for the reason
        an unknown body key is: a misspelled ``limit`` that silently paged at
        the default is a wrong answer nobody can see.
        """
        declared = {param.name: param for param in route.params if param.location == "query"}
        _, _, raw = self.path.partition("?")
        given = dict(parse_qsl(raw, keep_blank_values=True))
        unknown = sorted(set(given) - set(declared))
        if unknown:
            valid = ", ".join(sorted(declared)) or "none"
            raise _Problem(
                HTTPStatus.BAD_REQUEST,
                f"unknown query parameter(s): "
                f"{', '.join(repr(key) for key in unknown)}. Valid: {valid}",
            )
        parsed: dict[str, Any] = {}
        for name, param in declared.items():
            if name not in given:
                if param.required:
                    raise _Problem(
                        HTTPStatus.BAD_REQUEST, f"query parameter {name!r} is required"
                    )
                parsed[name] = param.schema.get("default")
                continue
            parsed[name] = self._coerced_query(name, param.schema, given[name])
        return parsed

    def _coerced_query(self, name: str, schema: Mapping[str, Any], text: str) -> Any:
        names = _type_names(schema)
        if "integer" in names:
            try:
                return int(text)
            except ValueError:
                raise _Problem(
                    HTTPStatus.BAD_REQUEST,
                    f"query parameter {name!r} must be a whole number, not {text!r}",
                ) from None
        if "boolean" in names:
            lowered = text.strip().casefold()
            if lowered in ("true", "1", "yes"):
                return True
            if lowered in ("false", "0", "no"):
                return False
            raise _Problem(
                HTTPStatus.BAD_REQUEST,
                f"query parameter {name!r} must be true or false, not {text!r}",
            )
        allowed = schema.get("enum")
        if allowed is not None and text not in allowed:
            raise _Problem(
                HTTPStatus.BAD_REQUEST,
                f"query parameter {name!r} must be one of: "
                f"{', '.join(str(one) for one in allowed)}",
            )
        return text

    def _if_match(self) -> str | None:
        raw = self.headers.get("If-Match")
        return None if raw is None else _etag_value(raw)

    def _idempotency_key(self) -> str | None:
        raw = self.headers.get("Idempotency-Key")
        return raw.strip() if raw is not None and raw.strip() else None

    # -- writing the response ------------------------------------------------
    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(
        self,
        status: HTTPStatus,
        payload: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._send_bytes(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            headers,
        )

    def _send_problem(self, problem: _Problem) -> None:
        payload: dict[str, Any] = {
            "type": routes.error_type(int(problem.status)),
            "title": problem.status.phrase,
            "status": int(problem.status),
            "detail": problem.detail,
            # RFC 9457 §3.1.4: which occurrence this is. The agent driving this
            # server has no trace context to correlate by — one process, no
            # outbound calls — so the request target is the correlation handle.
            "instance": self.path,
        }
        if problem.diagnostics is not None:
            payload["diagnostics"] = problem.diagnostics
        # The request body may not have been read (413, 401, ...), which would
        # desynchronise a kept-alive connection; close it instead.
        self.close_connection = True
        try:
            self._send_bytes(
                problem.status,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/problem+json; charset=utf-8",
                problem.headers,
            )
        except OSError:  # the client has gone; nothing to tell it
            pass

    # -- the served pages ----------------------------------------------------
    def _h_page(self, request: _Request) -> None:
        filename, content_type, inject = PAGES[self.path.split("?", 1)[0]]
        text = (resources.files("fivee_sim.web") / "static" / filename).read_text(
            encoding="utf-8"
        )
        if inject and CONFIG_MARKER in text:
            # The version travels with the launch configuration rather than
            # being fetched: it is a fact about this launch, it is wanted
            # before any request completes, and the page is a static asset no
            # release step rewrites, so being told is the only way it can know.
            config = (
                f"window.__FIVEE_EDITOR__ = "
                f"{{token: {json.dumps(self.engine.token)}, "
                f"apiBase: {json.dumps(API_PREFIX)}, "
                f"version: {json.dumps(__version__)}}};"
            )
            text = text.replace(CONFIG_MARKER, config)
        self._send_bytes(HTTPStatus.OK, text.encode("utf-8"), content_type)

    # -- the server itself ---------------------------------------------------
    def _h_ping(self, request: _Request) -> None:
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "version": __version__,
                "api": API_PREFIX,
                "maps_dir": str(self.engine.maps_dir),
                "replays_dir": str(self.engine.replays_dir),
            },
        )

    def _h_operations(self, request: _Request) -> None:
        self._send_json(HTTPStatus.OK, openapi.operations_index())

    def _h_openapi(self, request: _Request) -> None:
        self._send_json(HTTPStatus.OK, openapi.document())

    def _h_shutdown(self, request: _Request) -> None:
        self.close_connection = True
        self._send_json(HTTPStatus.ACCEPTED, {"stopping": True})
        # shutdown() blocks until serve_forever returns, so it must run on a
        # thread that is not serving; the response above is already written.
        threading.Thread(target=self.engine.shutdown, daemon=True).start()

    # -- dice: a result resource, returned rather than stored -----------------
    def _h_dice_roll(self, request: _Request) -> None:
        body = request.body
        self._send_json(
            HTTPStatus.OK,
            primitives.roll(
                self.state,
                body["expression"],
                body["advantage"],
                body["seed"],
                body["encounter_id"],
                self._idempotency_key(),
                body["label"],
            ),
        )

    def _h_dice_check(self, request: _Request) -> None:
        body = request.body
        self._send_json(
            HTTPStatus.OK,
            primitives.check(
                self.state,
                body["modifier"],
                body["dc"],
                body["advantage"],
                body["seed"],
                body["encounter_id"],
                self._idempotency_key(),
                body["ability"],
                body["skill"],
            ),
        )

    def _h_dice_save(self, request: _Request) -> None:
        body = request.body
        self._send_json(
            HTTPStatus.OK,
            primitives.save(
                self.state,
                body["modifier"],
                body["dc"],
                body["advantage"],
                body["auto_fail"],
                body["seed"],
                body["encounter_id"],
                self._idempotency_key(),
                body["ability"],
            ),
        )

    # -- rules and catalog ---------------------------------------------------
    def _h_rules_lookup(self, request: _Request) -> None:
        self._send_json(
            HTTPStatus.OK, primitives.lookup_rule(self.state, request.query["topic"])
        )

    def _h_catalog_search(self, request: _Request) -> None:
        query = request.query
        self._send_json(
            HTTPStatus.OK,
            catalog_service.search(
                sessions.active_registry(self.state),
                query["query"],
                query["kind"],
                query["simulation"],
                since=query["since"],
                limit=query["limit"],
            ),
        )

    def _h_catalog_get(self, request: _Request) -> None:
        self._send_json(
            HTTPStatus.OK,
            catalog_service.get_record(sessions.active_registry(self.state), request.id),
        )

    def _h_catalog_table(self, request: _Request) -> None:
        self._send_json(
            HTTPStatus.OK,
            catalog_service.get_table(
                sessions.active_registry(self.state),
                request.id,
                since=request.query["since"],
                limit=request.query["limit"],
            ),
        )

    # -- content -------------------------------------------------------------
    def _h_content_status(self, request: _Request) -> None:
        self._send_json(HTTPStatus.OK, content_ops.status(self.state))

    def _h_content_validate(self, request: _Request) -> None:
        body = request.body
        self._send_json(
            HTTPStatus.OK, content_ops.validate(self.state, body["paths"], body["builtin"])
        )

    def _h_content_configure(self, request: _Request) -> None:
        body = request.body
        self._send_json(
            HTTPStatus.OK,
            content_ops.configure(self.state, body["paths"], body["builtin"], body["add"]),
        )

    # -- analytics: also a result resource -----------------------------------
    def _h_analytics_rounds(self, request: _Request) -> None:
        body = request.body
        self._send_json(
            HTTPStatus.OK,
            analytics_service.simulate_rounds(
                self.state,
                body["combatants"],
                body["iterations"],
                body["seed"],
                body["max_rounds"],
                body["movement_rule"],
                body["map"],
                body["map_id"],
            ),
        )

    def _h_analytics_dpr(self, request: _Request) -> None:
        body = request.body
        self._send_json(
            HTTPStatus.OK,
            analytics_service.simulate_dpr(
                self.state,
                body["build"],
                body["target_ac"],
                body["rounds"],
                body["iterations"],
                body["seed"],
                body["distance"],
            ),
        )

    def _h_analytics_scenario_timing(self, request: _Request) -> None:
        body = request.body
        self._send_json(
            HTTPStatus.OK,
            primitives.scenario_timing(
                body["distance_feet"],
                body["speed_feet"],
                body["dash"],
                body["start_delay_rounds"],
                body["response_after_rounds"],
            ),
        )

    # -- encounters ----------------------------------------------------------
    def _encounter_etag(self, encounter_id: str) -> dict[str, str]:
        """The journal chain head as an ETag — the version a write must match."""
        session = self.state.sessions.get(encounter_id)
        if session is None or not session.journal_head:
            return {}
        return {"ETag": _etag_of(session.journal_head)}

    def _check_encounter_version(self, encounter_id: str) -> None:
        """Refuse a write whose ``If-Match`` is not this fight's journal head.

        Optional, unlike a map's: a caller who sends none is taking the engine's
        word for where the fight is, which is the ordinary case for the one
        agent driving it. Sent and stale, the write is refused with the service
        layer's own words rather than merged — two divergent copies of one fight
        produce a journal that replays as neither.
        """
        given = self._if_match()
        if given is None or given == "*":
            return
        session = sessions.session_for(self.state, encounter_id)
        if session.journal_head != given:
            raise StaleWriteError(
                f"encounter {encounter_id!r}",
                expected=given,
                current=session.journal_head or None,
            )

    def _h_encounter_list(self, request: _Request) -> None:
        self._send_json(
            HTTPStatus.OK,
            encounter_service.list_encounters(self.state, request.query["status"]),
        )

    def _h_encounter_create(self, request: _Request) -> None:
        body = request.body
        result = encounter_service.create(
            self.state,
            body["combatants"],
            body["seed"],
            body["movement_rule"],
            body["map"],
            body["map_id"],
            self._idempotency_key(),
        )
        encounter_id = str(result["encounter_id"])
        self._send_json(
            HTTPStatus.CREATED,
            result,
            headers={
                "Location": f"{API_PREFIX}/encounters/{quote(encounter_id, safe='')}",
                **self._encounter_etag(encounter_id),
            },
        )

    def _h_encounter_state(self, request: _Request) -> None:
        result = encounter_service.state_of(self.state, request.id)
        self._send_json(HTTPStatus.OK, result, headers=self._encounter_etag(request.id))

    def _h_encounter_log(self, request: _Request) -> None:
        query = request.query
        self._send_json(
            HTTPStatus.OK,
            encounter_service.event_log(
                self.state,
                request.id,
                query["since"],
                query["limit"],
                query["include_actions"],
            ),
            headers=self._encounter_etag(request.id),
        )

    def _h_encounter_act(self, request: _Request) -> None:
        body = request.body
        self._check_encounter_version(request.id)
        result = encounter_service.act(
            self.state,
            request.id,
            body["kind"],
            body["target"],
            body["attack"],
            body["item"],
            body["spell"],
            body["slot_level"],
            body["to_position"],
            body["targets"],
            body["center"],
            body["direction"],
            body["toward"],
            body["path"],
            body["feature"],
            body["set_open"],
            body["to_level"],
            body["movement_mode"],
            body["as_bonus_action"],
            self._idempotency_key(),
        )
        self._send_json(HTTPStatus.OK, result, headers=self._encounter_etag(request.id))

    def _h_encounter_advance(self, request: _Request) -> None:
        self._check_encounter_version(request.id)
        result = encounter_service.advance(self.state, request.id, self._idempotency_key())
        self._send_json(HTTPStatus.OK, result, headers=self._encounter_etag(request.id))

    def _h_encounter_note(self, request: _Request) -> None:
        body = request.body
        self._check_encounter_version(request.id)
        result = encounter_service.note(
            self.state, request.id, body["text"], body["category"], self._idempotency_key()
        )
        self._send_json(HTTPStatus.CREATED, result, headers=self._encounter_etag(request.id))

    def _h_encounter_resume(self, request: _Request) -> None:
        result = encounter_service.resume(self.state, request.id)
        self._send_json(HTTPStatus.OK, result, headers=self._encounter_etag(request.id))

    def _h_encounter_finalize(self, request: _Request) -> None:
        self._check_encounter_version(request.id)
        result = encounter_service.finalize(self.state, request.id, self.engine.viewer_link)
        self._send_json(HTTPStatus.OK, result, headers=self._encounter_etag(request.id))

    def _h_encounter_replay(self, request: _Request) -> None:
        body = request.body
        self._send_json(
            HTTPStatus.OK,
            map_ops.replay_export(
                self.state,
                request.id,
                body["path"],
                body["embed"],
                body["format_version"],
                self.engine.viewer_link,
            ),
        )

    # -- maps: files, addressed by id ----------------------------------------
    def _h_map_list(self, request: _Request) -> None:
        self._send_json(HTTPStatus.OK, map_ops.map_list(self.state))

    def _h_map_generate(self, request: _Request) -> None:
        body = request.body
        self._send_json(
            HTTPStatus.OK,
            map_ops.generate(
                self.state,
                body["kind"],
                body["params"],
                body["seed"],
                body["name"],
                body["save_as"],
            ),
        )

    def _h_map_render(self, request: _Request) -> None:
        body = request.body
        self._send_json(
            HTTPStatus.OK,
            map_ops.render(
                self.state,
                body["map_id"],
                body["document"],
                body["x"],
                body["y"],
                body["width"],
                body["height"],
                body["downsample"],
                body["show_features"],
                body["show_elevation"],
                body["level"],
                body["encounter_id"],
            ),
        )

    def _h_map_query(self, request: _Request) -> None:
        body = request.body
        self._send_json(
            HTTPStatus.OK,
            map_ops.query_map(
                self.state,
                body["map_id"],
                body["document"],
                body["query"],
                body["frm"],
                body["to"],
                body["level"],
            ),
        )

    def _h_map_uvtt(self, request: _Request) -> None:
        body = request.body
        self._send_json(
            HTTPStatus.OK,
            map_ops.uvtt_export(
                self.state,
                body["map_id"],
                body["document"],
                body["path"],
                body["pixels_per_grid"],
                body["include_image"],
                body["level"],
                body["open_features"],
            ),
        )

    def _h_map_validate(self, request: _Request) -> None:
        diagnostics = validate_document(
            request.body, source="request", terrain=map_ops.terrain_of(self.state)
        )
        errors = [d.as_dict() for d in diagnostics if d.severity is Severity.ERROR]
        warnings = [d.as_dict() for d in diagnostics if d.severity is Severity.WARNING]
        self._send_json(
            HTTPStatus.OK, {"ok": not errors, "errors": errors, "warnings": warnings}
        )

    def _h_map_get(self, request: _Request) -> None:
        found = map_ops.get_map(self.state, request.id)
        self._send_json(
            HTTPStatus.OK, found["document"], headers={"ETag": _etag_of(str(found["sha256"]))}
        )

    def _h_map_put(self, request: _Request) -> None:
        map_id = request.id
        if_match = self.headers.get("If-Match")
        if if_match is None:
            raise _Problem(
                HTTPStatus.PRECONDITION_REQUIRED,
                "If-Match is required: the sha256 ETag from the last GET, or * "
                "to create a new map",
            )
        expected = _etag_value(if_match)
        creating = map_id not in map_service.index(self.engine.maps_dir)
        if creating and expected != "*":
            raise _Problem(
                HTTPStatus.CONFLICT,
                f"there is no saved map {map_id!r} to match against; use "
                f"If-Match: * to create it",
            )
        saved = map_ops.save_map(self.state, map_id, request.body, expected_sha256=expected)
        self._send_json(
            HTTPStatus.CREATED if creating else HTTPStatus.OK,
            saved,
            headers={"ETag": _etag_of(str(saved["sha256"]))},
        )

    def _h_map_edit(self, request: _Request) -> None:
        result = map_ops.edit(
            self.state,
            request.id,
            request.body["operations"],
            expected_sha256=self._if_match(),
        )
        self._send_json(
            HTTPStatus.OK, result, headers={"ETag": _etag_of(str(result["sha256"]))}
        )

    # -- replays: read-only, and that asymmetry is the contract --------------
    def _h_replay_list(self, request: _Request) -> None:
        index = self.engine.replay_index()
        self._send_json(
            HTTPStatus.OK, {"replays": [index[replay_id] for replay_id in sorted(index)]}
        )

    def _h_replay_get(self, request: _Request) -> None:
        index = self.engine.replay_index()
        entry = index.get(request.id)
        if entry is None:
            known = ", ".join(sorted(index)) or "none"
            raise _Problem(
                HTTPStatus.NOT_FOUND, f"no replay {request.id!r}; replays here: {known}"
            )
        # A listing shows a broken bundle; a load refuses it. The 422 is what
        # tells the user *which* file is broken, which is the whole reason the
        # listing does not silently drop it.
        bundle = replay_service.load_bundle_file(Path(str(entry["path"])))
        self._send_json(
            HTTPStatus.OK, bundle, headers={"ETag": _etag_of(str(entry["sha256"]))}
        )

    def _h_replay_validate(self, request: _Request) -> None:
        self._send_json(HTTPStatus.OK, map_ops.replay_validate(request.body["bundle"]))


_RouteHandler = Callable[["_Handler", _Request], None]

#: ``Route.handler`` to the method that answers it. Written out rather than
#: resolved by name so the join is typed, and pinned by a test that fails if
#: the table and this registry ever name different things.
_HANDLERS: dict[str, _RouteHandler] = {
    "page": _Handler._h_page,
    "ping": _Handler._h_ping,
    "operations": _Handler._h_operations,
    "openapi": _Handler._h_openapi,
    "shutdown": _Handler._h_shutdown,
    "dice_roll": _Handler._h_dice_roll,
    "dice_check": _Handler._h_dice_check,
    "dice_save": _Handler._h_dice_save,
    "rules_lookup": _Handler._h_rules_lookup,
    "catalog_search": _Handler._h_catalog_search,
    "catalog_get": _Handler._h_catalog_get,
    "catalog_table": _Handler._h_catalog_table,
    "content_status": _Handler._h_content_status,
    "content_validate": _Handler._h_content_validate,
    "content_configure": _Handler._h_content_configure,
    "analytics_rounds": _Handler._h_analytics_rounds,
    "analytics_dpr": _Handler._h_analytics_dpr,
    "analytics_scenario_timing": _Handler._h_analytics_scenario_timing,
    "encounter_list": _Handler._h_encounter_list,
    "encounter_create": _Handler._h_encounter_create,
    "encounter_state": _Handler._h_encounter_state,
    "encounter_log": _Handler._h_encounter_log,
    "encounter_act": _Handler._h_encounter_act,
    "encounter_advance": _Handler._h_encounter_advance,
    "encounter_note": _Handler._h_encounter_note,
    "encounter_resume": _Handler._h_encounter_resume,
    "encounter_finalize": _Handler._h_encounter_finalize,
    "encounter_replay": _Handler._h_encounter_replay,
    "map_list": _Handler._h_map_list,
    "map_generate": _Handler._h_map_generate,
    "map_render": _Handler._h_map_render,
    "map_query": _Handler._h_map_query,
    "map_uvtt": _Handler._h_map_uvtt,
    "map_validate": _Handler._h_map_validate,
    "map_get": _Handler._h_map_get,
    "map_put": _Handler._h_map_put,
    "map_edit": _Handler._h_map_edit,
    "replay_list": _Handler._h_replay_list,
    "replay_get": _Handler._h_replay_get,
    "replay_validate": _Handler._h_replay_validate,
}

_unhandled = sorted({route.handler for route in routes.ROUTES} - set(_HANDLERS))
if _unhandled:  # pragma: no cover - a startup assertion, not a branch to take
    raise RuntimeError(f"routed operations with no handler: {', '.join(_unhandled)}")
