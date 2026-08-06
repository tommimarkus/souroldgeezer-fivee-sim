"""One statement of the map's rules, held against both readers that render it.

A map document is validated twice, and it has to be: :func:`parse_document`
reads a *file* and owes the author every problem at once, while
``Encounter._adopt_map`` receives an object that may have been hand-built with
no file behind it and owes the fight a refusal before the first roll. Two
readers, two failure shapes — accumulated :class:`~fivee_sim.validation.Diagnostic`
against a fail-fast ``EncounterError``.

They were also two *implementations*, and the drift that predicts is not
hypothetical. Held against each other case by case, the pair disagreed on five
concrete documents:

* a connector leading to its own level — the parser refused, the fight did not;
* ``sight_to_levels`` naming its own level or one the map lacks — the fight
  never looked at ``sight_to_levels`` at all;
* a fixture requiring itself — the fight's catalogue contained it, so the
  "requires something the map does not have" check passed it through;
* a requirement *cycle* — the fight ordered triggers, never requirements;
* a trigger naming one fixture twice — only the fight refused, because the
  parser reads ``when`` as a JSON object whose keys cannot repeat.

So the rules live once, in :mod:`fivee_sim.map_types`, as pure predicates that
*yield* :class:`~fivee_sim.map_types.MapFinding` and decide nothing about how a
refusal reaches anybody. This file is what keeps them one: every case below is
run through **both** readers and both must refuse it.

The case list is checked against :data:`~fivee_sim.map_types.DOCUMENT_RULES`
rather than against itself — a rule added to the tuple with no case here fails,
so the corpus cannot quietly stop covering the thing it exists to cover.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from random import Random
from types import MappingProxyType

import pytest

from fivee_sim.kernel.grid import TERRAIN, Square
from fivee_sim.map_document import MapError, as_payload, parse_document
from fivee_sim.map_types import (
    DOCUMENT_RULES,
    FeatureTrigger,
    MapDocument,
    MapElevation,
    MapFeatureRecord,
    MapFinding,
    MapGrid,
    MapLevel,
    MapOverlayRecord,
    MapProvenance,
    TerrainPair,
    TriggerMode,
    document_findings,
    requirement_findings,
    trigger_findings,
)
from fivee_sim.model.creature import Creature
from fivee_sim.model.encounter import Encounter, EncounterError

LEGEND = {".": "floor", "#": "wall", "%": "difficult", "~": "water"}
PROVENANCE = MapProvenance(
    generator="fixture",
    seed=1,
    params=MappingProxyType({}),
    edited=False,
    source="Original content; 5E-compatible",
)


def level(
    index: int = 0,
    *,
    width: int = 6,
    height: int = 6,
    features: tuple[MapFeatureRecord, ...] = (),
    ambient: str = "bright",
) -> MapLevel:
    """One all-floor storey carrying whatever features the case needs."""
    return MapLevel(
        index=index,
        name=f"level-{index}",
        tiles=tuple("." * width for _ in range(height)),
        features=features,
        elevation=MapElevation(),
        ambient_light=ambient,
    )


def document(*levels: MapLevel, width: int = 6, height: int = 6) -> MapDocument:
    return MapDocument(
        name="probe",
        grid=MapGrid(width=width, height=height),
        legend=LEGEND,
        provenance=PROVENANCE,
        levels=MappingProxyType({one.index: one for one in levels}),
    )


def fixture(
    name: str, at: Square, **extra: object
) -> MapFeatureRecord:
    """A lever: something with a ``state``, so the fight owns it."""
    return MapFeatureRecord(
        id=name,
        kind="lever",
        at=at,
        state="closed",
        terrain=TerrainPair(closed="floor", open="floor"),
        **extra,  # type: ignore[arg-type]
    )


def door(
    name: str,
    at: Square,
    *,
    linked_to: str | None = None,
    orientation: str = "horizontal",
    state: str = "closed",
    **extra: object,
) -> MapFeatureRecord:
    return MapFeatureRecord(
        id=name,
        kind="door",
        at=at,
        orientation=orientation,
        hinge="west" if orientation == "horizontal" else "north",
        swing="north" if orientation == "horizontal" else "west",
        state=state,
        linked_to=linked_to,
        **extra,  # type: ignore[arg-type]
    )


def overlay(*cells: Square) -> MapOverlayRecord:
    return MapOverlayRecord(
        cells=cells, terrain=TerrainPair(closed="floor", open="water")
    )


#: One violating document per rule, keyed by the rule it violates and a label.
#: The rule name is the ``__name__`` of the predicate in
#: :data:`~fivee_sim.map_types.DOCUMENT_RULES`, which is what makes the coverage
#: assertion below derive its obligation instead of restating it.
CASES: dict[str, dict[str, MapDocument]] = {
    "claim_findings": {
        "two fixtures on one square": document(
            level(features=(fixture("a", (3, 4)), fixture("b", (3, 4))))
        ),
        "a fixture claims its own square twice": document(
            level(features=(fixture("a", (3, 4), affects=(overlay((3, 4)),)),))
        ),
        "an overlay reaches another fixture's square": document(
            level(
                features=(
                    fixture("a", (3, 4), affects=(overlay((1, 1)),)),
                    fixture("b", (1, 1)),
                )
            )
        ),
    },
    "reach_findings": {
        "a fixture stands off the map": document(
            level(features=(fixture("a", (9, 9)),))
        ),
        "an overlay reaches off the map": document(
            level(features=(fixture("a", (3, 4), affects=(overlay((9, 9)),)),))
        ),
    },
    "connector_findings": {
        "to_level names a level the map lacks": document(
            level(
                features=(
                    MapFeatureRecord(
                        id="s", kind="stairs_up", at=(1, 1), to_level=7,
                        sight_to_levels=(7,),
                    ),
                )
            )
        ),
        "to_level names its own level": document(
            level(
                features=(
                    MapFeatureRecord(
                        id="s", kind="stairs_up", at=(1, 1), to_level=0,
                        sight_to_levels=(1,),
                    ),
                )
            ),
            level(1),
        ),
        "sight_to_levels names a level the map lacks": document(
            level(
                features=(
                    MapFeatureRecord(
                        id="s", kind="stairs_up", at=(1, 1), sight_to_levels=(7,)
                    ),
                )
            )
        ),
        "sight_to_levels names its own level": document(
            level(
                features=(
                    MapFeatureRecord(
                        id="s", kind="stairs_up", at=(1, 1), sight_to_levels=(0,)
                    ),
                )
            )
        ),
    },
    "requirement_findings": {
        "requires a feature the map does not have": document(
            level(features=(fixture("a", (3, 4), requires=("ghost",)),))
        ),
        "requires itself": document(
            level(features=(fixture("a", (3, 4), requires=("a",)),))
        ),
        "requires a feature with no state": document(
            level(
                features=(
                    fixture("a", (3, 4), requires=("drawn",)),
                    MapFeatureRecord(id="drawn", kind="statue", at=(0, 0)),
                )
            )
        ),
        "requirement cycle": document(
            level(
                features=(
                    fixture("a", (3, 4), requires=("b",)),
                    fixture("b", (1, 1), requires=("a",)),
                )
            )
        ),
    },
    "linked_door_findings": {
        "links to a feature the map does not have": document(
            level(features=(door("d1", (1, 1), linked_to="ghost"),))
        ),
        "the partner does not link back": document(
            level(
                features=(
                    door("d1", (1, 1), linked_to="d2"),
                    door("d2", (2, 1)),
                )
            )
        ),
        "the doors are not adjacent": document(
            level(
                features=(
                    door("d1", (1, 1), linked_to="d2"),
                    door("d2", (4, 1), linked_to="d1"),
                )
            )
        ),
        "the doors start in different states": document(
            level(
                features=(
                    door("d1", (1, 1), linked_to="d2"),
                    door("d2", (2, 1), linked_to="d1", state="open"),
                )
            )
        ),
        "the doors disagree about orientation": document(
            level(
                features=(
                    door("d1", (1, 1), linked_to="d2"),
                    door("d2", (2, 1), linked_to="d1", orientation="vertical"),
                )
            )
        ),
        "the doors stand on different levels": document(
            level(features=(door("d1", (1, 1), linked_to="d2"),)),
            level(1, features=(door("d2", (2, 1), linked_to="d1"),)),
        ),
        "the doors disagree about their interaction contract": document(
            level(
                features=(
                    door("d1", (1, 1), linked_to="d2", costs_action=True),
                    door("d2", (2, 1), linked_to="d1"),
                )
            )
        ),
        "the doors carry different triggers": document(
            level(
                features=(
                    door(
                        "d1", (1, 1), linked_to="d2",
                        trigger=FeatureTrigger(
                            when=(("lv", True),), set_open=True, mode=TriggerMode.EDGE
                        ),
                    ),
                    door("d2", (2, 1), linked_to="d1"),
                    fixture("lv", (5, 5)),
                )
            )
        ),
    },
    "trigger_findings": {
        "references a feature the map does not have": document(
            level(
                features=(
                    fixture(
                        "a", (3, 4),
                        trigger=FeatureTrigger(
                            when=(("ghost", True),), set_open=True,
                            mode=TriggerMode.EDGE,
                        ),
                    ),
                )
            )
        ),
        "references a feature with no state": document(
            level(
                features=(
                    fixture(
                        "a", (3, 4),
                        trigger=FeatureTrigger(
                            when=(("drawn", True),), set_open=True,
                            mode=TriggerMode.EDGE,
                        ),
                    ),
                    MapFeatureRecord(id="drawn", kind="statue", at=(0, 0)),
                )
            )
        ),
        "names no fixture at all": document(
            level(
                features=(
                    fixture(
                        "a", (3, 4),
                        trigger=FeatureTrigger(
                            when=(), set_open=True, mode=TriggerMode.EDGE
                        ),
                    ),
                )
            )
        ),
        "names one fixture twice": document(
            level(
                features=(
                    fixture(
                        "a", (3, 4),
                        trigger=FeatureTrigger(
                            when=(("b", True), ("b", False)), set_open=True,
                            mode=TriggerMode.EDGE,
                        ),
                    ),
                    fixture("b", (1, 1)),
                )
            )
        ),
        "opens without requiring its own prerequisite": document(
            level(
                features=(
                    fixture(
                        "a", (3, 4), requires=("b",),
                        trigger=FeatureTrigger(
                            when=(("c", True),), set_open=True, mode=TriggerMode.EDGE
                        ),
                    ),
                    fixture("b", (1, 1)),
                    fixture("c", (2, 2)),
                )
            )
        ),
        "a maintained trigger contradicts the state it is authored in": document(
            level(
                features=(
                    fixture(
                        "a", (3, 4),
                        trigger=FeatureTrigger(
                            when=(("b", False),), set_open=True,
                            mode=TriggerMode.MAINTAINED,
                        ),
                    ),
                    fixture("b", (1, 1)),
                )
            )
        ),
        "trigger cycle": document(
            level(
                features=(
                    fixture(
                        "a", (3, 4),
                        trigger=FeatureTrigger(
                            when=(("b", True),), set_open=True, mode=TriggerMode.EDGE
                        ),
                    ),
                    fixture(
                        "b", (1, 1),
                        trigger=FeatureTrigger(
                            when=(("a", True),), set_open=True, mode=TriggerMode.EDGE
                        ),
                    ),
                )
            )
        ),
    },
}


#: Cases a *file* cannot express, so only a hand-built document ever violates
#: them. Named rather than dropped: the parity assertion below still runs on
#: each, in the one direction that is meaningful — it proves the round trip
#: through :func:`~fivee_sim.map_document.as_payload` really does destroy the
#: violation, which is what makes the parser's silence correct rather than a
#: sixth divergence.
#:
#: ``when`` is a JSON object. Two entries for one fixture collapse to one on the
#: way out and there is no syntax that would keep them, so a parser refusing it
#: could never fire. The rule stays in :mod:`fivee_sim.map_types` because a
#: :class:`~fivee_sim.map_types.FeatureTrigger` built in memory is a plain tuple
#: and can carry the duplicate all the way into a fight.
UNWRITABLE: frozenset[tuple[str, str]] = frozenset(
    {("trigger_findings", "names one fixture twice")}
)


def every_case() -> Iterator[tuple[str, str, MapDocument]]:
    for rule, cases in CASES.items():
        for label, doc in cases.items():
            yield rule, label, doc


def combatants() -> list[Creature]:
    """Two fighters standing on plain floor, well clear of every fixture above."""
    return [
        Creature(name="Thora", max_hp=10, ac=10, position=(0, 0), team="party"),
        Creature(name="Wolf", max_hp=10, ac=10, position=(25, 25), team="foes"),
    ]


def start(doc: MapDocument) -> Encounter:
    return Encounter(
        combatants(), Random(1), map_document=doc, terrain_effects=TERRAIN
    )


def reparse(doc: MapDocument) -> MapDocument:
    return parse_document(as_payload(doc), source="probe", terrain=TERRAIN)


class TestTheRulesAreOne:
    """Both readers refuse the same documents, and neither has a rule alone."""

    def test_every_rule_has_a_case(self) -> None:
        """The corpus's obligation comes from the rule table, not from itself.

        A seventh rule appended to ``DOCUMENT_RULES`` with nothing here to
        violate it fails this, which is the only thing stopping the corpus from
        going stale the moment the rules grow.
        """
        declared = {rule.__name__ for rule in DOCUMENT_RULES}
        assert declared == set(CASES), (
            "every rule in map_types.DOCUMENT_RULES needs a violating document "
            f"in CASES and vice versa; declared={sorted(declared)} "
            f"covered={sorted(CASES)}"
        )

    @pytest.mark.parametrize(
        ("rule", "label", "doc"),
        [(rule, label, doc) for rule, label, doc in every_case()],
        ids=[f"{rule}: {label}" for rule, label, _ in every_case()],
    )
    def test_the_rule_that_owns_the_case_is_the_one_that_fires(
        self, rule: str, label: str, doc: MapDocument
    ) -> None:
        """Each case violates the rule it is filed under, and says so.

        Filed by rule rather than merely collected, because a case that happens
        to trip a *different* predicate would keep its own rule untested while
        looking covered.
        """
        by_name = {one.__name__: one for one in DOCUMENT_RULES}
        found = list(by_name[rule](doc.levels, doc.grid))
        assert found, f"{rule} found nothing wrong with {label!r}"
        assert all(isinstance(one, MapFinding) for one in found)
        assert all(one.message for one in found), "a finding must say something"

    @pytest.mark.parametrize(
        ("rule", "label", "doc"),
        [(rule, label, doc) for rule, label, doc in every_case()],
        ids=[f"{rule}: {label}" for rule, label, _ in every_case()],
    )
    def test_both_readers_refuse_it(
        self, rule: str, label: str, doc: MapDocument
    ) -> None:
        """The parser refuses the file and the fight refuses the object.

        This is the assertion the five measured divergences failed. It does not
        pin the wording — :data:`CASES` is wide and the messages are pinned
        one at a time in ``test_map_document`` and ``test_encounter`` — it pins
        that neither reader has an opinion the other lacks.

        A :data:`UNWRITABLE` case is held to the claim that earns its exemption
        instead: the violation must be *gone* after a round trip, so the parser
        has nothing left to refuse.
        """
        with pytest.raises(EncounterError, match="."):
            start(doc)
        if (rule, label) in UNWRITABLE:
            by_name = {one.__name__: one for one in DOCUMENT_RULES}
            written = reparse(doc)
            assert not list(by_name[rule](written.levels, written.grid)), (
                f"{label!r} survived a round trip through as_payload, so a file "
                f"can express it after all and the parser must refuse it too"
            )
            return
        with pytest.raises(MapError, match="map error"):
            reparse(doc)


#: How long a chain the scale cases below build. Sized against the two costs
#: it has to separate rather than picked round: cycle detection over a chain
#: this long is a fifth of a second when it is linear in the chain and about
#: seventeen seconds when it is one graph walk per node, which was the shape
#: it had. A document of this many features serialises to roughly 1.8 MiB of
#: compact JSON — well under ``MAX_MAP_BYTES`` — so nothing else in the engine
#: refuses it on the way in, and the byte cap cannot stand in for this bound.
CHAIN = 20_000

#: The ceiling all three scale cases are held to, and the two margins it was
#: chosen to sit between. Measured on the machine this was written on: the
#: linear form answers the slowest of the three in 0.09s and the quadratic one
#: took 17.4s (162s for the cycle). So the ceiling is roughly 20x the linear
#: cost and 9x under the quadratic one — a host twenty times slower than this
#: still passes, and one nine times faster still fails a quadratic
#: implementation. Neither margin is a coin toss, which is the whole
#: justification for putting a clock in a test suite.
CHAIN_SECONDS = 2.0


def chained_levers(count: int, *, closed: bool = False) -> MapDocument:
    """``count`` levers, each waiting on the one before it.

    A puzzle room's lever chain, only longer: nothing here is malformed, and
    with ``closed`` the last lever waits on the first, which is the one
    refusal such a room can earn.
    """
    width = 200
    height = -(-count // width)
    levers = tuple(
        MapFeatureRecord(
            id=f"lever-{index:05d}",
            kind="lever",
            at=(index % width, index // width),
            state="closed",
            terrain=TerrainPair(closed="floor", open="floor"),
            requires=(
                (f"lever-{count - 1:05d}",)
                if index == 0 and closed
                else (f"lever-{index - 1:05d}",) if index else ()
            ),
        )
        for index in range(count)
    )
    return MapDocument(
        name="lever room",
        grid=MapGrid(width=width, height=height),
        legend=LEGEND,
        provenance=PROVENANCE,
        levels=MappingProxyType(
            {
                0: MapLevel(
                    index=0,
                    name="level-0",
                    tiles=tuple("." * width for _ in range(height)),
                    features=levers,
                )
            }
        ),
    )


class TestTheDependencyRulesScaleWithTheDocument:
    """A long dependency chain costs time in the length of it, not its square.

    ``requirement_findings`` and ``trigger_findings`` both look for cycles, and
    the search used to ask "what can this node reach?" once per node with an
    edge — a fresh walk of the whole graph each time. On a chain that is
    O(N**2), and a chain is not an adversarial document: a puzzle room whose
    levers each wait on the one before it is exactly this graph.

    The cost is paid twice per ``encounter.create``, once when the parser reads
    the file and once when ``_adopt_map`` re-asks the same questions of the
    parsed document, and the byte cap does not bound it — see :data:`CHAIN`.

    **Why a clock and not a counter.** The property is a complexity class, and
    the only thing a caller ever feels of it is seconds. Counting graph walks
    would pin the current implementation's shape instead — a second linear
    algorithm that happened to walk twice would fail a counter and satisfy the
    claim. The flakiness that usually argues against a clock is a narrow
    margin; the ceiling here sits about 20x above the measured linear cost and
    9x below the measured quadratic one (see :data:`CHAIN_SECONDS`), and the
    case does no I/O and takes no lock.
    """

    def test_a_long_requirement_chain_is_answered_in_the_length_of_it(self) -> None:
        document = chained_levers(CHAIN)
        started = time.perf_counter()
        found = list(requirement_findings(document.levels, document.grid))
        elapsed = time.perf_counter() - started

        assert found == [], "a plain chain of prerequisites is a legal document"
        assert elapsed < CHAIN_SECONDS, (
            f"{CHAIN} chained requirements took {elapsed:.1f}s, which is the "
            f"quadratic cost rather than the linear one"
        )

    def test_a_long_trigger_chain_is_answered_in_the_length_of_it(self) -> None:
        # The same search, reached by the other rule, so a fix applied to one
        # caller and not the other cannot pass this class.
        source = chained_levers(CHAIN)
        plane = source.levels[0]
        triggered = tuple(
            MapFeatureRecord(
                id=one.id,
                kind=one.kind,
                at=one.at,
                state=one.state,
                terrain=one.terrain,
                trigger=(
                    FeatureTrigger(
                        when=((one.requires[0], True),),
                        set_open=False,
                        mode=TriggerMode.EDGE,
                    )
                    if one.requires
                    else None
                ),
            )
            for one in plane.features
        )
        levels = MappingProxyType(
            {0: MapLevel(index=0, name="level-0", tiles=plane.tiles, features=triggered)}
        )

        started = time.perf_counter()
        found = list(trigger_findings(levels, source.grid))
        elapsed = time.perf_counter() - started

        assert found == [], "a plain chain of triggers is a legal document"
        assert elapsed < CHAIN_SECONDS, (
            f"{CHAIN} chained triggers took {elapsed:.1f}s, which is the "
            f"quadratic cost rather than the linear one"
        )

    def test_a_cycle_closing_a_long_chain_is_still_reported_whole(self) -> None:
        """Speed that lost the refusal would be no bargain, and this is the
        slower half.

        Finding the cycle is one search; *naming* it was a second quadratic —
        the walk carried a whole candidate path per queued node, so reporting
        one cycle through N fixtures copied O(N**2) ids. This document measured
        169 seconds against the 17 the acyclic chain above cost, so the refusal
        was ten times more expensive to produce than the acceptance.

        The path is the whole cycle, from the lexicographically smallest id in
        it, exactly as the two-fixture case in ``test_map_document`` pins for a
        short one.
        """
        document = chained_levers(CHAIN, closed=True)
        started = time.perf_counter()
        found = list(requirement_findings(document.levels, document.grid))
        elapsed = time.perf_counter() - started

        assert elapsed < CHAIN_SECONDS, (
            f"naming one cycle through {CHAIN} fixtures took {elapsed:.1f}s, "
            f"which is the quadratic cost rather than the linear one"
        )
        assert len(found) == 1
        message = found[0].message
        assert message.startswith(
            "feature 'lever-00000' is in a requirement cycle: lever-00000 -> "
            f"lever-{CHAIN - 1:05d} -> lever-{CHAIN - 2:05d} -> "
        )
        assert message.endswith(
            "lever-00001 -> lever-00000; nothing in it could ever be opened first"
        )
        assert message.count(" -> ") == CHAIN


class TestAFindingSaysWhereAndWhat:
    """The finding type carries what *both* renderers need and nothing more."""

    def test_a_finding_names_a_path_and_a_message(self) -> None:
        found = MapFinding(path="features", message="something is wrong")
        assert (found.path, found.message) == ("features", "something is wrong")

    def test_a_finding_is_a_refusal_unless_it_says_otherwise(self) -> None:
        """``refusal`` is the rule's own word, not the reporter's severity.

        The fight has nowhere to put advice — it either starts or it does not —
        so a non-refusal finding is dropped there and warned about by the
        parser. Defaulting to a refusal keeps a rule that forgets to say from
        being silently advisory.
        """
        assert MapFinding(path="features", message="x").refusal is True
        assert MapFinding(path="features", message="x", refusal=False).refusal is False

    def test_a_connector_with_no_sight_link_is_advice_and_not_a_refusal(self) -> None:
        """The one non-refusal the rules produce, and the fight must start anyway.

        A storey reached by a stair that declares no ``sight_to_levels`` is
        sealed rather than malformed — occasionally deliberate, more often
        forgotten — so the parser warns and the fight says nothing at all.
        """
        doc = document(
            level(
                features=(
                    MapFeatureRecord(id="s", kind="stairs_up", at=(1, 1), to_level=1),
                )
            ),
            level(1),
        )
        advisory = [
            one
            for one in document_findings(doc.levels, doc.grid)
            if not one.refusal
        ]
        assert advisory, "a connector with no sight link should be warned about"
        assert "sight_to_levels" in advisory[0].message
        assert not [one for one in document_findings(doc.levels, doc.grid) if one.refusal]
        start(doc)  # the fight starts: advice is not a refusal
