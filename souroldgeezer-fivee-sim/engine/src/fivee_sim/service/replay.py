"""The replay bundle: one fight as one portable JSON document.

A bundle is everything the replay viewer needs and nothing it does not:
the seed, the map document the fight was created from (or ``None``), the
combatants' starting positions and hit points, and the structured event log.
Action records are deliberately absent — a bundle is for *watching* a fight,
and rebuilding one belongs to ``encounter_log``'s records plus the seed.

The format, ``fivee-sim-replay`` version 1::

    {
      "format": "fivee-sim-replay",
      "format_version": 1,
      "name": "...",                 # the map's name, or the encounter id
      "seed": 123,                   # the encounter's seed
      "map": {...} | null,           # a fivee-sim-map payload, by value
      "initial": {
        "creatures": [{"name", "team", "position": [x_feet, y_feet],
                       "hp", "max_hp"}, ...],
        "map_open_features": ["door-1", ...]   # feature ids open at the start
      },
      "events": [Event.as_dict(), ...]
    }

``map`` is the document **as the fight captured it**: an edit made after the
encounter was created must never change an exported replay, which is the same
by-value discipline the encounter itself applies to content and maps. A fight
built from an inline map spec carries ``map: null`` — the spec is not a
document — and replays on the viewer's neutral plane.

:func:`embed_in_viewer` turns the viewer page plus a bundle into a single
self-contained HTML file: the bundle lands in the page's embedded-data slot,
and the shared renderer can be inlined so the file works over ``file://``
with no server and no sibling assets.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "EMBED_SLOT",
    "FORMAT",
    "FORMAT_VERSION",
    "RENDERER_TAG",
    "embed_in_viewer",
    "replay_bundle",
    "serialize_bundle",
]

FORMAT = "fivee-sim-replay"
FORMAT_VERSION = 1

#: The exact slot the viewer page carries for embedded data. ``test_web_assets``
#: pins that the page contains it exactly once, exactly like this.
EMBED_SLOT = '<script type="application/json" id="embedded-data">null</script>'
#: The exact tag the viewer loads the shared renderer with, also pinned by
#: test — replacing it inlines the renderer for a standalone file.
RENDERER_TAG = '<script src="/assets/renderer.js"></script>'


def replay_bundle(
    *,
    name: str,
    seed: int,
    map_payload: Mapping[str, Any] | None,
    initial_creatures: Sequence[Mapping[str, Any]],
    map_open_features: Sequence[str],
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compose a replay bundle from facts a session captured at creation.

    Every container is copied on the way in, so the bundle shares no mutable
    state with the running encounter that produced it.
    """
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "name": name,
        "seed": seed,
        "map": dict(map_payload) if map_payload is not None else None,
        "initial": {
            "creatures": [dict(creature) for creature in initial_creatures],
            "map_open_features": list(map_open_features),
        },
        "events": [dict(event) for event in events],
    }


def serialize_bundle(bundle: Mapping[str, Any]) -> str:
    """The bundle as one line of JSON plus a trailing newline."""
    return json.dumps(bundle, ensure_ascii=False) + "\n"


def embed_in_viewer(
    viewer_html: str, bundle_json: str, *, renderer_js: str | None = None
) -> str:
    """Fill the viewer page's embedded-data slot with the bundle, exactly once.

    ``bundle_json`` is serialized bundle JSON; every ``<`` in it is re-escaped
    as ``\\u003c`` (valid JSON, byte-identical data) so no event's prose can
    smuggle a ``</script>`` into the page. With ``renderer_js`` given, the
    page's renderer reference is replaced by the script itself, making the
    result a single self-contained file that opens over ``file://``.
    """
    if viewer_html.count(EMBED_SLOT) != 1:
        raise ValueError(
            f"the viewer page must carry {EMBED_SLOT!r} exactly once; "
            f"found {viewer_html.count(EMBED_SLOT)}"
        )
    safe = bundle_json.strip().replace("<", "\\u003c")
    filled = viewer_html.replace(EMBED_SLOT, EMBED_SLOT.replace(">null<", f">{safe}<"), 1)
    if renderer_js is not None:
        if filled.count(RENDERER_TAG) != 1:
            raise ValueError(
                f"the viewer page must carry {RENDERER_TAG!r} exactly once; "
                f"found {filled.count(RENDERER_TAG)}"
            )
        filled = filled.replace(RENDERER_TAG, f"<script>\n{renderer_js}\n</script>", 1)
    return filled
