"""The map's types: what a map *is*, with nothing about how a file is read.

This module holds the document tree — :class:`MapDocument` and everything it is
made of — and the small vocabulary of pairs and enums a fixture is described
with. It holds no parser, no validator, and no diagnostic.

That split is the whole point of the module existing. :mod:`fivee_sim.validation`
is how a *file* enters the engine, and the parsing in
:mod:`fivee_sim.map_document` is built on it; but nothing below needs it, because
a dataclass does not read a file. So the types can sit in a module whose entire
import list is :mod:`fivee_sim.kernel.grid`, :mod:`fivee_sim.kernel.rules` and
the standard library, and a caller that wants to *hold* a map does not have to
drag the machinery for *reading* one in behind it.

Keep it that way. An import of ``validation`` here — or of anything that imports
it — puts the parser back into the dependency graph of everything that names a
map, and the module stops paying for itself.

:mod:`fivee_sim.map_document` re-exports every public name below, so the format's
own module remains the one door a reader of the file format needs.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .kernel.grid import FEET_PER_SQUARE, Facing, Square
from .kernel.rules import Ability

__all__ = [
    "DEFAULT_LEGEND",
    "GROUND_LEVEL",
    "RESERVED_GLYPHS",
    "FeatureCheck",
    "FeatureTrigger",
    "HeightPair",
    "LightLevel",
    "MapColor",
    "MapDocument",
    "MapElevation",
    "MapFeatureRecord",
    "MapGrid",
    "MapLevel",
    "MapLight",
    "MapOverlayRecord",
    "MapProvenance",
    "SquareClaim",
    "TerrainPair",
    "TriggerMode",
    "allocate_legend",
]

#: The level every fight starts on, and the only one a map without storeys has.
#: One constant for the document and the battle map alike: they were two, agreeing
#: by convention, which is a disagreement waiting to be written.
#:
#: It is also the one level the *file* keeps in its top-level
#: ``tiles``/``elevation``/``features`` keys rather than in ``levels``, so a
#: document with no storeys is byte-identical to one written before floors
#: existed.
GROUND_LEVEL = 0


# --- the vocabulary a fixture is described in -------------------------------
class LightLevel(StrEnum):
    BRIGHT = "bright"
    DIM = "dim"
    DARKNESS = "darkness"


@dataclass(frozen=True, slots=True)
class TerrainPair:
    """What one square is in each of a fixture's two states."""

    closed: str
    open: str


@dataclass(frozen=True, slots=True)
class HeightPair:
    """Ground height in feet in each of a fixture's two states.

    Optional everywhere a :class:`TerrainPair` is required, because most
    fixtures change what a square *is* without moving what it *sits at*. A
    sluice does both: the room becomes water, and the water is lower than the
    floor was.
    """

    closed: int
    open: int


@dataclass(frozen=True, slots=True)
class FeatureCheck:
    """The roll operating a fixture takes, if it takes one.

    A raw ability check: creatures carry ability modifiers and no skill
    proficiencies, so a DC here is set as if untrained.
    """

    ability: Ability
    dc: int


class TriggerMode(StrEnum):
    """When an active fixture predicate applies its configured state."""

    EDGE = "edge"
    MAINTAINED = "maintained"


@dataclass(frozen=True, slots=True)
class FeatureTrigger:
    """A target-local AND predicate over fixture states.

    ``when`` is sorted by fixture name at the document boundary. A tuple keeps
    the runtime definition immutable and makes linked-door equality structural.
    """

    when: tuple[tuple[str, bool], ...]
    set_open: bool
    mode: TriggerMode

    def active(self, open_features: Collection[str]) -> bool:
        return all((name in open_features) is expected for name, expected in self.when)


@dataclass(frozen=True, slots=True)
class SquareClaim:
    """What one square is, in either state, and which fixture decides it."""

    feature: str
    terrain: TerrainPair | None = None
    elevation: HeightPair | None = None


