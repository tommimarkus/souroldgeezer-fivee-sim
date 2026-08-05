"""``argv`` to one HTTP call, with the command list read off the live server.

The whole design goal is that an agent can drive this having read nothing. Four
properties carry that, and each is a decision rather than a convenience:

**Nothing has to be started.** Every command ensures a server first — find the
state file, ping it, spawn one if nothing answers — so there is no ordering to
get wrong. ``fivee serve`` and ``fivee stop`` exist for explicit control, and
``serve`` against a running server reports its URL instead of starting a second.

**Nothing has to be memorised.** ``fivee help`` renders
``GET /api/v1/operations`` and ``fivee help <operation>`` reads that operation's
parameters out of ``GET /api/v1/openapi.json``. Both come from the server that
will answer the call, so the help cannot describe an operation that is not
routed, or miss one that is. It also means the types a flag is coerced to are
the types the server publishes: a query parameter cannot be documented as an
integer here and sent as a string.

**Arguments compose.** ``--flag value`` is derived from the declared parameters,
``--json '{...}'`` (or ``--json -`` for stdin) supplies a whole object for the
nested payloads — a creature spec, a map document — that no flag grammar should
try to spell. Given both, ``--json`` is the base and flags override its keys, so
"the same fight with one thing changed" is an edit to the command line rather
than to the JSON.

**A failure is visible without being parsed.** Results are JSON on stdout and
nothing else, so ``$(fivee ...)`` is always either a parseable document or
empty. Prose, warnings, and refusals go to stderr. And the exit code separates
the four failures that have four different fixes: :data:`EXIT_USAGE` means the
command was wrong, :data:`EXIT_REFUSED` that the engine said no,
:data:`EXIT_FAULT` that the engine broke, :data:`EXIT_UNREACHABLE` that nothing
answered. Collapsing any pair of those would leave a caller unable to tell
"retry" from "fix the command" from "read the log".

``--json-errors`` swaps the human line for the raw problem object, still on
stderr. Deliberately not stdout: a caller capturing stdout must never receive
something shaped like a result that is actually an error.
"""

from __future__ import annotations

import difflib
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from . import http
from .discovery import (
    API_PREFIX,
    Server,
    UnreachableError,
    ensure_server,
    state_path_for,
)
from .discovery import stop as stop_server
from .http import ProblemError

__all__ = [
    "EXIT_FAULT",
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_UNREACHABLE",
    "EXIT_USAGE",
    "UsageError",
    "main",
]

#: The command ran and the engine answered.
EXIT_OK = 0
#: The command line was wrong: an unknown operation, an unknown flag, a missing
#: required argument, malformed ``--json``. Nothing was sent.
EXIT_USAGE = 2
#: The engine refused (4xx). The request was understood and declined; the
#: problem's ``detail`` says what to change.
EXIT_REFUSED = 3
#: The engine failed (5xx). Not the caller's to fix — read the server log.
EXIT_FAULT = 4
#: No server answered and none could be started.
EXIT_UNREACHABLE = 5

#: JSON type names to the phrase help uses for them. The same words the server
#: refuses with, so "must be a whole number" and "a whole number" line up.
_TYPE_WORDS: Mapping[str, str] = {
    "string": "text",
    "integer": "a whole number",
    "number": "a number",
    "boolean": "true or false",
    "array": "a list",
    "object": "an object",
    "null": "null",
}


class UsageError(Exception):
    """The command line was wrong, so nothing was sent."""


@dataclass(frozen=True)
class Param:
    """One declared input that is not the request body."""

    name: str
    location: str
    required: bool
    schema: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Operation:
    """One operation as the live server describes it."""

    name: str
    method: str
    path: str
    summary: str
    params: tuple[Param, ...] = ()
    #: The request body schema, or ``None`` where the operation reads no body.
    body: Mapping[str, Any] | None = None

    @property
    def properties(self) -> Mapping[str, Any]:
        if self.body is None:
            return {}
        found: Mapping[str, Any] = self.body.get("properties", {})
        return found

    @property
    def body_required(self) -> tuple[str, ...]:
        if self.body is None:
            return ()
        return tuple(str(name) for name in self.body.get("required", []))

    @property
    def free_body(self) -> bool:
        """True where the body is "any JSON" — a map document, a replay bundle.

        The schema declares no properties because validating the format belongs
        to the layer that owns it, so there is no flag grammar to derive and
        ``--json`` is how the body arrives.
        """
        return self.body is not None and "properties" not in self.body


