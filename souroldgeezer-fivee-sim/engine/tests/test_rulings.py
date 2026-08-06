"""The rulings register must stay tied to the code it governs.

A register of adjudications is only worth having if it cannot quietly stop
matching the engine.  ``unmodelled_facts`` is the cautionary case: nothing
obliged anyone to write an entry, so it came to measure attention rather than
omission and one entry sat stale across three releases.  So every claim the
register makes about the code is derived from the code here, never restated.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from fivee_sim.content import builtin_registry
from fivee_sim.rulings import (
    RULINGS,
    Concurrence,
    Ruling,
    RulingKind,
    render_markdown,
)

#: The source tree, not the imported package — the register names definition
#: sites, and ``test_layering.py`` reads the same way for the same reason.
SRC = Path(__file__).resolve().parents[1] / "src" / "fivee_sim"

#: Same shape as ``content.py``'s omission codes, so a ruling code and an
#: omission code are comparable strings rather than two spellings.
_CODE = re.compile(r"^[a-z0-9][a-z0-9_]*$")

#: ``# ruling: <code>`` — the marker a governed site carries.
_MARKER = re.compile(r"#\s*ruling:\s*([a-z0-9_]+)")

_LIVE_KINDS = frozenset(RulingKind) - {RulingKind.SUPERSEDED}

#: Kinds that describe something the engine cannot express at all.  They have
#: no line of code to point at — that absence *is* the ruling.
_SITELESS_KINDS = frozenset({RulingKind.SCHEMA_CEILING, RulingKind.OUT_OF_SCOPE})


def _live() -> tuple[Ruling, ...]:
    return tuple(r for r in RULINGS if r.kind is not RulingKind.SUPERSEDED)


def _python_sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _defined_symbols(module: Path) -> set[str]:
    """Every ``name`` and ``Class.name`` a module defines, read rather than imported.

    Parsed with ``ast`` for the reason ``check-api-smoke.py`` parses
    ``routes.py``: importing would resolve re-exports and decorated aliases the
    register does not mean, and the register names a *definition site*.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            found.add(node.name)
            for child in node.body:
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    found.add(f"{node.name}.{child.name}")
                elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                    found.add(f"{node.name}.{child.target.id}")
                elif isinstance(child, ast.Assign):
                    found.update(
                        f"{node.name}.{t.id}" for t in child.targets if isinstance(t, ast.Name)
                    )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.add(node.target.id)
        elif isinstance(node, ast.Assign):
            found.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return found


def test_the_register_is_not_empty() -> None:
    assert RULINGS, "a register with no entries pins nothing"


def test_every_code_is_a_unique_lowercase_identifier() -> None:
    codes = [ruling.code for ruling in RULINGS]
    duplicates = sorted({code for code in codes if codes.count(code) > 1})
    assert not duplicates, f"duplicate ruling codes: {', '.join(duplicates)}"
    bad = sorted(code for code in codes if not _CODE.fullmatch(code))
    assert not bad, f"ruling codes must match {_CODE.pattern}: {', '.join(bad)}"


def test_every_entry_states_a_question_a_decision_and_a_reason() -> None:
    for ruling in RULINGS:
        for field in ("question", "decision", "because"):
            assert getattr(ruling, field).strip(), f"{ruling.code}.{field} is empty"


def test_every_live_entry_states_a_revisit_trigger() -> None:
    """The field the register exists for.

    A ruling without a trigger is a note; with one it is a tripwire the next
    person to change the surrounding code trips on purpose.
    """
    for ruling in _live():
        assert ruling.revisit.strip(), (
            f"{ruling.code} has no revisit trigger — say what would make it wrong"
        )


def test_every_entry_cites_the_srd_by_section_not_by_quotation() -> None:
    """``basis`` is citation-only: the report ships, and prose does not.

    A citation is a section name and a page.  A sentence of rules text is the
    thing the licence boundary keeps out of shipped data.
    """
    for ruling in RULINGS:
        for citation in ruling.basis:
            assert citation.strip(), f"{ruling.code} has an empty citation"
            assert '"' not in citation and "'" not in citation, (
                f"{ruling.code} citation {citation!r} looks like quoted rules text"
            )
            assert len(citation) <= 80, (
                f"{ruling.code} citation {citation!r} is too long to be a citation"
            )


def test_every_site_names_a_symbol_that_exists() -> None:
    """A rename turns the register red rather than stale."""
    for ruling in RULINGS:
        for site in ruling.sites:
            assert ":" in site, f"{ruling.code} site {site!r} must be 'path.py:symbol'"
            relative, _, symbol = site.partition(":")
            module = SRC / relative
            assert module.is_file(), f"{ruling.code} names a missing module: {relative}"
            assert symbol in _defined_symbols(module), (
                f"{ruling.code} names {symbol!r}, which {relative} does not define"
            )


