"""Map documents: the file a battle map lives in between fights.

A map document is a single JSON file with a lifecycle — generated, hand-edited,
played — and the file is the source of truth. That is why it is not a fifth
content-pack section: packs are merged named-record registries, and merging two
maps by name is meaningless. It validates under the same idiom as packs, via
the shared :mod:`fivee_sim.validation` machinery: every problem collected,
located diagnostics, unknown keys refused with the valid list.

The format, ``fivee-sim-map`` version 1:

- ``grid`` — width and height in squares (1..``MAX_MAP_DIM``), with
  ``cell_feet`` fixed at 5 so the file says what its numbers mean.
- ``compass`` — optional: where *true* north lies, one of the eight
  :class:`~fivee_sim.kernel.grid.Facing` names. Presentation and narration only.
  **Grid north is −y whatever it says**, because the door hinge and swing
  vocabulary already spells four of those names and has meant −y and +y since
  the format existed; a compass that re-aimed them would silently change the
  meaning of every map already on disk. Omitted entirely when it is grid north,
  so a file written before it existed round-trips to the same bytes.
- ``tiles`` — one string per row, top row first, each character resolved
  through the per-document ``legend`` to a terrain-kind string. Opacity is
  tile-based; the format has no edge walls.
- ``palette`` — optional terrain colors: a terrain kind maps to one hex color,
  or to a ``{"light", "dark"}`` pair when the two themes want different ones.
  Colors are hex and nothing else, because the browser assets put them straight
  into a CSS background where a ``url(...)`` would reach the network and break
  the pages' offline guarantee. A kind the document does not paint may still be
  colored, so a palette survives re-legending. Omitted entirely when empty, so a
  file written before colors existed round-trips to the same bytes. What a kind
  looks like without an entry is the renderers' business — see ``renderer.js``
  and :mod:`fivee_sim.service.uvtt`, which compute one.
- ``elevation`` — optional ground height in feet: a ``default`` and a sparse
  list of ``[x, y, feet]`` for the squares that differ from it. Omitted
  entirely on a flat map, so a file written before heights existed round-trips
  to the same bytes. A reader that predates the key refuses the document as an
  unknown key rather than silently flattening it, which is why the format
  version does not move.
- ``features`` — doors, stairs, spawn hints, and anything else a fight can
  operate. A feature records its *default* state; live state belongs to the
  encounter overlay, never the document. Door cells are ordinary floor in
  ``tiles`` — the feature supplies the blocking.

  **Carrying ``state`` is what makes a feature a fixture the fight owns**, and
  not being a door: a spawn hint and a drawn stairway have none and stay
  document-level, while a spike, a lever and a sluice gate have one and cross
  to the battle map. Seven optional keys say what operating one does and costs —
  ``terrain`` and ``elevation`` for its own square, ``affects`` for the squares
  it reaches past it, ``requires`` for what must already stand open,
  ``trigger`` for automatic state changes, and ``costs_action`` and ``check``
  for what the attempt spends and rolls. All seven
  are omitted on write when absent, so the format version does not move and a
  file written before fixtures existed round-trips to the same bytes.

  Every square a fixture governs — its own plus every overlay cell — is
  governed by **exactly one** fixture per level. That is what leaves the format
  with no precedence question: no document order to consult, no history to
  replay, and so no way for a live fight and a stateless query to disagree
  about what a square is.
- ``provenance`` — generator, seed, fully-resolved params (defaults included,
  so the document alone reproduces the map), the ``edited`` flag, and a
  source string. Editing a map flips ``edited`` and leaves the rest alone.

The glyphs ``+`` ``/`` ``<`` ``>`` ``@`` are reserved for renderer overlays
(doors, stairs, spawns); a legend claiming one is a validation error.

:func:`serialize` writes canonical bytes — stable key order, sorted legend and
params, LF line endings, trailing newline — so parse → serialize → parse is
byte-stable and a saved file diffs cleanly.

**There is no bridge to a second map model any more.** ``to_grid`` built an
encounter-facing ``BattleMap`` out of every document, and a fight took that
rather than this; both are gone. A fight holds the :class:`MapDocument` and asks
it the questions ``to_grid`` used to precompute, which is why
:mod:`fivee_sim.map_types` exists and why ``model`` may import it.

**The tree itself lives in** :mod:`fivee_sim.map_types`, and this module is the
reading of a file into it. The split is not tidiness: a dataclass needs nothing
from :mod:`fivee_sim.validation`, only the parsing below does, so the types can
sit in a module that imports the kernel and the standard library alone — and a
caller that wants to *hold* a map stops dragging the machinery for *reading* one
in behind it. Every name that moved is re-exported here, so this module is still
the one door for the format.

**The rules moved with it, and that is the same split applied to behaviour.**
"each square a fixture governs is governed by exactly one" is a predicate over
the tree; ``Reader`` and ``Diagnostic`` are how a refusal reaches an author.
Only the second belongs here. The five ``_check_*`` passes that used to state
the first are now :data:`~fivee_sim.map_types.DOCUMENT_RULES`, and
:func:`_render` is all that is left of them — it walks the findings and calls
``fail`` or ``warn``. **It still accumulates**, which is the half of this module
the rules deliberately have no opinion about: :class:`MapError` promises every
diagnostic and not the first, because an author fixing a file wants the list,
while ``Encounter._adopt_map`` renders the same findings fail-fast because a
fight either starts or does not.

That was worth doing rather than tidy. Held against each other case by case,
the two implementations disagreed on five documents — a self-connector, an
unvalidated ``sight_to_levels``, a fixture requiring itself, a requirement
cycle, and a trigger naming one fixture twice. ``tests/test_map_rules.py`` runs
every case through both readers and is what keeps them one.

What stays here is what only a *raw payload* can be asked. ``at`` and overlay
cells off the grid, ragged ``tiles``, a glyph with no legend entry, an
``ambient_light`` that is not a light level, and every terrain kind naming
content that is not loaded are all checked while reading, where ``grid`` and
``legend`` may be missing entirely and the diagnostic can name the key that is
wrong. The fight asks those same questions of the built object through
:func:`~fivee_sim.map_types.plane_findings` and
:func:`~fivee_sim.map_types.terrain_findings`, because a hand-built document
never met the parser at all.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any

from .kernel.grid import FEET_PER_SQUARE, Facing, Square, TerrainTable
from .kernel.mapgen import GeneratedMap
from .kernel.rules import Ability
from .map_types import (
    DEFAULT_LEGEND,
    GROUND_LEVEL,
    RESERVED_GLYPHS,
    FeatureCheck,
    FeatureTrigger,
    HeightPair,
    LightLevel,
    MapColor,
    MapDocument,
    MapElevation,
    MapFeatureRecord,
    MapFinding,
    MapGrid,
    MapLevel,
    MapLight,
    MapOverlayRecord,
    MapProvenance,
    TerrainPair,
    TriggerMode,
    allocate_legend,
    document_findings,
)
from .validation import Diagnostic, Reader, Severity

#: Every name below that this module no longer *defines* is re-exported from
#: :mod:`fivee_sim.map_types`, which is where the document tree and the fixture
#: vocabulary now live. The format's module stays the one door: a caller that
#: wants ``MapDocument`` and ``parse_document`` together still asks here.
__all__ = [
    "DEFAULT_LEGEND",
    "DOOR_ORIENTATIONS",
    "FORMAT",
    "FORMAT_VERSION",
    "GENERATED_SOURCE",
    "GROUND_LEVEL",
    "MAX_MAP_BYTES",
    "MAX_MAP_DIM",
    "RESERVED_GLYPHS",
    "MapColor",
    "MapDocument",
    "MapElevation",
    "MapError",
    "MapFeatureRecord",
    "MapGrid",
    "MapLight",
    "MapLevel",
    "MapOverlayRecord",
    "MapProvenance",
    "allocate_legend",
    "as_payload",
    "document_from",
    "feature_payload",
    "parse_document",
    "serialize",
    "validate_document",
]

FORMAT = "fivee-sim-map"
FORMAT_VERSION = 1

#: The provenance ``source`` written for generator output. Original content:
#: generated layouts derive from no published material.
GENERATED_SOURCE = "Generated original content; 5E-compatible"

#: Documents refuse to grow past either cap: a 512-square side keeps every
#: whole-map pass affordable, and the byte cap stops a runaway file from
#: stalling a session before validation can even locate the problem.
MAX_MAP_DIM = 512
MAX_MAP_BYTES = 4 * 1024 * 1024

_DOCUMENT_KEYS = frozenset(
    {
        "format", "format_version", "name", "grid", "compass", "legend", "palette",
        "tiles", "elevation", "features", "levels", "ambient_light", "provenance",
    }
)
#: The two themes a palette entry may name; one color for both is the short form.
_PALETTE_THEMES = frozenset({"light", "dark"})
#: ``#rgb`` or ``#rrggbb``, and nothing else — see the module docstring on why
#: the format refuses every other CSS color syntax.
_HEX_COLOR = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\Z")
_GRID_KEYS = frozenset({"width", "height", "cell_feet"})
_ELEVATION_KEYS = frozenset({"default", "squares"})
_LEVEL_KEYS = frozenset(
    {"index", "name", "tiles", "elevation", "features", "ambient_light"}
)
_FEATURE_KEYS = frozenset(
    {
        "id", "kind", "at", "facing", "orientation", "hinge", "swing", "state",
        "linked_to", "team", "to_level", "sight_to_levels", "light",
        "terrain", "elevation", "affects", "requires", "costs_action", "check",
        "trigger",
    }
)
#: The keys the seven fixture keys add on top of a plain annotation. A feature
#: carrying any of them without a ``state`` is refused: a fixture nothing can
#: operate would flip nothing, silently.
_FIXTURE_KEYS = (
    "affects", "check", "costs_action", "elevation", "requires", "terrain", "trigger",
)
_OVERLAY_KEYS = frozenset({"cells", "terrain", "elevation"})
_CHECK_KEYS = frozenset({"ability", "dc"})
_LIGHT_KEYS = frozenset({"bright", "dim", "color"})
_TRIGGER_KEYS = frozenset({"when", "set", "mode"})
#: A fixture's two states, and so the two keys of every pair in the format.
_PAIR_STATES = frozenset({"closed", "open"})
#: The same two, in the order they are read and written — closed first, because
#: closed is the state a fixture is authored in.
_PAIR_ORDER = ("closed", "open")

_PROVENANCE_KEYS = frozenset({"generator", "seed", "params", "edited", "source"})
#: How a door may hang. Public because it is the format's vocabulary and the
#: authoring surfaces have to refuse the same words: ``service/specs.py`` reads
#: it so an inline map spec and a saved document cannot disagree about what
#: ``orientation`` may say.
DOOR_ORIENTATIONS = ("horizontal", "vertical")
_DOOR_STATES = ("open", "closed")
_DOOR_HINGES = {
    "horizontal": ("west", "east"),
    "vertical": ("north", "south"),
}
_DOOR_SWINGS = {
    "horizontal": ("north", "south"),
    "vertical": ("west", "east"),
}


class MapError(ValueError):
    """A map document is invalid. Carries every diagnostic, not the first."""

    def __init__(self, diagnostics: list[Diagnostic] | tuple[Diagnostic, ...]) -> None:
        self.diagnostics = tuple(diagnostics)
        errors = [d for d in self.diagnostics if d.severity is Severity.ERROR]
        super().__init__(
            f"{len(errors)} map error(s):\n" + "\n".join(f"  {d.describe()}" for d in errors)
        )


# --- parsing ---------------------------------------------------------------
def _parse_grid(
    payload: Mapping[str, Any], reader: Reader, diagnostics: list[Diagnostic], source: str
) -> MapGrid | None:
    raw = payload.get("grid")
    if raw is None:
        reader.fail("grid", "required")
        return None
    if not isinstance(raw, Mapping):
        reader.fail("grid", f"must be an object, got {type(raw).__name__}")
        return None
    sub = Reader(raw, diagnostics, source=source, section="map", name="grid")
    sub.unknown_keys(_GRID_KEYS)
    width = sub.integer("width", required=True, minimum=1, default=1)
    height = sub.integer("height", required=True, minimum=1, default=1)
    for label, value in (("width", width), ("height", height)):
        if value > MAX_MAP_DIM:
            sub.fail(label, f"must be at most {MAX_MAP_DIM} squares, got {value}")
    cell_feet = sub.integer("cell_feet", required=True, default=FEET_PER_SQUARE)
    if cell_feet != FEET_PER_SQUARE:
        sub.fail(
            "cell_feet",
            f"must be {FEET_PER_SQUARE}; this format is defined on 5-foot squares",
        )
    if not sub.ok:
        return None
    return MapGrid(width=width, height=height, cell_feet=cell_feet)


def _parse_legend(
    payload: Mapping[str, Any],
    reader: Reader,
    *,
    terrain: TerrainTable,
) -> dict[str, str] | None:
    """The legend as a plain dict, or ``None`` when it was not a usable mapping.

    Entries that fail a *value* check (reserved glyph, unknown kind) still land
    in the returned dict: the error is already reported once, and leaving the
    glyph resolvable stops every tile that uses it from re-reporting it.
    """
    raw = payload.get("legend")
    if raw is None:
        reader.fail("legend", "required")
        return None
    if not isinstance(raw, Mapping):
        reader.fail(
            "legend",
            "must be an object mapping single characters to terrain kinds, "
            'such as {".": "floor", "#": "wall"}',
        )
        return None
    available = ", ".join(sorted(terrain)) or "none"
    legend: dict[str, str] = {}
    for glyph in sorted(raw, key=str):
        kind = raw[glyph]
        if not isinstance(glyph, str) or len(glyph) != 1:
            reader.fail("legend", f"glyph {glyph!r} must be a single character")
            continue
        if not isinstance(kind, str) or not kind.strip():
            reader.fail("legend", f"glyph {glyph!r} must name a terrain kind, got {kind!r}")
            continue
        if glyph in RESERVED_GLYPHS:
            reader.fail(
                "legend",
                f"glyph {glyph!r} is reserved for renderer overlays "
                f"({' '.join(sorted(RESERVED_GLYPHS))}) and cannot name terrain",
            )
        if kind not in terrain:
            reader.fail(
                "legend",
                f"glyph {glyph!r} names terrain {kind!r}, which the active content "
                f"does not define. Available: {available}",
            )
        legend[glyph] = kind
    return legend


def canonical_color(value: str) -> str | None:
    """``value`` as lowercase ``#rrggbb``, or ``None`` if it is not a hex color.

    The one place the format's color syntax is decided; the map service reuses it
    so an edit operation and a hand-written file are held to the same rule.
    """
    text = value.strip()
    if not _HEX_COLOR.match(text):
        return None
    text = text.lower()
    if len(text) == 4:
        return "#" + "".join(char * 2 for char in text[1:])
    return text


def _color_of(
    kind: str, value: Any, reader: Reader, diagnostics: list[Diagnostic], source: str
) -> MapColor | None:
    """One palette entry: a color for both themes, or a ``{light, dark}`` pair."""
    if isinstance(value, str):
        canonical = canonical_color(value)
        if canonical is None:
            reader.fail("palette", _bad_color(kind, value))
            return None
        return MapColor(light=canonical, dark=canonical)
    if isinstance(value, Mapping):
        sub = Reader(value, diagnostics, source=source, section="map", name=f"palette.{kind}")
        sub.unknown_keys(_PALETTE_THEMES)
        themes: dict[str, str] = {}
        for theme in ("light", "dark"):
            raw = value.get(theme)
            if raw is None:
                sub.fail(
                    theme,
                    f'{kind!r} must give both "light" and "dark", or a single color '
                    f"for both themes",
                )
                continue
            canonical = canonical_color(raw) if isinstance(raw, str) else None
            if canonical is None:
                sub.fail(theme, _bad_color(kind, raw))
                continue
            themes[theme] = canonical
        if not sub.ok:
            return None
        return MapColor(light=themes["light"], dark=themes["dark"])
    reader.fail("palette", _bad_color(kind, value))
    return None


def _bad_color(kind: str, value: Any) -> str:
    return (
        f'{kind!r} must be a hex color like "#d2440f" or "#abc" — the format takes '
        f"no other color syntax — got {value!r}"
    )


def _parse_palette(
    payload: Mapping[str, Any],
    reader: Reader,
    diagnostics: list[Diagnostic],
    source: str,
    *,
    terrain: TerrainTable,
) -> dict[str, MapColor]:
    """The color layer, or empty when the document does not carry one.

    Always a usable mapping: a palette has no dependents inside the document, so
    a bad entry is reported and dropped rather than failing the whole key. The
    document is refused all the same — every problem here is an error.
    """
    raw = payload.get("palette")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        reader.fail(
            "palette",
            "must be an object mapping terrain kinds to colors, such as "
            '{"lava": "#d2440f"}',
        )
        return {}
    available = ", ".join(sorted(terrain)) or "none"
    palette: dict[str, MapColor] = {}
    for kind in sorted(raw, key=str):
        if not isinstance(kind, str) or not kind.strip():
            reader.fail("palette", f"{kind!r} must name a terrain kind")
            continue
        if kind not in terrain:
            reader.fail(
                "palette",
                f"names terrain {kind!r}, which the active content does not define. "
                f"Available: {available}",
            )
            continue
        color = _color_of(kind, raw[kind], reader, diagnostics, source)
        if color is not None:
            palette[kind] = color
    return palette


def _parse_tiles(
    payload: Mapping[str, Any],
    reader: Reader,
    grid: MapGrid | None,
    legend: dict[str, str] | None,
) -> tuple[str, ...]:
    raw = payload.get("tiles")
    if raw is None:
        reader.fail("tiles", "required")
        return ()
    if not isinstance(raw, list) or not all(isinstance(row, str) for row in raw):
        reader.fail("tiles", "must be a list of strings, one per map row, top row first")
        return ()
    tiles = tuple(raw)
    if grid is not None:
        if len(tiles) != grid.height:
            reader.fail("tiles", f"has {len(tiles)} rows; the grid is {grid.height} squares high")
        for y, row in enumerate(tiles):
            if len(row) != grid.width:
                reader.fail(
                    "tiles",
                    f"row {y} is {len(row)} characters; the grid is {grid.width} squares wide",
                )
    if legend is not None:
        # Each unknown glyph is reported once, at its first appearance — a map
        # whose legend dropped a glyph should not produce a thousand copies of
        # the same message.
        unknown: dict[str, Square] = {}
        for y, row in enumerate(tiles):
            for x, char in enumerate(row):
                if char not in legend and char not in unknown:
                    unknown[char] = (x, y)
        for char in sorted(unknown):
            x, y = unknown[char]
            reader.fail(
                "tiles",
                f"row {y} column {x} uses {char!r}, which the legend does not define",
            )
    return tiles


def _parse_elevation(
    payload: Mapping[str, Any],
    reader: Reader,
    diagnostics: list[Diagnostic],
    grid: MapGrid | None,
    source: str,
) -> MapElevation | None:
    """The height layer, or the flat default when the document does not carry one.

    ``None`` only when the key is present and unusable; an absent key is a flat
    map, which is what every document written before heights existed is.
    """
    raw = payload.get("elevation")
    if raw is None:
        return MapElevation()
    if not isinstance(raw, Mapping):
        reader.fail(
            "elevation",
            'must be an object with a "default" height in feet and a "squares" '
            'list of [x, y, feet], such as {"default": 0, "squares": [[3, 4, 20]]}',
        )
        return None
    sub = Reader(raw, diagnostics, source=source, section="map", name="elevation")
    sub.unknown_keys(_ELEVATION_KEYS)
    default = sub.integer("default")
    entries = raw.get("squares", [])
    if not isinstance(entries, list):
        sub.fail("squares", "must be a list of [x, y, feet] entries")
        return None
    squares: dict[Square, int] = {}
    for index, entry in enumerate(entries):
        if (
            not isinstance(entry, (list, tuple))
            or len(entry) != 3
            or any(isinstance(value, bool) or not isinstance(value, int) for value in entry)
        ):
            sub.fail("squares", f"entry #{index} must be [x, y, feet], got {entry!r}")
            continue
        square = (int(entry[0]), int(entry[1]))
        if grid is not None and not (
            0 <= square[0] < grid.width and 0 <= square[1] < grid.height
        ):
            sub.fail(
                "squares",
                f"entry #{index} is at ({square[0]}, {square[1]}), outside the "
                f"{grid.width}x{grid.height} grid",
            )
            continue
        if square in squares:
            sub.fail(
                "squares",
                f"entry #{index} names square ({square[0]}, {square[1]}) again; "
                f"it is already {squares[square]} ft",
            )
            continue
        squares[square] = int(entry[2])
    if not sub.ok:
        return None
    return MapElevation(default=default, squares=MappingProxyType(squares))


def _terrain_pair(
    raw: Any,
    reader: Reader,
    diagnostics: list[Diagnostic],
    source: str,
    *,
    field: str,
    name: str,
    terrain: TerrainTable,
) -> TerrainPair | None:
    """A ``{"closed", "open"}`` pair of terrain kinds, or ``None`` if unusable.

    Kinds are checked against the *loaded content*, never against this
    document's legend: a fixture may turn its square into a kind the map paints
    nowhere, which is the licence the palette already has and for the same
    reason — what a square becomes is not a drawing question.
    """
    if not isinstance(raw, Mapping):
        reader.fail(
            field,
            "must be an object naming the terrain kind in each state, such as "
            '{"closed": "door-closed", "open": "door-open"}',
        )
        return None
    sub = Reader(raw, diagnostics, source=source, section="map", name=f"{name}.{field}")
    sub.unknown_keys(_PAIR_STATES)
    available = ", ".join(sorted(terrain)) or "none"
    kinds: dict[str, str] = {}
    for state in _PAIR_ORDER:
        value = raw.get(state)
        if value is None:
            sub.fail(state, 'must give both "closed" and "open" terrain kinds')
            continue
        if not isinstance(value, str) or not value.strip():
            sub.fail(state, f"must name a terrain kind, got {value!r}")
            continue
        if value not in terrain:
            sub.fail(
                state,
                f"names terrain {value!r}, which the active content does not define. "
                f"Available: {available}",
            )
            continue
        kinds[state] = value
    if not sub.ok:
        return None
    return TerrainPair(closed=kinds["closed"], open=kinds["open"])


def _height_pair(
    raw: Any,
    reader: Reader,
    diagnostics: list[Diagnostic],
    source: str,
    *,
    field: str,
    name: str,
) -> HeightPair | None:
    """A ``{"closed", "open"}`` pair of ground heights in feet."""
    if not isinstance(raw, Mapping):
        reader.fail(
            field,
            "must be an object naming the ground height in feet in each state, "
            'such as {"closed": 0, "open": -5}',
        )
        return None
    sub = Reader(raw, diagnostics, source=source, section="map", name=f"{name}.{field}")
    sub.unknown_keys(_PAIR_STATES)
    feet: dict[str, int] = {}
    for state in _PAIR_ORDER:
        if raw.get(state) is None:
            sub.fail(state, 'must give both "closed" and "open" heights in feet')
            continue
        feet[state] = sub.integer(state)
    if not sub.ok:
        return None
    return HeightPair(closed=feet["closed"], open=feet["open"])


def _feature_check(
    raw: Any, reader: Reader, diagnostics: list[Diagnostic], source: str, *, name: str
) -> FeatureCheck | None:
    """The ability check operating a fixture takes, if it takes one.

    A raw ability check: creatures carry no skill proficiencies, so a DC here is
    set as if untrained, and the format has no place to say otherwise.
    """
    if not isinstance(raw, Mapping):
        reader.fail(
            "check",
            "must be an object naming an ability and a DC, such as "
            '{"ability": "strength", "dc": 15}',
        )
        return None
    sub = Reader(raw, diagnostics, source=source, section="map", name=f"{name}.check")
    sub.unknown_keys(_CHECK_KEYS)
    if raw.get("ability") is None:
        sub.fail(
            "ability",
            f"required: the ability the check rolls; one of "
            f"{', '.join(member.value for member in Ability)}",
        )
    ability = sub.enum("ability", Ability)
    if raw.get("dc") is None:
        sub.fail("dc", "required: the difficulty class the check is made against")
    dc = sub.integer("dc", minimum=1)
    if not sub.ok or ability is None:
        return None
    return FeatureCheck(ability=ability, dc=dc)


def _feature_trigger(
    raw: Any, reader: Reader, diagnostics: list[Diagnostic], source: str, *, name: str
) -> FeatureTrigger | None:
    """Parse one target-local fixture-state predicate."""
    if not isinstance(raw, Mapping):
        reader.fail("trigger", "trigger must be an object with when, set, and mode")
        return None
    sub = Reader(raw, diagnostics, source=source, section="map", name=f"{name}.trigger")
    sub.unknown_keys(_TRIGGER_KEYS)

    conditions: list[tuple[str, bool]] = []
    when = raw.get("when")
    if when is None:
        sub.fail("when", "when is required")
    elif not isinstance(when, Mapping):
        sub.fail("when", "when must be an object mapping fixture ids to open or closed")
    elif not when:
        sub.fail("when", "when must name at least one fixture")
    else:
        for fixture, state in sorted(when.items(), key=lambda item: str(item[0])):
            if not isinstance(fixture, str) or not fixture.strip():
                sub.fail("when", f"fixture ids must be non-empty text, got {fixture!r}")
                continue
            if state not in _DOOR_STATES:
                sub.fail(
                    "when",
                    f"state for {fixture!r} must be one of: open, closed; got {state!r}",
                )
                continue
            conditions.append((fixture, state == "open"))

    raw_set = raw.get("set")
    if raw_set is None:
        sub.fail("set", "set is required")
    elif raw_set not in _DOOR_STATES:
        sub.fail("set", f"set must be one of: open, closed; got {raw_set!r}")

    raw_mode = raw.get("mode")
    if raw_mode is None:
        sub.fail("mode", "mode is required")
    elif raw_mode not in {mode.value for mode in TriggerMode}:
        sub.fail(
            "mode", f"mode must be one of: edge, maintained; got {raw_mode!r}"
        )

    if not sub.ok or raw_set not in _DOOR_STATES or raw_mode not in {
        mode.value for mode in TriggerMode
    }:
        return None
    return FeatureTrigger(
        when=tuple(conditions),
        set_open=raw_set == "open",
        mode=TriggerMode(raw_mode),
    )


def _overlay_cells(raw: Any, reader: Reader, grid: MapGrid | None) -> tuple[Square, ...] | None:
    """One overlay's squares: at least one, on the grid, sorted row then column."""
    if raw is None:
        reader.fail("cells", "required: the squares this overlay governs, as a list of [x, y]")
        return None
    if not isinstance(raw, list):
        reader.fail("cells", f"must be a list of [x, y] squares, got {raw!r}")
        return None
    if not raw:
        reader.fail("cells", "must name at least one square")
        return None
    cells: list[Square] = []
    malformed = False
    for index, cell in enumerate(raw):
        if (
            not isinstance(cell, (list, tuple))
            or len(cell) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in cell)
        ):
            reader.fail("cells", f"cell #{index} must be [x, y] square indices, got {cell!r}")
            malformed = True
            continue
        square = (int(cell[0]), int(cell[1]))
        if grid is not None and not (
            0 <= square[0] < grid.width and 0 <= square[1] < grid.height
        ):
            reader.fail(
                "cells",
                f"cell #{index} is at ({square[0]}, {square[1]}), outside the "
                f"{grid.width}x{grid.height} grid",
            )
            malformed = True
            continue
        cells.append(square)
    if malformed:
        return None
    return tuple(sorted(cells, key=lambda square: (square[1], square[0])))


