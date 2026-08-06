"""The layer boundaries, enforced over the engine's own import graph.

CLAUDE.md states the dependency direction as a fact about this tree:
``web`` -> ``service`` -> the root tier
(``catalog``/``content``/``map_document``/``map_types``/``validation``/``coverage``)
-> ``model`` -> ``kernel``, with ``kernel`` importing nothing outside itself.
Nothing checked it, and it had already drifted:
``fivee_sim/data/__init__.py`` — a package named for the JSON files it carries —
imported ``content`` (up a layer), ``model`` (across), and six ``kernel``
modules, which made it the widest-reaching non-adapter module here.

``map_types`` is the one root-tier module the arrow does not describe, and the
exception is deliberate rather than a second drift: it is the map's *types* with
no parser in them, so ``model`` may hold a ``MapDocument`` without the file
machinery arriving behind it. Two tests below pin that carve-out at exactly its
intended width — ``model`` may import ``map_types``, never ``map_document``; and
``map_types`` may import nothing but the kernel, which is what makes the first
rule mean anything.

That drift also closed a **cycle**, which is why this file parses the graph
rather than trusting the layer names. ``content`` reads the bundled packs with
``resources.files("fivee_sim.data.srd")``; anchoring resources on a package
*imports* it, and importing ``fivee_sim.data.srd`` imports its parent
``fivee_sim.data`` first — which imported ``content`` straight back. It never
raised only because the resource read happens at call time, by which point
``content`` is fully initialised. A cycle that survives on timing is a cycle.

Five rules, each the smallest statement of the layer it bounds:

* ``fivee_sim.data`` is a **resource package**: it imports nothing from the
  engine at all. This is what breaks the cycle, and it is stronger than "no
  upward import" on purpose — there is no downward import a directory of JSON
  needs either.
* ``fivee_sim.kernel`` imports only ``fivee_sim.kernel``. The purity claim the
  rest of the engine's reproducibility rests on.
* ``fivee_sim.model`` imports only ``fivee_sim.model``, ``fivee_sim.kernel`` and
  ``fivee_sim.map_types``. In particular never ``content``: ``model`` owns
  creatures, so creature *construction* belongs to it, but a registry is
  content's concept. The construction seam therefore takes the two
  registry-derived values it cannot compute — the condition table and the
  provenance fallback — as arguments.
  ``test_the_model_builds_a_creature_with_no_registry_in_sight`` is the same
  claim proved from the other side: behaviourally, not just structurally.
* ``fivee_sim.client`` imports only itself and ``fivee_sim.paths``. This one
  points the other way from the three above, and it is the deliverable rather
  than a tidiness rule — see its own test for why.
* ``fivee_sim.service.scenes`` imports neither ``model`` nor ``kernel``. A scene
  is a saved request body, not a domain object, and the allowlist below is what
  keeps it one. This is the only rule naming a single module rather than a
  package, which is why :func:`_modules_under` answers for a file as well as a
  directory.

These are `ast` checks, not import-time checks, because an import that only
happens inside a function still couples the modules and a runtime probe would
miss it.
"""

from __future__ import annotations

import ast
from pathlib import Path

from fivee_sim.kernel.conditions import EFFECTS
from fivee_sim.kernel.dice import Dice
from fivee_sim.model.creature import Creature

SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGE = "fivee_sim"

FIXTURE = "synthetic test fixture, not SRD content"


def _module_name(path: Path) -> str:
    """Dotted name for a source file, e.g. ``fivee_sim.kernel.mapgen.caves``."""
    parts = path.relative_to(SRC).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_of(module: str, path: Path) -> str:
    """The package a relative import inside ``module`` resolves against."""
    if path.name == "__init__.py":
        return module
    return module.rpartition(".")[0]


