"""Private CLI plumbing shared by the committed generated Markdown reports."""

from __future__ import annotations

import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path


def write_generated_document(
    argv: list[str] | None = None,
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


def generated_document_main(
    *,
    source_file: str,
    default_filename: str,
    render: Callable[[], str],
) -> Callable[..., int]:
    """Bind one renderer's constants into its command-line entry point."""
    return partial(
        write_generated_document,
        source_file=source_file,
        default_filename=default_filename,
        render=render,
    )