def _flag(name: str) -> str:
    """``movement_rule`` and ``If-Match`` both to the flag that sets them."""
    return "--" + name.replace("_", "-").casefold()


def _key(text: str) -> str:
    """A flag or declared name reduced to what they are compared on.

    ``--if-match``, ``--If-Match`` and the declared ``If-Match`` all become
    ``if_match``; ``--movement-rule`` and ``movement_rule`` both become
    ``movement_rule``. So neither spelling of a separator is wrong.
    """
    return text.strip().lstrip("-").replace("-", "_").casefold()


def _type_names(schema: Mapping[str, Any]) -> tuple[str, ...]:
    declared = schema.get("type")
    if declared is None:
        return ()
    if isinstance(declared, str):
        return (declared,)
    return tuple(str(name) for name in declared)


def _type_phrase(schema: Mapping[str, Any]) -> str:
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return "one of: " + ", ".join(str(one) for one in enum)
    words = [_TYPE_WORDS.get(name, name) for name in _type_names(schema)]
    if not words:
        return "any JSON"
    if len(words) == 1:
        return words[0]
    return f"{', '.join(words[:-1])} or {words[-1]}"


def _value_for(name: str, schema: Mapping[str, Any], given: str | bool) -> Any:
    """One flag's value, whether it was written with one or left bare.

    A bare flag means true, and only for an argument that takes true or false.
    Left unchecked it meant ``"True"`` for a string argument too — which is how
    ``fivee map.put --if-match --json -`` came to send the literal text ``True``
    as a map's sha256 and get back a stale-write refusal quoting it. A flag
    whose value was swallowed by the next flag has to be a usage error, because
    the alternative is a plausible-looking request nobody wrote.
    """
    if not isinstance(given, bool):
        return _coerce(name, schema, given)
    if "boolean" in _type_names(schema):
        return given
    raise UsageError(
        f"{_flag(name)} needs a value ({_type_phrase(schema)}); a bare "
        f"{_flag(name)} means true, and only for a true/false argument"
    )