def _parse_overlays(
    raw: Any,
    reader: Reader,
    diagnostics: list[Diagnostic],
    grid: MapGrid | None,
    source: str,
    *,
    name: str,
    terrain: TerrainTable,
) -> tuple[MapOverlayRecord, ...] | None:
    """The squares a fixture reaches past its own, or ``None`` if any is unusable.

    The document stores **cells and never a rect**. A rect is an edit-op
    convenience for the author who would rather type one than forty pairs; by
    the time it reaches the file it has been expanded, so the format has one
    shape — which is what lets a resize translate an overlay square by square.
    """
    if not isinstance(raw, list):
        reader.fail(
            "affects",
            "must be a list of overlay objects, each naming the cells it governs "
            "and what they are in each state",
        )
        return None
    overlays: list[MapOverlayRecord] = []
    usable = True
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            reader.fail("affects", f"entry #{index} must be an object")
            usable = False
            continue
        where = f"{name} overlay #{index}"
        sub = Reader(entry, diagnostics, source=source, section="map", name=where)
        sub.unknown_keys(_OVERLAY_KEYS)
        cells = _overlay_cells(entry.get("cells"), sub, grid)
        pair: TerrainPair | None = None
        if entry.get("terrain") is not None:
            pair = _terrain_pair(
                entry["terrain"], sub, diagnostics, source,
                field="terrain", name=where, terrain=terrain,
            )
        heights: HeightPair | None = None
        if entry.get("elevation") is not None:
            heights = _height_pair(
                entry["elevation"], sub, diagnostics, source, field="elevation", name=where
            )
        if entry.get("terrain") is None and entry.get("elevation") is None:
            reader.fail(
                "affects",
                f"entry #{index} needs 'terrain', 'elevation', or both; an overlay "
                f"that moves neither governs nothing",
            )
            usable = False
        if cells is None or not sub.ok:
            usable = False
            continue
        overlays.append(MapOverlayRecord(cells=cells, terrain=pair, elevation=heights))
    return tuple(overlays) if usable else None


