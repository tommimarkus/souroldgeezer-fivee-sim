"""The localhost REST server behind the interactive map editor.

A stdlib :class:`~http.server.ThreadingHTTPServer` bound to ``127.0.0.1``,
speaking JSON over ``/api/*`` and serving the editor's static pages. It is an
adapter in exactly the sense the MCP server is one: every endpoint validates
input, calls :mod:`fivee_sim.service.maps`, and serialises the result — no
rules or map logic lives here, and error prose comes verbatim from the service
exception the tool surface would also report.

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
  refused with 413 before being read; map ids resolve strictly to files under
  the maps directory, so a traversal id is simply an unknown map.

Errors are RFC-9457 ``application/problem+json`` objects. A map validation
failure (422) carries the service layer's diagnostics; a bad edit operation
(400) names its operation index in ``detail``, exactly as ``map_edit`` does.
"""

from __future__ import annotations

import json
import re
import secrets
import sys
import threading
import traceback
from collections.abc import Callable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import unquote

from .. import __version__
from ..kernel.grid import TerrainTable
from ..maps import as_payload, serialize, validate_document
from ..service import maps as map_service
from ..service.common import resolve_seed, sha256_of, slugify
from ..service.errors import MapEditError, MapError
from ..validation import Severity

__all__ = ["CONFIG_MARKER", "MAX_BODY_BYTES", "TOKEN_HEADER", "EditorServer"]

#: The header every ``/api/*`` request must carry the per-launch token in.
TOKEN_HEADER = "X-Fivee-Editor-Token"
#: Request bodies above this are refused with 413 before being read.
MAX_BODY_BYTES = 8 * 1024 * 1024
#: The marker the served pages carry where the launch configuration goes. A
#: page is served with this replaced by ``window.__FIVEE_EDITOR__ = {...};``;
#: a page without the marker is served untouched.
CONFIG_MARKER = "/*__EDITOR_CONFIG__*/"

#: What a map id may look like: the ``slugify`` alphabet, nothing else. An id
#: outside this grammar cannot name a file under the maps directory, so it is
#: reported as an unknown map rather than half-resolved.
_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*")

_GENERATE_KEYS = frozenset({"kind", "params", "seed", "name"})
_EDITS_KEYS = frozenset({"operations"})

_STATIC_PAGES: dict[str, tuple[str, str, bool]] = {
    # path -> (filename under static/, content type, inject the config marker)
    "/": ("editor.html", "text/html; charset=utf-8", True),
    "/viewer": ("viewer.html", "text/html; charset=utf-8", True),
    "/assets/renderer.js": ("renderer.js", "text/javascript; charset=utf-8", False),
}


class _Problem(Exception):
    """One request failed; carries everything a problem+json response needs."""

    def __init__(
        self,
        status: HTTPStatus,
        detail: str,
        diagnostics: list[dict[str, Any]] | None = None,
    ) -> None:
        self.status = status
        self.detail = detail
        self.diagnostics = diagnostics
        super().__init__(detail)


def _validation_problem(error: MapError) -> _Problem:
    return _Problem(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        str(error),
        diagnostics=[diagnostic.as_dict() for diagnostic in error.diagnostics],
    )


def _etag_of(sha256: str) -> str:
    return f'"{sha256}"'


def _etag_value(raw: str) -> str:
    """The bare hash inside an ``If-Match`` value, tolerant of ``W/`` and quotes."""
    value = raw.strip()
    if value.startswith("W/"):
        value = value[2:]
    return value.strip('"')


class _EditorHTTPServer(ThreadingHTTPServer):
    """The stdlib server with a back-pointer to its :class:`EditorServer`."""

    daemon_threads = True
    editor: EditorServer

    def handle_error(self, request: Any, client_address: Any) -> None:
        print(f"editor: error handling a request from {client_address}", file=self.editor.log)
        traceback.print_exc(file=self.editor.log)