def _coerce(name: str, schema: Mapping[str, Any], text: str) -> Any:
    """One flag's text as the type the contract declares for it.

    The order matters where a schema admits several types. ``null`` first so an
    explicit ``null`` clears an optional value; then structured JSON, but only
    when the text actually looks structured, so a ``toward`` that accepts both
    a creature name and a point still takes the name; then text, since a schema
    that admits a string admits this one; then the scalars.
    """
    names = _type_names(schema)
    if not names:
        return text
    stripped = text.strip()
    if "null" in names and stripped == "null":
        return None
    if ("array" in names or "object" in names) and stripped[:1] in ("[", "{"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as error:
            raise UsageError(f"{_flag(name)} is not valid JSON: {error}") from None
    if "string" in names:
        return text
    if "array" in names or "object" in names:
        raise UsageError(
            f"{_flag(name)} takes {_type_phrase(schema)}; write it as JSON, "
            f"for example {_flag(name)} '[1, 2]'"
        )
    if "boolean" in names:
        lowered = stripped.casefold()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
        raise UsageError(f"{_flag(name)} must be true or false, not {text!r}")
    if "integer" in names:
        try:
            return int(stripped)
        except ValueError:
            raise UsageError(
                f"{_flag(name)} must be a whole number, not {text!r}"
            ) from None
    if "number" in names:
        try:
            return float(stripped)
        except ValueError:
            raise UsageError(f"{_flag(name)} must be a number, not {text!r}") from None
    return text


# --- the contract, read off the live server ---------------------------------
class Contract:
    """What this server can do, fetched from this server.

    Two documents, both lazily: the operations index names every operation and
    its request line, and the OpenAPI document types every argument. They are
    rendered from one route table by one module, so they cannot disagree — and
    fetching them rather than shipping a copy is why ``fivee help`` cannot go
    stale against the server it is about to call.
    """

    def __init__(self, server: Server) -> None:
        self.server = server
        self._index: dict[str, Any] | None = None
        self._document: dict[str, Any] | None = None

    @property
    def index(self) -> dict[str, Any]:
        if self._index is None:
            self._index = http.request(
                self.server, "GET", f"{API_PREFIX}/operations"
            ).body
        return self._index

    @property
    def document(self) -> dict[str, Any]:
        if self._document is None:
            self._document = http.request(
                self.server, "GET", f"{API_PREFIX}/openapi.json"
            ).body
        return self._document

    @property
    def entries(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = self.index.get("operations", [])
        return found

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(str(entry["operation"]) for entry in self.entries)

    def operation(self, name: str) -> Operation:
        entry = next(one for one in self.entries if one["operation"] == name)
        path, method = str(entry["path"]), str(entry["method"])
        spec = self.document.get("paths", {}).get(path, {}).get(method.lower())
        if spec is None:  # pragma: no cover - one table renders both documents
            raise RuntimeError(
                f"the server lists {name} at {method} {path} but its OpenAPI "
                f"document has no such operation; the two have drifted apart"
            )
        body = spec.get("requestBody")
        return Operation(
            name=name,
            method=method,
            path=path,
            summary=str(entry.get("summary", "")),
            params=tuple(
                Param(
                    name=str(one["name"]),
                    location=str(one["in"]),
                    required=bool(one.get("required")),
                    schema=one.get("schema", {}),
                )
                for one in spec.get("parameters", [])
            ),
            body=None
            if body is None
            else body.get("content", {}).get("application/json", {}).get("schema", {}),
        )


# --- turning tokens into a request ------------------------------------------
@dataclass
class Parsed:
    """The command line, before anything is checked against an operation."""

    flags: dict[str, str | bool] = field(default_factory=dict)
    positional: list[str] = field(default_factory=list)


def _parse_tokens(tokens: Sequence[str]) -> Parsed:
    """``--name value``, ``--name=value``, a bare ``--name``, and positionals.

    A bare flag is one followed by another flag or by nothing, which is what
    lets ``--embed`` mean true without a schema being consulted first. A value
    that itself begins with ``--`` is written ``--name=--value``; a negative
    number needs nothing special, since ``-5`` is not a flag.
    """
    parsed = Parsed()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            parsed.positional += list(tokens[index + 1 :])
            break
        if token.startswith("--"):
            name, separator, value = token[2:].partition("=")
            if not name:
                raise UsageError(f"{token!r} is not a flag")
            if separator:
                parsed.flags[_key(name)] = value
            else:
                following = tokens[index + 1] if index + 1 < len(tokens) else None
                if following is None or following.startswith("--"):
                    parsed.flags[_key(name)] = True
                else:
                    parsed.flags[_key(name)] = following
                    index += 1
        elif token.startswith("-") and len(token) > 1 and not token[1].isdigit():
            raise UsageError(
                f"{token!r} is not a flag; every argument is spelled out, "
                f"like --seed 7"
            )
        else:
            parsed.positional.append(token)
        index += 1
    return parsed


def _json_argument(raw: str | bool) -> dict[str, Any]:
    """``--json``'s payload: a literal object, or stdin when it is ``-``."""
    if isinstance(raw, bool):
        raise UsageError("--json takes a JSON object, or - to read one from stdin")
    text = sys.stdin.read() if raw.strip() == "-" else raw
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise UsageError(f"--json is not valid JSON: {error}") from None
    if not isinstance(payload, dict):
        raise UsageError(f"--json must be a JSON object, not {_json_kind(payload)}")
    return payload


def _json_kind(value: Any) -> str:
    """What a rejected ``--json`` payload actually was, in the help's words."""
    if value is None:
        return _TYPE_WORDS["null"]
    if isinstance(value, bool):
        return _TYPE_WORDS["boolean"]
    if isinstance(value, list):
        return _TYPE_WORDS["array"]
    if isinstance(value, str):
        return _TYPE_WORDS["string"]
    if isinstance(value, (int, float)):
        return _TYPE_WORDS["number"]
    return _TYPE_WORDS["object"]


@dataclass
class Call:
    """One resolved request: everything :func:`fivee_sim.client.http.request` needs."""

    method: str
    path: str
    query: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None


def build_call(operation: Operation, tokens: Sequence[str]) -> Call:
    """Resolve one operation's arguments, or refuse with what is missing.

    Every check here happens before anything is sent, so a command that is
    simply wrong costs no request and gets an answer that names the flag rather
    than a 400 that names the wire field.
    """
    parsed = _parse_tokens(tokens)
    by_key = {_key(param.name): param for param in operation.params}
    properties = operation.properties

    values: dict[str, Any] = {}
    body: dict[str, Any] = {}

    if "json" in parsed.flags:
        payload = _json_argument(parsed.flags.pop("json"))
        if operation.body is None and not by_key:
            raise UsageError(f"{operation.name} takes no arguments, so --json has nowhere to go")
        for name, value in payload.items():
            if operation.free_body:
                # The body *is* the document here, so nothing in it is routed
                # elsewhere: a map whose top-level key happened to be named
                # after a path parameter would otherwise be quietly dismantled.
                body[name] = value
            elif name in properties:
                body[name] = value
            elif _key(name) in by_key:
                values[_key(name)] = value
            elif operation.body is None:
                raise UsageError(
                    f"{operation.name} takes no request body, and {name!r} is not "
                    f"one of its arguments. Try: fivee help {operation.name}"
                )
            else:
                body[name] = value

    for flag, given in parsed.flags.items():
        param = by_key.get(flag)
        if param is not None:
            values[flag] = _value_for(param.name, param.schema, given)
            continue
        declared = next((one for one in properties if _key(one) == flag), None)
        if declared is not None:
            body[declared] = _value_for(declared, properties[declared], given)
            continue
        raise UsageError(_unknown_flag(operation, flag, properties, by_key))

    _fill_positional(operation, parsed, values)
    _require(operation, values, body, by_key)
    return _assemble(operation, values, body)


def _fill_positional(
    operation: Operation, parsed: Parsed, values: dict[str, Any]
) -> None:
    """A bare word fills the next unset path parameter, in declared order.

    ``fivee encounter.state enc-7f3a`` rather than
    ``fivee encounter.state --id enc-7f3a``: an id is the subject of the
    command, and every operation that has one has exactly one.
    """
    open_paths = [
        param for param in operation.params
        if param.location == "path" and _key(param.name) not in values
    ]
    for word in parsed.positional:
        if not open_paths:
            raise UsageError(
                f"{operation.name} has nothing left for {word!r}. "
                f"Try: fivee help {operation.name}"
            )
        values[_key(open_paths.pop(0).name)] = word


def _require(
    operation: Operation,
    values: Mapping[str, Any],
    body: Mapping[str, Any],
    by_key: Mapping[str, Param],
) -> None:
    """Name every required argument that is missing, all of them at once."""
    missing: list[str] = []
    for key, param in by_key.items():
        if (param.required or param.location == "path") and key not in values:
            missing.append(f"{_flag(param.name)} ({_type_phrase(param.schema)})")
    for name in operation.body_required:
        if name not in body:
            missing.append(f"{_flag(name)} ({_type_phrase(operation.properties.get(name, {}))})")
    if missing:
        raise UsageError(
            f"{operation.name} needs {_and_list(missing)}. "
            f"Try: fivee help {operation.name}"
        )


def _assemble(
    operation: Operation, values: Mapping[str, Any], body: Mapping[str, Any]
) -> Call:
    path = operation.path
    query: dict[str, Any] = {}
    headers: dict[str, str] = {}
    for param in operation.params:
        key = _key(param.name)
        if key not in values:
            continue
        value = values[key]
        if param.location == "path":
            path = path.replace(f"{{{param.name}}}", quote(str(value), safe=""))
        elif param.location == "query":
            query[param.name] = value
        else:
            headers[param.name] = str(value)
    return Call(
        method=operation.method,
        path=path,
        query=query,
        headers=headers,
        body=dict(body) if operation.body is not None else None,
    )


def _and_list(items: Sequence[str]) -> str:
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _unknown_flag(
    operation: Operation,
    flag: str,
    properties: Mapping[str, Any],
    by_key: Mapping[str, Param],
) -> str:
    known = [_flag(param.name) for param in by_key.values()] + [
        _flag(name) for name in properties
    ]
    near = difflib.get_close_matches(_flag(flag), known, n=3, cutoff=0.5)
    if near:
        return f"{operation.name} has no {_flag(flag)}. Did you mean {_or_list(near)}?"
    if operation.free_body:
        return (
            f"{operation.name} has no {_flag(flag)}: its body is a whole document, "
            f"so pass it with --json or --json -"
        )
    return (
        f"{operation.name} has no {_flag(flag)}. It takes: "
        f"{', '.join(sorted(known)) or 'no arguments'}"
    )


def _or_list(items: Sequence[str]) -> str:
    if len(items) == 1:
        return items[0]
    return f"{' or '.join([', '.join(items[:-1]), items[-1]])}"


# --- help --------------------------------------------------------------------
def render_index(contract: Contract) -> str:
    """``fivee help``: every operation this server serves, grouped."""
    entries = contract.entries
    width = max((len(str(one["operation"])) for one in entries), default=0)
    lines = [
        "fivee — the 5E-compatible simulation engine, over HTTP",
        f"  server  {contract.server.url}",
        f"  engine  {contract.index.get('version', '?')}   api {contract.index.get('base', '')}",
        "",
        f"{contract.index.get('count', len(entries))} operations. "
        f"Both spellings work: `fivee encounter.act` and `fivee encounter act`.",
        "`fivee help <operation>` gives one operation's arguments and an example.",
        "",
    ]
    group = ""
    for entry in entries:
        name = str(entry["operation"])
        head = name.split(".", 1)[0]
        if head != group:
            group = head
            lines.append(group)
        lines.append(f"  {name:<{width}}  {entry['summary']}")
    lines += [
        "",
        "commands of the client itself",
        "  fivee serve [--port N]        start the engine, or report the running one",
        "  fivee stop                    stop it",
        "  fivee help [<operation>]      this list, or one operation",
        "",
        "flags every command takes",
        "  --json '{...}' | --json -     the request body; --flags override its keys",
        "  --compact                     one-line JSON on stdout",
        "  --json-errors                 the raw problem object on stderr, not a line",
        "",
        "Results are JSON on stdout; everything else is stderr. Exit codes: "
        f"{EXIT_USAGE} bad command, {EXIT_REFUSED} refused, {EXIT_FAULT} server fault, "
        f"{EXIT_UNREACHABLE} unreachable.",
    ]
    return "\n".join(lines)


def render_operation(operation: Operation) -> str:
    """``fivee help <operation>``: what it takes, and something to paste."""
    lines = [f"{operation.name} — {operation.method} {operation.path}"]
    if operation.summary:
        lines.append(operation.summary)
    required: list[tuple[str, str, str]] = []
    optional: list[tuple[str, str, str]] = []
    for param in operation.params:
        row = (_flag(param.name), _type_phrase(param.schema), param.location)
        (required if param.required or param.location == "path" else optional).append(row)
    for name, schema in operation.properties.items():
        note = "body"
        if "default" in schema:
            note = f"body, default {json.dumps(schema['default'])}"
        row = (_flag(name), _type_phrase(schema), note)
        (required if name in operation.body_required else optional).append(row)

    for title, rows in (("required", required), ("optional", optional)):
        if not rows:
            continue
        lines += ["", title]
        width = max(len(row[0]) for row in rows)
        type_width = max(len(row[1]) for row in rows)
        lines += [
            f"  {flag:<{width}}  {phrase:<{type_width}}  {where}"
            for flag, phrase, where in rows
        ]
    if operation.free_body:
        lines += [
            "",
            "body",
            "  a whole JSON document, validated by the engine — pass it with "
            "--json '{...}' or --json -",
        ]
    lines += ["", "example", f"  {_example(operation)}"]
    return "\n".join(lines)


def _example(operation: Operation) -> str:
    """A line to paste. Placeholders are the argument's name in capitals."""
    parts = ["fivee", operation.name]
    payload: dict[str, Any] = {}
    for param in operation.params:
        if param.required or param.location == "path":
            parts += [_flag(param.name), _placeholder(param.name, param.schema)]
    for name in operation.body_required:
        schema = operation.properties.get(name, {})
        names = _type_names(schema)
        if "array" in names:
            payload[name] = []
        elif "object" in names:
            payload[name] = {}
        else:
            parts += [_flag(name), _placeholder(name, schema)]
    if payload:
        parts += ["--json", f"'{json.dumps(payload)}'"]
    elif operation.free_body:
        parts += ["--json", "-"]
    return " ".join(parts)


def _placeholder(name: str, schema: Mapping[str, Any]) -> str:
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return str(enum[0])
    names = _type_names(schema)
    if "integer" in names or "number" in names:
        return "0"
    if "boolean" in names:
        return "true"
    return name.replace("-", "_").upper()


# --- commands -----------------------------------------------------------------
@dataclass
class Options:
    """The flags that belong to the client rather than to any operation."""

    compact: bool = False
    json_errors: bool = False


def _print_json(value: Any, options: Options) -> None:
    if options.compact:
        sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    else:
        sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _note(message: str) -> None:
    sys.stderr.write(f"fivee: {message}\n")


def _lifecycle_note(server: Server) -> str | None:
    """How this server came to be answering, or ``None`` when nothing happened.

    A reload is a start *and* an end, and the end is the half worth saying:
    somebody's running engine was replaced, which is the difference between a
    command that looks slow and a command that threw away a fight. It replaces
    the start line rather than joining it, because two lines about one server
    read as two servers.

    One sentence, written once, because ``serve`` needs the same three cases and
    a second copy of them is how ``serve`` came to call a replacement a start.
    """
    if server.reloaded:
        return f"restarted the engine server at {server.url}; it was running older source"
    if server.spawned:
        return f"started the engine server at {server.url}"
    return None


def _announce(server: Server) -> Server:
    message = _lifecycle_note(server)
    if message is not None:
        _note(message)
    return server


def _serve(tokens: Sequence[str], options: Options) -> int:
    parsed = _parse_tokens(tokens)
    port: int | None = None
    if "port" in parsed.flags:
        given = parsed.flags.pop("port")
        if isinstance(given, bool):
            raise UsageError("--port takes a number")
        try:
            port = int(given)
        except ValueError:
            raise UsageError(f"--port must be a whole number, not {given!r}") from None
    if parsed.flags or parsed.positional:
        raise UsageError("serve takes only --port")
    server = ensure_server(port=port)
    _note(_lifecycle_note(server) or f"already serving at {server.url}")
    _print_json(
        {
            "url": server.url,
            "editor_url": f"{server.url}editor",
            "viewer_url": f"{server.url}viewer",
            "port": server.port,
            "maps_dir": server.maps_dir,
            "replays_dir": server.replays_dir,
            "already_running": not server.spawned,
            # Distinct from the line above, and not derivable from it:
            # `already_running` is False for a cold start and False for a
            # replacement, and only one of those cost the caller a process and
            # whatever it was holding.
            "reloaded": server.reloaded,
        },
        options,
    )
    return EXIT_OK


def _stop(tokens: Sequence[str], options: Options) -> int:
    parsed = _parse_tokens(tokens)
    if parsed.flags or parsed.positional:
        raise UsageError("stop takes no arguments")
    result = stop_server(state_path_for())
    _note("stopped the engine server" if result["stopped"] else "no engine server was running")
    _print_json(result, options)
    return EXIT_OK


def _help(tokens: Sequence[str], options: Options) -> int:
    contract = Contract(_announce(ensure_server()))
    if not tokens:
        sys.stdout.write(render_index(contract) + "\n")
        return EXIT_OK
    name = _resolve(contract, tokens)[0]
    sys.stdout.write(render_operation(contract.operation(name)) + "\n")
    return EXIT_OK


def _resolve(contract: Contract, tokens: Sequence[str]) -> tuple[str, list[str]]:
    """The operation these tokens name, and what is left. Both spellings.

    ``encounter.act`` is one token and ``encounter act`` is two; the index says
    which of the two readings exists, so neither is a special case in the
    grammar. A name that is neither is reported with its near misses, because
    "no such command" plus the whole list is a worse answer than the three
    operations it was probably meant to be.
    """
    names = contract.names
    if tokens[0] in names:
        return tokens[0], list(tokens[1:])
    # Only a bare word can be the verb half. Without this, a misspelled
    # ``encounter.akt --id x`` would be reported as no operation
    # ``encounter.akt.--id``, naming something nobody typed.
    second = tokens[1] if len(tokens) >= 2 and not tokens[1].startswith("-") else None
    if second is not None and f"{tokens[0]}.{second}" in names:
        return f"{tokens[0]}.{second}", list(tokens[2:])
    asked = tokens[0] if ("." in tokens[0] or second is None) else f"{tokens[0]}.{second}"
    raise UsageError(_no_such_operation(asked, tokens[0].split(".", 1)[0], names))


def _no_such_operation(asked: str, head: str, names: Iterable[str]) -> str:
    known = list(names)
    near = difflib.get_close_matches(asked, known, n=5, cutoff=0.4)
    group = [name for name in known if name.split(".", 1)[0] == head]
    suggestions = near or group
    if suggestions:
        return (
            f"there is no operation {asked!r}. Did you mean "
            f"{_or_list(suggestions[:5])}?"
        )
    return f"there is no operation {asked!r}. Try: fivee help"


def _operation(tokens: Sequence[str], options: Options) -> int:
    contract = Contract(_announce(ensure_server()))
    name, rest = _resolve(contract, tokens)
    operation = contract.operation(name)
    call = build_call(operation, rest)
    response = http.request(
        contract.server,
        call.method,
        call.path,
        query=call.query,
        body=call.body,
        headers=call.headers,
    )
    # The version this resource is now at travels as a header, so a caller who
    # only ever saw stdout could not obtain the value their next --if-match
    # needs. Reported as prose because it is not part of the result document —
    # a map's ETag put inside the map would corrupt the document itself.
    etag = response.header("ETag")
    if etag is not None:
        version = etag.strip().removeprefix("W/").strip('"')
        _note(f"etag {version} — pass it as --if-match to guard the next write")
    _print_json(response.body, options)
    return EXIT_OK


def _run(tokens: Sequence[str], options: Options) -> int:
    if tokens[0] in ("help", "--help", "-h"):
        return _help(tokens[1:], options)
    if tokens[0] == "serve":
        return _serve(tokens[1:], options)
    if tokens[0] == "stop":
        return _stop(tokens[1:], options)
    return _operation(tokens, options)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command. Returns the exit code rather than raising SystemExit."""
    tokens = list(sys.argv[1:] if argv is None else argv)
    options = Options(
        compact="--compact" in tokens, json_errors="--json-errors" in tokens
    )
    tokens = [token for token in tokens if token not in ("--compact", "--json-errors")]
    if not tokens:
        _note(
            "no command. `fivee help` lists every operation; `fivee serve` starts "
            "the engine."
        )
        return EXIT_USAGE
    try:
        return _run(tokens, options)
    except UsageError as error:
        _note(str(error))
        return EXIT_USAGE
    except ProblemError as error:
        if options.json_errors:
            sys.stderr.write(json.dumps(error.problem, ensure_ascii=False, indent=2) + "\n")
        else:
            sys.stderr.write(error.render() + "\n")
        return EXIT_FAULT if error.is_fault else EXIT_REFUSED
    except UnreachableError as error:
        _note(str(error))
        return EXIT_UNREACHABLE


if __name__ == "__main__":  # pragma: no cover - console script entry point
    raise SystemExit(main())