def _parse_features(
    raw: Any,
    reader: Reader,
    diagnostics: list[Diagnostic],
    grid: MapGrid | None,
    source: str,
    *,
    claimed: dict[str, str],
    terrain: TerrainTable,
    where: str = "",
) -> tuple[MapFeatureRecord, ...]:
    """One level's features. ``claimed`` spans the document, ``where`` locates it.

    Ids are unique across every level, not within one: the battle map keys its
    features by name in a single table, so two storeys sharing an id would
    resolve to one feature rather than two. Doors, by contrast, collide only
    within a level — two floors may each hang one over the same square.

    That door check stays here, where it can name the door it collided with;
    the wider rule that every square a fixture governs is governed by exactly
    one is :func:`~fivee_sim.map_types.claim_findings`, rendered by
    :func:`_render` after every level is read, because an overlay may name a
    square a fixture further down the list claims.
    """
    if not isinstance(raw, list):
        reader.fail("features", "must be a list of feature objects")
        return ()
    features: list[MapFeatureRecord] = []
    door_squares: dict[Square, str] = {}
    for index, entry in enumerate(raw):
        position = f"feature #{index}{where}"
        if not isinstance(entry, Mapping):
            reader.fail("features", f"{position} must be an object")
            continue
        raw_id = entry.get("id")
        label = raw_id if isinstance(raw_id, str) and raw_id.strip() else position
        sub = Reader(entry, diagnostics, source=source, section="map", name=label)
        sub.unknown_keys(_FEATURE_KEYS)
        feature_id = sub.string("id", required=True)
        if isinstance(raw_id, str) and not raw_id.strip():
            sub.fail("id", "must be non-empty text")
        kind = sub.string("kind", required=True)
        if isinstance(entry.get("kind"), str) and not kind.strip():
            sub.fail("kind", "must be non-empty text")

        at: Square | None = None
        raw_at = entry.get("at")
        if raw_at is None:
            sub.fail("at", "required")
        elif (
            not isinstance(raw_at, (list, tuple))
            or len(raw_at) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in raw_at)
        ):
            sub.fail("at", f"must be [x, y] square indices, got {raw_at!r}")
        else:
            at = (int(raw_at[0]), int(raw_at[1]))
            if grid is not None and not (
                0 <= at[0] < grid.width and 0 <= at[1] < grid.height
            ):
                sub.fail(
                    "at",
                    f"({at[0]}, {at[1]}) is outside the {grid.width}x{grid.height} grid",
                )

        parsed_facing = sub.enum("facing", Facing)
        facing = parsed_facing.value if parsed_facing is not None else None
        orientation = sub.string("orientation") or None
        if orientation is not None and orientation not in DOOR_ORIENTATIONS:
            sub.fail(
                "orientation",
                f"must be one of: {', '.join(DOOR_ORIENTATIONS)}; got {orientation!r}",
            )
        hinge = sub.string("hinge") or None
        swing = sub.string("swing") or None
        state = sub.string("state") or None
        if state is not None and state not in _DOOR_STATES:
            sub.fail("state", f"must be one of: {', '.join(_DOOR_STATES)}; got {state!r}")
        # Carrying a state is what makes a feature one a fight can operate, so a
        # feature that says what operating it costs but carries no state would
        # never be operated at all — a silent no-op, refused instead.
        carried = [key for key in _FIXTURE_KEYS if key in entry]
        if carried and state is None:
            sub.fail(
                "state",
                f"required for a feature carrying {', '.join(carried)}; only a "
                f"feature with a state is one a fight can operate",
            )
        team = sub.string("team") or None

        to_level: int | None = None
        if "to_level" in entry:
            raw_target = entry["to_level"]
            if isinstance(raw_target, bool) or not isinstance(raw_target, int):
                sub.fail(
                    "to_level",
                    f"must name a level by whole number, got {raw_target!r}",
                )
            else:
                to_level = int(raw_target)

        sight_to_levels: tuple[int, ...] = ()
        if "sight_to_levels" in entry:
            raw_sight = entry["sight_to_levels"]
            if not isinstance(raw_sight, list):
                sub.fail("sight_to_levels", "must be a list of level numbers")
            elif any(isinstance(value, bool) or not isinstance(value, int) for value in raw_sight):
                sub.fail("sight_to_levels", "every entry must be a whole-number level")
            else:
                sight_to_levels = tuple(sorted(set(int(value) for value in raw_sight)))

        light: MapLight | None = None
        if "light" in entry:
            raw_light = entry["light"]
            if not isinstance(raw_light, Mapping):
                sub.fail("light", "must be an object with bright, dim, and optional color")
            else:
                light_reader = Reader(
                    raw_light,
                    diagnostics,
                    source=source,
                    section="map",
                    name=f"{label} light",
                )
                light_reader.unknown_keys(_LIGHT_KEYS)
                bright = light_reader.integer("bright", minimum=0)
                dim = light_reader.integer("dim", minimum=0)
                raw_color = raw_light.get("color", "#ffffff")
                color = canonical_color(raw_color) if isinstance(raw_color, str) else None
                if color is None:
                    light_reader.fail("color", _bad_color("light", raw_color))
                if bright <= 0 and dim <= 0:
                    light_reader.fail("bright", "bright or dim must be greater than 0 feet")
                if light_reader.ok and color is not None:
                    light = MapLight(bright=bright, dim=dim, color=color)
                else:
                    sub.ok = False

        # A door must be fully resolved, like provenance params: reading the
        # document alone must answer how it starts and how it hangs.
        if kind == "door":
            if orientation is None:
                sub.fail("orientation", "required for a door")
            if state is None:
                sub.fail("state", "required for a door; the document stores the default")
            # Two answers to one question is the ambiguity this format refuses
            # everywhere else — a fixture key without a state, a legend claiming
            # a reserved glyph — and a door that both hangs and points would be
            # read one way by the renderer and the other by a reader.
            if "facing" in entry:
                sub.fail(
                    "facing",
                    "only a feature that is not a door may carry 'facing'; "
                    "orientation, hinge and swing already say where a door points",
                )
            if orientation in _DOOR_HINGES:
                allowed_hinges = _DOOR_HINGES[orientation]
                if hinge is not None and hinge not in allowed_hinges:
                    sub.fail(
                        "hinge",
                        f"a {orientation} door hinge must be one of: "
                        f"{', '.join(allowed_hinges)}; got {hinge!r}",
                    )
                allowed_swings = _DOOR_SWINGS[orientation]
                if swing is not None and swing not in allowed_swings:
                    sub.fail(
                        "swing",
                        f"a {orientation} door swing must be one of: "
                        f"{', '.join(allowed_swings)}; got {swing!r}",
                    )
            # One door per square: the encounter refuses a map whose doors
            # collide, so the document must refuse it first — a battle map
            # resolves a square to one feature state, never two. Annotations
            # (stairs, spawns) may share squares freely.
            if at is not None:
                if at in door_squares:
                    sub.fail(
                        "at",
                        f"({at[0]}, {at[1]}) already holds door "
                        f"'{door_squares[at]}'; one door per square",
                    )
                else:
                    door_squares[at] = label
        else:
            if hinge is not None:
                sub.fail("hinge", "only a door may carry 'hinge'")
            if swing is not None:
                sub.fail("swing", "only a door may carry 'swing'")

        raw_linked_to = entry.get("linked_to")
        linked_to = sub.string("linked_to") or None
        if isinstance(raw_linked_to, str) and not raw_linked_to.strip():
            sub.fail("linked_to", "must name another door with non-empty text")
        if linked_to is not None and kind != "door":
            sub.fail("linked_to", "only a door may carry 'linked_to'")

        if feature_id.strip():
            if feature_id in claimed:
                sub.fail(
                    "id",
                    f"is already used by {claimed[feature_id]}; ids must be unique",
                )
            else:
                claimed[feature_id] = position
        # The fixture keys, each parsed whether or not the ones before it were:
        # a feature with three mistakes reports three. A key that is present and
        # unusable drops the record, so the second passes below never walk a
        # half-read fixture.
        own_terrain: TerrainPair | None = None
        if entry.get("terrain") is not None:
            own_terrain = _terrain_pair(
                entry["terrain"], sub, diagnostics, source,
                field="terrain", name=label, terrain=terrain,
            )
        own_height: HeightPair | None = None
        if entry.get("elevation") is not None:
            own_height = _height_pair(
                entry["elevation"], sub, diagnostics, source, field="elevation", name=label
            )
        overlays: tuple[MapOverlayRecord, ...] | None = ()
        if entry.get("affects") is not None:
            overlays = _parse_overlays(
                entry["affects"], sub, diagnostics, grid, source,
                name=label, terrain=terrain,
            )
        requires = tuple(sub.string_list("requires"))
        trigger: FeatureTrigger | None = None
        if "trigger" in entry:
            trigger = _feature_trigger(
                entry["trigger"], sub, diagnostics, source, name=label
            )
        costs_action = sub.boolean("costs_action")
        check: FeatureCheck | None = None
        if entry.get("check") is not None:
            check = _feature_check(entry["check"], sub, diagnostics, source, name=label)
        unusable = any(
            parsed is None and entry.get(key) is not None
            for key, parsed in (
                ("terrain", own_terrain),
                ("elevation", own_height),
                ("affects", overlays),
                ("check", check),
                ("trigger", trigger),
            )
        )

        if sub.ok and not unusable and at is not None:
            features.append(
                MapFeatureRecord(
                    id=feature_id, kind=kind, at=at, facing=facing,
                    orientation=orientation, hinge=hinge, swing=swing, state=state,
                    linked_to=linked_to, team=team, to_level=to_level,
                    sight_to_levels=sight_to_levels, light=light,
                    terrain=own_terrain, elevation=own_height, affects=overlays or (),
                    requires=requires, trigger=trigger,
                    costs_action=costs_action, check=check,
                )
            )
    return tuple(features)


