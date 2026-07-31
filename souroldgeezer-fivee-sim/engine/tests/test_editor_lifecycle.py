"""The editor's launch lifecycle: the real CLI process, and the MCP tools.

These spawn ``python -m fivee_sim.editor`` for real — the state-file protocol
(written after bind, removed on shutdown) is exactly the part an in-process
test cannot vouch for. Linux is the target platform; the SIGTERM semantics are
skipped where they do not exist.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from fivee_sim.editor.cli import STATE_FILENAME, read_state, state_file_for
from fivee_sim.editor.http_server import TOKEN_HEADER
from fivee_sim.mcp_server import server as api

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


def _spawn_cli(arguments: list[str]) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ENGINE_SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-m", "fivee_sim.editor", *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
    )


def _ping(port: int, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/ping", headers={TOKEN_HEADER: token}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        answer: dict[str, Any] = json.loads(response.read())
    return answer


class TestCliLifecycle:
    def test_the_cli_binds_reports_answers_and_dies_cleanly_on_sigterm(
        self, tmp_path: Path
    ) -> None:
        maps_dir = tmp_path / "maps"
        state_path = tmp_path / "state" / "editor-server.json"
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


class TestEditorTools:
    def test_serve_is_idempotent_and_stop_tears_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        maps_dir = tmp_path / "maps"
        monkeypatch.setenv("FIVEE_SIM_MAPS", str(maps_dir))
        state_path = state_file_for(maps_dir)
        first = api.map_editor_serve()
        try:
            assert first["already_running"] is False
            assert first["url"] == f"http://127.0.0.1:{first['port']}/"
            assert first["maps_dir"] == str(maps_dir)
            assert Path(first["log"]).exists()
            assert state_path.exists()

            second = api.map_editor_serve()
            assert second["already_running"] is True
            assert second["port"] == first["port"]

            stopped = api.map_editor_stop()
            assert stopped == {"stopped": True, "was_running": True}
            assert not state_path.exists()
        finally:
            state = read_state(state_path)
            if state is not None and isinstance(state.get("pid"), int):
                try:
                    os.kill(state["pid"], signal.SIGKILL)
                except OSError:
                    pass

    def test_stop_without_a_server_reports_nothing_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIVEE_SIM_MAPS", str(tmp_path / "maps"))
        assert api.map_editor_stop() == {"stopped": False, "was_running": False}

    def test_a_stale_state_file_is_cleared_and_a_fresh_server_spawned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        maps_dir = tmp_path / "maps"
        monkeypatch.setenv("FIVEE_SIM_MAPS", str(maps_dir))
        state_path = state_file_for(maps_dir)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        # What makes this record stale is the *port*: ``_live_editor_state`` pings
        # the port with the token and only trusts a server that answers, so port 1
        # — which nothing serves — is the whole reason the stale branch is taken.
        # The pid is never consulted on this path; it is here so the record has the
        # shape a real one does, and it is a reaped child's so it names nothing live.
        state_path.write_text(
            json.dumps({
                "pid": _reaped_pid(), "port": 1, "token": "gone",
                "maps_dir": str(maps_dir),
            })
        )
        result = api.map_editor_serve()
        try:
            assert result["already_running"] is False
            assert result["port"] != 1
        finally:
            api.map_editor_stop()
