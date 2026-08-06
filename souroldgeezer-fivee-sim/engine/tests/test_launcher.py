"""The launcher: what makes ``scripts/fivee.py`` runnable before anything is built.

The launcher is not an installed module — it is the file a host runs to reach an
engine that has never been installed — so these tests load it **by path**, the way
the host does, rather than importing it. An import would prove the wrong thing.

Two claims are worth stating because they are why this file replaced 647 lines of
bash. The engine declares no runtime dependencies, so there is nothing to install
into a virtual environment and the launcher builds none: it puts the source on
``sys.path`` and calls the client. What remains is not dependency management but
*durability* — a host may retire the plugin root while a server is still reading
static assets out of it, so a host-managed launch runs from a content-addressed
copy under plugin data instead.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER_PATH = PLUGIN_ROOT / "scripts" / "fivee.py"


def _load_launcher() -> ModuleType:
    """The launcher as a module, loaded from the path a host would run."""
    spec = importlib.util.spec_from_file_location("_fivee_launcher", LAUNCHER_PATH)
    assert spec is not None and spec.loader is not None, LAUNCHER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launcher = _load_launcher()


def _plant_engine(root: Path, marker: str = "original") -> Path:
    """A throwaway plugin root: the two files identity is computed over, plus source."""
    engine = root / "engine"
    (engine / "src" / "fivee_sim").mkdir(parents=True)
    (engine / "pyproject.toml").write_text('[project]\nname = "fivee-sim"\n', encoding="utf-8")
    (engine / "src" / "fivee_sim" / "__init__.py").write_text(
        f'__version__ = "{marker}"\n', encoding="utf-8"
    )
    shutil.copy2(
        PLUGIN_ROOT / "engine" / "src" / "fivee_sim" / "configuration.py",
        engine / "src" / "fivee_sim" / "configuration.py",
    )
    return engine


# -- the interpreter guard --------------------------------------------------
# uv used to guarantee an interpreter that satisfied requires-python. Nothing
# does now, so the launcher has to say so itself, in a line an agent can act on.


def test_an_old_interpreter_is_refused_by_name() -> None:
    message = launcher.interpreter_refusal((3, 10, 7))
    assert message is not None
    assert "3.11" in message, message
    assert "3.10.7" in message, message


def test_the_supported_interpreter_is_not_refused() -> None:
    assert launcher.interpreter_refusal((3, 11, 0)) is None
    assert launcher.interpreter_refusal((3, 14, 6)) is None


# -- where durable storage lives --------------------------------------------


def test_plugin_data_wins_over_the_claude_variable() -> None:
    resolved = launcher.resolve_plugin_data(
        {"PLUGIN_DATA": "/data/generic", "CLAUDE_PLUGIN_DATA": "/data/claude"}
    )
    assert resolved == Path("/data/generic")


def test_the_claude_variable_is_used_when_the_generic_one_is_absent() -> None:
    resolved = launcher.resolve_plugin_data({"CLAUDE_PLUGIN_DATA": "/data/claude"})
    assert resolved == Path("/data/claude")


def test_codex_home_supplies_storage_when_no_host_variable_does() -> None:
    resolved = launcher.resolve_plugin_data({"CODEX_HOME": "/home/someone/.codex"})
    assert resolved is not None
    assert resolved.is_relative_to(Path("/home/someone/.codex")), resolved


def test_a_host_variable_wins_over_the_codex_fallback() -> None:
    """`CODEX_HOME` is the last resort, not a peer of the host-supplied variables."""
    resolved = launcher.resolve_plugin_data(
        {"CLAUDE_PLUGIN_DATA": "/data/claude", "CODEX_HOME": "/home/someone/.codex"}
    )
    assert resolved == Path("/data/claude")


def test_a_plain_checkout_has_no_durable_storage() -> None:
    assert launcher.resolve_plugin_data({}) is None


def test_a_home_directory_is_not_evidence_of_a_codex_install() -> None:
    """Guessing ``$HOME/.codex`` would put a developer's checkout on the host path."""
    assert launcher.resolve_plugin_data({"HOME": "/home/someone"}) is None