def _parse_levels(
    payload: Mapping[str, Any],
    reader: Reader,
    diagnostics: list[Diagnostic],
    grid: MapGrid | None,
    legend: dict[str, str] | None,
    source: str,
    claimed: dict[str, str],
    terrain: TerrainTable,
) -> dict[int, MapLevel]:
    """The storeys above and below the ground, keyed by index.

    The ground is not among them — it lives in the document's own
    ``tiles``/``elevation``/``features`` — which is why
    :data:`GROUND_LEVEL` is refused here rather than accepted as a second
    spelling of it.
    """
    raw = payload.get("levels")
    if raw is None:
        return {}
    if not isinstance(raw, list):
        reader.fail(
            "levels",
            "must be a list of level objects, each with an index and its own tiles",
        )
        return {}
    levels: dict[int, MapLevel] = {}
    for position, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            reader.fail("levels", f"level #{position} must be an object")
            continue
        raw_index = entry.get("index")
        label = f"level {raw_index}" if isinstance(raw_index, int) else f"level #{position}"
        sub = Reader(entry, diagnostics, source=source, section="map", name=label)
        sub.unknown_keys(_LEVEL_KEYS)
        if raw_index is None:
            sub.fail("index", "required: the storey's height order, above or below the ground")
            continue
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            sub.fail("index", f"must be a whole number, got {raw_index!r}")
            continue
        index = int(raw_index)
        if index == GROUND_LEVEL:
            sub.fail(
                "index",
                f"{GROUND_LEVEL} is the ground plane, which lives in the document's own "
                f"tiles, elevation, and features; a storey needs another index",
            )
            continue
        if index in levels:
            sub.fail("index", "is already declared by an earlier level")
            continue
        tiles = _parse_tiles(entry, sub, grid, legend)
        elevation = _parse_elevation(entry, sub, diagnostics, grid, source)
        features = _parse_features(
            entry.get("features", []), sub, diagnostics, grid, source,
            claimed=claimed, terrain=terrain, where=f" on level {index}",
        )
        levels[index] = MapLevel(
            index=index,
            name=sub.string("name") or f"level {index}",
            tiles=tiles,
            features=features,
            elevation=elevation if elevation is not None else MapElevation(),
            ambient_light=_parse_ambient_light(entry, sub),
        )
    return levels


