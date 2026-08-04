"""Transport-neutral catalog search, record lookup, and table pagination."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..catalog import (
    CatalogRecord,
    FactStatus,
    SearchCandidate,
    SimulationSupport,
    ranked_candidates,
    simulation_support,
)
from ..content import SECTIONS, ContentRegistry
from .common import slugify
from .errors import NotFoundError, RequestError

MAX_PAGE_SIZE = 25
_KIND_BY_SECTION = {
    "creatures": "creature",
    "spells": "spell",
    "conditions": "condition",
    "terrain": "terrain",
    "items": "item",
}


def _bounded_page(since: int, limit: int) -> tuple[int, int]:
    if since < 0:
        raise RequestError("since must be a non-negative whole number")
    if limit < 1:
        raise RequestError("limit must be at least 1")
    return since, min(limit, MAX_PAGE_SIZE)


def _execution_record(
    registry: ContentRegistry, record: CatalogRecord
) -> Mapping[str, Any] | None:
    if record.content_ref is None:
        return None
    return registry.records_for(record.content_ref.section).get(record.content_ref.name)


def _has_omissions(record: Mapping[str, Any] | None) -> bool:
    if record is None:
        return False
    return bool(record.get("unmodelled", []) or record.get("unmodelled_facts", []))


def _catalog_support(registry: ContentRegistry, record: CatalogRecord) -> SimulationSupport:
    executable = _execution_record(registry, record)
    return simulation_support(
        executable=executable is not None,
        has_omissions=bool(record.unmodelled_facts) or _has_omissions(executable),
    )


def _synthetic_id(section: str, name: str) -> str:
    return f"content:{section}:{slugify(name)}"


def _candidates(registry: ContentRegistry) -> list[SearchCandidate]:
    candidates: list[SearchCandidate] = []
    linked = {
        (record.content_ref.section, record.content_ref.name)
        for record in registry.catalog.values()
        if record.content_ref is not None
    }
    for record in registry.catalog.values():
        candidates.append(
            SearchCandidate(
                id=record.id,
                kind=record.kind,
                name=record.name,
                aliases=record.aliases,
                simulation=_catalog_support(registry, record),
                fact_status=record.fact_status,
                pages=record.pages,
                source=registry.source_of("catalog", record.id),
            )
        )
    for section in SECTIONS:
        for name, raw_record in registry.records_for(section).items():
            if (section, name) in linked:
                continue
            candidates.append(
                SearchCandidate(
                    id=_synthetic_id(section, name),
                    kind=_KIND_BY_SECTION[section],
                    name=name,
                    aliases=(),
                    simulation=simulation_support(
                        executable=True, has_omissions=_has_omissions(raw_record)
                    ),
                    fact_status=None,
                    pages=(),
                    source=registry.source_of(section, name),
                )
            )
    return candidates


def _candidate_dict(candidate: SearchCandidate) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": candidate.id,
        "kind": candidate.kind,
        "name": candidate.name,
        "simulation": candidate.simulation.value,
        "source": candidate.source,
    }
    if candidate.fact_status is not None:
        payload["fact_status"] = candidate.fact_status.value
    if candidate.pages:
        payload["pages"] = list(candidate.pages)
    return payload


def search(
    registry: ContentRegistry,
    query: str,
    kind: str | None = None,
    simulation: SimulationSupport | str | None = None,
    *,
    since: int = 0,
    limit: int = 10,
) -> dict[str, Any]:
    """Search the merged catalog and legacy executable rows with stable ranking."""
    since, limit = _bounded_page(since, limit)
    support = SimulationSupport(simulation) if simulation is not None else None
    normalized_kind = kind.strip().casefold() if kind else ""
    candidates = [
        candidate
        for candidate in _candidates(registry)
        if (not normalized_kind or candidate.kind.casefold() == normalized_kind)
        and (support is None or candidate.simulation is support)
    ]
    ranked = ranked_candidates(candidates, query)
    page = ranked[since : since + limit]
    next_since = since + len(page)
    return {
        "query": query,
        "kind": kind,
        "simulation": support.value if support is not None else None,
        "since": since,
        "limit": limit,
        "total": len(ranked),
        "next_since": next_since if next_since < len(ranked) else None,
        "results": [_candidate_dict(candidate) for candidate in page],
    }


def _synthetic_records(registry: ContentRegistry) -> dict[str, tuple[str, str]]:
    linked = {
        (record.content_ref.section, record.content_ref.name)
        for record in registry.catalog.values()
        if record.content_ref is not None
    }
    return {
        _synthetic_id(section, name): (section, name)
        for section in SECTIONS
        for name in registry.records_for(section)
        if (section, name) not in linked
    }


def _available_suffix(names: list[str]) -> str:
    shown = names[:10]
    listing = ", ".join(shown) or "none"
    return f" Available: {listing} (showing {len(shown)} of {len(names)})."


def get_record(registry: ContentRegistry, identifier: str) -> dict[str, Any]:
    """Return one identity while keeping catalog and execution provenance distinct."""
    record = registry.catalog.get(identifier)
    if record is not None:
        payload = record.as_dict()
        executable = _execution_record(registry, record)
        payload["simulation"] = _catalog_support(registry, record).value
        payload["sources"] = {
            "catalog": registry.source_of("catalog", record.id),
            "executable": (
                registry.source_of(record.content_ref.section, record.content_ref.name)
                if executable is not None and record.content_ref is not None
                else None
            ),
        }
        return payload

    synthetic = _synthetic_records(registry)
    if identifier in synthetic:
        section, name = synthetic[identifier]
        raw_record = registry.records_for(section)[name]
        return {
            "id": identifier,
            "kind": _KIND_BY_SECTION[section],
            "name": name,
            "source_ids": [],
            "pages": [],
            "fact_status": FactStatus.PENDING.value,
            "facts": {},
            "content_ref": {"section": section, "name": name},
            "simulation": simulation_support(
                executable=True, has_omissions=_has_omissions(raw_record)
            ).value,
            "sources": {
                "catalog": None,
                "executable": registry.source_of(section, name),
            },
        }

    names = sorted([*registry.catalog, *synthetic])
    raise NotFoundError(
        f"no catalog record with id {identifier!r}." + _available_suffix(names)
    )


def get_table(
    registry: ContentRegistry, identifier: str, *, since: int = 0, limit: int = 20
) -> dict[str, Any]:
    """Return one table's metadata and a bounded row window."""
    since, limit = _bounded_page(since, limit)
    table = registry.catalog_tables.get(identifier)
    if table is None:
        names = sorted(registry.catalog_tables)
        raise NotFoundError(
            f"no catalog table with id {identifier!r}." + _available_suffix(names)
        )
    rows = table.rows[since : since + limit]
    next_since = since + len(rows)
    payload = table.as_dict(include_rows=False)
    payload.update(
        {
            "source": registry.source_of("catalog_tables", identifier),
            "since": since,
            "limit": limit,
            "total": len(table.rows),
            "next_since": next_since if next_since < len(table.rows) else None,
            "rows": [row.as_dict() for row in rows],
        }
    )
    return payload