# --- the legend -------------------------------------------------------------
#: Glyphs the renderers draw *over* the terrain — doors, stairs, spawn marks.
#: A legend may not claim them, or a rendered map would be ambiguous.
RESERVED_GLYPHS = frozenset("+/<>@")

#: The glyph table the generators encode with. A document may define its own;
#: this one is the shared default, and every terrain kind a bundled generator
#: emits has an entry here.
DEFAULT_LEGEND: Mapping[str, str] = MappingProxyType(
    {
        ".": "floor",
        "#": "wall",
        "~": "water",
        ",": "plain",
        "T": "forest",
        "h": "hill",
        "^": "mountain",
        "%": "difficult",
    }
)

#: What :func:`allocate_legend` draws from once the author's own glyphs and
#: :data:`DEFAULT_LEGEND` are exhausted, with the renderer's overlay marks
#: filtered out rather than merely absent — the pool is a literal and the
#: reservation is the rule, so the rule does the removing.
_GLYPH_POOL: tuple[str, ...] = tuple(
    char
    for char in ".#~,:;!?$&*abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    if char not in RESERVED_GLYPHS
)
#: :data:`DEFAULT_LEGEND` read the way an allocator wants it.
_DEFAULT_GLYPH_OF: Mapping[str, str] = MappingProxyType(
    {kind: glyph for glyph, kind in DEFAULT_LEGEND.items()}
)


def allocate_legend(
    kinds: Iterable[str], *, prefer: Mapping[str, str] | None = None
) -> dict[str, str]:
    """A glyph for every kind in ``kinds``, as the document writes a legend.

    ``prefer`` is a legend somebody already wrote — glyph to kind, the format's
    own direction — and it is honoured wherever it is legal, because rewriting
    an author's ``#`` for wall into an allocator's ``b`` helps nobody reading
    the file afterwards. Exactly one thing overrides it: a glyph in
    :data:`RESERVED_GLYPHS`, which the renderers draw *over* the terrain and
    which :func:`~fivee_sim.map_document.parse_document` refuses in a legend. A
    caller that hands one over gets that kind moved and nothing else.

    Below the author come :data:`DEFAULT_LEGEND`'s glyphs, so a document built
    here spells floor ``.`` and wall ``#`` like a generated one, and below that
    :data:`_GLYPH_POOL` in sorted-kind order, which makes the result a function
    of the kinds and not of the order they were discovered in. A map with more
    terrain kinds than the pool has glyphs falls back to the private-use plane:
    unreadable, but single characters the format accepts, which beats refusing a
    document over a legend nobody has to read.

    It lives here rather than beside the parser because :meth:`MapDocument.flat`
    cannot build a document without it, and a constructor that had to reach into
    the parser's module for a glyph table would be the cycle this split exists to
    avoid. It reads no file and raises no diagnostic, so it belongs on this side.
    """
    chosen: dict[str, str] = {}  # kind -> glyph
    if prefer:
        for glyph in sorted(prefer):
            if len(glyph) == 1 and glyph not in RESERVED_GLYPHS:
                chosen.setdefault(prefer[glyph], glyph)

    legend: dict[str, str] = {}
    taken: set[str] = set()
    unplaced = sorted(set(kinds))
    for tier in (chosen, _DEFAULT_GLYPH_OF):
        still: list[str] = []
        for kind in unplaced:
            preferred = tier.get(kind)
            if preferred is None or preferred in taken:
                still.append(kind)
                continue
            legend[preferred] = kind
            taken.add(preferred)
        unplaced = still

    pool = (char for char in _GLYPH_POOL if char not in taken)
    for index, kind in enumerate(unplaced):
        glyph = next(pool, None) or chr(0xE000 + index)
        legend[glyph] = kind
        taken.add(glyph)
    return legend