class EditorServer:
    """The editor's HTTP server: configuration, routing state, and lifecycle.

    Binds ``127.0.0.1`` only, on an ephemeral port by default — :attr:`port`
    reports the one actually bound. ``token`` defaults to a fresh
    ``secrets.token_urlsafe(16)`` per launch. ``log`` is where request lines
    and handler errors go; it defaults to ``sys.stderr`` and must never be
    stdout when the server is spawned by the MCP process, whose stdout is the
    JSON-RPC channel — the launcher passes a logfile there.
    """

    def __init__(
        self,
        *,
        maps_dir: str | Path,
        terrain: TerrainTable,
        port: int = 0,
        token: str | None = None,
        log: TextIO | None = None,
    ) -> None:
        self.maps_dir = Path(maps_dir).expanduser()
        self.terrain = terrain
        self.token = token if token else secrets.token_urlsafe(16)
        self.log = log if log is not None else sys.stderr
        self._httpd = _EditorHTTPServer(("127.0.0.1", port), _Handler)
        self._httpd.editor = self

    @property
    def port(self) -> int:
        """The port actually bound — the answer when 0 (ephemeral) was asked for."""
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def serve_forever(self) -> None:
        """Serve until :meth:`shutdown`. Blocks; run it in a thread to embed."""
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        """Stop :meth:`serve_forever` gracefully. Safe to call more than once."""
        self._httpd.shutdown()

    def close(self) -> None:
        """Release the listening socket."""
        self._httpd.server_close()

    # -- map id resolution ---------------------------------------------------
    def map_index(self) -> dict[str, dict[str, Any]]:
        """Every map under the maps directory, keyed by id.

        An id is the ``slugify`` of the file's stem — the same name
        ``map_save`` writes by default — so the REST surface and the MCP tools
        agree on what a saved map is called. Two files slugifying to the same
        id would collide; the first in path order claims it, mirroring the
        first-wins rule everywhere content merges.
        """
        index: dict[str, dict[str, Any]] = {}
        for entry in map_service.list_maps([self.maps_dir]):
            map_id = slugify(Path(str(entry["path"])).stem)
            if map_id in index:
                continue
            index[map_id] = {"id": map_id, **entry}
        return index

    def path_for_new(self, map_id: str) -> Path:
        """Where a not-yet-existing map id would be stored, containment-checked."""
        target = self.maps_dir / f"{map_id}.json"
        if target.exists() and not target.resolve().is_relative_to(self.maps_dir.resolve()):
            raise _Problem(HTTPStatus.NOT_FOUND, f"no map {map_id!r} under {self.maps_dir}")
        return target


