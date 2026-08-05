"""``fivee``, driven against a server it starts for real.

Nothing here is stubbed out between the CLI and the engine. Each test either
lets the client spawn ``python -m fivee_sim.web`` the way a user's first command
would, or shares one server the module started the same way — the client opens a
socket, sends its launch token, and parses the bytes that come back. That is the
only way these tests are worth anything: the claim the client exists to make is
that every feature it has is a feature ``/api/v1`` serves, and a fake server
would let it be true of the fake instead.

The one exception is :class:`FaultServer`, and it is a fault injector rather
than a mock — see its docstring for why a real 500 is not available to ask for.

``tests/test_layering.py::test_the_client_reaches_the_engine_only_over_http`` is
the other half of this file. It proves structurally what these prove
behaviourally: there is no in-process path from the CLI to the engine.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from fivee_sim.client import cli, discovery
from fivee_sim.model.encounter import ActionKind
from fivee_sim.web.http_server import SOURCE_ID_ENV

#: SIGKILL where there is one, SIGTERM where there is not. Referencing
#: ``signal.SIGKILL`` directly would raise ``AttributeError`` at import on
#: Windows, which is a worse failure than the skip below: it would take the
#: whole module out before pytest could report why.
_HARD_KILL = getattr(signal, "SIGKILL", signal.SIGTERM)

# Module-wide, and the reason matters for whoever narrows it. It is *not* the
# teardown — that is portable now (see _HARD_KILL). It is that every test here
# needs a running server, and `discovery.spawn` detaches with
# `start_new_session=True`, which is POSIX-only and raises on Windows. So the
# ~19 tests in TestHelp/TestInvocation/TestFailures — argument parsing, exit
# codes, near-miss suggestions, none of it platform-dependent — lose Windows
# coverage as collateral, and the way to recover them is to give
# `discovery.spawn` a Windows branch (CREATE_NEW_PROCESS_GROUP), not to move
# this marker. Deliberately not attempted here: there is no Windows host in
# this environment to verify it on, and an unverified port is worse than a
# documented skip.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="discovery.spawn detaches with start_new_session, which is POSIX-only",
)

ENGINE_SRC = Path(__file__).resolve().parent.parent / "src"

#: The roots that would otherwise point every launch at the developer's own
#: game state. Cleared so a test's server reads and writes only its tmp_path.
ROOT_VARIABLES = (
    "FIVEE_SIM_MAPS",
    "FIVEE_SIM_REPLAYS",
    "FIVEE_SIM_ENCOUNTERS",
    "CLAUDE_PROJECT_DIR",
)

HERO: dict[str, Any] = {
    "name": "Thora",
    "team": "party",
    "ac": 16,
    "max_hp": 30,
    "position": [0, 0],
    "attacks": [
        {
            "name": "Longsword",
            "attack_bonus": 5,
            "damage": "1d8+3",
            "damage_type": "slashing",
            "kind": "melee",
        }
    ],
}
GOBLIN: dict[str, Any] = {
    "monster": "Goblin Warrior",
    "label": "Goblin",
    "team": "monsters",
    "position": [5, 0],
}

#: Two engine sources, named the way the launcher names them: a sha256 hex
#: digest. Nothing here hashes anything — what these stand in for is *the
#: launcher resolved different source than last time*, which is a fact about two
#: strings and not about their contents.
SOURCE_A = "a" * 64
SOURCE_B = "b" * 64


def _isolate(patch: pytest.MonkeyPatch, root: Path) -> None:
    patch.setenv("FIVEE_SIM_PROJECT_DIR", str(root))
    for name in ROOT_VARIABLES:
        patch.delenv(name, raising=False)


def _teardown() -> None:
    """Stop whatever a test left running, and make sure it is really gone.

    ``stop`` is the graceful path and is what the tests assert about; the
    SIGKILL after it is the safety net, because a server surviving a failed
    test would be discovered by the *next* test's state file and quietly serve
    a workspace it knows nothing about.
    """
    state_path = discovery.state_path_for()
    discovery.stop(state_path)
    # Re-read *after* the graceful stop, never before. A pid captured first is
    # a pid the OS may already have reissued by the time the kill lands, so an
    # unconditional SIGKILL on it can hit an unrelated process — and a
    # successful stop removes the record, which is exactly how this backstop
    # learns it has nothing to do. `test_web_lifecycle` and
    # `scripts/check-api-smoke.py` both read fresh here for the same reason.
    state = discovery.read_state(state_path)
    if state is not None and isinstance(state.get("pid"), int):
        with contextlib.suppress(OSError):
            os.kill(state["pid"], _HARD_KILL)


def _under(patch: pytest.MonkeyPatch, source_id: str | None) -> discovery.Server:
    """A live server with *source_id* named in the environment, or none named.

    One variable does both halves of the job, and that is the mechanism rather
    than a shortcut: the client reads it to learn what it expects, and a server
    the client spawns inherits it and reports it back. ``None`` unsets it, which
    is the only way to test an absent variable in a suite that inherits whatever
    the developer's shell exported.
    """
    if source_id is None:
        patch.delenv(SOURCE_ID_ENV, raising=False)
    else:
        patch.setenv(SOURCE_ID_ENV, source_id)
    return discovery.ensure_server()


def _unreachable(port: int, token: str, timeout: float = 5.0) -> bool:
    """Whether nothing answers on *port* any more — polled, not sampled once.

    A stopped server closes its socket on the way out, a moment after the record
    it removes disappears, so a single ping here would be a race dressed as a
    check. Its *pid* is no use for this: the replaced server was this process's
    child, and a child nobody has reaped is still a pid the OS answers for.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if discovery.ping(port, token) is None:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(scope="module")