def _parse_ambient_light(payload: Mapping[str, Any], reader: Reader) -> str:
    raw = payload.get("ambient_light", LightLevel.BRIGHT.value)
    if not isinstance(raw, str):
        reader.fail("ambient_light", "must be bright, dim, or darkness")
        return LightLevel.BRIGHT.value
    try:
        return LightLevel(raw).value
    except ValueError:
        reader.fail("ambient_light", f"must be bright, dim, or darkness; got {raw!r}")
        return LightLevel.BRIGHT.value


def _render(findings: Iterator[MapFinding], reader: Reader) -> None:
    """Every finding as a diagnostic, refusals failing and the rest warning.

    The whole of the parser's rendering, and it **accumulates**:
    :class:`MapError` promises every diagnostic and not the first, because an
    author fixing a file wants the list. ``Encounter._adopt_map`` renders the
    same findings the opposite way for the opposite reason — see
    :mod:`fivee_sim.map_types`, which holds the rules neither of us states any
    more.
    """
    for finding in findings:
        if finding.refusal:
            reader.fail(finding.path, finding.message)
        else:
            reader.warn(finding.path, finding.message)



def _parse_provenance(
    payload: Mapping[str, Any], reader: Reader, diagnostics: list[Diagnostic], source: str
) -> MapProvenance | None:
    raw = payload.get("provenance")
    if raw is None:
        reader.fail(
            "provenance",
            "required: generator, seed, params, edited, and source — an edited "
            "map must still say where it came from",
        )
        return None
    if not isinstance(raw, Mapping):
        reader.fail("provenance", f"must be an object, got {type(raw).__name__}")
        return None
    sub = Reader(raw, diagnostics, source=source, section="map", name="provenance")
    sub.unknown_keys(_PROVENANCE_KEYS)
    generator = sub.string("generator", required=True)
    seed = sub.integer("seed", required=True)
    raw_params = raw.get("params")
    params: Mapping[str, Any] | None = None
    if raw_params is None:
        sub.fail("params", "required; record the fully-resolved generator parameters")
    elif not isinstance(raw_params, Mapping):
        sub.fail("params", f"must be an object, got {type(raw_params).__name__}")
    else:
        params = raw_params
    if "edited" not in raw:
        sub.fail("edited", "required")
    edited = sub.boolean("edited")
    source_text = sub.string("source", required=True)
    if not sub.ok or params is None:
        return None
    return MapProvenance(
        generator=generator, seed=seed,
        params=MappingProxyType(dict(params)), edited=edited, source=source_text,
    )


