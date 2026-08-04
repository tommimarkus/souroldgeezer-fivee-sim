"""Find the engine's server, or start one. The client's whole lifecycle.

Every ``fivee`` command begins here, and the sequence is always the same: read
the state file, ping what it describes, and spawn a server only when nothing
answers. That is why the CLI has no "start it first" step to forget — a command
that needs a server gets one, and ``fivee serve`` exists for the case where
somebody wants the URL rather than a result.

**Liveness is a ping, never the file.** A state file outlives a killed process,
so a record alone proves nothing; :func:`find_running` trusts a record only
after the port it names answers ``GET /api/v1/ping`` with this launch's token.
A record nobody answers for is removed before spawning, so the fresh server's
record is the only one anybody can read.

**Two constants are copied here rather than imported**, and that is the
constraint working as intended: this package may not import
:mod:`fivee_sim.web`, because a client that imported the server could do things
over that import which the REST surface does not expose. :data:`TOKEN_HEADER`
and :data:`API_PREFIX` are wire protocol — the same kind of shared fact as the
state file's filename — and a mismatch fails loudly on the first request rather
than silently. :func:`read_state` is duplicated for the same reason, and stays
tolerant for the same reason the server's copy is: an unreadable record and an
absent one mean the same thing to every caller.

Spawning is ``sys.executable -m fivee_sim.web``, detached in its own session so
the server outlives the shell that first needed it, with stdout and stderr to a
log file beside the state file. Nothing is inherited: the parent's stdout may be
a pipe somebody is reading JSON from.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import maps_root, state_file_for

__all__ = [
    "API_PREFIX",
    "PING_TIMEOUT",
    "SPAWN_TIMEOUT",
    "STOP_TIMEOUT",
    "TOKEN_HEADER",
    "Server",
    "UnreachableError",
    "ensure_server",
    "find_running",
    "ping",
    "read_state",
    "spawn",
    "state_path_for",
    "stop",
]

#: The header every ``/api/*`` request carries the launch token in. Wire
#: protocol, shared with the server by agreement rather than by import.
TOKEN_HEADER = "X-Fivee-Editor-Token"
#: The version prefix every operation lives under. Also wire protocol.
API_PREFIX = "/api/v1"

#: How long a liveness ping waits. Short: it is a loopback round trip, and a
#: server too busy to answer in this long is one a command would rather report
#: than block behind.
PING_TIMEOUT = 3.0
#: How long :func:`spawn` waits for a fresh server to bind and record itself.
#: Generous next to the ping because a cold start imports the whole engine.
SPAWN_TIMEOUT = 20.0
#: How long :func:`stop` waits for a stopping server to remove its own record.
STOP_TIMEOUT = 5.0


class UnreachableError(RuntimeError):
    """No server answered, and none could be started.

    Distinct from a refusal on purpose: a 4xx means the engine was asked
    something and said no, while this means nothing was ever asked. The two
    reach a caller as different exit codes because the fixes are different —
    one is the command, the other is the machine.
    """


@dataclass(frozen=True)
class Server:
    """A server that answered its own ping, and how to reach it again."""

    port: int
    token: str
    maps_dir: str = ""
    replays_dir: str = ""
    pid: int | None = None
    #: True when *this* call started it, which is what ``fivee serve`` reports
    #: as ``already_running`` and what every other command announces on stderr.
    spawned: bool = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def api(self) -> str:
        return f"http://127.0.0.1:{self.port}{API_PREFIX}"


def state_path_for(maps_dir: str | Path | None = None) -> Path:
    """Where the server for ``maps_dir`` records itself; the default root's if
    none is given. The same resolution the server makes, from the same module,
    so the two cannot look in different places."""
    root = Path(maps_dir).expanduser() if maps_dir is not None else maps_root()
    return state_file_for(root)


def read_state(path: str | Path) -> dict[str, Any] | None:
    """The parsed state file, or ``None`` when missing, unreadable, or not JSON."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def ping(port: int, token: str, timeout: float = PING_TIMEOUT) -> dict[str, Any] | None:
    """The server's ``ping`` answer, or ``None`` when nothing there answers."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{API_PREFIX}/ping", headers={TOKEN_HEADER: token}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def find_running(state_path: str | Path) -> Server | None:
    """The server this record describes, but only if it still answers."""
    state = read_state(state_path)
    if state is None:
        return None
    port, token = state.get("port"), state.get("token")
    if not isinstance(port, int) or not isinstance(token, str):
        return None
    answer = ping(port, token)
    if answer is None:
        return None
    pid = state.get("pid")
    return Server(
        port=port,
        token=token,
        # The ping is preferred over the record for the directories: the record
        # says what the launch was asked for, the ping says what it is actually
        # serving, and only the second is worth reporting to a user.
        maps_dir=str(answer.get("maps_dir", state.get("maps_dir", ""))),
        replays_dir=str(answer.get("replays_dir", state.get("replays_dir", ""))),
        pid=pid if isinstance(pid, int) else None,
        spawned=False,
    )


def spawn(
    state_path: str | Path,
    *,
    maps_dir: str | Path | None = None,
    port: int | None = None,
    timeout: float = SPAWN_TIMEOUT,
) -> Server:
    """Start a detached server and wait for it to bind, record, and answer.

    Raises :class:`UnreachableError` when it exits early or never reports a
    port; the message names the log file, which is the only place a cold
    start's traceback can be.
    """
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    log_path = path.parent / "fivee-sim-server.log"
    arguments = [sys.executable, "-m", "fivee_sim.web", "--state-file", str(path)]
    if maps_dir is not None:
        arguments += ["--maps-dir", str(maps_dir)]
    if port is not None:
        arguments += ["--port", str(port)]
    with open(log_path, "ab") as log_file:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = find_running(path)
        if found is not None:
            return Server(
                port=found.port,
                token=found.token,
                maps_dir=found.maps_dir,
                replays_dir=found.replays_dir,
                pid=found.pid,
                spawned=True,
            )
        if process.poll() is not None:
            raise UnreachableError(
                f"the engine server exited with status {process.returncode} before "
                f"binding a port; its output is in {log_path}"
            )
        time.sleep(0.05)
    process.terminate()
    raise UnreachableError(
        f"the engine server did not report a bound port within {timeout:.0f}s; "
        f"its output is in {log_path}"
    )


def ensure_server(
    *,
    maps_dir: str | Path | None = None,
    state_path: str | Path | None = None,
    port: int | None = None,
    timeout: float = SPAWN_TIMEOUT,
) -> Server:
    """A live server: the one already running, or one started for this call."""
    path = Path(state_path) if state_path is not None else state_path_for(maps_dir)
    found = find_running(path)
    if found is not None:
        return found
    root = Path(maps_dir).expanduser() if maps_dir is not None else maps_root()
    return spawn(path, maps_dir=root, port=port, timeout=timeout)


def stop(state_path: str | Path, timeout: float = STOP_TIMEOUT) -> dict[str, Any]:
    """Ask the recorded server to stop; fall back to SIGTERM; clear the record.

    ``was_running`` reports whether there was a record at all, and ``stopped``
    whether either path worked — a record whose process is already gone is
    ``{"stopped": false, "was_running": true}``, which is a different fact from
    "there was nothing here".
    """
    path = Path(state_path)
    state = read_state(path)
    if state is None:
        return {"stopped": False, "was_running": False}
    port, token, pid = state.get("port"), state.get("token"), state.get("pid")
    stopped = False
    if isinstance(port, int) and isinstance(token, str):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{API_PREFIX}/shutdown",
            method="POST",
            headers={TOKEN_HEADER: token, "Content-Type": "application/json"},
            data=b"{}",
        )
        try:
            with urllib.request.urlopen(request, timeout=PING_TIMEOUT):
                stopped = True
        except (OSError, ValueError):
            stopped = False
    if not stopped and isinstance(pid, int):
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except OSError:
            stopped = False
    if stopped:
        # The exiting server removes its own record; give it a moment so the
        # file disappears with the process rather than being yanked from under
        # it and leaving the next launch's record racing this unlink.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and path.exists():
            time.sleep(0.05)
    path.unlink(missing_ok=True)
    return {
        "stopped": stopped,
        "was_running": True,
        "port": port if isinstance(port, int) else None,
    }
