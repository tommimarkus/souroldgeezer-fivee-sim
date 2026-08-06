"""The server's launch lifecycle: the real CLI process, and finding it again.

These spawn ``python -m fivee_sim.web`` for real — the state-file protocol
(written after bind, removed on shutdown) is exactly the part an in-process
test cannot vouch for. Linux is the target platform; the SIGTERM semantics are
skipped where they do not exist.

The discovery half used to be a pair of MCP tools that spawned the editor and
shut it back down. That is :mod:`fivee_sim.client.discovery` now, and it is the
same claims about the same state file: a second call finds the first server
rather than starting a second, a record nobody answers for is cleared rather
than trusted, and stopping removes it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from fivee_sim import paths
from fivee_sim.client import discovery
from fivee_sim.web.cli import STATE_FILENAME, read_state, state_file_for
from fivee_sim.web.http_server import API_PREFIX, SOURCE_ID_ENV, TOKEN_HEADER

from . import api
from .conftest import mapless_fight

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the lifecycle rests on POSIX signal semantics"
)

ENGINE_SRC = Path(__file__).resolve().parent.parent / "src"


def _wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _reaped_pid() -> int:
    """A PID that is certainly not live: a child we spawned, waited for, and reaped.

    Replaces a literal ``2**22 + 1``, which encoded Linux's *default* ``pid_max``
    and would name a live process on a host with ``kernel.pid_max`` raised.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", ""], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    child.wait()
    return child.pid


def _spawn_cli(
    arguments: list[str], environment: Mapping[str, str | None] | None = None
) -> subprocess.Popen[str]:
    """Spawn the real CLI. ``environment`` overrides inherited variables; ``None``
    as a value unsets one, which is the only way to test an absent variable in a
    suite that inherits whatever the developer's shell exported."""
    env = dict(os.environ)
    for name, value in (environment or {}).items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    env["PYTHONPATH"] = str(ENGINE_SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-m", "fivee_sim.web", *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
    )