def test_an_empty_variable_is_unset_not_root() -> None:
    """An exported-but-blank variable must not resolve storage to the filesystem root."""
    assert launcher.resolve_plugin_data({"PLUGIN_DATA": "", "CLAUDE_PLUGIN_DATA": ""}) is None


# -- source identity --------------------------------------------------------


def test_identity_is_stable_for_unchanged_source(tmp_path: Path) -> None:
    engine = _plant_engine(tmp_path)
    assert launcher.source_identity(engine) == launcher.source_identity(engine)


def test_identity_changes_when_a_source_file_changes(tmp_path: Path) -> None:
    engine = _plant_engine(tmp_path)
    before = launcher.source_identity(engine)
    (engine / "src" / "fivee_sim" / "__init__.py").write_text(
        '__version__ = "changed"\n', encoding="utf-8"
    )
    assert launcher.source_identity(engine) != before


def test_identity_changes_when_the_manifest_changes(tmp_path: Path) -> None:
    engine = _plant_engine(tmp_path)
    before = launcher.source_identity(engine)
    (engine / "pyproject.toml").write_text(
        '[project]\nname = "fivee-sim"\nversion = "2"\n', encoding="utf-8"
    )
    assert launcher.source_identity(engine) != before


def test_identity_ignores_compiled_artefacts(tmp_path: Path) -> None:
    """``__pycache__`` appears the moment anything imports the tree it is keyed on."""
    engine = _plant_engine(tmp_path)
    before = launcher.source_identity(engine)
    cache = engine / "src" / "fivee_sim" / "__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-311.pyc").write_bytes(b"\x00compiled")
    assert launcher.source_identity(engine) == before