def test_superseded_entries_name_a_release_and_hold_no_sites() -> None:
    for ruling in RULINGS:
        if ruling.kind is not RulingKind.SUPERSEDED:
            assert not ruling.superseded_by, (
                f"{ruling.code} names a release but is not superseded"
            )
            continue
        assert re.fullmatch(r"\d{4}\.\d{2}\.\d+", ruling.superseded_by), (
            f"{ruling.code} must name the calver release that closed it"
        )
        assert not ruling.sites, (
            f"{ruling.code} is superseded; its sites are gone, so it must name none"
        )


def test_a_ruling_the_engine_cannot_express_needs_no_site() -> None:
    """The kinds are what decide whether a site is required, not taste."""
    for ruling in _live():
        if ruling.kind in _SITELESS_KINDS:
            continue
        assert ruling.sites, (
            f"{ruling.code} is {ruling.kind.value} and must point at the code it governs"
        )


def test_only_open_readings_claim_a_concurrence_verdict() -> None:
    """An approximation has no rules question, so it cannot agree or disagree.

    The printed Loading rule is explicit; modelling it per turn is a granularity
    choice.  Grading that against outside readings would invent a controversy.
    """
    for ruling in RULINGS:
        if ruling.kind is RulingKind.SRD_SILENT:
            assert ruling.concurrence is not Concurrence.NOT_A_RULES_QUESTION, (
                f"{ruling.code} is srd_silent — an open reading has an outside answer or none"
            )
        else:
            assert ruling.concurrence is Concurrence.NOT_A_RULES_QUESTION, (
                f"{ruling.code} is {ruling.kind.value}; only srd_silent entries are graded"
            )


@pytest.mark.parametrize("kind", sorted(_LIVE_KINDS))
def test_each_live_kind_is_used(kind: RulingKind) -> None:
    """A kind nobody uses is a category that has not been thought through."""
    assert any(ruling.kind is kind for ruling in _live()), f"no entry is {kind.value}"


# --- the link to the per-record ledger --------------------------------------


def _omission_codes_in_bundled_data() -> set[str]:
    """Every ``unmodelled_facts`` code the bundled packs actually carry."""
    registry = builtin_registry()
    found: set[str] = set()
    for record in registry.catalog.values():
        found.update(str(fact["code"]) for fact in record.unmodelled_facts if "code" in fact)
    for section in registry.summary()["counts"]:
        for executable in registry.records_for(section).values():
            for fact in executable.get("unmodelled_facts", []):
                if isinstance(fact, dict) and "code" in fact:
                    found.add(str(fact["code"]))
    return found


def test_every_referenced_omission_code_still_occurs_in_the_data() -> None:
    """The two ledgers answer different questions, so the link is explicit.

    A record's code says "this record drops a printed feature"; a ruling says
    "the engine decided this".  Matching them by spelling would collapse that
    difference, so a ruling *names* the codes it explains and this checks the
    names still resolve.  The Ogre's stale ``unsupported_ammunition_count`` is
    the failure this exists to catch.
    """
    present = _omission_codes_in_bundled_data()
    for ruling in RULINGS:
        for code in ruling.omission_codes:
            assert code in present, (
                f"{ruling.code} names omission code {code!r}, which no bundled "
                "record carries any more"
            )


def test_at_least_one_ruling_explains_a_record_level_omission() -> None:
    """Guards the link itself: an unused field is one nobody notices breaking."""
    assert any(ruling.omission_codes for ruling in RULINGS)


# --- the generated report --------------------------------------------------

DOC = Path(__file__).resolve().parents[2] / "docs" / "RULINGS.md"


def test_the_committed_report_is_current() -> None:
    assert DOC.is_file(), f"rulings report missing at {DOC}"
    if DOC.read_text(encoding="utf-8") != render_markdown():
        pytest.fail(
            "docs/RULINGS.md is stale. Regenerate it with "
            "`uv run python -m fivee_sim.rulings`."
        )


def test_the_report_carries_every_entry_and_its_trigger() -> None:
    """The report is the register, not a summary of it."""
    report = render_markdown()
    for ruling in RULINGS:
        assert f"### `{ruling.code}`" in report, f"{ruling.code} missing from the report"
    for ruling in _live():
        assert ruling.revisit in report, f"{ruling.code}'s revisit trigger is not rendered"


def test_the_report_names_no_third_party_source() -> None:
    """The licence split, asserted on the artifact that actually ships.

    The survey names sources and stays in the repo-root ``docs/``; this file
    goes to every install and carries our own classification only.
    """
    report = render_markdown().lower()
    for forbidden in ("http://", "https://", "sage advice", "d&d", "dnd", "wizards"):
        assert forbidden not in report, (
            f"the shipped rulings report must not contain {forbidden!r}"
        )


