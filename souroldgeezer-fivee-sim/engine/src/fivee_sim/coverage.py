"""Generate the compact catalog and executable-support coverage report.

Detailed identities belong in the catalog tools.  This document is deliberately
totals-only so adding thousands of source entries does not create a second,
hand-browsed catalog that can drift from the data.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ._generated_document import generated_document_main
from .catalog import simulation_support
from .content import ContentRegistry, builtin_registry


def _has_omissions(record: dict[str, Any]) -> bool:
    return bool(record.get("unmodelled", []) or record.get("unmodelled_facts", []))


def _support_counts(registry: ContentRegistry) -> Counter[str]:
    counts: Counter[str] = Counter()
    for record in registry.catalog.values():
        executable: dict[str, Any] | None = None
        if record.content_ref is not None:
            executable = registry.records_for(record.content_ref.section).get(
                record.content_ref.name
            )
        support = simulation_support(
            executable=executable is not None,
            has_omissions=bool(record.unmodelled_facts)
            or (executable is not None and _has_omissions(executable)),
        )
        counts[support.value] += 1
    return counts


def render_markdown() -> str:
    registry = builtin_registry()
    kinds = Counter(record.kind for record in registry.catalog.values())
    record_progress = Counter(record.fact_status.value for record in registry.catalog.values())
    table_progress = Counter(table.fact_status.value for table in registry.catalog_tables.values())
    support = _support_counts(registry)
    lines: list[str] = []

    def add(line: str = "") -> None:
        lines.append(line)

    add("# Coverage")
    add()
    add(
        "Generated totals for the bundled SRD 5.2.1 structured catalog and the "
        "smaller executable combat subset. Rules content remains CC-BY-4.0; see "
        "[NOTICE](../NOTICE)."
    )
    add()
    add("## Source inventory")
    add()
    add("| Inventory | Count |")
    add("| --- | ---: |")
    add(f"| Source sections | {len(registry.catalog)} |")
    add(f"| Printed tables | {len(registry.catalog_tables)} |")
    add()
    add("## Catalog categories")
    add()
    add("| Kind | Count |")
    add("| --- | ---: |")
    for kind, count in sorted(kinds.items()):
        add(f"| {kind} | {count} |")
    add()
    add("## Structured-fact progress")
    add()
    add("| Status | Sections | Tables |")
    add("| --- | ---: | ---: |")
    for status in ("pending", "complete", "no_structured_facts"):
        add(f"| {status} | {record_progress[status]} | {table_progress[status]} |")
    add()
    add("## Simulation support")
    add()
    add("| State | Catalog records |")
    add("| --- | ---: |")
    for state in ("reference_only", "partial", "executable"):
        add(f"| {state} | {support[state]} |")
    add()
    add("## Loaded executable records")
    add()
    add("| Section | Count |")
    add("| --- | ---: |")
    for section, count in registry.summary()["counts"].items():
        add(f"| {section} | {count} |")
    add()
    add("## Detailed lookup")
    add()
    add(
        "Use `fivee rules.lookup`, `fivee catalog.search`, `fivee catalog.get`, "
        "`fivee catalog.table`, and `fivee content.status` for current lookup and "
        "loaded-content commands. See the "
        "[encounter-sim skill](../skills/encounter-sim/SKILL.md) for the detailed workflow."
    )
    return "\n".join(lines) + "\n"


main = generated_document_main(
    source_file=__file__, default_filename="COVERAGE.md", render=render_markdown
)


if __name__ == "__main__":
    raise SystemExit(main())