def test_identity_covers_a_file_being_added(tmp_path: Path) -> None:
    engine = _plant_engine(tmp_path)
    before = launcher.source_identity(engine)
    (engine / "src" / "fivee_sim" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    assert launcher.source_identity(engine) != before


# -- the durable copy -------------------------------------------------------


def test_the_durable_copy_is_content_addressed_and_importable(tmp_path: Path) -> None:
    engine = _plant_engine(tmp_path / "plugin")
    data = tmp_path / "data"
    root = launcher.ensure_durable_source(engine, data)

    assert launcher.source_identity(engine) in str(root), root
    assert (root / "fivee_sim" / "__init__.py").is_file()


def test_a_second_call_reuses_the_copy_rather_than_rewriting_it(tmp_path: Path) -> None:
    engine = _plant_engine(tmp_path / "plugin")
    data = tmp_path / "data"

    first = launcher.ensure_durable_source(engine, data)
    stamp = (first / "fivee_sim" / "__init__.py").stat().st_mtime_ns
    witness = first / "fivee_sim" / "not-from-the-source"
    witness.write_text("survives\n", encoding="utf-8")

    second = launcher.ensure_durable_source(engine, data)

    assert second == first
    assert (second / "fivee_sim" / "__init__.py").stat().st_mtime_ns == stamp
    assert witness.is_file(), "an existing copy was rebuilt instead of reused"


def test_a_new_build_lands_beside_the_live_one(tmp_path: Path) -> None:
    """The failure this exists for: upgrading must not mutate what a server is reading."""
    engine = _plant_engine(tmp_path / "plugin")
    data = tmp_path / "data"
    original = launcher.ensure_durable_source(engine, data)

    (engine / "src" / "fivee_sim" / "__init__.py").write_text(
        '__version__ = "upgraded"\n', encoding="utf-8"
    )
    upgraded = launcher.ensure_durable_source(engine, data)

    assert upgraded != original
    assert original.is_dir(), "the older runtime was removed under a live process"
    assert '"original"' in (original / "fivee_sim" / "__init__.py").read_text(encoding="utf-8")


def test_the_copy_leaves_no_temporary_directory_behind(tmp_path: Path) -> None:
    engine = _plant_engine(tmp_path / "plugin")
    data = tmp_path / "data"
    root = launcher.ensure_durable_source(engine, data)

    siblings = sorted(child.name for child in root.parent.iterdir())
    assert siblings == [launcher.source_identity(engine)], siblings


def test_concurrent_callers_agree_on_one_copy(tmp_path: Path) -> None:
    """No lock guards this, so the race is the contract and has to be exercised.

    Every caller must return the same published directory, exactly one copy must
    exist, and no staging directory may survive the losers of the rename.
    """
    engine = _plant_engine(tmp_path / "plugin")
    data = tmp_path / "data"

    with ThreadPoolExecutor(max_workers=8) as pool:
        roots = list(pool.map(lambda _: launcher.ensure_durable_source(engine, data), range(8)))

    assert len(set(roots)) == 1, roots
    siblings = sorted(child.name for child in (data / "src").iterdir())
    assert siblings == [launcher.source_identity(engine)], siblings


# -- what children inherit ---------------------------------------------------


def test_the_source_root_leads_the_python_path(tmp_path: Path) -> None:
    assert launcher.python_path_for(tmp_path / "src", "") == str(tmp_path / "src")


def test_an_inherited_python_path_is_kept_behind_the_source_root(tmp_path: Path) -> None:
    """A caller may have put something there for reasons of their own."""
    result = launcher.python_path_for(tmp_path / "src", f"/borrowed{os.pathsep}/other")

    assert result == os.pathsep.join([str(tmp_path / "src"), "/borrowed", "/other"])


def test_re_entering_does_not_grow_the_python_path(tmp_path: Path) -> None:
    """`fivee` shells out to itself in places; the value must not compound."""
    once = launcher.python_path_for(tmp_path / "src", "/borrowed")
    twice = launcher.python_path_for(tmp_path / "src", once)

    assert twice == once


def test_no_one_else_can_plant_a_copy(tmp_path: Path) -> None:
    """The directory name is trusted as the content address and never re-verified.

    Re-hashing the tree on every start is the cost this design exists to avoid,
    so the integrity of a published copy rests on nobody else being able to
    create one. That holds while the directory holding them is owner-only.
    """
    engine = _plant_engine(tmp_path / "plugin")
    data = tmp_path / "data"
    root = launcher.ensure_durable_source(engine, data)

    mode = stat.S_IMODE(root.parent.stat().st_mode)
    assert not mode & (stat.S_IWGRP | stat.S_IWOTH), oct(mode)


def test_a_checkout_runs_from_its_own_source(tmp_path: Path) -> None:
    engine = _plant_engine(tmp_path)
    assert launcher.resolve_source_root(engine, None) == engine / "src"


def test_durable_storage_redirects_the_source_root(tmp_path: Path) -> None:
    engine = _plant_engine(tmp_path / "plugin")
    data = tmp_path / "data"
    root = launcher.resolve_source_root(engine, data)

    assert root != engine / "src"
    assert root.is_relative_to(data), root


# -- the launcher as a host runs it -----------------------------------------


#: The interpreter these tests drive the launcher with, and it is deliberately
#: **not** ``sys.executable``. Under pytest that is the development venv, which
#: has the engine on its path through an editable ``.pth`` — so a subprocess test
#: using it would pass whether or not the launcher put anything on ``sys.path``.
#: The base interpreter is the honest stand-in for the ``python3`` a host
#: resolves from the shebang, and it has the engine installed nowhere.
HOST_PYTHON = Path(sys.base_prefix) / "bin" / "python3"

requires_host_python = pytest.mark.skipif(
    not HOST_PYTHON.exists(), reason=f"no base interpreter at {HOST_PYTHON}"
)


@requires_host_python
def test_the_host_interpreter_does_not_already_have_the_engine() -> None:
    """The guard for every subprocess test below.

    If this interpreter could import ``fivee_sim`` on its own, those tests would
    be green against a launcher that did nothing at all. This is the assertion
    that keeps them honest, and it is why they do not use ``sys.executable``.
    """
    probe = subprocess.run(
        [str(HOST_PYTHON), "-c", "import fivee_sim"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.returncode != 0, (
        "the host interpreter already imports fivee_sim, so the subprocess tests "
        "below would pass without the launcher doing anything"
    )
    assert "No module named" in probe.stderr, probe.stderr


def _run(*arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HOST_PYTHON), str(LAUNCHER_PATH), *arguments],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(cwd) if cwd is not None else None,
    )


@requires_host_python
def test_the_launcher_reaches_the_client_and_returns_its_exit_code() -> None:
    """No arguments is the client's usage branch: exit 2, one note, nothing on stdout."""
    result = _run()
    assert result.returncode == 2, result.stderr
    assert "fivee help" in result.stderr, result.stderr


@requires_host_python
def test_diagnostics_never_reach_stdout() -> None:
    """Callers capture stdout and parse it; a stray line there is a broken contract."""
    result = _run()
    assert result.stdout == "", result.stdout


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    """An environment whose engine state lives entirely under ``tmp_path``.

    Any call that reaches an operation starts a server, and a server started
    against the ambient environment writes ``.fivee-sim/`` into whatever
    directory pytest happens to be in and then outlives the test. Every
    subprocess case that can reach an operation takes this and stops what it
    started.
    """
    environment = dict(os.environ)
    environment["FIVEE_SIM_MAPS"] = str(tmp_path / "maps")
    environment["FIVEE_SIM_REPLAYS"] = str(tmp_path / "replays")
    environment["FIVEE_SIM_ENCOUNTERS"] = str(tmp_path / "encounters")
    environment.pop("PYTHONPATH", None)
    return environment


def _call(
    environment: dict[str, str], *arguments: str, launcher: Path = LAUNCHER_PATH
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HOST_PYTHON), str(launcher), *arguments],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )


