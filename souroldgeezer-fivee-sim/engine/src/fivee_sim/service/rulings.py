"""Transport-neutral access to the rulings register.

The generated report serves someone reading the repository.  This serves the
caller mid-fight: a game master who has just watched a roll go a way they did
not expect, and wants to know whether that was the printed rule or a decision
this engine made on the rules' behalf.

Reads only, and from a frozen tuple — there is no state here and nothing to
configure.  The register is compiled into the engine rather than loaded from
content, because a ruling describes the engine's own code and a pack cannot
change it.
"""

from __future__ import annotations

from typing import Any

from ..rulings import RULINGS, Ruling, RulingKind
from .errors import NotFoundError, RequestError


def _as_dict(ruling: Ruling) -> dict[str, Any]:
    """One entry, with the empty optional fields left out.

    Omitted rather than sent as empty strings: a closed ruling has no revisit
    trigger, and a key whose value is ``""`` reads as one that was forgotten.
    """
    payload: dict[str, Any] = {
        "code": ruling.code,
        "kind": ruling.kind.value,
        "question": ruling.question,
        "decision": ruling.decision,
        "because": ruling.because,
        "basis": list(ruling.basis),
        "concurrence": ruling.concurrence.value,
        "sites": list(ruling.sites),
    }
    if ruling.revisit:
        payload["revisit"] = ruling.revisit
    if ruling.omission_codes:
        payload["omission_codes"] = list(ruling.omission_codes)
    if ruling.superseded_by:
        payload["superseded_by"] = ruling.superseded_by
    return payload


def listing(*, code: str = "", kind: str = "") -> dict[str, Any]:
    """The register, optionally narrowed to one entry or one kind.

    Both refusals name what was asked for.  The unknown *kind* also lists the
    legal ones, because a kind is a closed vocabulary a caller can be told;
    a code is an open set and listing every one of them would be the answer to
    a different question.
    """
    wanted: RulingKind | None = None
    if kind:
        try:
            wanted = RulingKind(kind)
        except ValueError:
            legal = ", ".join(member.value for member in RulingKind)
            raise RequestError(
                f"unknown ruling kind {kind!r}; expected one of: {legal}"
            ) from None
    selected = [
        ruling
        for ruling in RULINGS
        if (not code or ruling.code == code) and (wanted is None or ruling.kind is wanted)
    ]
    if code and not selected:
        raise NotFoundError(f"no ruling with code {code!r}")
    return {"count": len(selected), "rulings": [_as_dict(ruling) for ruling in selected]}
