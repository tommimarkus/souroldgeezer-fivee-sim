"""Errors the service layer raises. Adapters translate them; nothing else does.

:class:`~fivee_sim.map_document.MapError` is re-exported so an adapter catching
the service layer's failures needs one import, not a tour of the engine.
"""

from __future__ import annotations

from ..map_document import MapError as MapError

__all__ = ["MapEditError", "MapError", "StaleWriteError"]


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