def _imports(path: Path) -> list[tuple[str, int]]:
    """Every ``fivee_sim.*`` module this file imports, with its line number.

    Relative imports are resolved the way Python resolves them, so
    ``from ..grid import Square`` inside ``kernel/mapgen/caves.py`` is reported
    as ``fivee_sim.kernel.grid`` rather than being missed for not naming the
    package out loud.
    """
    module = _module_name(path)
    package = _package_of(module, path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [
                (alias.name, node.lineno)
                for alias in node.names
                if alias.name == PACKAGE or alias.name.startswith(f"{PACKAGE}.")
            ]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            if target == PACKAGE or target.startswith(f"{PACKAGE}."):
                found.append((target, node.lineno))
    return found


def _modules_under(prefix: str) -> list[Path]:
    """Every source file a rule covers: a package's tree, or one module's file.

    The single-module case is the scene rule's, and it is why this does not
    simply ``rglob``: ``fivee_sim.service.scenes`` is a file beside its
    siblings, and a resolver that only understood directories would score it as
    no source at all and pass whatever it imported.
    """
    root = SRC / Path(*prefix.split("."))
    if root.is_dir():
        return sorted(root.rglob("*.py"))
    module = root.with_suffix(".py")
    return [module] if module.is_file() else []


def _offenders(prefix: str, allowed: tuple[str, ...]) -> list[str]:
    """Every import under ``prefix`` that no entry in ``allowed`` permits."""
    out: list[str] = []
    for path in _modules_under(prefix):
        for target, line in _imports(path):
            if any(target == ok or target.startswith(f"{ok}.") for ok in allowed):
                continue
            out.append(f"{path.relative_to(SRC)}:{line} imports {target}")
    return out


def test_the_data_package_imports_nothing_from_the_engine() -> None:
    offenders = _offenders("fivee_sim.data", allowed=())
    assert not offenders, (
        "fivee_sim.data carries the bundled JSON and nothing else. An import here "
        "closes a cycle: content anchors resources on 'fivee_sim.data.srd', which "
        "imports this package's __init__ first, so anything this package imports "
        "is imported by content on its way to reading the packs. Move the code to "
        "the layer that owns it — creature construction to model, registry lookup "
        "to content:\n  " + "\n  ".join(offenders)
    )


def test_the_kernel_imports_nothing_outside_the_kernel() -> None:
    offenders = _offenders("fivee_sim.kernel", allowed=("fivee_sim.kernel",))
    assert not offenders, (
        "The kernel is the bottom layer and holds the rules primitives: it knows "
        "nothing about creatures, content, or I/O, and every caller passes in the "
        "handful of values a roll depends on. An outward import here is the defect "
        "that reproducibility claims rest on not existing:\n  " + "\n  ".join(offenders)
    )


def test_the_model_imports_only_itself_and_the_kernel() -> None:
    offenders = _offenders(
        "fivee_sim.model",
        allowed=("fivee_sim.model", "fivee_sim.kernel", "fivee_sim.map_types"),
    )
    assert not offenders, (
        "model/ owns creatures and encounter state and sits directly above the "
        "kernel. An import of content in particular inverts the declared direction "
        "(content -> model -> kernel): a ContentRegistry is content's concept, so "
        "construction here takes the values it needs passed in rather than reaching "
        "up for the registry that holds them:\n  " + "\n  ".join(offenders)
    )


def test_the_model_may_hold_a_map_document_but_never_read_one() -> None:
    """The ``map_types`` entry above is one prefix; the rule is narrower than it.

    ``model`` may name the map's *types* — a fight has to say what a
    ``MapDocument`` is — but never ``map_document``, which is the parser: a
    ``Reader``, the ``_check_*`` passes, ``MapError`` and the ``validation``
    machinery under all three. That is the whole reason the types were split out
    of it, so writing the allowlist entry down without writing the narrower rule
    down would leave the carve-out free to widen back into the thing it was made
    to avoid.

    The vacuity guard matters as much as the assertion. Before ``map_types``
    existed this rule was true for free, because ``model`` imported neither
    module; a check that keeps passing after somebody deletes the carve-out is
    not guarding it.
    """
    offenders = [
        f"{path.relative_to(SRC)}:{line}"
        for path in _modules_under("fivee_sim.model")
        for target, line in _imports(path)
        if target == "fivee_sim.map_document"
        or target.startswith("fivee_sim.map_document.")
    ]
    assert not offenders, (
        "model/ may import fivee_sim.map_types and not fivee_sim.map_document. "
        "The types carry no parser; map_document carries the Reader, the "
        "validation machinery and MapError, and pulling that into a fight is "
        "exactly what splitting the two prevented. Whatever this needs, either "
        "it is a type — and belongs in map_types — or it is a file being read, "
        "and belongs to the layer that owns files:\n  " + "\n  ".join(offenders)
    )
    reaches = {
        target
        for path in _modules_under("fivee_sim.model")
        for target, _ in _imports(path)
    }
    assert "fivee_sim.map_types" in reaches, (
        "no model/ module imports fivee_sim.map_types, so the allowlist entry "
        "that permits it is dead and this rule guards nothing. Either the "
        "carve-out is still needed — in which case something here uses it — or "
        "it should come back out of the model allowlist."
    )


def test_the_map_types_carry_no_parser_behind_them() -> None:
    """What makes ``model`` importing ``map_types`` safe, stated as its own rule.

    The allowlist entry says ``model`` may reach one root-tier module. It is only
    a narrow permission while that module stays narrow: an import of
    ``validation`` here — or of anything that imports it — hands the whole parser
    to every module that names a map, through a door the model allowlist would go
    on reporting as clean.

    ``kernel`` alone, then. ``map_types`` needs ``Square``, ``FEET_PER_SQUARE``
    and ``Facing`` from the grid and ``Ability`` for a fixture's check, and
    nothing else from this package.
    """
    assert _modules_under("fivee_sim.map_types"), (
        "fivee_sim.map_types is gone; the model allowlist entry for it and this "
        "rule both guard nothing"
    )
    offenders = _offenders("fivee_sim.map_types", allowed=("fivee_sim.kernel",))
    assert not offenders, (
        "fivee_sim.map_types holds the map's types and imports the kernel and the "
        "standard library, nothing else. It is what model/ is allowed to reach, "
        "so an import of validation, content or the model itself here reaches "
        "straight back through that permission — and the model rule would still "
        "read as clean while it happened:\n  " + "\n  ".join(offenders)
    )


def test_the_client_reaches_the_engine_only_over_http() -> None:
    """``fivee``'s features are the REST surface's features, proved by import.

    This rule is not hygiene, it is the claim the client exists to make. The
    engine's operations used to be reachable two ways — in-process through an MCP
    adapter, and over HTTP — and "the REST surface can do everything" was
    something a reader had to take on faith while auditing two call graphs. The
    MCP adapter is gone; this is what stops the second route growing back.

    ``fivee_sim.client`` may import ``fivee_sim.paths`` (where the state file
    lives) and ``fivee_sim.configuration`` (the project launch description),
    neither of which is an engine operation. It has no way to roll a die,
    resolve an attack, load a pack, or read a map except by asking the server
    over HTTP. Every feature the CLI demonstrably has is therefore a feature
    ``/api/v1`` demonstrably serves, and "all the features moved" stops being a
    claim and becomes this test.

    Starting the server is a ``subprocess`` running ``sys.executable -m
    fivee_sim.web``, which is deliberately not an import: the client spawns a
    process it then talks to over a socket, exactly as a user at a shell would,
    and shares no objects with it.

    The corollary is the part worth stating: an operation the CLI cannot
    express is an operation missing from the contract, not a gap in the client.
    """
    offenders = _offenders(
        "fivee_sim.client",
        allowed=("fivee_sim.client", "fivee_sim.configuration", "fivee_sim.paths"),
    )
    assert not offenders, (
        "fivee_sim.client speaks to the engine over HTTP and nowhere else. An "
        "import of service/, kernel/, model/ or web/ here would let the CLI do "
        "something the REST surface cannot, which is precisely the thing this "
        "client is built to disprove. Whatever this needs, ask the server for "
        "it — or add the route it is missing:\n  " + "\n  ".join(offenders)
    )


#: What a scene service may reach for: where files live, how a file's problems
#: are reported, and the three transport-neutral helpers in its own layer. An
#: allowlist rather than a "not model, not kernel" denylist for the reason the
#: player projection names what it emits — a denylist admits every layer added
#: after it is written, silently.
SCENE_ALLOWED = (
    "fivee_sim.paths",
    "fivee_sim.validation",
    "fivee_sim.service.common",
    "fivee_sim.service.durable",
    "fivee_sim.service.errors",
)


def test_the_scene_service_holds_a_request_body_and_never_the_domain() -> None:
    """A scene is a saved ``encounter.create`` body, and that is all it is.

    What it must never become is a second opinion about the fight it describes.
    ``encounter.create`` owns what a combatant spec is and ``kernel.grid`` owns
    which movement rules exist; a scene that could reach either would be able to
    re-decide both, and the copy would drift the first time a spec field is
    added — which is exactly the duplication the design rejected.

    Reaching for the maps directory is the same trap wearing a different hat:
    resolving a ``map_id`` here would import the map document, the grid, and the
    kernel behind them, so the known ids are passed in instead.
    """
    scenes = SRC / "fivee_sim" / "service" / "scenes.py"
    assert scenes.is_file(), "the scene service is gone; this rule guards nothing"
    assert _imports(scenes), (
        "scenes.py imports nothing from the engine at all, which would make this "
        "rule vacuous — has it been emptied?"
    )
    offenders = _offenders("fivee_sim.service.scenes", allowed=SCENE_ALLOWED)
    assert not offenders, (
        "service/scenes.py stores a request body: an id, a seed, a list of "
        "combatant specs it does not interpret. An import of model/ or kernel/ "
        "here — or of a service module that drags them in — makes it a second "
        "owner of what a fight is, and two owners of one rule is how the copy "
        "drifts. Pass the value in, the way the known map ids are passed to "
        "validate():\n  " + "\n  ".join(offenders)
    )


def test_the_suites_own_door_stays_a_door_and_never_becomes_an_adapter() -> None:
    """``tests/api.py`` may only forward. A branch there is a second engine.

    Deleting the MCP server left 186 in-process call sites across eleven test
    modules, and ``tests/api.py`` is what they now go through: one
    ``EngineState`` threaded in, one call out, no error translation. Its
    docstring says exactly that — "not an adapter, not the contract, not a
    second implementation. Every body below is one call."

    That claim is load-bearing and was unenforced, which is the asymmetry this
    closes: the sibling claim about ``fivee_sim.client`` gets an AST check
    directly above, while this one relied on nobody ever finding it convenient
    to add a default here. The failure it prevents is quiet — a coercion or a
    fallback added to make one test's life easier means those 186 sites stop
    describing what ``service/`` does and start describing what this file does
    instead, and every one of them keeps passing while it happens.

    So: one statement per function, and it must be a ``return`` of a call.
    Anything richer belongs in ``service/``, where the shipped adapters and
    their tests can see it too.
    """
    path = Path(__file__).with_name("api.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        body = [item for item in node.body if not isinstance(item, ast.Expr)]
        if len(body) != 1 or not isinstance(body[0], ast.Return):
            offenders.append(f"api.py:{node.lineno} {node.name} is more than one return")
            continue
        if not isinstance(body[0].value, ast.Call):
            offenders.append(f"api.py:{node.lineno} {node.name} returns something not a call")
    assert not offenders, (
        "tests/api.py forwards to service/ and does nothing else — that is what "
        "makes the 186 call sites behind it evidence about the engine rather "
        "than about this file. Put the branch in service/, where /api/v1 and "
        "its tests reach it too:\n  " + "\n  ".join(offenders)
    )


def test_every_layer_rule_actually_saw_some_source() -> None:
    """A resolver that silently matched nothing would make all six vacuous."""
    for prefix in (
        "fivee_sim.data",
        "fivee_sim.kernel",
        "fivee_sim.model",
        "fivee_sim.map_types",
        "fivee_sim.client",
        "fivee_sim.service.scenes",
    ):
        assert _modules_under(prefix), f"no source files found under {prefix}"
    # And the resolver really does follow relative imports: the kernel's mapgen
    # modules reach their sibling with ``from ..grid``, which a naive reader that
    # only understood absolute names would score as zero imports.
    caves = SRC / "fivee_sim" / "kernel" / "mapgen" / "caves.py"
    assert ("fivee_sim.kernel.grid", 24) in _imports(caves)


def test_the_model_builds_a_creature_with_no_registry_in_sight() -> None:
    """The layering rule above, proved behaviourally rather than structurally.

    A bare ``dict`` and the condition table are the whole input. Nothing loads a
    pack, nothing builds a registry, and no name is looked up anywhere — which is
    what makes ``model`` free of ``content`` rather than merely not importing it
    on the line the parser happens to read.
    """
    record = {
        "name": "Vale Stalker",
        "provenance": FIXTURE,
        "ac": 14,
        "max_hp": 22,
        "speed": 40,
        "abilities": {"strength": 16, "dexterity": 12},
        "attacks": [
            {
                "name": "Claw",
                "attack_bonus": 5,
                "damage": "2d6+3",
                "damage_type": "slashing",
                "provenance": FIXTURE,
            }
        ],
    }
    creature = Creature.from_record(record, condition_effects=EFFECTS, source="nowhere")

    assert creature.name == "Vale Stalker"
    assert creature.ac == 14
    assert creature.max_hp == 22 and creature.hp == 22
    assert creature.speed == 40
    assert creature.attacks[0].name == "Claw"
    assert creature.attacks[0].damage == Dice(2, 6, 3)
    assert creature.provenance == FIXTURE
