"""The contributor catalog batch utility fails safely at its source boundary."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "srd-catalog-batch.py"


@pytest.fixture
def batch_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("srd_catalog_batch", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_batch_utility_documents_its_bounded_packet_and_merge_modes() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0
    assert "packet" in result.stdout
    assert "merge" in result.stdout
    assert "validate" in result.stdout
    assert "20" in result.stdout
    assert "40000" in result.stdout


def test_batch_utility_refuses_an_unverified_source_root(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--source-root", str(tmp_path), "validate"],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 2
    assert "verified SRD 5.2.1 extraction" in result.stderr


def test_packet_interleaves_pending_sections_and_tables(
    batch_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = tmp_path / "agent"
    (agent / "sections").mkdir(parents=True)
    (agent / "tables").mkdir()
    (agent / "sections" / "section.md").write_text("short section", encoding="utf-8")
    source_table = {"columns": ["Roll", "Result"], "rows": [{"cells": ["1", "A"]}]}
    (agent / "tables" / "table.json").write_text(json.dumps(source_table), encoding="utf-8")
    source = {
        "sections": [
            {
                "id": "s4",
                "index": 0,
                "section": "4.1",
                "text_path": "sections/section.md",
            }
        ],
        "tables": [
            {
                "id": "t4",
                "section_id": "s4",
                "path": "tables/table.json",
            }
        ],
    }
    packs: dict[int, dict[str, list[dict[str, object]]]] = {
        chapter: {"catalog": [], "catalog_tables": []} for chapter in range(1, 17)
    }
    packs[4] = {
        "catalog": [
            {
                "id": "s4",
                "kind": "section",
                "name": "Rules",
                "pages": [1],
                "fact_status": "pending",
            }
        ],
        "catalog_tables": [
            {
                "id": "t4",
                "name": "Results",
                "section_id": "s4",
                "page": 1,
                "fact_status": "pending",
            }
        ],
    }
    monkeypatch.setattr(batch_module, "verify_source", lambda _root: source)
    monkeypatch.setattr(batch_module, "validate_committed", lambda _root: {})
    monkeypatch.setattr(batch_module, "_catalog_packs", lambda: packs)
    output = tmp_path / "packet.json"

    result = batch_module.emit_packet(
        tmp_path, output, record_limit=2, character_limit=40_000
    )

    packet = json.loads(output.read_text(encoding="utf-8"))
    assert result["primary_records"] == 2
    assert packet["actual"]["primary_records"] == 2
    assert [record["id"] for record in packet["records"]] == ["s4"]
    assert [table["id"] for table in packet["catalog_tables"]] == ["t4"]
    assert packet["catalog_tables"][0]["source_table"] == source_table


def test_packet_refuses_to_write_a_review_copy_below_the_repository(
    batch_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(batch_module, "verify_source", lambda _root: {})
    monkeypatch.setattr(batch_module, "validate_committed", lambda _root: {})

    with pytest.raises(batch_module.BatchError, match="under /tmp"):
        batch_module.emit_packet(
            Path("/tmp/source"),
            SCRIPT.parent / "review-packet.json",
            record_limit=1,
            character_limit=1,
        )


def test_packet_refuses_an_evidence_path_that_escapes_the_extraction(
    batch_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = {
        "sections": [
            {
                "id": "s4",
                "index": 0,
                "section": "4.1",
                "text_path": "../../outside.md",
            }
        ],
        "tables": [],
    }
    packs: dict[int, dict[str, list[dict[str, object]]]] = {
        chapter: {"catalog": [], "catalog_tables": []} for chapter in range(1, 17)
    }
    packs[4]["catalog"] = [
        {
            "id": "s4",
            "kind": "section",
            "name": "Rules",
            "pages": [1],
            "fact_status": "pending",
        }
    ]
    monkeypatch.setattr(batch_module, "verify_source", lambda _root: source)
    monkeypatch.setattr(batch_module, "validate_committed", lambda _root: {})
    monkeypatch.setattr(batch_module, "_catalog_packs", lambda: packs)

    with pytest.raises(batch_module.BatchError, match="escapes the extraction"):
        batch_module.emit_packet(
            tmp_path,
            tmp_path / "packet.json",
            record_limit=1,
            character_limit=40_000,
        )


def test_reviewed_pack_payloads_pass_the_runtime_schema_before_commit(
    batch_module: ModuleType,
) -> None:
    invalid = {
        "pack": "reviewed",
        "provenance": "SRD 5.2.1",
        "catalog": [
            {
                "id": "s4",
                "kind": "section",
                "name": "Rules",
                "source_ids": ["s4"],
                "pages": [1],
                "fact_status": "complete",
                "facts": {"description": "copied prose"},
                "provenance": "SRD 5.2.1",
            }
        ],
    }

    with pytest.raises(batch_module.BatchError, match="runtime pack validation"):
        batch_module._validate_pack_payloads({4: invalid})