def module_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One project root for the whole module, and so one place a server lives."""
    return tmp_path_factory.mktemp("fivee-client")


@pytest.fixture(autouse=True)
def _client_roots(monkeypatch: pytest.MonkeyPatch, module_root: Path) -> None:
    """Re-point the roots this module needs, over conftest's per-test ones.

    ``conftest._isolate_server_state`` sets ``FIVEE_SIM_MAPS`` and its siblings
    to a *different* ``tmp_path`` for every test, which is exactly right for a
    suite whose subject is in-process. It is wrong here: the launch state file
    lives beside the maps root, so a per-test maps root means a per-test state
    file, and a module-scoped server would be invisible to every test meant to
    share it. Each command would silently start another server and leak it —
    which is what happened, and it was invisible because every assertion still
    passed against the server that command had just started.

    Autouse in this module, so it applies after the conftest fixture and wins;
    :func:`workspace` is requested explicitly and so applies after this one.
    """
    _isolate(monkeypatch, module_root)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A project root of this test's own, with nothing running in it yet."""
    _isolate(monkeypatch, tmp_path)
    try:
        yield tmp_path
    finally:
        _teardown()


@pytest.fixture(scope="module")
def shared(module_root: Path) -> Iterator[discovery.Server]:
    """One server for the tests that only read from it.

    Module-scoped because a cold start imports the whole engine, and the
    lifecycle claims — that a command starts one, that a second command finds
    it — are pinned by :class:`TestLifecycle` against servers of their own.
    """
    with pytest.MonkeyPatch.context() as patch:
        _isolate(patch, module_root)
        server = discovery.ensure_server()
        try:
            yield server
        finally:
            _teardown()


def run(*tokens: str) -> int:
    return cli.main(list(tokens))


def out(capsys: pytest.CaptureFixture[str]) -> Any:
    """The command's stdout, parsed. Fails loudly when it is not JSON.

    Asserted rather than assumed: "results are JSON on stdout" is one of the
    properties this client promises, so a command that printed prose there
    should fail the test that reads it, not raise a decode error nobody reads.
    """
    captured = capsys.readouterr()
    try:
        return json.loads(captured.out)
    except json.JSONDecodeError as error:  # pragma: no cover - a failure path
        raise AssertionError(
            f"stdout was not JSON ({error}); it held {captured.out!r} "
            f"and stderr held {captured.err!r}"
        ) from None