def _parse(
    payload: Mapping[str, Any],
    diagnostics: list[Diagnostic],
    *,
    source: str,
    terrain: TerrainTable,
) -> MapDocument | None:
    if not isinstance(payload, Mapping):
        diagnostics.append(
            Diagnostic(source=source, section="map", problem="a map document must be a JSON object")
        )
        return None
    size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if size > MAX_MAP_BYTES:
        diagnostics.append(
            Diagnostic(
                source=source, section="map",
                problem=(
                    f"is {size} bytes, over the {MAX_MAP_BYTES} byte limit for a map document"
                ),
            )
        )
        return None

    reader = Reader(payload, diagnostics, source=source, section="map", name="")
    reader.unknown_keys(_DOCUMENT_KEYS)
    declared = reader.string("format", required=True, default=FORMAT)
    if declared != FORMAT:
        reader.fail("format", f'must be "{FORMAT}", got {declared!r}')
    version = reader.integer("format_version", required=True, default=FORMAT_VERSION)
    if version != FORMAT_VERSION:
        reader.fail("format_version", f"must be {FORMAT_VERSION}, got {version}")
    name = reader.string("name", required=True)
    if isinstance(payload.get("name"), str) and not name.strip():
        reader.fail("name", "must be non-empty text")

    grid = _parse_grid(payload, reader, diagnostics, source)
    # Absent means grid north, which is also what a document declaring it says:
    # the compass never re-aims the grid, so the default is not a guess.
    compass = reader.enum("compass", Facing)
    legend = _parse_legend(payload, reader, terrain=terrain)
    palette = _parse_palette(payload, reader, diagnostics, source, terrain=terrain)
    tiles = _parse_tiles(payload, reader, grid, legend)
    elevation = _parse_elevation(payload, reader, diagnostics, grid, source)
    claimed: dict[str, str] = {}
    features = _parse_features(
        payload.get("features", []), reader, diagnostics, grid, source,
        claimed=claimed, terrain=terrain,
    )
    levels = _parse_levels(
        payload, reader, diagnostics, grid, legend, source, claimed, terrain
    )
    levels[GROUND_LEVEL] = MapLevel(
        index=GROUND_LEVEL,
        name="ground",
        tiles=tiles,
        features=features,
        elevation=elevation if elevation is not None else MapElevation(),
        ambient_light=_parse_ambient_light(payload, reader),
    )
    # Every cross-reference rule, once, from the module that owns them. The
    # grid may be ``None`` here — a document whose ``grid`` key was unusable is
    # still walked for everything else it gets wrong — which is why the rules
    # take it as an optional and why this runs before the bail-out below.
    _render(document_findings(levels, grid), reader)
    provenance = _parse_provenance(payload, reader, diagnostics, source)

    if (
        grid is None
        or legend is None
        or elevation is None
        or provenance is None
        or any(d.severity is Severity.ERROR for d in diagnostics)
    ):
        return None
    return MapDocument(
        name=name,
        grid=grid,
        legend=MappingProxyType(dict(legend)),
        provenance=provenance,
        levels=MappingProxyType(levels),
        palette=MappingProxyType(palette),
        compass=compass if compass is not None else Facing.NORTH,
    )