@requires_host_python
def test_arguments_survive_the_launcher(tmp_path: Path) -> None:
    """A launcher that dropped argv would pass every other case and then answer wrong."""
    environment = _isolated_env(tmp_path)
    try:
        result = _call(environment, "definitely-not-an-operation")
        assert result.stdout == "", result.stdout
        assert "definitely-not-an-operation" in result.stderr, result.stderr
    finally:
        _call(environment, "stop")


@requires_host_python
def test_a_plugin_root_with_no_engine_refuses_on_stderr(tmp_path: Path) -> None:
    stranded = tmp_path / "scripts"
    stranded.mkdir()
    copy = stranded / "fivee.py"
    copy.write_bytes(LAUNCHER_PATH.read_bytes())

    result = subprocess.run(
        [str(HOST_PYTHON), str(copy)], capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 1, result.stderr
    assert result.stdout == "", result.stdout
    assert "engine" in result.stderr, result.stderr


def test_the_launcher_runs_with_no_virtual_environment_and_no_uv() -> None:
    """The whole point: first call works with nothing built and no uv anywhere.

    ``PATH`` is emptied rather than trimmed — if the launcher shelled out to any
    tool at all, this is where it would show up.
    """
    environment = dict(os.environ)
    environment["PATH"] = str(HOST_PYTHON.parent)
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("UV_PROJECT_ENVIRONMENT", None)
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [str(HOST_PYTHON), str(LAUNCHER_PATH)],
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )

    assert result.returncode == 2, result.stderr
    assert "fivee help" in result.stderr, result.stderr