class TestLifecycle:
    """Find a server, or start one. The part a caller must never think about."""

    def test_a_command_starts_a_server_and_the_next_one_finds_it(
        self, workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state_path = discovery.state_path_for()
        assert not state_path.exists(), "the workspace was meant to start empty"

        assert run("dice.roll", "--expression", "1d1", "--seed", "3") == cli.EXIT_OK
        first = capsys.readouterr()
        assert json.loads(first.out)["total"] == 1
        assert "started the engine server" in first.err
        started = discovery.read_state(state_path)
        assert started is not None and isinstance(started["port"], int)

        # The second command must not spawn a second server: the state file is
        # the rendezvous, and a client that started one per invocation would
        # leave every fight in a process the next command cannot see.
        assert run("dice.roll", "--expression", "1d1", "--seed", "3") == cli.EXIT_OK
        second = capsys.readouterr()
        assert "started" not in second.err, second.err
        again = discovery.read_state(state_path)
        assert again is not None and again["port"] == started["port"]

    def test_serve_reports_the_running_server_rather_than_starting_a_second(
        self, workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("serve") == cli.EXIT_OK
        first = out(capsys)
        assert first["already_running"] is False
        assert first["url"] == f"http://127.0.0.1:{first['port']}/"
        # Three URLs, and `url` is the landing page rather than the editor.
        # The editor used to be the root, so a caller handed `url` to open it;
        # naming it separately is what stops that habit reaching a browser at
        # the wrong page and being reported as "the editor is broken".
        assert first["editor_url"] == f"{first['url']}editor"
        assert first["viewer_url"] == f"{first['url']}viewer"

        assert run("serve") == cli.EXIT_OK
        second = out(capsys)
        assert second["already_running"] is True
        assert second["port"] == first["port"]

    def test_stop_tears_one_down_and_says_when_there_was_nothing_to_stop(
        self, workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("serve") == cli.EXIT_OK
        capsys.readouterr()

        assert run("stop") == cli.EXIT_OK
        stopped = out(capsys)
        assert stopped["stopped"] is True and stopped["was_running"] is True
        assert not discovery.state_path_for().exists()

        # A second stop is not an error: "there was nothing running" is an
        # answer, and a caller tidying up should not have to check first.
        assert run("stop") == cli.EXIT_OK
        assert out(capsys) == {"stopped": False, "was_running": False}

    def test_stop_never_starts_a_server_in_order_to_stop_it(
        self, workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("stop") == cli.EXIT_OK
        assert out(capsys)["was_running"] is False
        assert not discovery.state_path_for().exists(), (
            "stop spawned a server; 'every command ensures a server' has exactly "
            "one exception and this is it"
        )

    def test_a_server_that_cannot_start_is_unreachable_not_a_refusal(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A spawn that dies is a machine problem, and gets its own exit code.

        The interpreter is replaced with one that exits immediately, so the
        real ``subprocess`` really runs and really fails to bind — the branch
        under test is the one that notices the child died before the state file
        appeared, not a patched-out version of it.
        """
        stub = workspace / "not-really-python"
        stub.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
        stub.chmod(0o755)
        monkeypatch.setattr(sys, "executable", str(stub))

        assert run("dice.roll", "--expression", "1d1") == cli.EXIT_UNREACHABLE
        captured = capsys.readouterr()
        assert captured.out == "", "nothing reached the engine, so stdout must be empty"
        assert "exited with status 9" in captured.err
        assert "fivee-sim-server.log" in captured.err, (
            "the message must name the log, which is the only place a cold "
            "start's traceback can be"
        )

    def test_the_spawned_servers_log_is_readable_only_by_its_owner(
        self, workspace: Path
    ) -> None:
        """0600, for the reason the state file beside it is 0600.

        Nothing written here carries the token today — every message names the
        port and URL only — so this is resilience, not a live leak: the file a
        future traceback would echo a header dict into should not be
        world-readable by default, and a write-then-chmod leaves a window the
        umask governs.
        """
        state_path = discovery.state_path_for()
        discovery.ensure_server(state_path=state_path)
        try:
            log_path = state_path.parent / "fivee-sim-server.log"
            assert log_path.is_file(), "the spawn writes its child's output here"
            mode = stat.S_IMODE(log_path.stat().st_mode)
            assert mode == 0o600, f"the log is {oct(mode)}, not 0600"
        finally:
            discovery.stop(state_path)

    def test_the_command_works_as_its_own_process_not_only_in_this_one(
        self, workspace: Path
    ) -> None:
        """The console script's real shape: a process, a shell, an exit status.

        Everything else here calls ``main`` in-process, which cannot catch a
        module that only imports under pytest's ``pythonpath``, or an entry
        point that writes its result somewhere other than a real stdout.
        """
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ENGINE_SRC) + os.pathsep + environment.get(
            "PYTHONPATH", ""
        )
        environment["FIVEE_SIM_PROJECT_DIR"] = str(workspace)
        for name in ROOT_VARIABLES:
            environment.pop(name, None)
        finished = subprocess.run(
            [sys.executable, "-m", "fivee_sim.client", "dice.roll",
             "--expression", "2d1", "--compact"],
            capture_output=True, text=True, env=environment, timeout=120,
        )
        assert finished.returncode == cli.EXIT_OK, finished.stderr
        assert json.loads(finished.stdout)["total"] == 2
        assert finished.stdout.count("\n") == 1, "--compact is one line of JSON"


class TestSourceReload:
    """A server running other engine source is replaced — and only then.

    Every case here reads a **pid**, because the claim is that one process ended
    and another began. Asserted as a call count instead, a restart would pass
    against a ``stop`` that stopped nothing and a client that went on talking to
    the server it believed it had killed.

    The environment variable is imported from the server rather than spelled
    here: the client keeps its own copy of the name because it may not import
    :mod:`fivee_sim.web`, and that copy is what is under test. A drifted copy
    leaves the client expecting nothing at all, which is silent — no reload ever
    happens and nothing raises — so it is the replacement cases below that
    notice.
    """

    def test_a_server_running_the_expected_source_is_left_alone(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = _under(monkeypatch, SOURCE_A)
        assert first.spawned is True and first.reloaded is False
        assert first.source_id == SOURCE_A, (
            "the spawned server did not inherit the id, so nothing below can "
            "distinguish a match from a server that reports nothing"
        )

        second = _under(monkeypatch, SOURCE_A)

        assert second.pid == first.pid, "same source, so it must be the same process"
        assert second.spawned is False and second.reloaded is False

    def test_the_source_that_counts_is_the_one_the_server_answers_for(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The record is a file; the ping is the process that would be restarted.

        They agree when both are written by one launch, which is why this makes
        them disagree — a record is the only half of the pair that anything else
        can rewrite, and a reader that took its word would stop a server for
        running source it demonstrably answers to.
        """
        state_path = discovery.state_path_for()
        first = _under(monkeypatch, SOURCE_A)
        record = discovery.read_state(state_path)
        assert record is not None and record["source_id"] == SOURCE_A
        record["source_id"] = SOURCE_B
        state_path.write_text(json.dumps(record), encoding="utf-8")

        second = _under(monkeypatch, SOURCE_A)

        assert second.pid == first.pid, "the file was believed over the process"
        assert second.reloaded is False
        assert second.source_id == SOURCE_A

    def test_a_server_running_other_source_is_replaced_and_the_old_one_stops(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The feature itself, and the property that keeps it from thrashing.

        A fresh server that did not report the source the client expects would
        be replaced again by the next command, and by the one after that — so
        the third call reusing the second's process is as much the deliverable
        as the restart is.
        """
        first = _under(monkeypatch, SOURCE_A)

        second = _under(monkeypatch, SOURCE_B)

        assert second.reloaded is True and second.spawned is True
        assert second.pid != first.pid, "the same process cannot be running new source"
        assert _unreachable(first.port, first.token), "the replaced server is still serving"
        assert second.source_id == SOURCE_B

        third = _under(monkeypatch, SOURCE_B)
        assert third.pid == second.pid and third.reloaded is False

    def test_with_no_source_expected_a_server_reporting_another_is_still_reused(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Off by default, held against the server most likely to trip it.

        This one *does* report an id, and a different one. Nothing in the
        environment asked about it, so nothing may act on it — a plain
        ``fivee`` command that restarted the engine somebody was mid-fight in
        would be the worst outcome this feature could have.
        """
        first = _under(monkeypatch, SOURCE_A)

        absent = _under(monkeypatch, None)
        assert absent.pid == first.pid
        assert absent.reloaded is False and absent.spawned is False

        # Exported empty is the shape a shell gives when it passes one unset
        # variable through another, and it means the same as never set.
        blank = _under(monkeypatch, "")
        assert blank.pid == first.pid
        assert blank.reloaded is False and blank.spawned is False

    def test_a_server_naming_no_source_is_replaced_when_one_is_expected(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An untracked server cannot be shown to be current, so it is not kept.

        ``""`` is not a mismatch — it says nobody was tracking when this server
        started — but a server from before the tracking is exactly the shape of
        the one a dev reload exists to replace, and the cost of being wrong runs
        one way only: a needless cold start against editing source that the
        engine answering never loaded.
        """
        first = _under(monkeypatch, None)
        assert first.source_id == "", "a launch told no id must report no id"

        second = _under(monkeypatch, SOURCE_A)

        assert second.reloaded is True and second.pid != first.pid
        assert _unreachable(first.port, first.token), "the replaced server is still serving"

    def test_a_cold_start_under_an_expected_source_is_a_start_and_not_a_reload(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing was replaced, because nothing was there.

        Reported as a reload, the first command in a fresh workspace would tell
        every developer their engine had just been restarted for staleness that
        never existed.
        """
        assert not discovery.state_path_for().exists(), "the workspace was meant to be empty"

        server = _under(monkeypatch, SOURCE_A)

        assert server.spawned is True
        assert server.reloaded is False

    def test_a_command_that_reloads_says_so_on_stderr_and_still_answers_on_stdout(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The restart is news, and news is prose, and prose is never stdout.

        A caller in ``$(fivee ...)`` is parsing what comes back; a line about
        the engine's lifecycle mixed into it would break the command that
        succeeded.
        """
        _under(monkeypatch, SOURCE_A)
        capsys.readouterr()
        monkeypatch.setenv(SOURCE_ID_ENV, SOURCE_B)

        assert run("dice.roll", "--expression", "1d1", "--seed", "3") == cli.EXIT_OK

        captured = capsys.readouterr()
        assert json.loads(captured.out)["total"] == 1
        assert "restarted the engine server" in captured.err, captured.err

    def test_serve_reports_a_replacement_as_one_on_both_of_its_channels(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``serve`` is the command whose whole job is reporting the server.

        It does not go through the announcement every other command shares, so
        it gets its own sentence and its own three cases to get wrong — and the
        one worth getting right is the one where somebody's running engine was
        thrown away. "started" is true of a replacement and still the wrong
        thing to tell them.

        ``already_running`` cannot carry this on its own: it is ``False`` for a
        cold start and ``False`` for a replacement, and only one of those cost
        the caller a process.
        """
        _under(monkeypatch, SOURCE_A)
        capsys.readouterr()
        monkeypatch.setenv(SOURCE_ID_ENV, SOURCE_B)

        assert run("serve") == cli.EXIT_OK

        captured = capsys.readouterr()
        assert "restarted the engine server" in captured.err, captured.err
        answer = json.loads(captured.out)
        assert answer["reloaded"] is True
        assert answer["already_running"] is False

    def test_serve_calls_an_ordinary_start_a_start(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The negative half: nothing was replaced, so nothing says it was."""
        assert not discovery.state_path_for().exists(), "the workspace was meant to be empty"

        assert run("serve") == cli.EXIT_OK

        captured = capsys.readouterr()
        assert "restarted" not in captured.err, captured.err
        assert json.loads(captured.out)["reloaded"] is False


class TestHelp:
    """The command list comes from the server, so it cannot go stale."""

    def test_help_lists_exactly_the_operations_the_server_serves(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Compared against the live index, not against a list kept here.

        A fixed expectation would be a second copy of the route table, and the
        drift it would catch is precisely the drift the design already makes
        impossible. What is worth pinning is that the *rendering* drops nothing:
        an operation the server serves and the help does not mention is one no
        agent will ever call.
        """
        served = {str(entry["operation"]) for entry in cli.Contract(shared).entries}
        assert run("help") == cli.EXIT_OK
        rendered = capsys.readouterr().out
        missing = sorted(name for name in served if f"  {name} " not in rendered)
        assert not missing, f"the operations index lists these and help does not: {missing}"
        assert len(served) == 47, (
            f"the contract now has {len(served)} operations, not 47; this number is "
            f"here so a route silently disappearing is a failure, not a shorter list"
        )

    def test_help_for_one_operation_separates_required_from_optional(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("help", "encounter.create") == cli.EXIT_OK
        rendered = capsys.readouterr().out
        # Split on the section heading as a whole line: the summary itself ends
        # "optionally on a battle map", and a substring split would cut there.
        required, _, optional = rendered.partition("\noptional\n")
        assert "POST /api/v1/encounters" in rendered
        assert "--combatants" in required and "a list" in required
        assert "--combatants" not in optional
        assert "--seed" in optional and "default null" in optional
        assert "--movement-rule" in optional and 'default "5-5-5"' in optional

    def test_the_example_help_prints_is_a_command_that_runs(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The example is parsed by the client that printed it, then sent.

        An example nobody ever runs is an example that drifts. This one is
        taken off the page, split, and executed, and it must come back
        :data:`cli.EXIT_OK`. That is stronger than it looks: this case used to
        settle for "not a usage error", which passed against the example the
        client synthesised from the schema alone —
        ``--json '{"combatants": []}'``, an empty list the engine then refused.
        A pasteable example is one the engine *answers*, so nothing weaker will
        do here.
        """
        assert run("help", "encounter.create") == cli.EXIT_OK
        example = [
            line.strip()
            for line in capsys.readouterr().out.splitlines()
            if line.strip().startswith("fivee ")
        ][-1]
        assert example.startswith("fivee encounter.create --json ")
        payload = example.partition("--json ")[2].strip("'")
        assert run("encounter.create", "--json", payload) == cli.EXIT_OK

    def test_an_example_shows_the_shape_of_an_object_valued_argument(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--combatants`` is a list of *what*? The example is the only answer.

        The types the help prints are the ones the contract publishes, and for
        a list of objects that is the word "list" and nothing else. So the
        example has to carry the keys, and this reads them back off the printed
        page rather than off the route table — the page is what an agent sees.
        """
        assert run("help", "encounter.create") == cli.EXIT_OK
        example = capsys.readouterr().out.rpartition("--json ")[2].strip().strip("'")
        combatants = json.loads(example)["combatants"]
        assert len(combatants) >= 2, "one combatant is not a fight"
        keys = set().union(*(set(one) for one in combatants))
        assert {"team", "attacks"} <= keys, (
            "the example is what teaches a hand-built creature's shape; without "
            "its attacks it teaches nothing a bare {} did not"
        )

    def test_help_prints_every_legal_value_of_a_closed_set(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--kind`` takes ten values and used to print none of them.

        Derived from ``ActionKind`` rather than listed here: this file asserting
        its own copy of the set would pass against a contract that had drifted
        from the engine, which is the failure the derivation exists to remove.

        Searched in the ``--kind`` line rather than the whole page, because the
        page says several of these words for unrelated reasons: ``attack`` is
        also a flag, ``move`` sits inside ``--movement-mode``, and ``dodge`` is
        the declared example. Stripping the enum out entirely still left those
        three "present" in a page-wide search — the case failed on the other
        seven, so it worked, but three tenths of it were not testing anything.
        """
        assert run("help", "encounter.act") == cli.EXIT_OK
        rendered = capsys.readouterr().out
        kind_line = next(
            (
                line
                for line in rendered.splitlines()
                if line.strip().startswith("--kind ")
            ),
            "",
        )
        assert kind_line, "the help prints no --kind line at all"
        missing = sorted(
            kind.value for kind in ActionKind if kind.value not in kind_line
        )
        assert not missing, (
            f"`fivee help encounter.act` prints no way to learn these action "
            f"kinds short of guessing one wrong: {missing}"
        )

    def test_an_unknown_operation_offers_the_near_misses(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("encounter.akt", "--id", "enc-1") == cli.EXIT_USAGE
        message = capsys.readouterr().err
        assert "no operation 'encounter.akt'" in message, (
            "the name reported must be the one typed; joining the next token "
            "would report 'encounter.akt.--id', which nobody wrote"
        )
        assert "encounter.act" in message


class TestScalarsInSchemasThatAlsoTakeArrays:
    """A bare scalar for an argument that also accepts a list.

    Several arguments admit both — ``to_position`` and ``center`` take a point
    *or* a bare number of feet along the x-axis, and ``natural`` takes one
    reported d20 face or two. The coercion answered the array branch first and
    raised "write it as JSON" before it ever reached the integer branch, so the
    bare-number spelling the schema and the skill both advertise was refused by
    the one surface anybody types into.
    """

    def test_a_bare_number_is_accepted_where_the_schema_takes_one(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(
            "dice.roll", "--expression", "1d20", "--seed", "3", "--natural", "17"
        ) == cli.EXIT_OK
        assert out(capsys)["natural"] == 17

    def test_the_list_spelling_still_works_for_the_same_argument(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(
            "dice.roll", "--expression", "1d20", "--seed", "3",
            "--advantage", "advantage", "--natural", "[3, 18]",
        ) == cli.EXIT_OK
        assert out(capsys)["natural"] == 18

    def test_a_word_is_still_refused_by_the_scalar_branch(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The guard on the fix: falling through to the integer branch must not
        # mean falling through to accepting anything.
        assert run(
            "dice.roll", "--expression", "1d20", "--natural", "seventeen"
        ) == cli.EXIT_USAGE


class TestInvocation:
    """Both spellings, both ways of giving arguments, and how they compose."""

    def test_the_dotted_and_spaced_spellings_are_the_same_command(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("dice.roll", "--expression", "3d6", "--seed", "17") == cli.EXIT_OK
        dotted = out(capsys)
        assert run("dice", "roll", "--expression", "3d6", "--seed", "17") == cli.EXIT_OK
        assert out(capsys) == dotted

    def test_json_is_the_base_object_and_flags_override_its_keys(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The composition rule, checked where the override is observable.

        ``--seed`` decides the roll, so a client that let ``--json`` win would
        return a different total. Seeds 5 and 41 are here because they roll
        differently on ``4d20``, which is what makes the assertion able to fail.
        """
        assert run("dice.roll", "--json", '{"expression": "4d20", "seed": 5}') == cli.EXIT_OK
        base = out(capsys)
        assert base["seed"] == 5

        assert run(
            "dice.roll", "--json", '{"expression": "4d20", "seed": 5}', "--seed", "41"
        ) == cli.EXIT_OK
        overridden = out(capsys)
        assert overridden["seed"] == 41
        assert overridden["expression"] == "4d20", "the base object still supplied this"
        assert overridden["rolls"] != base["rolls"], (
            "same rolls under two seeds means the override never reached the engine"
        )

    def test_json_reads_stdin_when_it_is_given_a_dash(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The form that matters for a payload no shell should have to quote."""
        monkeypatch.setattr(
            sys, "stdin", io.StringIO(json.dumps({"combatants": [HERO, GOBLIN]}))
        )
        assert run("encounter.create", "--json", "-", "--seed", "23") == cli.EXIT_OK
        created = out(capsys)
        assert created["state"]["order"] == ["Thora", "Goblin"]

    def test_a_bare_word_fills_the_path_parameter(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(
            "encounter.create", "--seed", "29",
            "--json", json.dumps({"combatants": [HERO, GOBLIN]}),
        ) == cli.EXIT_OK
        encounter_id = out(capsys)["encounter_id"]

        assert run("encounter.state", encounter_id) == cli.EXIT_OK
        positional = out(capsys)
        assert run("encounter.state", "--id", encounter_id) == cli.EXIT_OK
        assert out(capsys) == positional

    def test_a_whole_fight_runs_over_http_and_the_engine_keeps_the_state(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Create, act, advance, read — four commands, four processes' worth of
        statelessness on this side. The CLI holds nothing between calls, so the
        fight advancing at all is the server owning it."""
        assert run(
            "encounter.create", "--seed", "31",
            "--json", json.dumps({"combatants": [HERO, GOBLIN]}),
        ) == cli.EXIT_OK
        created = out(capsys)
        encounter_id = created["encounter_id"]
        actor = created["state"]["turn"]

        assert run("encounter.act", encounter_id, "--kind", "dodge") == cli.EXIT_OK
        acted = out(capsys)
        assert acted["state"]["combatants"]
        assert any(event["kind"] == "dodge" for event in acted["events"]), acted["events"]

        assert run("encounter.advance", encounter_id) == cli.EXIT_OK
        capsys.readouterr()

        assert run("encounter.state", encounter_id) == cli.EXIT_OK
        after = out(capsys)
        assert after["turn"] != actor, "advance did not move the turn on"

    def test_the_version_header_comes_back_so_the_next_write_can_be_guarded(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The ownership half of the durable-write contract, from a shell.

        An encounter's version is its journal head and it travels as an
        ``ETag`` response header, so a client that returned only the parsed body
        would leave ``--if-match`` unusable — the caller could pass one but
        never learn what to pass. Both directions are checked here: the version
        the client reported is accepted, and a different one is refused with the
        service layer's own stale-write words rather than merged.
        """
        assert run(
            "encounter.create", "--seed", "37",
            "--json", json.dumps({"combatants": [HERO, GOBLIN]}),
        ) == cli.EXIT_OK
        created = capsys.readouterr()
        encounter_id = json.loads(created.out)["encounter_id"]
        reported = [
            line.partition("etag ")[2].partition(" ")[0]
            for line in created.err.splitlines()
            if line.startswith("fivee: etag ")
        ]
        assert reported, f"no version was reported; stderr held {created.err!r}"

        assert run(
            "encounter.act", encounter_id, "--if-match", reported[-1], "--kind", "dodge"
        ) == cli.EXIT_OK
        capsys.readouterr()

        assert run(
            "encounter.act", encounter_id, "--if-match", reported[-1], "--kind", "dodge"
        ) == cli.EXIT_REFUSED, "the head moved with the first action, so this one is stale"
        assert "has advanced since you read it" in capsys.readouterr().err

    def test_compact_puts_the_whole_result_on_one_line(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("dice.roll", "--expression", "1d6", "--seed", "2") == cli.EXIT_OK
        indented = capsys.readouterr().out
        assert run("dice.roll", "--expression", "1d6", "--seed", "2", "--compact") == (
            cli.EXIT_OK
        )
        compact = capsys.readouterr().out
        assert compact.count("\n") == 1 and indented.count("\n") > 1
        assert json.loads(compact) == json.loads(indented)

    def test_a_query_parameter_goes_on_the_query_string_typed(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--limit 3`` must arrive as an integer the server accepts.

        The schema says integer and the server refuses a non-number, so a
        client that sent every flag as text would be refused here rather than
        paging at 3.
        """
        assert run("catalog.search", "--query", "goblin", "--limit", "3") == cli.EXIT_OK
        found = out(capsys)
        assert len(found["results"]) <= 3


class TestFailures:
    """Four failures, four exit codes, and a refusal that names its fix."""

    def test_a_missing_required_argument_names_the_argument(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("dice.roll", "--seed", "1") == cli.EXIT_USAGE
        message = capsys.readouterr().err
        assert "--expression" in message and "dice.roll" in message
        assert "fivee help dice.roll" in message

    def test_an_unknown_flag_suggests_the_flag_that_was_meant(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("dice.roll", "--expresion", "1d6") == cli.EXIT_USAGE
        message = capsys.readouterr().err
        assert "--expresion" in message and "--expression" in message

    def test_a_flag_whose_value_was_eaten_by_the_next_flag_is_refused(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bare flag means true, and only where true is a value it can take.

        Found by driving it: ``fivee map.put --if-match --json -`` sent the
        literal text ``True`` as the map's sha256 and came back with a
        stale-write refusal quoting it — a plausible-looking request nobody
        wrote, and a confusing answer to a command that was simply missing a
        word.
        """
        assert run("dice.roll", "--expression", "--seed", "3") == cli.EXIT_USAGE
        message = capsys.readouterr().err
        assert "--expression needs a value (text)" in message

    def test_a_bare_flag_still_means_true_where_the_argument_is_boolean(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The control for the case above: the guard must not eat the feature."""
        assert run(
            "analytics.scenario-timing", "--distance-feet", "120",
            "--speed-feet", "30", "--dash",
        ) == cli.EXIT_OK
        assert out(capsys)["traveller"]["dash"] is True

    def test_a_refusal_leads_with_the_detail_and_carries_the_type(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The engine's own words, not a status the caller has to look up.

        ``detail`` is where this repository puts what a branch refused, so a
        client that printed only "400 Bad Request" would throw away the entire
        content of the error.
        """
        assert run("encounter.state", "not-an-id") == cli.EXIT_REFUSED
        captured = capsys.readouterr()
        assert captured.out == "", "a refusal must not put anything on stdout"
        assert "invalid encounter id 'not-an-id'" in captured.err
        assert "urn:fivee-sim:error:invalid-parameter" in captured.err

    def test_json_errors_puts_the_problem_object_on_stderr_not_stdout(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Machine-readable, and still not where a result would be.

        stdout stays "the result, or nothing", so ``$(fivee ...)`` can never
        capture something shaped like an answer that is really an error.
        """
        assert run("encounter.state", "not-an-id", "--json-errors") == cli.EXIT_REFUSED
        captured = capsys.readouterr()
        assert captured.out == ""
        problem = json.loads(captured.err)
        assert problem["status"] == 400
        assert problem["detail"] == "invalid encounter id 'not-an-id'"
        assert problem["type"] == "urn:fivee-sim:error:invalid-parameter"
        assert problem["instance"] == "/api/v1/encounters/not-an-id"

    def test_a_404_is_a_refusal_like_any_other_and_still_names_what_is_there(
        self, shared: discovery.Server, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("catalog.get", "no-such-record") == cli.EXIT_REFUSED
        assert "no catalog record" in capsys.readouterr().err

    def test_a_server_fault_is_a_different_exit_code_from_a_refusal(
        self, workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The 5xx branch, served on purpose because it cannot be asked for.

        Every way the engine says no is a typed service error mapped to a 4xx,
        so a reachable 500 would be a defect — and a test that depended on one
        would go red the day it was fixed. :class:`FaultServer` speaks the same
        HTTP over the same kind of socket and writes the same state file, so
        the client discovers it, pings it and calls it exactly as it would the
        engine. Only the answer is arranged.
        """
        with FaultServer(discovery.state_path_for()):
            assert run("dice.roll", "--expression", "1d6") == cli.EXIT_FAULT
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "failed (500)" in captured.err, (
            "a fault must not read as a refusal; the caller's fix is the server "
            "log, not the command"
        )
        assert "the engine fell over" in captured.err

    def test_the_four_failures_have_four_different_exit_codes(self) -> None:
        """Stated once, as itself, because collapsing two is the regression.

        Each of these is asserted by a test above; what no single one of them
        can assert is that no two of them are equal. A client that returned 1
        for everything would pass every other test in this class.
        """
        codes = [cli.EXIT_USAGE, cli.EXIT_REFUSED, cli.EXIT_FAULT, cli.EXIT_UNREACHABLE]
        assert len(set(codes)) == 4
        assert cli.EXIT_OK not in codes


class FaultServer:
    """A real HTTP server that pings healthy and then answers 500.

    Not a mock: the client opens a socket to it, sends its launch token, and
    parses real ``application/problem+json`` off the wire. What it fakes is the
    engine's *fault*, which is the one thing a correct engine will not produce
    on request.

    It writes the launch state file itself, so the client finds it by the same
    discovery path it uses for a real server rather than being handed a handle.
    """

    def __init__(self, state_path: Path, token: str = "fault-server-token") -> None:
        self.state_path = state_path
        self.token = token
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FaultHandler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def __enter__(self) -> FaultServer:
        self._thread.start()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "port": self.port,
                    "token": self.token,
                    "maps_dir": str(self.state_path.parent / "maps"),
                    "replays_dir": str(self.state_path.parent / "replays"),
                }
            ),
            encoding="utf-8",
        )
        return self

    def __exit__(self, *_: object) -> None:
        self._httpd.shutdown()
        self._thread.join(timeout=5)
        self._httpd.server_close()
        self.state_path.unlink(missing_ok=True)


class _FaultHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib
        pass

    def _send(self, status: int, payload: dict[str, Any], content_type: str) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib dispatch names
        if self.path == "/api/v1/ping":
            self._send(200, {"ok": True, "version": "fault"}, "application/json")
            return
        self._fault()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self._fault()

    def _fault(self) -> None:
        self.close_connection = True
        self._send(
            500,
            {
                "type": "urn:fivee-sim:error:internal",
                "title": "Internal Server Error",
                "status": 500,
                "detail": "the engine fell over answering this",
                "instance": self.path,
            },
            "application/problem+json",
        )