def parse_document(
    payload: Mapping[str, Any], *, source: str, terrain: TerrainTable
) -> MapDocument:
    """Parse and validate one map document, or raise with every diagnostic.

    ``terrain`` is the active terrain table — passed in, never read from module
    state, so a pack-defined kind validates and an unknown one is refused with
    the loaded list, exactly as conditions work.
    """
    diagnostics: list[Diagnostic] = []
    document = _parse(payload, diagnostics, source=source, terrain=terrain)
    if document is None:
        raise MapError(diagnostics)
    return document


def validate_document(
    payload: Mapping[str, Any], *, source: str, terrain: TerrainTable
) -> list[Diagnostic]:
    """Every problem with the document, without raising. The authoring aid."""
    diagnostics: list[Diagnostic] = []
    _parse(payload, diagnostics, source=source, terrain=terrain)
    return diagnostics


# --- serialization ---------------------------------------------------------
def feature_payload(feature: MapFeatureRecord) -> dict[str, Any]:
    """One feature as JSON-ready primitives, omitting the fields it does not carry.

    The omission is what makes :func:`serialize` byte-stable across a parse
    round-trip, so this shape is the document's, not a caller's convenience —
    the render overlay in :mod:`fivee_sim.service.maps` reports features through
    it too, and a second copy would be free to drift from the written form.
    """
    entry: dict[str, Any] = {
        "id": feature.id,
        "kind": feature.kind,
        "at": [feature.at[0], feature.at[1]],
    }
    # Beside ``at``: where it stands and which way it points are one fact about
    # the feature's geometry, and reading them apart helps nobody.
    if feature.facing is not None:
        entry["facing"] = feature.facing
    if feature.orientation is not None:
        entry["orientation"] = feature.orientation
    if feature.hinge is not None:
        entry["hinge"] = feature.hinge
    if feature.swing is not None:
        entry["swing"] = feature.swing
    if feature.state is not None:
        entry["state"] = feature.state
    if feature.linked_to is not None:
        entry["linked_to"] = feature.linked_to
    if feature.team is not None:
        entry["team"] = feature.team
    if feature.to_level is not None:
        entry["to_level"] = feature.to_level
    if feature.sight_to_levels:
        entry["sight_to_levels"] = list(feature.sight_to_levels)
    if feature.light is not None:
        entry["light"] = {
            "bright": feature.light.bright,
            "dim": feature.light.dim,
            "color": feature.light.color,
        }
    if feature.terrain is not None:
        entry["terrain"] = _pair_payload(feature.terrain)
    if feature.elevation is not None:
        entry["elevation"] = _pair_payload(feature.elevation)
    if feature.affects:
        entry["affects"] = [_overlay_payload(overlay) for overlay in feature.affects]
    if feature.requires:
        entry["requires"] = list(feature.requires)
    if feature.trigger is not None:
        entry["trigger"] = {
            "when": {
                name: "open" if expected else "closed"
                for name, expected in feature.trigger.when
            },
            "set": "open" if feature.trigger.set_open else "closed",
            "mode": feature.trigger.mode.value,
        }
    if feature.costs_action:
        entry["costs_action"] = True
    if feature.check is not None:
        entry["check"] = {"ability": feature.check.ability.value, "dc": feature.check.dc}
    return entry


def _pair_payload(pair: TerrainPair | HeightPair) -> dict[str, Any]:
    """One ``{"closed", "open"}`` pair, closed first because that is the default."""
    return {"closed": pair.closed, "open": pair.open}


def _overlay_payload(overlay: MapOverlayRecord) -> dict[str, Any]:
    """One overlay group, its cells sorted by row then column.

    The same canonicalisation the height layer applies to its squares, and for
    the same reason: the service layer may hand a rect's worth of cells over in
    whatever order it expanded them, and painting a flood and painting it back
    should write the bytes it started with.
    """
    entry: dict[str, Any] = {
        "cells": [
            [square[0], square[1]]
            for square in sorted(overlay.cells, key=lambda s: (s[1], s[0]))
        ]
    }
    if overlay.terrain is not None:
        entry["terrain"] = _pair_payload(overlay.terrain)
    if overlay.elevation is not None:
        entry["elevation"] = _pair_payload(overlay.elevation)
    return entry


