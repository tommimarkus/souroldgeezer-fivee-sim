"""One request, and one way of reporting a refusal.

The engine answers errors as RFC 9457 ``application/problem+json``, and the
field that matters is ``detail``: this repository's error discipline is that
``detail`` names what the branch refused, in the engine's own words. So
:class:`ProblemError` keeps the whole problem object and renders it as one
line that leads with that sentence — the fix, not the status.

**A refusal and a fault are different failures and stay different.** A 4xx is
the engine saying no to what was asked; a 5xx is the engine failing at it. They
carry the same media type and are told apart only by status, so
:attr:`ProblemError.is_fault` is where that split is made once, and the CLI maps
it onto two exit codes. Collapsing them would make "my request was wrong" and
"the server has a bug" indistinguishable to a caller that only reads ``$?``.

**Headers come back with the body, because this API's writes are conditional.**
A map's version is its ``ETag`` and an encounter's is its journal head, and both
travel as response headers — a client that returned only the parsed body could
never tell a caller the value its next ``If-Match`` needs, which would make the
whole ownership half of the durable-write contract unreachable from a shell.

Nothing here knows what an operation is. This module takes a method, a path, a
query and a body, and gives back a :class:`Response`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from urllib.parse import urlencode

from .discovery import TOKEN_HEADER, Server, UnreachableError

__all__ = ["DEFAULT_TIMEOUT", "ProblemError", "Response", "request"]

#: How long a call waits. Well past any local operation — a 1000-iteration
#: Monte Carlo run is a real request — and short enough that a wedged server is
#: reported rather than waited on forever.
DEFAULT_TIMEOUT = 300.0


@dataclass(frozen=True)
class Response:
    """One answer: its status, its headers, and its parsed body."""

    status: int
    headers: Mapping[str, str]
    body: Any

    def header(self, name: str) -> str | None:
        """One header, matched case-insensitively as HTTP defines them."""
        wanted = name.casefold()
        for key, value in self.headers.items():
            if key.casefold() == wanted:
                return value
        return None


class ProblemError(RuntimeError):
    """The server refused, or failed, and said so in problem+json."""

    def __init__(self, status: int, problem: Mapping[str, Any], target: str) -> None:
        self.status = status
        self.problem: dict[str, Any] = dict(problem)
        self.target = target
        super().__init__(self.detail)

    @property
    def detail(self) -> str:
        """What the engine said it refused. Falls back to the reason phrase.

        A problem with no ``detail`` is a problem this engine did not write —
        every branch here carries one — but a client that crashed on the
        difference would report its own defect instead of the server's.
        """
        detail = self.problem.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail
        try:
            return HTTPStatus(self.status).phrase
        except ValueError:
            return f"HTTP {self.status}"

    @property
    def type_uri(self) -> str:
        found = self.problem.get("type")
        return found if isinstance(found, str) else ""

    @property
    def is_fault(self) -> bool:
        """5xx: the engine failed at the request rather than refusing it."""
        return self.status >= 500

    def render(self) -> str:
        """The human line: what was refused, then the machine-readable type.

        Diagnostics ride along where the failure has any — a content pack or a
        map document is refused with a list of field-level problems, and
        dropping them would leave the caller with "it is invalid" and no idea
        which line to edit.
        """
        word = "failed" if self.is_fault else "refused"
        lines = [f"fivee: {word} ({self.status}) {self.detail}"]
        if self.type_uri:
            lines.append(f"  type: {self.type_uri}")
        diagnostics = self.problem.get("diagnostics")
        if isinstance(diagnostics, list) and diagnostics:
            for entry in diagnostics[:10]:
                lines.append(f"  - {_diagnostic_line(entry)}")
            if len(diagnostics) > 10:
                lines.append(f"  ... and {len(diagnostics) - 10} more")
        return "\n".join(lines)


def _diagnostic_line(entry: Any) -> str:
    if not isinstance(entry, Mapping):
        return str(entry)
    where = " ".join(
        str(entry[key]) for key in ("source", "section", "record", "field") if entry.get(key)
    )
    message = str(entry.get("message", entry))
    return f"{where}: {message}" if where else message


def request(
    server: Server,
    method: str,
    path: str,
    *,
    query: Mapping[str, Any] | None = None,
    body: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Response:
    """Call one operation and return its status, headers and parsed body.

    Raises :class:`ProblemError` for any 4xx or 5xx, and
    :class:`~fivee_sim.client.discovery.UnreachableError` when the connection
    itself fails — a server that died between the ping and this call is a
    machine problem, not a refusal, and must not be reported as one.
    """
    target = path
    if query:
        pairs = [(name, _query_text(value)) for name, value in query.items() if value is not None]
        if pairs:
            target = f"{path}?{urlencode(pairs)}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    sent = {TOKEN_HEADER: server.token, "Accept": "application/json"}
    if data is not None:
        sent["Content-Type"] = "application/json"
    sent.update(headers or {})
    call = urllib.request.Request(
        f"http://127.0.0.1:{server.port}{target}", method=method, data=data, headers=sent
    )
    try:
        with urllib.request.urlopen(call, timeout=timeout) as response:
            raw = response.read()
            status = int(response.status)
            answered: dict[str, str] = dict(response.getheaders())
    except urllib.error.HTTPError as error:
        raise _problem_from(error, target) from None
    except OSError as error:
        raise UnreachableError(
            f"could not reach the engine server on port {server.port}: {error}"
        ) from None
    parsed = json.loads(raw.decode("utf-8")) if raw.strip() else None
    return Response(status=status, headers=answered, body=parsed)


def _query_text(value: Any) -> str:
    """A query value as the server's own coercion will read it back.

    ``True`` must go over the wire as ``true``, not Python's ``True``: the
    server accepts ``true``/``1``/``yes`` and refuses anything else, so
    ``str(value)`` would turn a working flag into a 400.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _problem_from(error: urllib.error.HTTPError, target: str) -> ProblemError:
    """The problem object the server sent, or one built from what it did send.

    A response that is not problem+json still has to become a
    :class:`ProblemError`: a proxy, or a stdlib error page, would otherwise
    surface as a ``json.JSONDecodeError`` from inside the client and read as a
    client bug.
    """
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict):
        payload = {"detail": f"the server answered {error.code} with no problem detail"}
    return ProblemError(error.code, payload, target)
