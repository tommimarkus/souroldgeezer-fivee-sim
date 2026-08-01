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
CURRENT_SOURCE_URL = (
    "https://media.dndbeyond.com/compendium-images/srd/5.2/SRD_CC_v5.2.1.pdf"
)
LEGACY_SOURCE_URL = "https://media.wizards.com/2025/downloads/dnd/SRD_CC_v5.2.1.pdf"
PINNED_SOURCE_SHA256 = "8974902d109d6e63672d7c490bde9ccf052410503d9cfa768237154fbc5e3d87"


@pytest.fixture
def batch_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("srd_catalog_batch", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _minimal_verified_source(
    batch_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_url: str,
    *,
    manifest_sha256: str = PINNED_SOURCE_SHA256,
    pdf_sha256: str = PINNED_SOURCE_SHA256,
) -> Path:
    agent = tmp_path / "agent"
    agent.mkdir()
    source_pdf = tmp_path / "SRD_CC_v5.2.1.pdf"
    source_pdf.write_bytes(b"test PDF")
    (agent / "agent-manifest.json").write_text(
        json.dumps(
            {
                "source_pdf": {
                    "source_url": source_url,
                    "sha256": manifest_sha256,
                    "path": str(source_pdf),
                }
            }
        ),
        encoding="utf-8",
    )
    for name in (
        "sections-index.json",
        "tables-index.json",
        "statblocks.json",
        "term-index.json",
    ):
        (agent / name).write_text("[]", encoding="utf-8")
    monkeypatch.setattr(
        batch_module,
        "EXPECTED_COUNTS",
        {key: 0 for key in batch_module.EXPECTED_COUNTS},
    )
    monkeypatch.setattr(batch_module, "_sha256", lambda _path: pdf_sha256)
    return tmp_path


@pytest.mark.parametrize("source_url", [CURRENT_SOURCE_URL, LEGACY_SOURCE_URL])
def test_source_verifier_accepts_current_and_legacy_official_urls_for_the_pinned_pdf(
    batch_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_url: str,
) -> None:
    source_root = _minimal_verified_source(
        batch_module, monkeypatch, tmp_path, source_url
    )

    verified = batch_module.verify_source(source_root)

    assert batch_module.SOURCE_URL == CURRENT_SOURCE_URL
    assert batch_module.SOURCE_SHA256 == PINNED_SOURCE_SHA256
    assert verified["pdf"] == tmp_path / "SRD_CC_v5.2.1.pdf"


def test_source_verifier_rejects_an_unknown_url_even_when_the_hash_matches(
    batch_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_root = _minimal_verified_source(
        batch_module, monkeypatch, tmp_path, "https://example.com/copied-srd.pdf"
    )

    with pytest.raises(batch_module.BatchError, match="verified SRD 5.2.1 extraction"):
        batch_module.verify_source(source_root)


@pytest.mark.parametrize(
    ("manifest_sha256", "pdf_sha256"),
    [
        ("0" * 64, PINNED_SOURCE_SHA256),
        (PINNED_SOURCE_SHA256, "0" * 64),
    ],
    ids=["wrong-manifest-hash", "wrong-pdf-hash"],
)
def test_legacy_source_url_still_requires_the_exact_declared_and_pdf_hashes(
    batch_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifest_sha256: str,
    pdf_sha256: str,
) -> None:
    source_root = _minimal_verified_source(
        batch_module,
        monkeypatch,
        tmp_path,
        LEGACY_SOURCE_URL,
        manifest_sha256=manifest_sha256,
        pdf_sha256=pdf_sha256,
    )

    with pytest.raises(batch_module.BatchError, match="verified SRD 5.2.1 extraction"):
        batch_module.verify_source(source_root)


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
    assert packet["source"] == {
        "name": "System Reference Document 5.2.1",
        "url": CURRENT_SOURCE_URL,
        "sha256": PINNED_SOURCE_SHA256,
    }


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


def _reviewed_table_merge(
    batch_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    patch: dict[str, object],
) -> tuple[dict[str, object], Path]:
    table = {
        "id": "t4",
        "name": "Results",
        "section_id": "s4",
        "page": 1,
        "fact_status": "pending",
        "columns": [{"id": "result", "name": "Result", "type": "string"}],
        "rows": [],
        "source_row_count": 2,
        "omissions": [],
        "provenance": "SRD 5.2.1",
    }
    packs = {
        4: {
            "pack": "reviewed",
            "version": "1.0",
            "provenance": "SRD 5.2.1",
            "catalog": [],
            "catalog_tables": [table],
        }
    }
    reviewed = tmp_path / "reviewed.json"
    reviewed.write_text(
        json.dumps({"catalog_tables": [{"id": "t4", **patch}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(batch_module, "verify_source", lambda _root: {})
    monkeypatch.setattr(batch_module, "validate_committed", lambda _root: {})
    monkeypatch.setattr(batch_module, "_catalog_packs", lambda: packs)
    monkeypatch.setattr(batch_module, "DATA_ROOT", tmp_path / "catalog")
    batch_module.DATA_ROOT.mkdir()
    return table, reviewed


def test_reviewed_table_can_correct_an_extracted_source_row_count(
    batch_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rows = [
        {"cells": [{"value": "A"}]},
        {"cells": [{"value": "B"}]},
        {"cells": [{"value": "C"}]},
    ]
    table, reviewed = _reviewed_table_merge(
        batch_module,
        monkeypatch,
        tmp_path,
        {
            "fact_status": "complete",
            "source_row_count": 3,
            "rows": rows,
        },
    )

    result = batch_module.merge_reviewed(tmp_path, reviewed)

    assert result == {"changed": 1}
    assert table["source_row_count"] == 3
    assert table["rows"] == rows
    committed = json.loads((batch_module.DATA_ROOT / "catalog-04.json").read_text())
    assert committed["catalog_tables"][0]["source_row_count"] == 3


def test_reviewed_table_rejects_a_corrected_count_that_does_not_match_rows(
    batch_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, reviewed = _reviewed_table_merge(
        batch_module,
        monkeypatch,
        tmp_path,
        {
            "fact_status": "complete",
            "source_row_count": 3,
            "rows": [
                {"cells": [{"value": "A"}]},
                {"cells": [{"value": "B"}]},
            ],
        },
    )

    with pytest.raises(batch_module.BatchError, match="account for every source row"):
        batch_module.merge_reviewed(tmp_path, reviewed)

    assert not (batch_module.DATA_ROOT / "catalog-04.json").exists()


@pytest.mark.parametrize("invalid_count", [[], True], ids=["list", "boolean"])
def test_reviewed_table_rejects_an_invalid_source_row_count_without_a_traceback(
    batch_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_count: object,
) -> None:
    _, reviewed = _reviewed_table_merge(
        batch_module,
        monkeypatch,
        tmp_path,
        {
            "fact_status": "complete",
            "source_row_count": invalid_count,
            "rows": [],
        },
    )

    with pytest.raises(batch_module.BatchError, match="source_row_count must be an integer"):
        batch_module.merge_reviewed(tmp_path, reviewed)

    assert not (batch_module.DATA_ROOT / "catalog-04.json").exists()


def test_reviewed_table_validates_corrected_data_before_persistence(
    batch_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, reviewed = _reviewed_table_merge(
        batch_module,
        monkeypatch,
        tmp_path,
        {
            "fact_status": "complete",
            "source_row_count": 3,
            "columns": [{"id": "result", "name": "Result", "type": "invalid"}],
            "rows": [
                {"cells": [{"value": "A"}]},
                {"cells": [{"value": "B"}]},
                {"cells": [{"value": "C"}]},
            ],
        },
    )

    with pytest.raises(batch_module.BatchError, match="runtime pack validation"):
        batch_module.merge_reviewed(tmp_path, reviewed)

    assert not (batch_module.DATA_ROOT / "catalog-04.json").exists()