# --- the survey behind the concurrence verdicts ----------------------------

#: Repo-root ``docs/``, deliberately outside the plugin directory: the survey
#: names third-party sources and must not ship.  That also puts it outside an
#: installed copy, so this check is a development-time one.
RESEARCH = Path(__file__).resolve().parents[3] / "docs" / "RULINGS-RESEARCH.md"


def _research_text() -> str:
    if not RESEARCH.is_file():
        pytest.skip(f"survey absent at {RESEARCH} — repo-only artifact, not packaged")
    return RESEARCH.read_text(encoding="utf-8")


def test_every_open_reading_was_surveyed() -> None:
    """A verdict with no survey behind it is an opinion wearing an enum."""
    text = _research_text()
    for ruling in RULINGS:
        if ruling.kind is not RulingKind.SRD_SILENT:
            continue
        assert f"## `{ruling.code}`" in text, (
            f"{ruling.code} is an open reading with no section in {RESEARCH.name}"
        )


def test_the_survey_covers_nothing_it_should_not() -> None:
    """The other direction: a surveyed code must still be an open reading.

    Reclassifying an entry to ``approximation`` without dropping its section
    leaves a verdict nobody grades any more.
    """
    text = _research_text()
    open_codes = {r.code for r in RULINGS if r.kind is RulingKind.SRD_SILENT}
    surveyed = set(re.findall(r"^## `([a-z0-9_]+)`", text, flags=re.MULTILINE))
    stale = sorted(surveyed - open_codes)
    assert not stale, (
        f"{RESEARCH.name} surveys entries that are no longer open readings: "
        f"{', '.join(stale)}"
    )


def test_the_survey_never_sits_inside_the_packaged_plugin() -> None:
    """The licence split this artifact exists to keep.

    The register carries our own classification and no third-party name; the
    survey carries the sources and stays out of the plugin directory, which is
    what each host packages.

    **Stated as a search rather than a path comparison, and that is the whole
    point.** The obvious spelling — assert ``RESEARCH`` is outside the plugin
    root — reads the repo-relative constant, which still points outside it
    however the file was actually moved. So the one misuse this exists to catch
    (someone moves the survey into ``souroldgeezer-fivee-sim/docs/``) made the
    content checks below *skip* on a missing file and the suite stayed green.
    A control that cannot fail against its own target scenario is not a control.

    Searching the plugin tree fails on the move and needs no file to be present,
    so unlike the two below it is also meaningful in an installed copy.
    """
    plugin_root = Path(__file__).resolve().parents[2]
    strays = sorted(
        path.relative_to(plugin_root) for path in plugin_root.rglob(RESEARCH.name)
    )
    assert not strays, (
        f"the outside-readings survey names third-party sources and must stay out "
        f"of the packaged plugin; found: {', '.join(str(p) for p in strays)}"
    )


# --- the two-way partition -------------------------------------------------


def _markers() -> dict[str, list[str]]:
    """Every ``# ruling:`` marker in the shipped source, by code."""
    out: dict[str, list[str]] = {}
    for path in _python_sources():
        if path.name == "rulings.py":
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _MARKER.search(line)
            if match:
                out.setdefault(match.group(1), []).append(str(path.relative_to(SRC)))
    return out


def test_every_marker_in_the_source_has_a_register_entry() -> None:
    known = {ruling.code for ruling in RULINGS}
    orphans = sorted(set(_markers()) - known)
    assert not orphans, (
        f"code carries markers nothing declares: {', '.join(orphans)}"
    )


def test_every_governed_entry_is_marked_at_its_sites() -> None:
    """The other half.  Together these two make the register total.

    An allowlist alone answers "is this declared?" and not "does anything still
    do it?" — the same reason the player brief carries a visible/withheld pair
    rather than one filter.
    """
    markers = _markers()
    for ruling in _live():
        if ruling.kind in _SITELESS_KINDS:
            continue
        assert ruling.code in markers, (
            f"{ruling.code} declares sites but no code carries `# ruling: {ruling.code}`"
        )
        marked = set(markers[ruling.code])
        for site in ruling.sites:
            relative = site.partition(":")[0]
            assert relative in marked, (
                f"{ruling.code} names {relative} but that file carries no marker for it"
            )


def test_a_siteless_ruling_is_not_marked() -> None:
    """A schema ceiling has no code to mark; a marker would be claiming otherwise."""
    markers = _markers()
    for ruling in RULINGS:
        if ruling.kind in _SITELESS_KINDS or ruling.kind is RulingKind.SUPERSEDED:
            assert ruling.code not in markers, (
                f"{ruling.code} is {ruling.kind.value} and must not be marked in code"
            )