# --- the document tree ------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MapGrid:
    """The document's dimensions, in squares, with the cell size spelt out."""

    width: int
    height: int
    cell_feet: int = FEET_PER_SQUARE


@dataclass(frozen=True, slots=True)
class MapElevation:
    """Ground height in feet: a default, and the squares that differ from it.

    Heights are plain feet and may be negative — a pit floor is below the datum
    the rest of the map sits on. The default instance is a flat map at zero, and
    it is the one shape :func:`~fivee_sim.map_document.as_payload` leaves out of
    the document entirely.
    """

    default: int = 0
    squares: Mapping[Square, int] = dataclasses.field(default_factory=dict)

    def at(self, square: Square) -> int:
        return self.squares.get(square, self.default)


@dataclass(frozen=True, slots=True)
class MapColor:
    """One terrain kind's authored fill, per theme.

    Both values are canonical ``#rrggbb`` in lowercase, whatever the file spelled.
    A document naming one color parses to a pair whose themes match, and that is
    the shape :func:`~fivee_sim.map_document.as_payload` writes back as the
    single color it came from.
    """

    light: str
    dark: str


@dataclass(frozen=True, slots=True)
class MapOverlayRecord:
    """Squares a fixture governs beyond its own, as the document records them.

    A canonically-sorted list of cells, because that is what the file wants to
    write back byte-for-byte. A fight reads it through
    :meth:`MapFeatureRecord.claims`, which flattens every overlay into the
    square-keyed index a pathfinding loop can afford — once, at adoption. There
    was a runtime ``FeatureOverlay`` beside this holding the same three fields
    in a second shape, and one translation between the two; both are gone.
    """

    cells: tuple[Square, ...]
    terrain: TerrainPair | None = None
    elevation: HeightPair | None = None


@dataclass(frozen=True, slots=True)
class MapLight:
    """An authored light attached to a feature square."""

    bright: int = 0
    dim: int = 0
    color: str = "#ffffff"


@dataclass(frozen=True, slots=True)
class MapFeatureRecord:
    """One feature as the document records it — defaults, not live state.

    ``to_level`` is what makes a stairway more than a drawn glyph: it names the
    level the feature leads to, and the square it lands on is the one it stands
    on. A feature without it is an ordinary fixture that goes nowhere.

    ``state`` is what makes a feature something the fight can *operate*, and the
    seven keys after it are what operating it does and costs: what its own square
    becomes (``terrain``, ``elevation``), what else changes with it
    (``affects``), what must already stand open (``requires``), and what the
    attempt spends and rolls (``costs_action``, ``check``), and what may operate
    it automatically (``trigger``). All seven are optional and omitted on write,
    so a file that predates them is unchanged
    by a round trip.

    ``facing`` is which way it points — an arrow slit out of the corridor, a
    statue down it — in the eight :class:`~fivee_sim.kernel.grid.Facing` names.
    Grid-relative like everything else here, and refused on a door, which
    already answers the question three ways over. A plain ``str``, like a
    condition: what the eight are is the vocabulary's business, not this
    record's.
    """

    id: str
    kind: str
    at: Square
    facing: str | None = None
    orientation: str | None = None
    hinge: str | None = None
    swing: str | None = None
    state: str | None = None
    linked_to: str | None = None
    team: str | None = None
    to_level: int | None = None
    sight_to_levels: tuple[int, ...] = ()
    light: MapLight | None = None
    terrain: TerrainPair | None = None
    elevation: HeightPair | None = None
    affects: tuple[MapOverlayRecord, ...] = ()
    requires: tuple[str, ...] = ()
    trigger: FeatureTrigger | None = None
    costs_action: bool = False
    check: FeatureCheck | None = None

    def own_terrain(self, level: MapLevel, legend: Mapping[str, str]) -> TerrainPair:
        """What this fixture's own square is in each state, when it does not say.

        A door is what a door has always been — the hardcoded pair, now merely
        expressible. Anything else is the tile it stands on, in *both* states, so
        a lever driven into a wall leaves a wall behind it whichever way it is
        thrown.

        It takes the **level**, not the document, and that is not a convenience.
        A signature reaching for ``document.tiles`` reads the ground plane
        whatever storey it was asked about, so every lever, spike and pressure
        plate upstairs would quietly take the terrain of the room below it — and
        on the ground floor, where such a rule is usually written and tested, the
        two answers are identical.
        """
        if self.terrain is not None:
            return self.terrain
        if self.kind == "door":
            return TerrainPair(closed="door-closed", open="door-open")
        kind = level.terrain_at(self.at, legend)
        return TerrainPair(closed=kind, open=kind)

    def claims(
        self, level: MapLevel, legend: Mapping[str, str]
    ) -> Iterator[tuple[Square, SquareClaim]]:
        """Every square this fixture decides, and what it decides about it.

        The single derivation of it, and the yield order is part of the answer:
        the fixture's own square first, then each overlay's cells in the order
        the file wrote them. Both real callers — ``Encounter._adopt_map`` and
        :class:`~fivee_sim.service.maps.ResolvedLevel` — build a ``dict`` from
        this, where the last claim for a square wins, so the order decides which
        of two conflicting claims survives, and therefore which one the refusal
        that follows names.

        The own square's pair comes from :meth:`own_terrain` rather than straight
        off ``terrain``, which is optional: a claim carrying no terrain falls
        through to the plane, and a door that fell through would stop being a
        door.

        It reports rather than polices: a square named twice is yielded twice,
        because the document parser refuses that, and it can only refuse what it
        can see.
        """
        yield self.at, SquareClaim(
            feature=self.id,
            terrain=self.own_terrain(level, legend),
            elevation=self.elevation,
        )
        for overlay in self.affects:
            for square in overlay.cells:
                yield square, SquareClaim(
                    feature=self.id,
                    terrain=overlay.terrain,
                    elevation=overlay.elevation,
                )


