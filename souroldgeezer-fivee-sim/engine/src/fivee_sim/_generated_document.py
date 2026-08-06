"""Private CLI plumbing shared by the committed generated Markdown reports."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path


def write_generated_document(
    argv: list[str] | None,
    *,
    source_file: str,
    default_filename: str,
    render: Callable[[], str],
) -> int:
    """Render one document to its explicit or source-relative default path."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    target = (
        Path(arguments[0])
        if arguments
        else Path(source_file).resolve().parents[3] / "docs" / default_filename
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(), encoding="utf-8")
    print(f"wrote {target}", file=sys.stderr)
    return 0
