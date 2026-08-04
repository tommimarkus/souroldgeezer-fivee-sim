"""Errors the service layer raises. Adapters translate them; nothing else does.

:class:`~fivee_sim.map_document.MapError` is re-exported so an adapter catching
the service layer's failures needs one import, not a tour of the engine.
"""

from __future__ import annotations

from typing import Any

from ..map_document import MapError as MapError

__all__ = [
    "MapEditError",
    "MapError",
    "ReplayError",
    "RequestError",
    "StaleWriteError",
]


class RequestError(ValueError):
    """The caller asked for something this layer will not do, and said why.

    The catch-all of this family: bad input, an unknown id, a refused action, a
    file that cannot be written. It exists because the service layer used to
    raise ``ToolError`` — an MCP concept by name and by import direction — for
    all of that, which is the one thing a transport-neutral layer may not do.
    Each adapter translates it: the MCP server into a ``ToolError``, the editor
    into problem+json.

    Where a failure has more to say than a sentence, it gets its own class here:
    :class:`MapError` and :class:`ReplayError` carry diagnostics,
    :class:`MapEditError` the offending operation's index, and
    :class:`StaleWriteError` both versions. This is what the rest is.
    """


class StaleWriteError(ValueError):
    """The durable record moved on between the caller's read and its write.

    Retryable only by re-reading. Nothing here merges the two versions: for a
    map that would silently drop an edit, and for an encounter it would splice
    two divergent fights into a journal that replays as neither.
    """

    def __init__(self, subject: str, *, expected: str | None, current: str | None) -> None:
        self.subject = subject
        self.expected = expected
        self.current = current
        super().__init__(
            f"{subject} has advanced since you read it "
            f"(expected {expected or 'nothing'}, found {current or 'nothing'}); "
            "read it again and reapply"
        )


class MapEditError(ValueError):
    """One edit operation was invalid, so none were applied.

    ``op_index`` names the offending operation's position in the submitted
    list, and the message opens with it — an atomic refusal is only useful if
    the caller can see which brick was bad.
    """

    def __init__(self, op_index: int, message: str) -> None:
        self.op_index = op_index
        super().__init__(f"operation #{op_index}: {message}")


class ReplayError(ValueError):
    """One replay bundle could not be read, or could not be played.

    ``diagnostics`` carries whatever :func:`~fivee_sim.service.replay.validate_replay`
    reported — already plain ``{"path", "message"}`` dictionaries rather than
    :class:`~fivee_sim.validation.Diagnostic` objects, which is the one way this
    differs from :class:`~fivee_sim.map_document.MapError`. The replay validator
    was written to answer an MCP tool *and* the browser's own copy of the same
    checks, so its diagnostics were JSON from the start; re-wrapping them in a
    dataclass here would only make every adapter unwrap them again.

    It is empty for a file that never reached the validator — unreadable, or not
    JSON at all. Those failures are about the file, not about the bundle, so the
    message carries them and there is nothing per-field to say.
    """

    def __init__(self, message: str, diagnostics: list[dict[str, Any]] | None = None) -> None:
        self.diagnostics = diagnostics if diagnostics is not None else []
        super().__init__(message)