@dataclass(frozen=True, slots=True)
class MapLevel:
    """One storey: a full plane of tiles, heights, and fixtures over the grid.

    Every level shares the document's ``grid`` and ``legend`` — floors of one
    building, not unrelated maps — so only what differs between them lives here.
    ``elevation.default`` is the level's own datum, which is how a first floor
    sits ten feet above the ground one without a second concept for it.
    """

    index: int
    name: str
    tiles: tuple[str, ...]
    features: tuple[MapFeatureRecord, ...]
    elevation: MapElevation = dataclasses.field(default_factory=MapElevation)
    ambient_light: str = "bright"

    def terrain_at(self, square: Square, legend: Mapping[str, str]) -> str:
        """The terrain kind at one square of *this* storey.

        The tiles are dense, so there is nothing to fall back to and no default
        to compute: the glyph is there, and the legend says what it means.

        A square off the grid raises :class:`KeyError` rather than answering.
        Python would read ``tiles[-1][-1]`` as the far corner with a straight
        face, and a reader that asked about a square which is not there has a
        defect rather than a terrain kind.

        Only the *negative* half of that is a check: an index past the end
        raises :class:`IndexError` on its own, and the ``try`` costs nothing on
        the path that does not take it. This is the hottest read in the engine —
        every step of every route goes through ``Encounter._terrain_at_level``
        and lands here — and the arithmetic form measured 64 ns against this
        one's 42, on a floor of 36 with no check at all.
        """
        x, y = square
        if x < 0 or y < 0:
            raise KeyError(square)
        try:
            return legend[self.tiles[y][x]]
        except IndexError:
            raise KeyError(square) from None

    def fixtures(self) -> Mapping[str, MapFeatureRecord]:
        """This storey's features a fight can operate, keyed by id.

        Carrying a ``state`` is what makes a feature a fixture the fight owns,
        and not being a door: a spawn hint and a drawn stairway have none and
        stay document-level, while a spike, a lever and a sluice gate have one.
        The single statement of that gate — :meth:`MapDocument.fixtures` merges
        what this returns rather than testing ``state`` a second time.
        """
        return MappingProxyType(
            {feature.id: feature for feature in self.features if feature.state is not None}
        )

    def connectors(self) -> Mapping[Square, int]:
        """Where a creature standing here can step to another storey.

        Read off every feature and not only the fixtures: a drawn stairway
        carries no ``state``, which is exactly why it is a connector rather than
        something to throw.
        """
        return MappingProxyType(
            {
                feature.at: feature.to_level
                for feature in self.features
                if feature.to_level is not None
            }
        )

    def sight_links(self) -> Mapping[Square, frozenset[int]]:
        """Squares that see onto other storeys. Floors are opaque everywhere else."""
        return MappingProxyType(
            {
                feature.at: frozenset(feature.sight_to_levels)
                for feature in self.features
                if feature.sight_to_levels
            }
        )

    def lights(self) -> tuple[tuple[Square, MapLight], ...]:
        """Every authored light on this storey, paired with the square it burns on.

        A tuple of pairs rather than a square-keyed table, in document order: two
        features may stand on one square, and a mapping would quietly keep one of
        them.
        """
        return tuple(
            (feature.at, feature.light)
            for feature in self.features
            if feature.light is not None
        )


