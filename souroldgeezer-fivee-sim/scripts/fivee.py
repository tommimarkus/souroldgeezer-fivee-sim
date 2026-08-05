#!/usr/bin/env python3
"""Launcher for the bundled `fivee` command: the plugin's whole entry point.

Nothing spawns this. A skill runs it the way a person would — with the operation
and its arguments — and `fivee` finds the engine's HTTP server or starts one
before it answers. Every argument reaches the command unchanged.

**There is no virtual environment, and that is the design rather than a
shortcut.** The engine declares no runtime dependencies, so a venv here would
install exactly one thing: this package, whose source is already on disk beside
this file. `python -m` is what the language gives you for a zero-dependency pure
Python package, so the launcher puts the source on `sys.path` and calls the
client. uv is still how the *development* environment is built; it is not
involved in running anything.

What survives from the launcher this replaced is not dependency management but
**durability**. `web/http_server.py` reads its static assets through
`resources.files(...)` at request time, so a live server keeps reading from
wherever the package lives, and a host may retire the plugin root while another
session is mid-fight. A host-managed launch therefore runs from a
content-addressed copy under plugin data: the directory name is the hash of the
source, so its existence *is* the freshness check, publishing it is one atomic
rename, and a newer build lands beside a live one instead of overwriting it.
Old copies are kept on purpose — this process cannot know which other one is
still reading from one.

stdout belongs to `fivee`, which puts results there as JSON and nothing else.
Every diagnostic here goes to stderr, so `$(fivee.py ...)` is always either a
parseable document or empty.

This file must stay parseable by interpreters older than the engine supports,
because printing a usable refusal is its job on such a host. Keep annotations
postponed and keep the syntax conservative.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

#: The floor `engine/pyproject.toml` declares as `requires-python`.
REQUIRED_PYTHON = (3, 11)

#: Codex exports no plugin-data variable, so its durable storage is derived from
#: `CODEX_HOME` — the one variable that says a Codex install is what is running
#: this. Never from `$HOME/.codex`: a home directory is not evidence of a Codex
#: install, and guessing one would put a developer's checkout on the host path.
CODEX_PLUGIN_SLUG = "souroldgeezer-fivee-sim-souroldgeezer-tabletop"

#: Compiled artefacts appear the moment anything imports the tree, so they are
#: excluded from both the identity and the copy — otherwise importing a durable
#: copy would change the hash of the thing it was named after.
_EXCLUDED_NAMES = ("__pycache__",)
_EXCLUDED_SUFFIXES = (".pyc", ".pyo")


def note(message: str) -> None:
    """One diagnostic line, on stderr, where it cannot corrupt a captured result."""
    sys.stderr.write("fivee: " + message + "\n")


def interpreter_refusal(version_info: tuple[int, ...]) -> str | None:
    """The refusal for an interpreter too old to run the engine, or None.

    uv used to guarantee an interpreter satisfying `requires-python`. Nothing
    does now, so this says so in a line a caller can act on rather than letting
    the import fail with a traceback further in.
    """
    if tuple(version_info[:2]) >= REQUIRED_PYTHON:
        return None
    running = ".".join(str(part) for part in version_info[:3])
    required = ".".join(str(part) for part in REQUIRED_PYTHON)
    return (
        "the engine needs Python " + required + " or newer, and this is " + running
        + ". Install a newer Python, or run the engine through uv with "
        "`uv run --project <plugin>/engine fivee`."
    )


def resolve_plugin_data(env: dict[str, str]) -> Path | None:
    """Durable host storage for this plugin, or None for a plain checkout.

    A variable exported empty is *unset*, not the filesystem root.
    """
    for name in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
        value = env.get(name, "").strip()
        if value:
            return Path(value).expanduser()
    codex_home = env.get("CODEX_HOME", "").strip()
    if codex_home:
        return Path(codex_home).expanduser() / "plugins" / "data" / CODEX_PLUGIN_SLUG
    return None


def _source_files(source_dir: Path) -> list[Path]:
    """Every file the identity covers, in one stable order."""
    found = []
    for path in source_dir.rglob("*"):
        if path.is_dir():
            continue
        if any(part in _EXCLUDED_NAMES for part in path.parts):
            continue
        if path.suffix in _EXCLUDED_SUFFIXES:
            continue
        found.append(path)
    found.sort()
    return found


def source_identity(engine_dir: Path) -> str:
    """A content hash over the manifest and every shipped source file.

    Paths go into the digest alongside contents, so moving a file changes the
    identity even when no byte of any file does.
    """
    digest = hashlib.sha256()
    digest.update((engine_dir / "pyproject.toml").read_bytes())
    source_dir = engine_dir / "src"
    for path in _source_files(source_dir):
        digest.update(str(path.relative_to(source_dir)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _ignore_compiled(directory: str, names: list[str]) -> set[str]:
    ignored = set()
    for name in names:
        if name in _EXCLUDED_NAMES or name.endswith(_EXCLUDED_SUFFIXES):
            ignored.add(name)
    return ignored


def ensure_durable_source(engine_dir: Path, plugin_data: Path) -> Path:
    """A content-addressed copy of the source under plugin data, made once.

    Staging then renaming is what makes this safe without a lock: two launchers
    racing the same identity each copy into their own staging directory, and the
    loser of the rename discards its copy and uses the winner's — which is
    byte-identical, because the directory name is the hash of the content.
    """
    identity = source_identity(engine_dir)
    published = plugin_data / "src" / identity
    if published.is_dir():
        return published

    published.parent.mkdir(parents=True, exist_ok=True)
    staging = published.parent / (".staging-" + identity[:12] + "-" + str(os.getpid()))
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(engine_dir / "src", staging, ignore=_ignore_compiled)
    try:
        os.replace(staging, published)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        if not published.is_dir():
            raise
    return published


def resolve_source_root(engine_dir: Path, plugin_data: Path | None) -> Path:
    """Where to import the engine from: the checkout, or a durable copy of it."""
    if plugin_data is None:
        return engine_dir / "src"
    return ensure_durable_source(engine_dir, plugin_data)


def main(argv: list[str], env: dict[str, str]) -> int:
    refusal = interpreter_refusal(sys.version_info[:3])
    if refusal is not None:
        note(refusal)
        return 1

    engine_dir = Path(__file__).resolve().parent.parent / "engine"
    if not (engine_dir / "pyproject.toml").is_file():
        note("engine not found at " + str(engine_dir) + "; nothing to run.")
        return 1

    plugin_data = resolve_plugin_data(env)
    if plugin_data is not None:
        try:
            plugin_data.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            note("could not use the durable runtime directory at " + str(plugin_data)
                 + " (" + str(error) + "); nothing run.")
            return 1

    try:
        source_root = resolve_source_root(engine_dir, plugin_data)
    except OSError as error:
        note("could not prepare the engine source (" + str(error) + "); nothing run.")
        return 1

    sys.path.insert(0, str(source_root))

    # `sys.path` does not cross a process boundary, and the client spawns its
    # server as `sys.executable -m fivee_sim.web` — a fresh interpreter that
    # inherits this environment and nothing else. Without this, every in-process
    # import succeeds and every operation that needs a server dies in the child
    # with ModuleNotFoundError. Exporting the root is what makes "run from
    # source" hold for the whole process tree rather than just this process.
    inherited = env.get("PYTHONPATH", "")
    entries = inherited.split(os.pathsep) if inherited else []
    if not entries or entries[0] != str(source_root):
        os.environ["PYTHONPATH"] = os.pathsep.join([str(source_root)] + entries)

    # Maps and encounter journals may resolve a default from the working
    # directory when an operation runs, and a process left inside a plugin cache
    # the host later retires would turn its next journal append into raw ENOENT.
    if plugin_data is not None:
        os.chdir(plugin_data)

    from fivee_sim.client.cli import main as client_main

    return client_main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], dict(os.environ)))