class _Handler(BaseHTTPRequestHandler):
    """One request: host check, token check for ``/api``, route, serialise."""

    protocol_version = "HTTP/1.1"
    server_version = "fivee-sim-editor"
    server: _EditorHTTPServer

    # -- plumbing ------------------------------------------------------------
    @property
    def editor(self) -> EditorServer:
        return self.server.editor

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        print(f"{self.address_string()} - {format % args}", file=self.editor.log)

    def do_GET(self) -> None:  # noqa: N802 - stdlib dispatch names
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            self._route(method)
        except _Problem as problem:
            self._send_problem(problem)
        except Exception as error:  # a handler bug must not kill the server
            print(f"editor: unhandled error serving {self.path}", file=self.editor.log)
            traceback.print_exc(file=self.editor.log)
            self._send_problem(
                _Problem(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(error).__name__}: {error}")
            )

    def _route(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        self._check_host()
        if path.startswith("/api/"):
            self._check_token()
        path_matched = False
        for route_method, pattern, handler in _ROUTES:
            match = pattern.fullmatch(path)
            if match is None:
                continue
            path_matched = True
            if route_method != method:
                continue
            handler(self, match)
            return
        if path_matched:
            raise _Problem(
                HTTPStatus.METHOD_NOT_ALLOWED, f"{method} is not supported on {path}"
            )
        raise _Problem(HTTPStatus.NOT_FOUND, f"no route for {path}")

    def _check_host(self) -> None:
        host = self.headers.get("Host", "")
        name = host.rsplit(":", 1)[0] if ":" in host else host
        if name not in ("127.0.0.1", "localhost"):
            raise _Problem(
                HTTPStatus.FORBIDDEN,
                f"host {host!r} is not this editor; it answers only as "
                f"127.0.0.1 or localhost",
            )

    def _check_token(self) -> None:
        given = self.headers.get(TOKEN_HEADER)
        if given is None or not secrets.compare_digest(given, self.editor.token):
            raise _Problem(
                HTTPStatus.UNAUTHORIZED,
                f"missing or invalid {TOKEN_HEADER} header; the served editor "
                f"page carries the token for this launch",
            )

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

    def _read_json(self) -> Any:
        try:
            return json.loads(self._read_body())
        except json.JSONDecodeError as error:
            raise _Problem(
                HTTPStatus.BAD_REQUEST, f"request body is not valid JSON: {error}"
            ) from None

    def _read_object(self, valid_keys: frozenset[str]) -> dict[str, Any]:
        body = self._read_json()
        if not isinstance(body, dict):
            raise _Problem(
                HTTPStatus.BAD_REQUEST,
                f"request body must be a JSON object with keys from: "
                f"{', '.join(sorted(valid_keys))}",
            )
        unknown = sorted(set(body) - valid_keys)
        if unknown:
            raise _Problem(
                HTTPStatus.BAD_REQUEST,
                f"unknown key(s): {', '.join(repr(key) for key in unknown)}. "
                f"Valid keys: {', '.join(sorted(valid_keys))}",
            )
        return body

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
            "type": "about:blank",
            "title": problem.status.phrase,
            "status": int(problem.status),
            "detail": problem.detail,
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
            )
        except OSError:  # the client has gone; nothing to tell it
            pass

    # -- ids and documents ---------------------------------------------------
    def _map_id(self, match: re.Match[str]) -> str:
        map_id = unquote(match.group(1))
        if _ID_PATTERN.fullmatch(map_id) is None:
            # Traversal attempts and other junk land here: nothing outside the
            # id grammar can name a file under the maps directory.
            raise _Problem(HTTPStatus.NOT_FOUND, f"no map {map_id!r}")
        return map_id

    def _entry_for(self, map_id: str) -> dict[str, Any]:
        entry = self.editor.map_index().get(map_id)
        if entry is None:
            known = ", ".join(sorted(self.editor.map_index())) or "none"
            raise _Problem(
                HTTPStatus.NOT_FOUND, f"no map {map_id!r}; maps here: {known}"
            )
        return entry

    def _load(self, path: str) -> Any:
        try:
            return map_service.load_file(path, terrain=self.editor.terrain)
        except MapError as error:
            raise _validation_problem(error) from None

    # -- static pages --------------------------------------------------------
    def _static(self, filename: str, content_type: str, *, inject: bool) -> None:
        text = (resources.files("fivee_sim.editor") / "static" / filename).read_text(
            encoding="utf-8"
        )
        if inject and CONFIG_MARKER in text:
            config = (
                f"window.__FIVEE_EDITOR__ = "
                f'{{token: {json.dumps(self.editor.token)}, apiBase: "/api"}};'
            )
            text = text.replace(CONFIG_MARKER, config)
        self._send_bytes(HTTPStatus.OK, text.encode("utf-8"), content_type)

    def _h_static(self, match: re.Match[str]) -> None:
        filename, content_type, inject = _STATIC_PAGES[match.group(0)]
        self._static(filename, content_type, inject=inject)

    # -- API endpoints -------------------------------------------------------
    def _h_ping(self, match: re.Match[str]) -> None:
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "version": __version__, "maps_dir": str(self.editor.maps_dir)},
        )

    def _h_list_maps(self, match: re.Match[str]) -> None:
        index = self.editor.map_index()
        self._send_json(
            HTTPStatus.OK, {"maps": [index[map_id] for map_id in sorted(index)]}
        )

    def _h_get_map(self, match: re.Match[str]) -> None:
        entry = self._entry_for(self._map_id(match))
        document, _warnings = self._load(str(entry["path"]))
        sha256 = sha256_of(serialize(document))
        self._send_json(
            HTTPStatus.OK, as_payload(document), headers={"ETag": _etag_of(sha256)}
        )

    def _h_put_map(self, match: re.Match[str]) -> None:
        map_id = self._map_id(match)
        body = self._read_json()
        if_match = self.headers.get("If-Match")
        if if_match is None:
            raise _Problem(
                HTTPStatus.PRECONDITION_REQUIRED,
                "If-Match is required: the sha256 ETag from the last GET, or * "
                "to create a new map",
            )
        try:
            document, warnings = map_service.parse_payload(
                body, source=map_id, terrain=self.editor.terrain
            )
        except MapError as error:
            raise _validation_problem(error) from None

        entry = self.editor.map_index().get(map_id)
        creating = entry is None
        if creating:
            if _etag_value(if_match) != "*":
                raise _Problem(
                    HTTPStatus.CONFLICT,
                    f"there is no saved map {map_id!r} to match against; use "
                    f"If-Match: * to create it",
                )
            target = self.editor.path_for_new(map_id)
        else:
            assert entry is not None
            target = Path(str(entry["path"]))
            if _etag_value(if_match) != "*":
                current = self._current_sha(target)
                if _etag_value(if_match) != current:
                    raise _Problem(
                        HTTPStatus.CONFLICT,
                        f"the saved map {map_id!r} has changed since it was read "
                        f"(its sha256 is now {current}); GET it again and reapply",
                    )
        try:
            saved = map_service.save_file(document, target, overwrite=True)
        except OSError as error:
            raise _Problem(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"cannot write {target}: {error}"
            ) from None
        self._send_json(
            HTTPStatus.CREATED if creating else HTTPStatus.OK,
            {
                "saved": True,
                "id": map_id,
                "sha256": saved["sha256"],
                "warnings": [warning.as_dict() for warning in warnings],
                "provenance": as_payload(document)["provenance"],
            },
            headers={"ETag": _etag_of(str(saved["sha256"]))},
        )

    def _current_sha(self, path: Path) -> str:
        """The saved file's canonical sha — falling back to raw bytes when the
        file no longer parses, so an If-Match against it still gets a truthful
        mismatch rather than a validation error for a document the client is
        trying to replace."""
        try:
            document, _warnings = map_service.load_file(path, terrain=self.editor.terrain)
        except MapError:
            try:
                return sha256_of(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                return "unreadable"
        return sha256_of(serialize(document))

    def _h_post_edits(self, match: re.Match[str]) -> None:
        map_id = self._map_id(match)
        body = self._read_object(_EDITS_KEYS)
        operations = body.get("operations")
        if not isinstance(operations, list):
            raise _Problem(
                HTTPStatus.BAD_REQUEST, "'operations' must be a list of edit operations"
            )
        entry = self._entry_for(map_id)
        document, _warnings = self._load(str(entry["path"]))
        try:
            edited = map_service.apply_edits(
                document, operations, terrain=self.editor.terrain
            )
        except MapEditError as error:
            raise _Problem(HTTPStatus.BAD_REQUEST, str(error)) from None
        except MapError as error:
            raise _validation_problem(error) from None
        saved = map_service.save_file(edited, str(entry["path"]), overwrite=True)
        self._send_json(
            HTTPStatus.OK,
            {
                "saved": True,
                "id": map_id,
                "sha256": saved["sha256"],
                "edited": edited.provenance.edited,
                "document": as_payload(edited),
            },
            headers={"ETag": _etag_of(str(saved["sha256"]))},
        )

    def _h_generate(self, match: re.Match[str]) -> None:
        body = self._read_object(_GENERATE_KEYS)
        kind = body.get("kind")
        if not isinstance(kind, str):
            raise _Problem(
                HTTPStatus.BAD_REQUEST, "'kind' must name a generator: caves, dungeon, overland"
            )
        params = body.get("params")
        if params is not None and not isinstance(params, dict):
            raise _Problem(HTTPStatus.BAD_REQUEST, "'params' must be an object")
        seed = body.get("seed")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise _Problem(HTTPStatus.BAD_REQUEST, "'seed' must be a whole number")
        name = body.get("name")
        if name is not None and not isinstance(name, str):
            raise _Problem(HTTPStatus.BAD_REQUEST, "'name' must be text")
        used = resolve_seed(seed)
        try:
            document = map_service.generate(kind, params, used, name=name)
        except MapError as error:
            raise _validation_problem(error) from None
        except ValueError as error:
            raise _Problem(HTTPStatus.BAD_REQUEST, str(error)) from None
        # Deliberately not persisted: the page reviews the result and saves the
        # keeper with PUT, exactly as map_generate hands off to map_save.
        self._send_json(
            HTTPStatus.OK,
            {"seed": used, "name": document.name, "document": as_payload(document)},
        )

    def _h_validate(self, match: re.Match[str]) -> None:
        body = self._read_json()
        diagnostics = validate_document(body, source="request", terrain=self.editor.terrain)
        errors = [d.as_dict() for d in diagnostics if d.severity is Severity.ERROR]
        warnings = [d.as_dict() for d in diagnostics if d.severity is Severity.WARNING]
        self._send_json(
            HTTPStatus.OK, {"ok": not errors, "errors": errors, "warnings": warnings}
        )

    def _h_shutdown(self, match: re.Match[str]) -> None:
        self._read_body()
        self.close_connection = True
        self._send_json(HTTPStatus.ACCEPTED, {"stopping": True})
        # shutdown() blocks until serve_forever returns, so it must run on a
        # thread that is not serving; the response above is already written.
        threading.Thread(target=self.editor.shutdown, daemon=True).start()


_RouteHandler = Callable[[_Handler, "re.Match[str]"], None]
_ROUTES: tuple[tuple[str, re.Pattern[str], _RouteHandler], ...] = (
    ("GET", re.compile(r"/"), _Handler._h_static),
    ("GET", re.compile(r"/viewer"), _Handler._h_static),
    ("GET", re.compile(r"/assets/renderer\.js"), _Handler._h_static),
    ("GET", re.compile(r"/api/ping"), _Handler._h_ping),
    ("GET", re.compile(r"/api/maps"), _Handler._h_list_maps),
    ("GET", re.compile(r"/api/maps/([^/]+)"), _Handler._h_get_map),
    ("PUT", re.compile(r"/api/maps/([^/]+)"), _Handler._h_put_map),
    ("POST", re.compile(r"/api/maps/([^/]+)/edits"), _Handler._h_post_edits),
    ("POST", re.compile(r"/api/generate"), _Handler._h_generate),
    ("POST", re.compile(r"/api/validate"), _Handler._h_validate),
    ("POST", re.compile(r"/api/shutdown"), _Handler._h_shutdown),
)