def _ping(port: int, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{API_PREFIX}/ping", headers={TOKEN_HEADER: token}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        answer: dict[str, Any] = json.loads(response.read())
        return answer


def _get(port: int, token: str, path: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{API_PREFIX}{path}", headers={TOKEN_HEADER: token}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        answer: dict[str, Any] = json.loads(response.read())
    return answer


def _record_and_ping(
    tmp_path: Path, environment: Mapping[str, str | None]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One launch under *environment*, answered for twice: on disk, and over HTTP.

    Both are asked of the same process because a launch fact that the record and
    the ping disagreed about would be worse than one neither carried.
    """
    state_path = tmp_path / "state" / "fivee-sim-server.json"
    process = _spawn_cli(
        [
            "--maps-dir", str(tmp_path / "maps"),
            "--state-file", str(state_path),
            "--port", "0",
        ],
        environment,
    )
    try:
        assert _wait_for(
            lambda: read_state(state_path) is not None
        ), "the state file never appeared"
        state = read_state(state_path)
        assert state is not None
        return state, _ping(state["port"], state["token"])
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


def test_the_state_file_reader_has_one_canonical_owner() -> None:
    assert discovery.read_state is paths.read_state
    assert read_state is paths.read_state


def test_the_state_file_reader_keeps_its_tolerant_contract(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff")
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    non_object = tmp_path / "non-object.json"
    non_object.write_text("[]", encoding="utf-8")

    assert read_state(tmp_path / "missing.json") is None
    assert read_state(directory) is None
    assert read_state(invalid_utf8) is None
    assert read_state(malformed) is None
    assert read_state(non_object) is None

    valid = tmp_path / "valid.json"
    expected = {"port": 4312, "token": "still-the-same", "nested": {"ready": True}}
    valid.write_text(json.dumps(expected), encoding="utf-8")
    assert read_state(valid) == expected


class TestCliLifecycle:
    def test_a_config_file_owns_launch_roots_and_content_mode(
        self, tmp_path: Path
    ) -> None:
        config_dir = tmp_path / "campaign" / ".fivee-sim"
        config_dir.mkdir(parents=True)
        config = config_dir / "config.toml"
        config.write_text(
            """\
format_version = 1

[content]
builtin = "exclude"

[storage]
maps = "battle-maps"
replays = "frozen-replays"
""",
            encoding="utf-8",
        )
        state_path = config_dir / "fivee-sim-server.json"
        process = _spawn_cli(
            ["--config", str(config), "--port", "0"],
            {
                "FIVEE_SIM_MAPS": str(tmp_path / "wrong-maps"),
                "FIVEE_SIM_REPLAYS": str(tmp_path / "wrong-replays"),
                "FIVEE_SIM_BUILTIN": "include",
            },
        )
        try:
            assert _wait_for(lambda: read_state(state_path) is not None), (
                "the state file never appeared"
            )
            state = read_state(state_path)
            assert state is not None
            assert state["maps_dir"] == str(config_dir / "battle-maps")
            assert state["replays_dir"] == str(config_dir / "frozen-replays")

            status = _get(state["port"], state["token"], "/content")
            assert status["builtin"] == "exclude"
            assert status["configuration"] == {
                "source": "file",
                "path": str(config),
            }
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def test_the_cli_binds_reports_answers_and_dies_cleanly_on_sigterm(
        self, tmp_path: Path
    ) -> None:
        maps_dir = tmp_path / "maps"
        state_path = tmp_path / "state" / "fivee-sim-server.json"
        process = _spawn_cli(
            ["--maps-dir", str(maps_dir), "--state-file", str(state_path), "--port", "0"]
        )
        try:
            assert _wait_for(
                lambda: read_state(state_path) is not None
            ), "the state file never appeared"
            state = read_state(state_path)
            assert state is not None
            assert state["pid"] == process.pid
            assert isinstance(state["port"], int)
            assert state["maps_dir"] == str(maps_dir)
            assert state["started"]
            assert state["token"]

            answer = _ping(state["port"], state["token"])
            assert answer["ok"] is True
            assert answer["maps_dir"] == str(maps_dir)

            process.send_signal(signal.SIGTERM)
            output, _ = process.communicate(timeout=10)
            assert process.returncode == 0
            assert not state_path.exists(), "SIGTERM must remove the state file"
            # The URL is announced, and the token is not — not on stdout, not
            # in the URL, nowhere a shell history could keep it.
            assert f"http://127.0.0.1:{state['port']}/" in output
            assert state["token"] not in output
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)

    def test_the_state_file_defaults_to_beside_the_maps_dir(self, tmp_path: Path) -> None:
        maps_dir = tmp_path / ".fivee-sim" / "maps"
        assert state_file_for(maps_dir) == tmp_path / ".fivee-sim" / STATE_FILENAME

    def test_one_launch_serves_maps_and_replays_and_says_so(self, tmp_path: Path) -> None:
        """The launch is one service, so its record and its ping describe both.

        Without this, a caller that wanted to know where the running server
        reads replays from would have to guess it from ``maps_dir`` — which is
        exactly the derivation the replay root deliberately does not make.
        """
        maps_dir = tmp_path / "maps"
        replays_dir = tmp_path / "elsewhere" / "replays"
        state_path = tmp_path / "state" / "fivee-sim-server.json"
        process = _spawn_cli(
            [
                "--maps-dir", str(maps_dir),
                "--replays-dir", str(replays_dir),
                "--state-file", str(state_path),
                "--port", "0",
            ]
        )
        try:
            assert _wait_for(
                lambda: read_state(state_path) is not None
            ), "the state file never appeared"
            state = read_state(state_path)
            assert state is not None
            assert state["replays_dir"] == str(replays_dir)

            answer = _ping(state["port"], state["token"])
            assert answer["replays_dir"] == str(replays_dir)
            assert answer["maps_dir"] == str(maps_dir)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)

    def test_a_launch_reports_the_source_id_it_was_started_with(
        self, tmp_path: Path
    ) -> None:
        """A running server names the source it is running, or nothing can tell it is old.

        The launcher knows the digest of the source it just resolved; only the
        server knows the digest it was started from months ago. Reported, the
        two are comparable, and a server outliving its source becomes a fact
        rather than a symptom someone debugs from the wrong end.
        """
        digest = "0" * 63 + "d"
        state, answer = _record_and_ping(tmp_path, {SOURCE_ID_ENV: digest})

        assert state["source_id"] == digest
        assert answer["source_id"] == digest

    def test_a_launch_told_no_source_id_still_answers_the_question(
        self, tmp_path: Path
    ) -> None:
        """No id is an empty string, and never a missing key.

        The reader compares two ids, so it needs one to compare: dropping the
        key would turn "this launch was not started from a tracked source" —
        the ordinary case, every launch that is not a dev reload — into a
        ``KeyError`` on the ordinary path.
        """
        state, answer = _record_and_ping(tmp_path, {SOURCE_ID_ENV: None})

        assert "source_id" in state, "an unset variable is no id, not an absent field"
        assert state["source_id"] == ""
        assert "source_id" in answer, "an unset variable is no id, not an absent field"
        assert answer["source_id"] == ""

    def test_a_source_id_exported_empty_reads_as_no_id_at_all(
        self, tmp_path: Path
    ) -> None:
        """Blank is unset, the reading every other variable this engine takes.

        A shell that exports one variable from another unset one hands the child
        an empty string, and a server that treated it as an id would be claiming
        to be a source no digest can ever match.
        """
        state, answer = _record_and_ping(tmp_path, {SOURCE_ID_ENV: ""})

        assert state["source_id"] == ""
        assert answer["source_id"] == ""


class TestServerDiscovery:
    """Find the server, or start one. The client's half of the state file."""

    def _kill_leftover(self, state_path: Path) -> None:
        state = read_state(state_path)
        if state is not None and isinstance(state.get("pid"), int):
            try:
                os.kill(state["pid"], signal.SIGKILL)
            except OSError:
                pass

    def test_ensure_is_idempotent_and_stop_tears_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        maps_dir = tmp_path / "maps"
        monkeypatch.setenv("FIVEE_SIM_MAPS", str(maps_dir))
        state_path = discovery.state_path_for()
        first = discovery.ensure_server()
        try:
            assert first.spawned is True
            assert first.url == f"http://127.0.0.1:{first.port}/"
            assert first.maps_dir == str(maps_dir)
            assert (state_path.parent / "fivee-sim-server.log").exists()
            assert state_path.exists()

            second = discovery.ensure_server()
            assert second.spawned is False
            assert second.port == first.port

            stopped = discovery.stop(state_path)
            assert stopped["stopped"] is True and stopped["was_running"] is True
            assert not state_path.exists()
        finally:
            self._kill_leftover(state_path)

    def test_one_launch_answers_for_maps_and_replays_alike(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One process, two roots, so one record reports both.

        Before this the viewer page was served and unreachable in practice —
        nothing told anyone where its bundles came from. A caller handing a URL
        to the user should not have to derive the replays root from the maps one.
        """
        maps_dir = tmp_path / "maps"
        replays_dir = tmp_path / "replays"
        monkeypatch.setenv("FIVEE_SIM_MAPS", str(maps_dir))
        monkeypatch.setenv("FIVEE_SIM_REPLAYS", str(replays_dir))
        state_path = discovery.state_path_for()
        server = discovery.ensure_server()
        try:
            assert server.replays_dir == str(replays_dir)
            assert server.maps_dir == str(maps_dir)

            again = discovery.ensure_server()
            assert again.spawned is False
            assert again.replays_dir == server.replays_dir
        finally:
            discovery.stop(state_path)
            self._kill_leftover(state_path)

    def test_an_export_with_no_path_lands_in_the_replays_root_not_under_maps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Replays are a sibling of maps, never a child.

        Written under the maps root they would sit inside every ``list_maps``
        walk, and the served editor would be offering to open them as maps.
        """
        maps_dir = tmp_path / ".fivee-sim" / "maps"
        monkeypatch.setenv("FIVEE_SIM_MAPS", str(maps_dir))
        monkeypatch.setenv("FIVEE_SIM_PROJECT_DIR", str(tmp_path))
        monkeypatch.delenv("FIVEE_SIM_REPLAYS", raising=False)
        encounter_id = mapless_fight(seed=94)

        # Forced to a file rather than inline, so there is a path to check.
        written = Path(api.replay_export(encounter_id, embed=True)["path"])

        assert written.parent == tmp_path / ".fivee-sim" / "replays"
        assert maps_dir not in written.parents

    def test_stop_without_a_server_reports_nothing_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIVEE_SIM_MAPS", str(tmp_path / "maps"))
        assert discovery.stop(discovery.state_path_for()) == {
            "stopped": False, "was_running": False
        }

    def test_a_stale_state_file_is_cleared_and_a_fresh_server_spawned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        maps_dir = tmp_path / "maps"
        monkeypatch.setenv("FIVEE_SIM_MAPS", str(maps_dir))
        state_path = discovery.state_path_for()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        # What makes this record stale is the *port*: ``find_running`` pings the
        # port with the token and only trusts a server that answers, so port 1 —
        # which nothing serves — is the whole reason the stale branch is taken.
        # The pid is never consulted on this path; it is here so the record has
        # the shape a real one does, and it is a reaped child's so it names
        # nothing live.
        state_path.write_text(
            json.dumps({
                "pid": _reaped_pid(), "port": 1, "token": "gone",
                "maps_dir": str(maps_dir),
            })
        )
        server = discovery.ensure_server()
        try:
            assert server.spawned is True
            assert server.port != 1
        finally:
            discovery.stop(state_path)
            self._kill_leftover(state_path)
