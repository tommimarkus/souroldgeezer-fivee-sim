#!/usr/bin/env python3
"""Build and advance the facts-only SRD 5.2.1 catalog in bounded review packets.

The extraction is contributor evidence, never a runtime input.  This utility
requires its official PDF hash and reconciles committed IDs before it emits or
merges anything.  Review packets may contain source prose, so they default to
``/tmp`` and are never written below the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

SOURCE_URL = "https://media.wizards.com/2025/downloads/dnd/SRD_CC_v5.2.1.pdf"
SOURCE_SHA256 = "8974902d109d6e63672d7c490bde9ccf052410503d9cfa768237154fbc5e3d87"
SOURCE_NAME = "System Reference Document 5.2.1"
EXPECTED_COUNTS = {
    "sections": 2062,
    "tables": 227,
    "stat_blocks": 336,
    "spells": 339,
    "glossary_terms": 155,
}
PROVENANCE = "SRD 5.2.1"
DEFAULT_RECORD_LIMIT = 20
DEFAULT_CHARACTER_LIMIT = 40_000
CHAPTER_ORDER = (*range(4, 10), 10, 11, 12, 13, 14, 15, 16, 1, 2, 3)
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = (
    REPO_ROOT
    / "souroldgeezer-fivee-sim"
    / "engine"
    / "src"
    / "fivee_sim"
    / "data"
    / "srd"
)
COMMITTED_MANIFEST = DATA_ROOT / "catalog-manifest.json"
ENGINE_SOURCE = DATA_ROOT.parents[2]


class BatchError(ValueError):
    """A source or reviewed batch failed a contributor-facing invariant."""


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BatchError(f"cannot read {path}: {error}") from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise BatchError(f"cannot hash source PDF: {error}") from error
    return digest.hexdigest()


def _evidence_path(source_root: Path, relative: object) -> Path:
    """Resolve an extraction-owned evidence path without following it outside."""
    agent_root = (source_root / "agent").resolve()
    candidate = (agent_root / str(relative)).resolve()
    if not candidate.is_relative_to(agent_root):
        raise BatchError(f"source evidence path escapes the extraction: {relative!r}")
    return candidate


def _review_output(path: Path) -> Path:
    """Keep source-bearing review packets in the machine-local temporary tree."""
    resolved = path.resolve()
    if not resolved.is_relative_to(Path("/tmp").resolve()):
        raise BatchError("review packets must be written under /tmp")
    return resolved


def _validate_pack_payloads(packs: dict[int, dict[str, Any]]) -> None:
    """Apply the runtime pack schema before any reviewed data reaches the repo."""
    source_path = str(ENGINE_SOURCE)
    if source_path not in sys.path:
        sys.path.insert(0, source_path)
    from fivee_sim.content import ContentError, load_packs

    try:
        with tempfile.TemporaryDirectory(prefix="fivee-srd-review-", dir="/tmp") as raw:
            scratch = Path(raw)
            for chapter, payload in packs.items():
                (scratch / f"catalog-{chapter:02d}.json").write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            load_packs([scratch], builtin="exclude", include_environment=False)
    except ContentError as error:
        raise BatchError(f"runtime pack validation failed: {error}") from error


def _write_text_atomic(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def verify_source(source_root: Path) -> dict[str, Any]:
    """Return source inventories only after the pinned PDF and counts reconcile."""
    agent = source_root / "agent"
    manifest_path = agent / "agent-manifest.json"
    required = {
        "manifest": manifest_path,
        "sections": agent / "sections-index.json",
        "tables": agent / "tables-index.json",
        "stat_blocks": agent / "statblocks.json",
        "terms": agent / "term-index.json",
    }
    if any(not path.is_file() for path in required.values()):
        raise BatchError("source root is not a verified SRD 5.2.1 extraction")

    manifest = _json(manifest_path)
    source = manifest.get("source_pdf", {})
    if source.get("source_url") != SOURCE_URL or source.get("sha256") != SOURCE_SHA256:
        raise BatchError("source root is not a verified SRD 5.2.1 extraction")
    pdf_candidates = [Path(str(source.get("path", ""))), source_root.parent / "SRD_CC_v5.2.1.pdf"]
    pdf = next((candidate for candidate in pdf_candidates if candidate.is_file()), None)
    if pdf is None or _sha256(pdf) != SOURCE_SHA256:
        raise BatchError("source root is not a verified SRD 5.2.1 extraction")

    inventories = {name: _json(path) for name, path in required.items() if name != "manifest"}
    sections = inventories["sections"]
    tables = inventories["tables"]
    stat_blocks = inventories["stat_blocks"]
    terms = inventories["terms"]
    if not all(isinstance(value, list) for value in (sections, tables, stat_blocks, terms)):
        raise BatchError("source root is not a verified SRD 5.2.1 extraction")

    spell_ids = _spell_ids(sections, stat_blocks)
    counts = {
        "sections": len(sections),
        "tables": len(tables),
        "stat_blocks": len(stat_blocks),
        "spells": len(spell_ids),
        "glossary_terms": len(terms),
    }
    if counts != EXPECTED_COUNTS:
        raise BatchError(f"source inventory changed: expected {EXPECTED_COUNTS}, got {counts}")
    if len({str(entry["id"]) for entry in sections}) != len(sections):
        raise BatchError("source section IDs are not unique")
    if len({str(entry["id"]) for entry in tables}) != len(tables):
        raise BatchError("source table IDs are not unique")
    return {
        **inventories,
        "spell_ids": spell_ids,
        "pdf": pdf,
    }


def _spell_ids(sections: list[dict[str, Any]], stat_blocks: list[dict[str, Any]]) -> list[str]:
    by_title = {str(entry["title"]): int(entry["index"]) for entry in sections}
    start = by_title.get("Spell Descriptions")
    glossary = by_title.get("Glossary Conventions")
    if start is None or glossary is None:
        return []
    stat_ids = {str(entry["section_id"]) for entry in stat_blocks}
    return [
        str(entry["id"])
        for entry in sections
        if start < int(entry["index"]) < glossary
        and int(entry["level"]) == 3
        and str(entry["id"]) not in stat_ids
    ]


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    return slug or "column"


def _column_records(table: dict[str, Any], source_root: Path) -> list[dict[str, str]]:
    payload = _json(_evidence_path(source_root, table["path"]))
    rows = payload.get("rows", [])
    seen: defaultdict[str, int] = defaultdict(int)
    columns: list[dict[str, str]] = []
    for index, name in enumerate(table["columns"]):
        base = _slug(str(name))
        seen[base] += 1
        identifier = base if seen[base] == 1 else f"{base}-{seen[base]}"
        values = [
            raw_values[index] if index < len(raw_values) else None
            for row in rows
            for raw_values in [row.get("values", [])]
        ]
        column_type = "integer" if values and all(
            value is None or isinstance(value, int) and not isinstance(value, bool)
            for value in values
        ) and any(value is not None for value in values) else "string"
        display_name = str(name).strip() or f"Column {index + 1}"
        columns.append({"id": identifier, "name": display_name, "type": column_type})
    return columns


def _chapter_number(section: dict[str, Any]) -> int:
    return int(str(section["section"]).split(".", 1)[0])


def _parents(
    sections: list[dict[str, Any]], stat_ids: set[str]
) -> tuple[dict[str, str], dict[int, str]]:
    parent: dict[str, str] = {}
    chapter_ids: dict[int, str] = {}
    stack: dict[int, str] = {}
    previous_spell = ""
    for section in sections:
        identifier = str(section["id"])
        chapter = _chapter_number(section)
        level = int(section["level"])
        if level == 1:
            chapter_ids[chapter] = identifier
        if chapter == 10 and identifier in stat_ids:
            if previous_spell:
                parent[identifier] = previous_spell
            continue
        if level > 1 and level - 1 in stack:
            parent[identifier] = stack[level - 1]
        stack = {depth: value for depth, value in stack.items() if depth < level}
        stack[level] = identifier
        if chapter == 10 and level == 3:
            previous_spell = identifier
    return parent, chapter_ids


def bootstrap(source_root: Path) -> dict[str, int]:
    """Generate the sixteen metadata-only chapter packs and committed manifest."""
    source = verify_source(source_root)
    sections: list[dict[str, Any]] = source["sections"]
    tables: list[dict[str, Any]] = source["tables"]
    stat_blocks: list[dict[str, Any]] = source["stat_blocks"]
    terms: list[dict[str, Any]] = source["terms"]
    spell_ids = set(source["spell_ids"])
    stat_by_id = {str(entry["section_id"]): str(entry["name"]) for entry in stat_blocks}
    glossary_ids = {str(entry["defined_in"]) for entry in terms}
    parent_ids, chapter_ids = _parents(sections, set(stat_by_id))

    executable_creatures = {
        "Goblin Warrior",
        "Goblin Boss",
        "Ogre",
        "Skeleton",
        "Wolf",
        "Zombie",
    }
    executable_spells = {"Fireball", "Guiding Bolt", "Hold Person", "Shatter"}
    packs: dict[int, dict[str, Any]] = {
        chapter: {
            "pack": f"srd-5.2.1-catalog-{chapter:02d}",
            "version": "1.0",
            "provenance": PROVENANCE,
            "attribution": "See NOTICE.",
            "catalog": [],
            "catalog_tables": [],
        }
        for chapter in range(1, 17)
    }
    for section in sections:
        identifier = str(section["id"])
        name = str(section["title"])
        chapter = _chapter_number(section)
        kind = "section"
        if identifier in spell_ids:
            kind = "spell"
        elif identifier in glossary_ids:
            kind = "glossary"
        elif identifier in stat_by_id:
            kind = "creature"
        elif chapter == 13 and int(section["level"]) >= 3:
            kind = "magic_item"
        page_start = int(section["page_start"])
        page_end = int(section["page_end"])
        record: dict[str, Any] = {
            "id": identifier,
            "kind": kind,
            "name": name,
            "source_ids": [identifier],
            "chapter_id": chapter_ids[chapter],
            "pages": list(range(page_start, page_end + 1)),
            "fact_status": "pending",
            "facts": {},
            "provenance": PROVENANCE,
        }
        if identifier in parent_ids:
            record["parent_id"] = parent_ids[identifier]
        if kind == "creature" and stat_by_id[identifier] in executable_creatures:
            record["content_ref"] = {
                "section": "creatures",
                "name": stat_by_id[identifier],
            }
        elif kind == "spell" and name in executable_spells:
            record["content_ref"] = {"section": "spells", "name": name}
        packs[chapter]["catalog"].append(record)

    section_chapter = {str(entry["id"]): _chapter_number(entry) for entry in sections}
    for table in tables:
        identifier = str(table["id"])
        chapter = section_chapter[str(table["section_id"])]
        packs[chapter]["catalog_tables"].append(
            {
                "id": identifier,
                "name": str(table["name"]),
                "section_id": str(table["section_id"]),
                "page": int(table["page"]),
                "fact_status": "pending",
                "columns": _column_records(table, source_root),
                "rows": [],
                "source_row_count": int(table["row_count"]),
                "omissions": [],
                "provenance": PROVENANCE,
            }
        )

    _validate_pack_payloads(packs)
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    for chapter, payload in packs.items():
        target = DATA_ROOT / f"catalog-{chapter:02d}.json"
        _write_text_atomic(
            target, json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        )

    manifest = {
        "source": {"name": SOURCE_NAME, "url": SOURCE_URL, "sha256": SOURCE_SHA256},
        "counts": EXPECTED_COUNTS,
        "section_ids": [str(entry["id"]) for entry in sections],
        "table_ids": [str(entry["id"]) for entry in tables],
        "stat_block_section_ids": [str(entry["section_id"]) for entry in stat_blocks],
        "spell_section_ids": list(source["spell_ids"]),
        "glossary_section_ids": [str(entry["defined_in"]) for entry in terms],
    }
    _write_text_atomic(
        COMMITTED_MANIFEST,
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
    )
    return {"records": len(sections), "tables": len(tables)}


def _catalog_packs() -> dict[int, dict[str, Any]]:
    packs: dict[int, dict[str, Any]] = {}
    for chapter in range(1, 17):
        path = DATA_ROOT / f"catalog-{chapter:02d}.json"
        payload = _json(path)
        if not isinstance(payload, dict):
            raise BatchError(f"committed catalog pack is not an object: {path}")
        packs[chapter] = payload
    return packs


def validate_committed(source_root: Path) -> dict[str, int]:
    source = verify_source(source_root)
    manifest = _json(COMMITTED_MANIFEST)
    packs = _catalog_packs()
    records = [record for pack in packs.values() for record in pack.get("catalog", [])]
    tables = [table for pack in packs.values() for table in pack.get("catalog_tables", [])]
    record_ids = [str(record.get("id", "")) for record in records]
    table_ids = [str(table.get("id", "")) for table in tables]
    source_record_ids = [str(entry["id"]) for entry in source["sections"]]
    source_table_ids = [str(entry["id"]) for entry in source["tables"]]
    if record_ids != source_record_ids or manifest.get("section_ids") != source_record_ids:
        raise BatchError("committed section inventory does not reconcile with the pinned source")
    if set(table_ids) != set(source_table_ids) or manifest.get("table_ids") != source_table_ids:
        raise BatchError("committed table inventory does not reconcile with the pinned source")
    if len(record_ids) != len(set(record_ids)) or len(table_ids) != len(set(table_ids)):
        raise BatchError("committed catalog IDs are not unique")
    if any(record.get("provenance") != PROVENANCE for record in records):
        raise BatchError("every committed catalog record must name SRD 5.2.1 provenance")
    if any(table.get("provenance") != PROVENANCE for table in tables):
        raise BatchError("every committed catalog table must name SRD 5.2.1 provenance")
    progress: defaultdict[str, int] = defaultdict(int)
    for record in records:
        progress[str(record.get("fact_status", "missing"))] += 1
    return {"records": len(records), "tables": len(tables), **dict(progress)}


def emit_packet(
    source_root: Path,
    output: Path,
    *,
    record_limit: int,
    character_limit: int,
) -> dict[str, int]:
    output = _review_output(output)
    source = verify_source(source_root)
    validate_committed(source_root)
    packs = _catalog_packs()
    source_sections = {str(entry["id"]): entry for entry in source["sections"]}
    source_tables = {str(entry["id"]): entry for entry in source["tables"]}
    selected_records: list[dict[str, Any]] = []
    selected_tables: list[dict[str, Any]] = []
    characters = 0
    primary_records = 0
    stop = False
    for chapter in CHAPTER_ORDER:
        tables_by_section: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for table in packs[chapter].get("catalog_tables", []):
            tables_by_section[str(table["section_id"])].append(table)
        for record in packs[chapter].get("catalog", []):
            tasks: list[tuple[str, dict[str, Any]]] = []
            if record.get("fact_status") == "pending":
                tasks.append(("record", record))
            tasks.extend(
                ("table", table)
                for table in tables_by_section[str(record["id"])]
                if table.get("fact_status") == "pending"
            )
            for task_kind, task in tasks:
                if task_kind == "record":
                    source_entry = source_sections[str(task["id"])]
                    text_path = _evidence_path(source_root, source_entry["text_path"])
                    try:
                        evidence: str | dict[str, Any] = text_path.read_text(
                            encoding="utf-8"
                        )
                    except OSError as error:
                        raise BatchError(f"cannot read {text_path}: {error}") from error
                    size = len(evidence)
                else:
                    source_entry = source_tables[str(task["id"])]
                    evidence = _json(_evidence_path(source_root, source_entry["path"]))
                    size = len(json.dumps(evidence, ensure_ascii=False))
                if primary_records and (
                    primary_records >= record_limit or characters + size > character_limit
                ):
                    stop = True
                    break
                if task_kind == "record":
                    selected_records.append(
                        {
                            "id": task["id"],
                            "kind": task["kind"],
                            "name": task["name"],
                            "pages": task["pages"],
                            "source_text": evidence,
                        }
                    )
                else:
                    selected_tables.append(
                        {
                            "id": task["id"],
                            "name": task["name"],
                            "section_id": task["section_id"],
                            "page": task["page"],
                            "source_table": evidence,
                        }
                    )
                primary_records += 1
                characters += size
                if primary_records >= record_limit or characters >= character_limit:
                    stop = True
                    break
            if stop:
                break
        if stop:
            break
    packet = {
        "source": {"name": SOURCE_NAME, "url": SOURCE_URL, "sha256": SOURCE_SHA256},
        "limits": {
            "primary_records": record_limit,
            "source_characters": character_limit,
        },
        "actual": {
            "primary_records": primary_records,
            "records": len(selected_records),
            "tables": len(selected_tables),
            "source_characters": characters,
        },
        "records": selected_records,
        "catalog_tables": selected_tables,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(packet, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {
        "primary_records": primary_records,
        "records": len(selected_records),
        "tables": len(selected_tables),
        "source_characters": characters,
    }


def merge_reviewed(source_root: Path, reviewed_path: Path) -> dict[str, int]:
    verify_source(source_root)
    validate_committed(source_root)
    reviewed = _json(reviewed_path)
    if not isinstance(reviewed, dict):
        raise BatchError("reviewed batch must be a JSON object")
    packs = _catalog_packs()
    record_locations = {
        str(record["id"]): record
        for pack in packs.values()
        for record in pack.get("catalog", [])
    }
    table_locations = {
        str(table["id"]): table
        for pack in packs.values()
        for table in pack.get("catalog_tables", [])
    }
    changed = 0
    for patch in reviewed.get("catalog", []):
        if not isinstance(patch, dict) or str(patch.get("id", "")) not in record_locations:
            raise BatchError("reviewed catalog entry names an unknown id")
        status = patch.get("fact_status")
        if status not in {"complete", "no_structured_facts"}:
            raise BatchError("reviewed catalog fact_status must close the pending entry")
        facts = patch.get("facts", {})
        if not isinstance(facts, dict):
            raise BatchError("reviewed catalog facts must be an object")
        target = record_locations[str(patch["id"])]
        target["fact_status"] = status
        target["facts"] = facts
        if "unmodelled_facts" in patch:
            target["unmodelled_facts"] = patch["unmodelled_facts"]
        changed += 1
    for patch in reviewed.get("catalog_tables", []):
        if not isinstance(patch, dict) or str(patch.get("id", "")) not in table_locations:
            raise BatchError("reviewed table entry names an unknown id")
        target = table_locations[str(patch["id"])]
        for key in ("fact_status", "columns", "rows", "omissions"):
            if key in patch:
                target[key] = patch[key]
        if target.get("fact_status") not in {"complete", "no_structured_facts"}:
            raise BatchError("reviewed table fact_status must close the pending entry")
        if target.get("fact_status") == "complete" and len(target.get("rows", [])) != int(
            target["source_row_count"]
        ):
            raise BatchError("a complete table must account for every source row")
        changed += 1
    _validate_pack_payloads(packs)
    for chapter, payload in packs.items():
        target = DATA_ROOT / f"catalog-{chapter:02d}.json"
        _write_text_atomic(
            target, json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        )
    validate_committed(source_root)
    return {"changed": changed}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Advance the SRD 5.2.1 catalog in batches of at most 20 primary records "
            "or 40000 source characters."
        )
    )
    parser.add_argument("--source-root", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="reconcile committed coverage with the source")
    subparsers.add_parser("bootstrap", help="regenerate metadata-only chapter skeletons")
    packet = subparsers.add_parser("packet", help="emit the next bounded review packet")
    packet.add_argument("--output", type=Path, default=Path("/tmp/srd-catalog-batch.json"))
    packet.add_argument("--max-records", type=int, default=DEFAULT_RECORD_LIMIT)
    packet.add_argument("--max-characters", type=int, default=DEFAULT_CHARACTER_LIMIT)
    merge = subparsers.add_parser("merge", help="merge a reviewed facts packet")
    merge.add_argument("reviewed", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            result = validate_committed(args.source_root)
        elif args.command == "bootstrap":
            result = bootstrap(args.source_root)
        elif args.command == "packet":
            if args.max_records < 1 or args.max_characters < 1:
                raise BatchError("packet limits must be positive")
            result = emit_packet(
                args.source_root,
                args.output,
                record_limit=min(args.max_records, DEFAULT_RECORD_LIMIT),
                character_limit=min(args.max_characters, DEFAULT_CHARACTER_LIMIT),
            )
            result["output"] = str(args.output)  # type: ignore[assignment]
        else:
            result = merge_reviewed(args.source_root, args.reviewed)
    except BatchError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
