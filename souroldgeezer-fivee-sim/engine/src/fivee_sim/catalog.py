"""Pure immutable records and ranking primitives for structured rules catalogs.

This module owns no files and no active registry.  Content-pack I/O and precedence
stay in :mod:`fivee_sim.content`; transport-neutral tool bodies stay in
``fivee_sim.service``.  Keeping the records here makes the catalog usable without
pulling either boundary into the rules layers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class FactStatus(StrEnum):
    """How completely a source entry's structured facts have been reviewed."""

    PENDING = "pending"
    COMPLETE = "complete"
    NO_STRUCTURED_FACTS = "no_structured_facts"


class SimulationSupport(StrEnum):
    """How much of a catalog entry the active execution engine can run."""

    REFERENCE_ONLY = "reference_only"
    PARTIAL = "partial"
    EXECUTABLE = "executable"


class CatalogValueType(StrEnum):
    """Portable scalar types available to a printed-table column."""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


@dataclass(frozen=True, slots=True)
class ContentRef:
    """A link from a catalog identity to an executable content record."""

    section: str
    name: str

    def as_dict(self) -> dict[str, str]:
        return {"section": self.section, "name": self.name}


def freeze_json(value: Any) -> Any:
    """Recursively freeze a JSON-shaped value without changing its information."""
    if isinstance(value, dict):
        return MappingProxyType({str(key): freeze_json(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(freeze_json(child) for child in value)
    return value


def thaw_json(value: Any) -> Any:
    """Return a plain JSON-serialisable copy of a frozen value."""
    if isinstance(value, Mapping):
        return {str(key): thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(child) for child in value]
    return value


@dataclass(frozen=True, slots=True)
class CatalogRecord:
    """One source section represented without descriptive or rules prose."""

    id: str
    kind: str
    name: str
    source_ids: tuple[str, ...]
    pages: tuple[int, ...]
    fact_status: FactStatus
    facts: Mapping[str, Any]
    provenance: str
    chapter_id: str = ""
    parent_id: str = ""
    aliases: tuple[str, ...] = ()
    content_ref: ContentRef | None = None
    unmodelled_facts: tuple[Mapping[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "source_ids": list(self.source_ids),
            "pages": list(self.pages),
            "fact_status": self.fact_status.value,
            "facts": thaw_json(self.facts),
            "provenance": self.provenance,
        }
        if self.chapter_id:
            payload["chapter_id"] = self.chapter_id
        if self.parent_id:
            payload["parent_id"] = self.parent_id
        if self.aliases:
            payload["aliases"] = list(self.aliases)
        if self.content_ref is not None:
            payload["content_ref"] = self.content_ref.as_dict()
        if self.unmodelled_facts:
            payload["unmodelled_facts"] = [
                thaw_json(omission) for omission in self.unmodelled_facts
            ]
        return payload


@dataclass(frozen=True, slots=True)
class CatalogColumn:
    id: str
    name: str
    type: CatalogValueType

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "type": self.type.value}


@dataclass(frozen=True, slots=True)
class CatalogCell:
    value: str | int | float | bool | None
    numeric_value: int | float | None = None
    omission_code: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"value": self.value}
        if self.numeric_value is not None:
            payload["numeric_value"] = self.numeric_value
        if self.omission_code:
            payload["omission_code"] = self.omission_code
        return payload


@dataclass(frozen=True, slots=True)
class CatalogRow:
    cells: tuple[CatalogCell, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"cells": [cell.as_dict() for cell in self.cells]}


@dataclass(frozen=True, slots=True)
class CatalogTable:
    """A typed, row-addressable transcription of one printed table."""

    id: str
    name: str
    section_id: str
    page: int
    fact_status: FactStatus
    columns: tuple[CatalogColumn, ...]
    rows: tuple[CatalogRow, ...]
    source_row_count: int
    omissions: tuple[Mapping[str, Any], ...]
    provenance: str

    def as_dict(self, *, include_rows: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "section_id": self.section_id,
            "page": self.page,
            "fact_status": self.fact_status.value,
            "columns": [column.as_dict() for column in self.columns],
            "source_row_count": self.source_row_count,
            "omissions": [thaw_json(omission) for omission in self.omissions],
            "provenance": self.provenance,
        }
        if include_rows:
            payload["rows"] = [row.as_dict() for row in self.rows]
        return payload


@dataclass(frozen=True, slots=True)
class SearchCandidate:
    """The small common shape searched across catalog and legacy content rows."""

    id: str
    kind: str
    name: str
    aliases: tuple[str, ...]
    simulation: SimulationSupport
    fact_status: FactStatus | None
    pages: tuple[int, ...]
    source: str


def search_rank(candidate: SearchCandidate, query: str) -> tuple[int, str, str]:
    """Stable exact/prefix/token/substring ranking for a case-insensitive query."""
    needle = query.strip().casefold()
    name = candidate.name.casefold()
    aliases = tuple(alias.casefold() for alias in candidate.aliases)
    identity = candidate.id.casefold()
    values = (name, *aliases, identity)
    if not needle:
        rank = 4
    elif needle in values:
        rank = 0
    elif any(value.startswith(needle) for value in values):
        rank = 1
    elif any(any(token.startswith(needle) for token in value.split()) for value in values):
        rank = 2
    elif any(needle in value for value in values):
        rank = 3
    else:
        rank = 5
    return rank, name, candidate.id


def ranked_candidates(
    candidates: Sequence[SearchCandidate], query: str
) -> list[SearchCandidate]:
    """Return matching candidates in a deterministic order."""
    ranked = sorted(candidates, key=lambda candidate: search_rank(candidate, query))
    if not query.strip():
        return ranked
    return [candidate for candidate in ranked if search_rank(candidate, query)[0] < 5]


def simulation_support(*, executable: bool, has_omissions: bool) -> SimulationSupport:
    if not executable:
        return SimulationSupport.REFERENCE_ONLY
    if has_omissions:
        return SimulationSupport.PARTIAL
    return SimulationSupport.EXECUTABLE
