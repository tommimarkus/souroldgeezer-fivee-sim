"""Structured catalog loading, indexing, and transport-neutral query behavior."""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from fivee_sim.catalog import FactStatus, SimulationSupport
from fivee_sim.content import ContentError, builtin_registry, load_packs
from fivee_sim.service import catalog as catalog_service

from . import api


def _write_pack(root: Path, name: str, payload: dict[str, Any]) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _custom_pack() -> dict[str, Any]:
    return {
        "pack": "vale-catalog",
        "version": "1",
        "provenance": "Original campaign content",
        "creatures": [
            {
                "name": "Vale Stalker",
                "ac": 14,
                "max_hp": 22,
                "provenance": "Original campaign content",
                "unmodelled_facts": [
                    {"code": "unsupported_sense", "feature": "Blood Scent"}
                ],
            }
        ],
        "catalog": [
            {
                "id": "vale-stalker",
                "kind": "creature",
                "name": "Vale Stalker",
                "source_ids": ["campaign:vale-stalker"],
                "pages": [7],
                "fact_status": "complete",
                "facts": {"challenge_rating": 2, "habitats": ["forest"]},
                "aliases": ["Stalker of the Vale"],
                "content_ref": {"section": "creatures", "name": "Vale Stalker"},
                "unmodelled_facts": [
                    {"code": "unsupported_sense", "feature": "Blood Scent"}
                ],
                "provenance": "Original campaign content",
            }
        ],
        "catalog_tables": [
            {
                "id": "vale-difficulty",
                "name": "Vale Difficulty",
                "section_id": "campaign:vale-stalker",
                "page": 7,
                "fact_status": "complete",
                "columns": [
                    {"id": "difficulty", "name": "Difficulty", "type": "string"},
                    {"id": "dc", "name": "DC", "type": "integer"},
                ],
                "rows": [
                    {"cells": [{"value": "ordinary"}, {"value": 12}]},
                    {"cells": [{"value": "dire"}, {"value": 18}]},
                ],
                "source_row_count": 2,
                "omissions": [],
                "provenance": "Original campaign content",
            }
        ],
    }


class TestCatalogSchemaAndMerge:
    def test_existing_packs_remain_compatible_and_new_sections_are_optional(
        self, tmp_path: Path
    ) -> None:
        path = _write_pack(
            tmp_path,
            "legacy.json",
            {
                "pack": "legacy",
                "provenance": "Original campaign content",
                "spells": [
                    {"name": "Vale Spark", "level": 0, "provenance": "Original"}
                ],
            },
        )
        registry = load_packs([path], builtin="exclude", include_environment=False)
        assert "Vale Spark" in registry.spells
        assert registry.catalog == {}
        assert registry.catalog_tables == {}

    def test_catalog_and_tables_load_into_an_immutable_snapshot(self, tmp_path: Path) -> None:
        path = _write_pack(tmp_path, "catalog.json", _custom_pack())
        registry = load_packs([path], builtin="exclude", include_environment=False)

        assert isinstance(registry.catalog, MappingProxyType)
        assert registry.catalog["vale-stalker"].fact_status is FactStatus.COMPLETE
        assert registry.catalog_tables["vale-difficulty"].source_row_count == 2
        catalog_for_mutation: Any = registry.catalog
        with pytest.raises(TypeError):
            catalog_for_mutation["another"] = registry.catalog["vale-stalker"]
        facts_for_mutation: Any = registry.catalog["vale-stalker"].facts
        with pytest.raises(TypeError):
            facts_for_mutation["challenge_rating"] = 3

    def test_same_level_catalog_collisions_fail(self, tmp_path: Path) -> None:
        first = _custom_pack()
        second = _custom_pack()
        second["pack"] = "other"
        first_path = _write_pack(tmp_path / "a", "first.json", first)
        second_path = _write_pack(tmp_path / "b", "second.json", second)

        with pytest.raises(ContentError, match="same level"):
            load_packs(
                [first_path, second_path], builtin="exclude", include_environment=False
            )

    def test_a_higher_level_catalog_replacement_requires_override_intent(
        self, tmp_path: Path
    ) -> None:
        payload = _custom_pack()
        payload["catalog"][0]["id"] = "1800-15-157-goblin-warrior"
        path = _write_pack(tmp_path, "override.json", payload)

        with pytest.raises(ContentError, match='Set "overrides": true'):
            load_packs([path], include_environment=False)

        payload["catalog"][0]["overrides"] = True
        path = _write_pack(tmp_path, "override.json", payload)
        registry = load_packs([path], include_environment=False)
        assert registry.catalog["1800-15-157-goblin-warrior"].name == "Vale Stalker"

    def test_a_higher_level_table_replacement_requires_override_intent(
        self, tmp_path: Path
    ) -> None:
        payload = _custom_pack()
        del payload["catalog"]
        payload["catalog_tables"][0]["id"] = "006-ability-modifiers"
        path = _write_pack(tmp_path, "table-override.json", payload)

        with pytest.raises(ContentError, match='Set "overrides": true'):
            load_packs([path], include_environment=False)

        payload["catalog_tables"][0]["overrides"] = True
        path = _write_pack(tmp_path, "table-override.json", payload)
        registry = load_packs([path], include_environment=False)
        assert registry.catalog_tables["006-ability-modifiers"].name == "Vale Difficulty"

    def test_structured_omissions_coexist_with_legacy_strings(self, tmp_path: Path) -> None:
        payload = _custom_pack()
        payload["creatures"][0]["unmodelled"] = ["Legacy author note"]
        path = _write_pack(tmp_path, "mixed.json", payload)
        registry = load_packs([path], builtin="exclude", include_environment=False)
        record = registry.creatures["Vale Stalker"]
        assert record["unmodelled"] == ["Legacy author note"]
        assert record["unmodelled_facts"][0]["code"] == "unsupported_sense"

    def test_table_cells_match_their_declared_types(self, tmp_path: Path) -> None:
        payload = _custom_pack()
        payload["catalog_tables"][0]["rows"][0]["cells"][1]["value"] = "twelve"
        path = _write_pack(tmp_path, "bad-table.json", payload)

        with pytest.raises(ContentError, match="does not match integer column"):
            load_packs([path], builtin="exclude", include_environment=False)

    def test_an_omitted_table_cell_requires_a_structured_code(self, tmp_path: Path) -> None:
        payload = _custom_pack()
        payload["catalog_tables"][0]["rows"][0]["cells"][0]["value"] = None
        path = _write_pack(tmp_path, "bad-omission.json", payload)

        with pytest.raises(ContentError, match="null only with an omission_code"):
            load_packs([path], builtin="exclude", include_environment=False)