@dataclass(frozen=True, slots=True)
class MapProvenance:
    """Where the map came from, completely enough to regenerate it.

    ``params`` is fully resolved — defaults included — so the document alone
    reproduces the map; ``edited`` records that a human or a tool has touched
    the tiles since, at which point the file, not the generator, is the truth.
    """

    generator: str
    seed: int
    params: Mapping[str, Any]
    edited: bool
    source: str


@dataclass(frozen=True, slots=True)
class MapDocument:
    """One parsed, validated map file. Frozen: every edit builds a new one.

    ``levels`` always holds :data:`GROUND_LEVEL`, and holds only that for a map
    with no storeys. The ground is reachable as :attr:`ground`, and the three
    accessors below read it, because that is what a caller asking a map for its
    tiles has always meant — the storeys are the addition, never a repointing.
    """

    name: str
    grid: MapGrid
    legend: Mapping[str, str]
    provenance: MapProvenance
    levels: Mapping[int, MapLevel]
    #: Terrain colors the document names for itself. Document-wide rather than
    #: per level, like the legend: a kind that looks one way downstairs and
    #: another way up is two kinds.
    palette: Mapping[str, MapColor] = dataclasses.field(default_factory=dict)
    #: Where *true* north lies, for a compass rose and for narration. It
    #: redefines nothing: grid north is −y here permanently, because four of
    #: these eight names are already spent on door hinge and swing and mean −y
    #: and +y on every map already saved. A document is free to say true north
    #: is east; its horizontal doors still hinge west or east and swing north or
    #: south. Document-wide like the legend — a storey of a building does not
    #: get its own north — and omitted on write when it is the default, so a
    #: file that predates it round-trips byte-for-byte.
    compass: Facing = Facing.NORTH

    @property
    def ground(self) -> MapLevel:
        return self.levels[GROUND_LEVEL]

    @property
    def tiles(self) -> tuple[str, ...]:
        return self.ground.tiles

    @property
    def features(self) -> tuple[MapFeatureRecord, ...]:
        return self.ground.features

    @property
    def elevation(self) -> MapElevation:
        return self.ground.elevation

    def fixtures(self) -> Mapping[str, MapFeatureRecord]:
        """Every storey's fixtures under one name table, the ground's first.

        Feature ids are unique across a whole document, so the order is not a
        precedence rule — it is what makes the merge deterministic, and it is
        the order ``Encounter._fixtures`` and every refusal that lists the map's
        fixtures answer in.

        The ``state`` gate itself lives on :meth:`MapLevel.fixtures`; this
        merges. A second ``state is None`` written here is how a document and a
        storey would start disagreeing about what a fight owns.
        """
        merged: dict[str, MapFeatureRecord] = {}
        for index in sorted(self.levels):
            merged.update(self.levels[index].fixtures())
        return MappingProxyType(merged)

    def level_of(self, feature_name: str) -> int:
        """Which storey holds a named fixture. Raises :class:`KeyError` if none does.

        Fixtures, not features: a spawn hint is not something a fight owns, so
        a caller asking which storey one stands on is asking about something
        that is not there to be reached.
        """
        for index in sorted(self.levels):
            if feature_name in self.levels[index].fixtures():
                return index
        raise KeyError(feature_name)

    @classmethod
    def flat(
        cls,
        *,
        name: str,
        width: int,
        height: int,
        default_terrain: str = "normal",
        terrain: Mapping[Square, str] | None = None,
        default_elevation: int = 0,
        elevation: Mapping[Square, int] | None = None,
        features: Sequence[MapFeatureRecord] = (),
        legend: Mapping[str, str] | None = None,
        ambient_light: str = LightLevel.BRIGHT.value,
        provenance: MapProvenance | None = None,
    ) -> MapDocument:
        """A one-plane document, built from the shape a caller already has.

        The shape a caller who is not writing a file already has: a default
        terrain kind and the squares that differ from it, a default height and
        the squares that differ from that, and the fixtures. What it adds is the
        three things the *format* wants and that caller does not have — a
        legend, dense ``tiles``, and a provenance — none of which anybody should
        have to spell to say "a 20x20 room with a wall down one side".

        ``legend`` is a preference, not a requirement: see
        :func:`allocate_legend` for what is honoured and what is moved. The
        tiles are written from the *allocation*, so a reallocated glyph cannot
        leave a row pointing at a legend entry that no longer exists.

        A square outside the grid is dropped from both layers rather than
        recorded: a document would write it into the file and
        :func:`~fivee_sim.map_document.parse_document` would refuse to read the
        file back.

        Terrain *kinds* are not resolved here — this builds a document, it does
        not validate one against a content table — so the caller that knows
        which terrain table is active is the one that owes the refusal.
        """
        squares = {
            square: kind
            for square, kind in (terrain or {}).items()
            if 0 <= square[0] < width and 0 <= square[1] < height
        }
        allocated = allocate_legend({default_terrain, *squares.values()}, prefer=legend)
        glyph_of = {kind: glyph for glyph, kind in allocated.items()}
        tiles = tuple(
            "".join(glyph_of[squares.get((x, y), default_terrain)] for x in range(width))
            for y in range(height)
        )
        return cls(
            name=name,
            grid=MapGrid(width=width, height=height),
            legend=MappingProxyType(allocated),
            provenance=provenance if provenance is not None else _CALLER_PROVENANCE,
            levels=MappingProxyType(
                {
                    GROUND_LEVEL: MapLevel(
                        index=GROUND_LEVEL,
                        name="ground",
                        tiles=tiles,
                        features=tuple(features),
                        elevation=MapElevation(
                            default=default_elevation,
                            squares=MappingProxyType(
                                {
                                    square: feet
                                    for square, feet in (elevation or {}).items()
                                    if 0 <= square[0] < width and 0 <= square[1] < height
                                }
                            ),
                        ),
                        ambient_light=ambient_light,
                    )
                }
            ),
        )


#: What :meth:`MapDocument.flat` records when the caller says nothing. A map
#: built in memory came from whoever built it, and the format still insists on
#: being told: ``seed`` and ``params`` are empty because there is no generator
#: run to reproduce, and ``edited`` is false because nobody has touched it since.
_CALLER_PROVENANCE = MapProvenance(
    generator="flat",
    seed=0,
    params=MappingProxyType({}),
    edited=False,
    source="Caller-supplied map",
)
