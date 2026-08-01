"""Source-independent pins for the committed SRD 5.2.1 catalog inventory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fivee_sim.content import builtin_registry

ENGINE = Path(__file__).resolve().parents[1]
PLUGIN = ENGINE.parent
REPO = PLUGIN.parent
DATA = ENGINE / "src" / "fivee_sim" / "data" / "srd"
MANIFEST = DATA / "catalog-manifest.json"
EXPECTED_ATTRIBUTION = (
    'This work includes material from the System Reference Document 5.2.1 ("SRD 5.2.1") '
    "by Wizards of the Coast LLC, available at https://www.dndbeyond.com/srd. The SRD "
    "5.2.1 is licensed under the Creative Commons Attribution 4.0 International License, "
    "available at https://creativecommons.org/licenses/by/4.0/legalcode."
)


def _walk(value: Any) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.append((str(key), child))
            found.extend(_walk(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk(child))
    return found


def test_committed_manifest_pins_the_official_source_and_complete_inventory() -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert payload["source"] == {
        "name": "System Reference Document 5.2.1",
        "url": "https://media.wizards.com/2025/downloads/dnd/SRD_CC_v5.2.1.pdf",
        "sha256": "8974902d109d6e63672d7c490bde9ccf052410503d9cfa768237154fbc5e3d87",
    }
    assert payload["counts"] == {
        "sections": 2062,
        "tables": 227,
        "stat_blocks": 336,
        "spells": 339,
        "glossary_terms": 155,
    }
    assert len(payload["section_ids"]) == len(set(payload["section_ids"])) == 2062
    assert len(payload["table_ids"]) == len(set(payload["table_ids"])) == 227
    assert len(payload["stat_block_section_ids"]) == 336
    assert len(payload["spell_section_ids"]) == 339
    assert len(payload["glossary_section_ids"]) == 155
    assert len(set(payload["stat_block_section_ids"])) == 336
    assert len(set(payload["spell_section_ids"])) == 339
    assert len(set(payload["glossary_section_ids"])) == 155


def test_bundled_registry_reconciles_exactly_with_the_committed_manifest() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    registry = builtin_registry()

    assert set(registry.catalog) == set(manifest["section_ids"])
    assert set(registry.catalog_tables) == set(manifest["table_ids"])
    assert sum(row.kind == "creature" for row in registry.catalog.values()) == 336
    assert sum(row.kind == "spell" for row in registry.catalog.values()) == 339
    assert sum(row.kind == "glossary" for row in registry.catalog.values()) == 155
    assert {
        identifier for identifier, row in registry.catalog.items() if row.kind == "creature"
    } == set(manifest["stat_block_section_ids"])
    assert {
        identifier for identifier, row in registry.catalog.items() if row.kind == "spell"
    } == set(manifest["spell_section_ids"])
    assert {
        identifier for identifier, row in registry.catalog.items() if row.kind == "glossary"
    } == set(manifest["glossary_section_ids"])
    assert {row.fact_status.value for row in registry.catalog.values()} == {"pending"}
    assert {row.fact_status.value for row in registry.catalog_tables.values()} == {"pending"}


def test_bundled_catalog_is_facts_only_and_record_bounded() -> None:
    registry = builtin_registry()
    prose_keys = {"body", "description", "flavor", "rules", "text"}
    for record in registry.catalog.values():
        payload = record.as_dict()
        assert not (prose_keys & {key.casefold() for key, _ in _walk(payload)})
        assert len(json.dumps(payload, ensure_ascii=False).encode()) <= 48 * 1024
        assert record.provenance == "SRD 5.2.1"
    for table in registry.catalog_tables.values():
        assert table.provenance == "SRD 5.2.1"


def test_notice_copies_are_identical_and_keep_the_exact_attribution() -> None:
    root_notice = (REPO / "NOTICE").read_text(encoding="utf-8")
    plugin_notice = (PLUGIN / "NOTICE").read_text(encoding="utf-8")
    assert root_notice == plugin_notice
    assert root_notice.splitlines()[0] == EXPECTED_ATTRIBUTION


def test_every_bundled_executable_srd_record_uses_5_2_1_provenance() -> None:
    registry = builtin_registry()
    for section in ("creatures", "spells", "conditions"):
        for record in registry.records_for(section).values():
            assert record["provenance"] == "SRD 5.2.1"