class TestCatalogQueries:
    def test_search_ranking_filters_and_pagination_are_stable(self) -> None:
        registry = builtin_registry()
        exact = catalog_service.search(registry, "Goblin Warrior", limit=25)
        assert exact["results"][0]["name"] == "Goblin Warrior"

        page = catalog_service.search(registry, "", kind="spell", since=3, limit=25)
        assert len(page["results"]) <= 25
        assert page["since"] == 3
        assert page["total"] == 339
        assert page["results"] == catalog_service.search(
            registry, "", kind="spell", since=3, limit=25
        )["results"]

        executable = catalog_service.search(
            registry, "", simulation=SimulationSupport.EXECUTABLE, limit=25
        )
        assert all(row["simulation"] == "executable" for row in executable["results"])

    def test_search_includes_loaded_legacy_content_without_a_catalog_record(
        self, tmp_path: Path
    ) -> None:
        payload = _custom_pack()
        del payload["catalog"]
        del payload["catalog_tables"]
        path = _write_pack(tmp_path, "legacy.json", payload)
        registry = load_packs([path], builtin="exclude", include_environment=False)

        result = catalog_service.search(registry, "Vale Stalker")
        assert result["total"] == 1
        assert result["results"][0]["id"].startswith("content:creatures:")
        assert result["results"][0]["simulation"] == "partial"

    def test_get_reports_catalog_and_execution_sources_separately(
        self, tmp_path: Path
    ) -> None:
        payload = {
            "pack": "goblin-house-rule",
            "provenance": "Original campaign content",
            "creatures": [
                {
                    "name": "Goblin Warrior",
                    "ac": 99,
                    "max_hp": 10,
                    "provenance": "Original campaign content",
                    "overrides": True,
                }
            ],
        }
        path = _write_pack(tmp_path, "goblin.json", payload)
        registry = load_packs([path], include_environment=False)

        entry = catalog_service.get_record(registry, "1800-15-157-goblin-warrior")
        assert entry["sources"]["catalog"] == "bundled:catalog-15-monsters-a-z.json"
        assert entry["sources"]["executable"] == str(path)
        assert entry["content_ref"] == {
            "section": "creatures",
            "name": "Goblin Warrior",
        }

        fireball = catalog_service.get_record(registry, "904-10-15-4-fireball")
        assert fireball["sources"] == {
            "catalog": "bundled:catalog-10-spells.json",
            "executable": "bundled:catalog-10-spells.json",
        }

        wolf = catalog_service.get_record(registry, "2062-16-95-wolf")
        assert wolf["sources"] == {
            "catalog": "bundled:catalog-16-animals.json",
            "executable": "bundled:catalog-16-animals.json",
        }

    def test_table_rows_are_bounded_and_paginated(self, tmp_path: Path) -> None:
        payload = _custom_pack()
        rows = payload["catalog_tables"][0]["rows"]
        payload["catalog_tables"][0]["rows"] = rows * 20
        payload["catalog_tables"][0]["source_row_count"] = 40
        path = _write_pack(tmp_path, "table.json", payload)
        registry = load_packs([path], builtin="exclude", include_environment=False)

        result = catalog_service.get_table(registry, "vale-difficulty", since=4, limit=99)
        assert result["since"] == 4
        assert len(result["rows"]) == 25
        assert result["total"] == 40
        assert result["next_since"] == 29

    def test_missing_catalog_ids_and_invalid_pagination_are_clear(self) -> None:
        registry = builtin_registry()
        with pytest.raises(ValueError, match="no catalog record"):
            catalog_service.get_record(registry, "missing")
        with pytest.raises(ValueError, match="since"):
            catalog_service.search(registry, "", since=-1)


class TestCatalogTools:
    def test_topicless_lookup_is_compact_counts_and_guidance(self) -> None:
        result = api.lookup_rule()
        assert result["counts"]["creatures"] == 6
        assert "creatures" not in result
        assert result["guidance"]["search_tool"] == "catalog_search"
        assert len(json.dumps(result).encode()) <= 16 * 1024

    def test_tool_responses_are_bounded(self) -> None:
        status = api.content_status()
        search = api.catalog_search("", limit=25)
        record = api.catalog_get("1800-15-157-goblin-warrior")
        table = api.catalog_table("006-ability-modifiers", limit=25)

        assert len(json.dumps(status).encode()) <= 16 * 1024
        assert len(json.dumps(search).encode()) <= 16 * 1024
        assert len(json.dumps(record).encode()) <= 64 * 1024
        assert len(json.dumps(table).encode()) <= 64 * 1024