@requires_host_python
def test_a_spawned_server_can_import_the_engine(tmp_path: Path) -> None:
    """``sys.path`` does not cross a process boundary, so the source root must be exported.

    The client spawns its server as ``sys.executable -m fivee_sim.web``. Running
    from source works in *this* process the moment `sys.path` is set, and then
    fails in the child with ``ModuleNotFoundError`` — which is every operation
    that needs a server, so nearly every operation. Only an end-to-end call
    catches it; the in-process tests above all pass with this broken.
    """
    environment = _isolated_env(tmp_path)
    try:
        result = _call(
            environment, "dice.roll", "--expression", "2d6+3", "--seed", "20260805", "--compact"
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["seed"] == 20260805, payload
    finally:
        _call(environment, "stop")


_ignore_build_artefacts = shutil.ignore_patterns(".venv", ".cache", "__pycache__", "*.pyc")


#: Workspace furniture, not plugin content. The development container bind-mounts
#: ``/dev/null`` over these and the sandbox denies reading them (CLAUDE.md
#: § Environment hazards); a real install carries none of them.
_WORKSPACE_ONLY = frozenset({".claude", ".claude-local", ".mcp.json", ".worktrees"})


def _ignore_artefacts_and_dev_mounts(directory: str, entries: list[str]) -> set[str]:
    """Build artefacts, workspace furniture, and anything mounted as a device.

    ``copytree`` fails the *whole* copy on a single unreadable entry, and this
    workspace grows and drops such entries while sessions come and go — so
    without this the test passes or fails on which checkout ran it and what else
    was live at the time, neither of which is a property of the code under test.

    Both mechanisms are covered because they are separate: the entry may be a
    character device, or it may be an ordinary file the sandbox refuses to read.
    The name set catches what CLAUDE.md documents; the mode check catches a mount
    the container adds later without telling anyone.
    """
    ignored = set(_ignore_build_artefacts(directory, entries))
    ignored.update(entry for entry in entries if entry in _WORKSPACE_ONLY)
    for entry in entries:
        try:
            mode = os.lstat(os.path.join(directory, entry)).st_mode
        except OSError:
            ignored.add(entry)
            continue
        if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
            ignored.add(entry)
        elif not os.access(os.path.join(directory, entry), os.R_OK):
            ignored.add(entry)
    return ignored


def test_the_plugin_copy_skips_what_it_cannot_read(tmp_path: Path) -> None:
    """The copy must survive workspace furniture, or it reports the container's mood.

    Reproduces both hazards this workspace actually produces: an entry the sandbox
    refuses to read, and the `.claude/` tree the container mounts over. Without the
    ignore callable ``copytree`` aborts the entire copy on either one.
    """
    source = tmp_path / "plugin"
    (source / "scripts").mkdir(parents=True)
    (source / "scripts" / "fivee.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (source / ".claude").mkdir()
    (source / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    unreadable = source / "unreadable.json"
    unreadable.write_text("{}", encoding="utf-8")
    unreadable.chmod(0o000)

    with pytest.raises(shutil.Error):
        shutil.copytree(source, tmp_path / "naive", ignore=_ignore_build_artefacts)

    shutil.copytree(source, tmp_path / "installed", ignore=_ignore_artefacts_and_dev_mounts)
    assert (tmp_path / "installed" / "scripts" / "fivee.py").is_file()
    assert not (tmp_path / "installed" / ".claude").exists()
    assert not (tmp_path / "installed" / "unreadable.json").exists()


@requires_host_python
def test_a_live_server_survives_its_plugin_root_being_retired(tmp_path: Path) -> None:
    """The whole reason the durable copy exists, exercised rather than reasoned about.

    A host can retire the installed plugin root while a session is mid-fight, and
    ``web/http_server.py`` reads static assets through ``resources.files(...)`` at
    *request* time. Running from an editable checkout would serve a 500 here.
    """
    installed = tmp_path / "installed"
    shutil.copytree(PLUGIN_ROOT, installed, ignore=_ignore_artefacts_and_dev_mounts)
    environment = _isolated_env(tmp_path)
    environment["PLUGIN_DATA"] = str(tmp_path / "data")

    started = _call(environment, "serve", "--compact", launcher=installed / "scripts" / "fivee.py")
    assert started.returncode == 0, started.stderr
    url = json.loads(started.stdout)["url"]
    try:
        shutil.rmtree(installed)
        assert not installed.exists()

        with urllib.request.urlopen(url + "editor", timeout=30) as response:
            assert response.status == 200
            body = response.read().decode("utf-8")
        assert "<title>" in body, body[:200]
    finally:
        _call(environment, "stop")


def test_the_launcher_is_executable_and_names_an_interpreter() -> None:
    """Hosts and skills invoke it as a path; both need the shebang and the bit."""
    assert os.access(LAUNCHER_PATH, os.X_OK), f"{LAUNCHER_PATH} must be executable"
    first_line = LAUNCHER_PATH.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#!"), first_line
    assert "python3" in first_line, first_line


def test_the_launcher_parses_on_the_oldest_interpreter_it_claims() -> None:
    """The version guard is useless if the file cannot be parsed to reach it.

    Stated honestly, because it would be easy to read more into this than it
    proves: ``compile`` here uses the *running* interpreter's grammar, so it is a
    smoke check, not a 3.8 check. The two textual assertions are the actual
    guards, and they cover the ways this file has a realistic chance of losing
    old-interpreter parseability — an un-postponed annotation or a match
    statement. A genuine check needs an old interpreter this suite cannot assume.
    """
    source = LAUNCHER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, str(LAUNCHER_PATH))

    assert "from __future__ import annotations" in source, (
        "annotations must be postponed so 3.8 can parse modern type syntax"
    )
    offending = [type(node).__name__ for node in ast.walk(tree) if isinstance(node, ast.Match)]
    assert offending == [], f"match statements do not parse on 3.8: {offending}"


@requires_host_python
@pytest.mark.parametrize("variable", ["PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"])
def test_a_host_managed_launch_runs_from_durable_storage(
    tmp_path: Path, variable: str
) -> None:
    """The durable copy is used in anger, not merely computed."""
    data = tmp_path / "data"
    environment = dict(os.environ)
    environment[variable] = str(data)
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [str(HOST_PYTHON), str(LAUNCHER_PATH)],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )

    assert result.returncode == 2, result.stderr
    copies = sorted((data / "src").iterdir())
    assert len(copies) == 1, copies
    assert (copies[0] / "fivee_sim" / "web" / "static" / "editor.html").is_file(), (
        "the durable copy must carry the assets a live server reads at request time"
    )


# -- naming the source a reload watches --------------------------------------
# Watching the source is opt-in, and the launcher's whole part in it is to say
# *which* source: it hashes the tree it is about to run and hands that id to the
# process tree. Everything below is about the hand-off, not about the watching.


class _StubClient(ModuleType):
    """Stands in for ``fivee_sim.client.cli`` at the door ``main`` calls through.

    The launcher's job ends when it hands argv to the client, and the real one
    would start a server for anything that is not the usage branch. Which client
    answers is also not a thing these cases should depend on: the planted tree
    below carries no client, and whether the *installed* one is importable from
    this interpreter depends on what else pytest happened to collect.
    """

    def __init__(self) -> None:
        super().__init__("fivee_sim.client.cli")
        self.argv: list[str] | None = None
        self.configuration: Any | None = None
        self.configuration_resolved = False

    def main(
        self,
        argv: list[str],
        *,
        configuration: Any | None = None,
        configuration_resolved: bool = False,
    ) -> int:
        self.argv = argv
        self.configuration = configuration
        self.configuration_resolved = configuration_resolved
        return 0


def _plant_plugin(root: Path, marker: str = "original") -> tuple[ModuleType, Path]:
    """A planted engine with the launcher beside it, loaded from that copy.

    ``main`` finds its engine relative to its own ``__file__``, so a case that
    drives it against a two-file tree has to move the launcher next to one. The
    alternative — hashing and copying the real engine per case — would make
    these report the size of the repository rather than the behaviour.
    """
    engine = _plant_engine(root, marker)
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    copy = scripts / "fivee.py"
    copy.write_bytes(LAUNCHER_PATH.read_bytes())

    spec = importlib.util.spec_from_file_location("_fivee_launcher_planted", copy)
    assert spec is not None and spec.loader is not None, copy
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, engine


def _drive_main(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    argv: list[str] | None = None,
    client: _StubClient | None = None,
) -> dict[str, str]:
    """Run ``main`` in this process and hand back the environment it exported into.

    The mapping ``main`` is *given* and the one it *writes to* are two objects
    here, exactly as they are at the entry point, where the argument is
    ``dict(os.environ)``. A launcher that wrote its exports into that copy would
    satisfy every assertion below and still leave a spawned child knowing
    nothing, so the two must not be conflated.

    ``sys.path`` and the working directory are restored for the ordinary reason:
    ``main`` mutates process state that would otherwise outlive the test.
    """
    exported = dict(environment)
    monkeypatch.setattr(os, "environ", exported)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setitem(sys.modules, "fivee_sim.client.cli", client or _StubClient())

    working_directory = Path.cwd()
    try:
        assert module.main(list(argv or []), environment) == 0
    finally:
        os.chdir(working_directory)
    return exported


def test_the_reload_flag_exports_the_identity_of_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, engine = _plant_plugin(tmp_path / "plugin")

    exported = _drive_main(module, monkeypatch, {"FIVEE_SIM_RELOAD": "1"})

    assert exported["FIVEE_SIM_SOURCE_ID"] == module.source_identity(engine)


def test_a_discovered_config_owns_settings_before_the_durable_chdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, engine = _plant_plugin(tmp_path / "plugin")
    workspace = tmp_path / "workspace"
    config_dir = workspace / ".fivee-sim"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """\
format_version = 1

[content]
builtin = "exclude"

[storage]
maps = "battle-maps"

[development]
reload = true
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(workspace)

    exported = _drive_main(
        module,
        monkeypatch,
        {
            "PLUGIN_DATA": str(tmp_path / "durable"),
            "FIVEE_SIM_MAPS": str(tmp_path / "wrong"),
            "FIVEE_SIM_BUILTIN": "include",
        },
    )

    assert exported["FIVEE_SIM_MAPS"] == str(config_dir / "battle-maps")
    assert exported["FIVEE_SIM_BUILTIN"] == "exclude"
    assert exported["FIVEE_SIM_SOURCE_ID"] == module.source_identity(engine)


def test_an_explicit_config_path_wins_outside_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _ = _plant_plugin(tmp_path / "plugin")
    config_dir = tmp_path / "chosen"
    config_dir.mkdir()
    config = config_dir / "settings.toml"
    config.write_text(
        "format_version = 1\n[storage]\nreplays = 'frozen'\n",
        encoding="utf-8",
    )

    client = _StubClient()
    exported = _drive_main(
        module,
        monkeypatch,
        {"FIVEE_SIM_REPLAYS": str(tmp_path / "wrong")},
        ["server.ping", "--config", str(config)],
        client,
    )

    assert exported["FIVEE_SIM_REPLAYS"] == str(config_dir / "frozen")
    assert client.argv == ["server.ping"]
    assert client.configuration is not None
    assert client.configuration.path == config.resolve()
    assert client.configuration_resolved is True


def test_a_selected_config_can_turn_off_a_legacy_reload_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _ = _plant_plugin(tmp_path / "plugin")
    workspace = tmp_path / "workspace"
    config_dir = workspace / ".fivee-sim"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        "format_version = 1\n[development]\nreload = false\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(workspace)

    exported = _drive_main(module, monkeypatch, {"FIVEE_SIM_RELOAD": "1"})

    assert "FIVEE_SIM_SOURCE_ID" not in exported, exported


def test_nothing_is_exported_without_the_reload_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent, not blank: a reader has one question to ask, and it is ``in``."""
    module, _ = _plant_plugin(tmp_path / "plugin")

    exported = _drive_main(module, monkeypatch, {})

    assert "FIVEE_SIM_SOURCE_ID" not in exported, exported


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_reload_flag_is_unset_rather_than_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """The reading `resolve_plugin_data` gives the host variables, applied here too."""
    module, _ = _plant_plugin(tmp_path / "plugin")

    exported = _drive_main(module, monkeypatch, {"FIVEE_SIM_RELOAD": blank})

    assert "FIVEE_SIM_SOURCE_ID" not in exported, exported


def test_the_exported_identity_changes_when_a_source_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The id is worth exporting only if it distinguishes one tree from the next."""
    first, engine = _plant_plugin(tmp_path / "before")
    before = _drive_main(first, monkeypatch, {"FIVEE_SIM_RELOAD": "1"})

    second, changed = _plant_plugin(tmp_path / "after", marker="changed")
    after = _drive_main(second, monkeypatch, {"FIVEE_SIM_RELOAD": "1"})

    assert before["FIVEE_SIM_SOURCE_ID"] != after["FIVEE_SIM_SOURCE_ID"]
    assert after["FIVEE_SIM_SOURCE_ID"] == second.source_identity(changed)


def test_a_durable_reload_hashes_the_source_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two callers want the same digest of the same tree, and it is a whole-tree read.

    ``main`` needs the identity to export it and ``ensure_durable_source`` needs
    it to name the copy. Hashing per caller is the cost content-addressing pays
    once by design, so the count here is exact rather than a bound.
    """
    module, engine = _plant_plugin(tmp_path / "plugin")
    hash_source: Callable[[Path], str] = module.source_identity
    hashed: list[Path] = []

    def counted(engine_dir: Path) -> str:
        hashed.append(engine_dir)
        return hash_source(engine_dir)

    monkeypatch.setattr(module, "source_identity", counted)

    exported = _drive_main(
        module,
        monkeypatch,
        {"FIVEE_SIM_RELOAD": "1", "PLUGIN_DATA": str(tmp_path / "data")},
    )

    assert hashed == [engine.resolve()], hashed
    assert exported["FIVEE_SIM_SOURCE_ID"] == hash_source(engine)
    published = sorted((tmp_path / "data" / "src").iterdir())
    assert published == [tmp_path / "data" / "src" / hash_source(engine)], published


def test_a_durable_reload_names_the_tree_it_actually_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exported id is the name of the directory the engine is imported from.

    Content-addressing makes those one string, and deriving the export from the
    resolved path rather than from a second hash is what keeps them one. The
    alternative — handing a digest to ``ensure_durable_source`` to save the
    second read — would let a caller name a directory that function never
    re-verifies, which is the single way its content-addressing could be made to
    lie about what it holds.
    """
    module, engine = _plant_plugin(tmp_path / "plugin")

    exported = _drive_main(
        module,
        monkeypatch,
        {"FIVEE_SIM_RELOAD": "1", "PLUGIN_DATA": str(tmp_path / "data")},
    )

    imported = Path(exported["PYTHONPATH"].split(os.pathsep)[0])
    assert imported.parent == tmp_path / "data" / "src", imported
    assert exported["FIVEE_SIM_SOURCE_ID"] == imported.name
    assert exported["FIVEE_SIM_SOURCE_ID"] == module.source_identity(engine)


#: A client that does one thing: spawn a child the way the real one spawns its
#: server, and report what that child inherited. Planted rather than imported,
#: because the point is the process boundary and not this interpreter.
_REPORTING_CLIENT = '''\
import subprocess
import sys


def main(argv, *, configuration=None, configuration_resolved=False):
    child = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ.get('FIVEE_SIM_SOURCE_ID', ''))"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    sys.stdout.write(child.stdout)
    return child.returncode
'''


@requires_host_python
def test_a_spawned_server_inherits_the_source_identity(tmp_path: Path) -> None:
    """The boundary ``test_a_spawned_server_can_import_the_engine`` names, one variable along.

    The client spawns its server as a fresh interpreter that inherits this
    process's environment and nothing else. An id held in ``sys.path``, in a
    local, or in the copy of the environment ``main`` was handed passes every
    in-process case above and never reaches the process that has to act on it.
    """
    root = tmp_path / "plugin"
    engine = _plant_engine(root)
    client = engine / "src" / "fivee_sim" / "client"
    client.mkdir()
    (client / "__init__.py").write_text("", encoding="utf-8")
    (client / "cli.py").write_text(_REPORTING_CLIENT, encoding="utf-8")
    copy = root / "scripts" / "fivee.py"
    copy.parent.mkdir()
    copy.write_bytes(LAUNCHER_PATH.read_bytes())

    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    # A durable copy is beside the point here, and honouring a host variable this
    # session happens to carry would write one into the host's real plugin data.
    for host_variable in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA", "CODEX_HOME"):
        environment.pop(host_variable, None)
    environment["FIVEE_SIM_RELOAD"] = "1"

    result = _call(environment, launcher=copy)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == launcher.source_identity(engine), result.stdout
