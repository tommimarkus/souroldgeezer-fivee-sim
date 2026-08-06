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

**The rendezvous is beside the selected project config.** That keeps discovery
stable when a config edit changes the maps directory. The legacy no-file path
keeps its historical record beside the maps directory.

**Three constants are copied here rather than imported**, and that is the
constraint working as intended: this package may not import
:mod:`fivee_sim.web`, because a client that imported the server could do things
over that import which the REST surface does not expose. :data:`TOKEN_HEADER`
and :data:`API_PREFIX` are wire protocol — the same kind of shared fact as the
state file's filename — and a mismatch fails loudly on the first request rather
than silently. :func:`read_state` is duplicated for the same reason, and stays
tolerant for the same reason the server's copy is: an unreadable record and an
absent one mean the same thing to every caller.

:data:`SOURCE_ID_ENV` is the third, and it is the one worth watching, because it
is the copy whose drift is *quiet*. Misspell the header and the first request is
refused; misspell this and every launch simply expects nothing, no reload ever
happens, and nothing anywhere raises. So the cases that pin it start real
servers under a real id rather than checking this name against the other one.

Spawning is ``sys.executable -m fivee_sim.web``, detached in its own session so
the server outlives the shell that first needed it, with stdout and stderr to a
log file beside the state file. No *stream* is inherited: the parent's stdout may
be a pipe somebody is reading JSON from. The environment is, and since the
reload was added that is load-bearing rather than incidental — a fresh server
reports the id this process expects because it was handed the same variable, and
a spawn that scrubbed it would leave every command restarting the engine the
last one started.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..configuration import Configuration, configuration_identity
from ..paths import STATE_FILENAME, maps_root, state_file_for

__all__ = [
    "API_PREFIX",
    "PING_TIMEOUT",
    "SOURCE_ID_ENV",
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
#: Names the engine source this run was built from, as a sha256 hex digest. The
#: launcher exports it only when it was asked to watch the source; an ordinary
#: run leaves it unset, and unset here means "no opinion" rather than "no id".
SOURCE_ID_ENV = "FIVEE_SIM_SOURCE_ID"

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
    #: The engine source this process is running, as it answered for itself, and
    #: ``""`` when it was started without one being named. Never read off the
    #: state file — see :func:`find_running`.
    source_id: str = ""
    #: The selected project configuration, if any, and its semantic identity.
    #: The path tells a caller what owns the process; the identity tells it
    #: whether that file still means what the running process loaded.
    configuration_path: str = ""
    configuration_id: str = ""
    #: True when this call stopped a server running other source and started
    #: this one in its place. Always implies :attr:`spawned`: the sibling says a
    #: process began here, this one says a process also ended here, and a caller
    #: telling a user "started the engine" when it replaced theirs is reporting
    #: half of what happened.
    reloaded: bool = False
    #: True when this call replaced a process whose project configuration no
    #: longer matches the selected file (including file versus legacy mode).
    reconfigured: bool = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def api(self) -> str:
        return f"http://127.0.0.1:{self.port}{API_PREFIX}"


def state_path_for(
    maps_dir: str | Path | None = None,
    *,
    configuration: Configuration | None = None,
) -> Path:
    """Where this project records its server.

    A selected configuration is the stable project identity, even when one of
    its storage paths changes. Without one, retain the historical maps-adjacent
    location. The server makes the same choice from the same inputs.
    """
    if configuration is not None:
        return configuration.path.parent / STATE_FILENAME
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
        # For the source id there is no falling back to the record at all, and
        # the difference is not fussiness. A record is a file: it outlives the
        # process, anyone can rewrite it, and it says what a launch was asked
        # for. This answer came from the process that would be restarted, and
        # that is the only thing worth holding against the source on disk. So a
        # record naming an id the ping does not is a file making a claim, and it
        # reads here as no id — the answer that gets a server replaced.
        source_id=str(answer.get("source_id", "")),
        configuration_path=str(answer.get("configuration_path", "")),
        configuration_id=str(answer.get("configuration_id", "")),
        pid=pid if isinstance(pid, int) else None,
        spawned=False,
    )


def spawn(
    state_path: str | Path,
    *,
    maps_dir: str | Path | None = None,
    configuration: Configuration | None = None,
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
    if configuration is not None:
        arguments += ["--config", str(configuration.path)]
    if port is not None:
        arguments += ["--port", str(port)]
    # 0600 from the first byte, for the reason the state file beside it is:
    # nothing written here carries the token today — every print in web/cli.py
    # and every handler log line reports the port and URL only — but a future
    # traceback that echoed a header dict would, and a write-then-chmod leaves
    # a window the umask governs.
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(log_fd, "ab") as log_file:
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
            return replace(found, spawned=True)
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


def _runs_other_source(found: Server) -> bool:
    """Whether *found* is serving source this run was not built from.

    The environment decides whether the question is asked at all. Unset — every
    launch that is not a dev reload — is not "no id", it is *no opinion*, and no
    answer from the server can override it: a plain command must never restart
    an engine somebody is mid-fight in on the strength of a digest nobody asked
    about. That is the whole of the feature being off by default.

    Asked, an untracked server loses. ``""`` is not a mismatch — it says nobody
    was tracking when that process started — but "cannot be shown to be current"
    is the position a server from before the tracking is in, and it is exactly
    the process a reload exists to replace. The costs are not symmetric either:
    keeping it means editing source and testing an engine that never loaded it,
    which is the failure this was built for, and replacing it costs one cold
    start.
    """
    expected = os.environ.get(SOURCE_ID_ENV, "")
    return bool(expected) and found.source_id != expected


def ensure_server(
    *,
    maps_dir: str | Path | None = None,
    configuration: Configuration | None = None,
    state_path: str | Path | None = None,
    port: int | None = None,
    timeout: float = SPAWN_TIMEOUT,
) -> Server:
    """A live server: the one already running, or one started for this call.

    A running server whose source or semantic project configuration differs is
    stopped and replaced. Source tracking remains opt-in; configuration matching
    is always exact because selecting a file is itself the user's instruction.
    The replacement inherits the process environment and is also handed the
    config path, so its ping reports the identities the next command compares.
    """
    path = (
        Path(state_path)
        if state_path is not None
        else state_path_for(maps_dir, configuration=configuration)
    )
    found = find_running(path)
    if found is not None:
        expected_configuration = (
            configuration_identity(configuration) if configuration is not None else ""
        )
        source_changed = _runs_other_source(found)
        configuration_changed = found.configuration_id != expected_configuration
        if not source_changed and not configuration_changed:
            return found
        stop(path)
    root = (
        Path(maps_dir).expanduser()
        if maps_dir is not None
        else configuration.map_paths[0]
        if configuration is not None
        else maps_root()
    )
    fresh = spawn(
        path,
        maps_dir=root,
        configuration=configuration,
        port=port,
        timeout=timeout,
    )
    if found is None:
        return fresh
    return replace(
        fresh,
        reloaded=source_changed,
        reconfigured=configuration_changed,
    )


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
