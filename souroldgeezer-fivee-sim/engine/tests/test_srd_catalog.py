"""Source-independent pins for the committed SRD 5.2.1 catalog inventory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fivee_sim import content as content_module
from fivee_sim.catalog import FactStatus
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
EXPECTED_DISCLAIMER_NOTICE = (
    "Section 5 of CC-BY-4.0 includes a Disclaimer of Warranties and Limitation "
    "of Liability that limits our liability to you."
)
EXPECTED_CATALOG_CHAPTERS = {
    1: "legal-information",
    2: "contents",
    3: "index-of-stat-blocks",
    4: "playing-the-game",
    5: "character-creation",
    6: "classes",
    7: "character-origins",
    8: "feats",
    9: "equipment",
    10: "spells",
    11: "rules-glossary",
    12: "gameplay-toolbox",
    13: "magic-items",
    14: "monsters",
    15: "monsters-a-z",
    16: "animals",
}


def _catalog_filename(chapter: int, slug: str) -> str:
    return f"catalog-{chapter:02d}-{slug}.json"


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


def test_bundled_catalog_layout_names_chapters_and_owns_executable_records() -> None:
    expected_files = tuple(
        _catalog_filename(chapter, slug)
        for chapter, slug in EXPECTED_CATALOG_CHAPTERS.items()
    )
    assert content_module.CATALOG_CHAPTERS == EXPECTED_CATALOG_CHAPTERS
    assert content_module.CATALOG_FILES == expected_files
    assert content_module.BUILTIN_FILES == expected_files
    assert {path.name for path in DATA.glob("*.json")} == {
        "catalog-manifest.json",
        *expected_files,
    }

    packs = {
        chapter: json.loads((DATA / _catalog_filename(chapter, slug)).read_text())
        for chapter, slug in EXPECTED_CATALOG_CHAPTERS.items()
    }
    for chapter, slug in EXPECTED_CATALOG_CHAPTERS.items():
        assert packs[chapter]["pack"] == f"srd-5.2.1-catalog-{chapter:02d}-{slug}"

    assert {spell["name"] for spell in packs[10]["spells"]} == {
        "Cure Wounds",
        "Fireball",
        "Guiding Bolt",
        "Hold Person",
        "Shatter",
        "Healing Word",
        "Mass Healing Word",
        "Mass Cure Wounds",
        "Heal",
        "Prayer of Healing",
        "Regenerate",
        "Fire Bolt",
        "Sacred Flame",
    }
    assert {item["name"] for item in packs[13]["items"]} == {"Potion of Healing"}
    assert {creature["name"] for creature in packs[15]["creatures"]} == {
        "Goblin Warrior",
        "Goblin Boss",
        "Ogre",
        "Skeleton",
        "Zombie",
    }
    assert [creature["name"] for creature in packs[16]["creatures"]] == ["Wolf"]

    links = {
        record["name"]: record["content_ref"]
        for chapter in (10, 13, 15, 16)
        for record in packs[chapter]["catalog"]
        if "content_ref" in record
    }
    assert links == {
        "Cure Wounds": {"section": "spells", "name": "Cure Wounds"},
        "Potions of Healing": {"section": "items", "name": "Potion of Healing"},
        "Fireball": {"section": "spells", "name": "Fireball"},
        "Guiding Bolt": {"section": "spells", "name": "Guiding Bolt"},
        "Hold Person": {"section": "spells", "name": "Hold Person"},
        "Shatter": {"section": "spells", "name": "Shatter"},
        "Healing Word": {"section": "spells", "name": "Healing Word"},
        "Mass Healing Word": {"section": "spells", "name": "Mass Healing Word"},
        "Mass Cure Wounds": {"section": "spells", "name": "Mass Cure Wounds"},
        "Heal": {"section": "spells", "name": "Heal"},
        "Prayer of Healing": {"section": "spells", "name": "Prayer of Healing"},
        "Regenerate": {"section": "spells", "name": "Regenerate"},
        "Fire Bolt": {"section": "spells", "name": "Fire Bolt"},
        "Sacred Flame": {"section": "spells", "name": "Sacred Flame"},
        "Goblin Warrior": {"section": "creatures", "name": "Goblin Warrior"},
        "Goblin Boss": {"section": "creatures", "name": "Goblin Boss"},
        "Ogre": {"section": "creatures", "name": "Ogre"},
        "Skeleton": {"section": "creatures", "name": "Skeleton"},
        "Zombie": {"section": "creatures", "name": "Zombie"},
        "Wolf": {"section": "creatures", "name": "Wolf"},
    }


def test_committed_manifest_pins_the_official_source_and_complete_inventory() -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert payload["source"] == {
        "name": "System Reference Document 5.2.1",
        "url": "https://media.dndbeyond.com/compendium-images/srd/5.2/SRD_CC_v5.2.1.pdf",
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

def test_bundled_catalog_progress_obeys_the_fact_lifecycle() -> None:
    registry = builtin_registry()
    for row in registry.catalog.values():
        if row.fact_status in {FactStatus.PENDING, FactStatus.NO_STRUCTURED_FACTS}:
            assert not row.facts
        else:
            assert row.facts
    for table in registry.catalog_tables.values():
        if table.fact_status is FactStatus.PENDING:
            assert not table.rows
        elif table.fact_status is FactStatus.COMPLETE:
            assert len(table.rows) == table.source_row_count
        else:
            assert not table.rows


def test_trinkets_d100_keys_preserve_printed_boundaries_and_numeric_rolls() -> None:
    table = builtin_registry().catalog_tables["026-trinkets"]
    key_cells = [row.cells[0] for row in table.rows]

    assert [cell.value for cell in key_cells] == [
        *(f"{roll:02d}" for roll in range(1, 100)),
        "00",
    ]
    assert [cell.numeric_value for cell in key_cells] == list(range(1, 101))


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
    assert EXPECTED_DISCLAIMER_NOTICE in root_notice.splitlines()


def test_every_bundled_executable_srd_record_uses_5_2_1_provenance() -> None:
    registry = builtin_registry()
    for section in ("creatures", "spells", "conditions"):
        for record in registry.records_for(section).values():
            assert record["provenance"] == "SRD 5.2.1"