def _elevation_payload(elevation: MapElevation) -> dict[str, Any] | None:
    """The canonical height block, or ``None`` for a flat plane at zero.

    Squares already sitting at the datum are dropped and the rest sorted by row
    then column, so painting a height and painting it back writes the bytes it
    started with.
    """
    raised = {
        square: feet for square, feet in elevation.squares.items() if feet != elevation.default
    }
    if not raised and not elevation.default:
        return None
    return {
        "default": elevation.default,
        "squares": [
            [square[0], square[1], raised[square]]
            for square in sorted(raised, key=lambda s: (s[1], s[0]))
        ],
    }


def _level_payload(level: MapLevel) -> dict[str, Any]:
    """One storey as JSON-ready primitives. The ground never comes through here."""
    entry: dict[str, Any] = {
        "index": level.index,
        "name": level.name,
        "tiles": list(level.tiles),
    }
    elevation = _elevation_payload(level.elevation)
    if elevation is not None:
        entry["elevation"] = elevation
    if level.ambient_light != LightLevel.BRIGHT.value:
        entry["ambient_light"] = level.ambient_light
    entry["features"] = [feature_payload(feature) for feature in level.features]
    return entry


def _color_payload(color: MapColor) -> str | dict[str, str]:
    """One color, in the shortest shape that says the same thing."""
    if color.light == color.dark:
        return color.light
    return {"light": color.light, "dark": color.dark}


def as_payload(document: MapDocument) -> dict[str, Any]:
    """The document as JSON-ready primitives, in the canonical key order.

    Legend and params are sorted by key; features keep document order and omit
    the optional fields they do not carry. Elevation is canonicalised — squares
    already sitting at the default are dropped, the rest sorted by row then
    column — and the whole key is omitted from a flat map at zero, so a document
    written before heights existed writes back byte-for-byte. The palette sorts by
    kind, writes a matched pair as the single color it is, and is likewise omitted
    when empty, as is a compass pointing at grid north. This is what makes
    :func:`serialize` byte-stable across a parse round-trip.

    The ground plane writes to the top-level ``tiles``/``elevation``/``features``
    keys and the storeys to ``levels``, sorted by index. A map with no storeys
    writes no ``levels`` key at all, which is what keeps a document written
    before floors existed byte-identical too.
    """
    payload: dict[str, Any] = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "name": document.name,
        "grid": {
            "width": document.grid.width,
            "height": document.grid.height,
            "cell_feet": document.grid.cell_feet,
        },
    }
    if document.compass is not Facing.NORTH:
        payload["compass"] = document.compass.value
    payload["legend"] = {glyph: document.legend[glyph] for glyph in sorted(document.legend)}
    if document.palette:
        payload["palette"] = {
            kind: _color_payload(document.palette[kind]) for kind in sorted(document.palette)
        }
    payload["tiles"] = list(document.tiles)
    if document.ground.ambient_light != LightLevel.BRIGHT.value:
        payload["ambient_light"] = document.ground.ambient_light
    elevation = _elevation_payload(document.elevation)
    if elevation is not None:
        payload["elevation"] = elevation
    payload["features"] = [feature_payload(feature) for feature in document.features]
    storeys = [
        _level_payload(document.levels[index])
        for index in sorted(document.levels)
        if index != GROUND_LEVEL
    ]
    if storeys:
        payload["levels"] = storeys
    payload["provenance"] = {
        "generator": document.provenance.generator,
        "seed": document.provenance.seed,
        "params": {
            key: document.provenance.params[key]
            for key in sorted(document.provenance.params)
        },
        "edited": document.provenance.edited,
        "source": document.provenance.source,
    }
    return payload


def serialize(document: MapDocument) -> str:
    """Canonical text for the document: stable key order, LF, trailing newline.

    parse → serialize → parse is byte-stable, pinned by test, so saving an
    unchanged map rewrites identical bytes and version control stays quiet.
    """
    return json.dumps(as_payload(document), indent=2, ensure_ascii=False) + "\n"


# --- encoding generator output ---------------------------------------------
def _elevation_from(generated: GeneratedMap, name: str) -> MapElevation:
    """A generator's dense height grid as the document's datum-plus-sparse form.

    A generator that emits no heights at all gets the flat default, which
    :func:`as_payload` then omits from the file entirely.
    """
    if not generated.elevation:
        return MapElevation()
    if len(generated.elevation) != generated.height or any(
        len(row) != generated.width for row in generated.elevation
    ):
        raise MapError(
            [
                Diagnostic(
                    source=name, section="map", field="elevation",
                    problem=(
                        f"the height grid is not {generated.width}x{generated.height}; "
                        f"a generator emits one height per cell or none at all"
                    ),
                )
            ]
        )
    counts: Counter[int] = Counter(feet for row in generated.elevation for feet in row)
    datum = min(counts, key=lambda feet: (-counts[feet], feet))
    return MapElevation(
        default=datum,
        squares={
            (x, y): feet
            for y, row in enumerate(generated.elevation)
            for x, feet in enumerate(row)
            if feet != datum
        },
    )


def document_from(
    generated: GeneratedMap, *, name: str, generator: str, seed: int, params: Any
) -> MapDocument:
    """Encode a generator's output as a map document.

    Tiles are written through :data:`DEFAULT_LEGEND` — every kind a bundled
    generator emits has a glyph there, and a kind without one is refused
    rather than assigned an invented glyph, because a silently extended
    legend would make two runs of the same generator disagree. ``params`` is
    the generator's params dataclass (or an equivalent mapping), recorded
    fully resolved so the document alone reproduces the map; ``edited``
    starts false and ``source`` says the content is generated and original.

    A generator's dense height grid is reduced here rather than in the
    generator: the commonest height becomes the document's datum and only the
    squares departing from it are written, so the file stays small and a run of
    flat ground costs nothing to record.
    """
    glyph_of = {kind: glyph for glyph, kind in DEFAULT_LEGEND.items()}
    tiles: list[str] = []
    for y, row in enumerate(generated.cells):
        glyphs: list[str] = []
        for x, kind in enumerate(row):
            glyph = glyph_of.get(kind)
            if glyph is None:
                raise MapError(
                    [
                        Diagnostic(
                            source=name, section="map", field="tiles",
                            problem=(
                                f"cell ({x}, {y}) is {kind!r}, which has no glyph in the "
                                f"default legend; add one before encoding"
                            ),
                        )
                    ]
                )
            glyphs.append(glyph)
        tiles.append("".join(glyphs))
    if dataclasses.is_dataclass(params) and not isinstance(params, type):
        resolved: dict[str, Any] = dataclasses.asdict(params)
    else:
        resolved = dict(params)
    return MapDocument(
        name=name,
        grid=MapGrid(width=generated.width, height=generated.height),
        legend=DEFAULT_LEGEND,
        provenance=MapProvenance(
            generator=generator, seed=seed, params=MappingProxyType(resolved),
            edited=False, source=GENERATED_SOURCE,
        ),
        levels=MappingProxyType(
            {
                GROUND_LEVEL: MapLevel(
                    index=GROUND_LEVEL,
                    name="ground",
                    tiles=tuple(tiles),
                    elevation=_elevation_from(generated, name),
                    features=tuple(
                        MapFeatureRecord(
                            id=feature.id, kind=feature.kind, at=feature.at,
                            orientation=feature.orientation, state=feature.state,
                            team=feature.team,
                        )
                        for feature in generated.features
                    ),
                )
            }
        ),
    )
