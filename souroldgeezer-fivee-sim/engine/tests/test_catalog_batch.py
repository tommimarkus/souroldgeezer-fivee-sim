"""The contributor catalog batch utility fails safely at its source boundary."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

from fivee_sim import content as content_module

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


def test_batch_utility_catalog_mapping_is_pinned_to_the_runtime(
    batch_module: ModuleType,
) -> None:
    assert batch_module.CATALOG_CHAPTERS == content_module.CATALOG_CHAPTERS
    assert batch_module._catalog_filename(4) == "catalog-04-playing-the-game.json"


def test_bootstrap_preserves_executable_records_in_their_catalog_chapters(
    batch_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_root = _minimal_verified_source(
        batch_module, monkeypatch, source_dir, CURRENT_SOURCE_URL
    )
    committed = batch_module.DATA_ROOT
    spells_before = json.loads(
        (committed / batch_module._catalog_filename(10)).read_text()
    )["spells"]
    creatures_15_before = json.loads(
        (committed / batch_module._catalog_filename(15)).read_text()
    )["creatures"]
    creatures_16_before = json.loads(
        (committed / batch_module._catalog_filename(16)).read_text()
    )["creatures"]
    spells_before[0]["range_feet"] = 151
    spells_before[0]["unmodelled_facts"].append(
        {"code": "test_nested_sentinel", "feature": "spell bootstrap sentinel"}
    )
    creatures_15_before[0]["abilities"]["intelligence"] = 11
    creatures_15_before[0]["attacks"][1]["long_range"] = 319
    creatures_15_before[0]["unmodelled_facts"].append(
        {"code": "test_nested_sentinel", "feature": "creature bootstrap sentinel"}
    )
    creatures_16_before[0]["abilities"]["wisdom"] = 13
    creatures_16_before[0]["attacks"][0]["on_hit_max_size"] = "large"

    output = tmp_path / "committed"
    output.mkdir()
    monkeypatch.setattr(batch_module, "DATA_ROOT", output)
    monkeypatch.setattr(
        batch_module, "COMMITTED_MANIFEST", output / "catalog-manifest.json"
    )
    executable_sections = {
        10: {"spells": spells_before},
        15: {"creatures": creatures_15_before},
        16: {"creatures": creatures_16_before},
    }
    for chapter in content_module.CATALOG_CHAPTERS:
        payload = {
            "pack": batch_module._catalog_pack_name(chapter),
            "version": "1.0",
            "provenance": "SRD 5.2.1",
            "attribution": "See NOTICE.",
            **executable_sections.get(chapter, {}),
            "catalog": [],
            "catalog_tables": [],
        }
        (output / batch_module._catalog_filename(chapter)).write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )

    batch_module.bootstrap(source_root)

    chapter_10 = json.loads((output / "catalog-10-spells.json").read_text())
    chapter_15 = json.loads((output / "catalog-15-monsters-a-z.json").read_text())
    chapter_16 = json.loads((output / "catalog-16-animals.json").read_text())
    assert chapter_10["spells"] == spells_before
    assert chapter_15["creatures"] == creatures_15_before
    assert chapter_16["creatures"] == creatures_16_before


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


def test_packet_refuses_to_write_a_review_copy_outside_the_temporary_directory(
    batch_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path in the checkout is outside the temporary tree, so this is the
    branch it trips. The repository refusal is a *second* branch, and the test
    below reaches it with a root that satisfies this one.
    """
    monkeypatch.setattr(batch_module, "verify_source", lambda _root: {})
    monkeypatch.setattr(batch_module, "validate_committed", lambda _root: {})

    with pytest.raises(batch_module.BatchError, match="temporary directory"):
        batch_module.emit_packet(
            Path(tempfile.gettempdir()) / "source",
            SCRIPT.parent / "review-packet.json",
            record_limit=1,
            character_limit=1,
        )


def test_review_packets_follow_the_resolved_temporary_root_not_a_literal_tmp(
    batch_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sandbox may mount ``/tmp`` read-only and hand the process its own root.

    The guard exists to keep source-bearing packets out of the repository, so it
    has to ask ``tempfile`` where the temporary tree is rather than assume. The
    root here is deliberately *outside* ``/tmp`` — under it, a guard that still
    said ``/tmp`` would pass this and prove nothing. Nothing is created: the
    guard resolves and compares, so the path need not exist.
    """
    session_root = Path("/var/tmp/fivee-review-root")
    monkeypatch.setattr(tempfile, "tempdir", str(session_root))

    accepted = batch_module._review_output(session_root / "packet.json")

    assert accepted == (session_root / "packet.json").resolve()


def test_review_packets_stay_out_of_the_repository_even_when_tmpdir_points_into_it(
    batch_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading the root from the environment must not hand over the repository.

    ``/tmp`` was its own proof that a packet landed outside the checkout. A
    caller-controlled root is not, so the refusal is checked on its own terms.
    """
    inside = batch_module.REPO_ROOT / ".worktrees" / "pretend-tmp"
    monkeypatch.setattr(tempfile, "tempdir", str(inside))

    with pytest.raises(batch_module.BatchError, match="below the repository"):
        batch_module._review_output(inside / "packet.json")


def test_the_packet_output_default_lives_under_the_resolved_temporary_root(
    batch_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session_root = tmp_path / "session-tmp"
    session_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(session_root))

    parsed = batch_module._parser().parse_args(["--source-root", str(tmp_path), "packet"])

    assert parsed.output == session_root / "srd-catalog-batch.json"


def test_pack_validation_scratch_follows_the_resolved_temporary_root(
    batch_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pre-commit schema check writes reviewed payloads to scratch first.

    Pinning where that scratch lands is what keeps this green on a host whose
    ``/tmp`` is not writable — the failure it replaces was an ``OSError``.

    The root handed to ``load_packs`` is the only observable: the scratch tree
    is a ``TemporaryDirectory`` and is gone before the call returns, so there is
    no published side effect left to assert on afterwards.
    """
    session_root = tmp_path / "session-tmp"
    session_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(session_root))
    seen: list[Path] = []
    monkeypatch.setattr(
        content_module,
        "load_packs",
        lambda roots, **_kwargs: seen.append(Path(roots[0])),
    )

    batch_module._validate_pack_payloads({})

    assert seen and seen[0].is_relative_to(session_root.resolve())


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
    committed = json.loads(
        (batch_module.DATA_ROOT / "catalog-04-playing-the-game.json").read_text()
    )
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

    assert not (batch_module.DATA_ROOT / "catalog-04-playing-the-game.json").exists()


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

    assert not (batch_module.DATA_ROOT / "catalog-04-playing-the-game.json").exists()


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

    assert not (batch_module.DATA_ROOT / "catalog-04-playing-the-game.json").exists()
