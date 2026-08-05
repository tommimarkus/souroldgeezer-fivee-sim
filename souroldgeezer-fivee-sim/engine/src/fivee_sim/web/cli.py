"""The ``fivee-sim-server`` launcher and its state-file conventions.

Two callers start this server: a developer at a shell, and an agent spawning
``python -m fivee_sim.web`` detached. Both find a running one the same way —
through the **state file**, a small JSON record
``{pid, port, token, maps_dir, replays_dir, source_id, started}`` written
*after* the socket is bound, next to the maps directory. The helpers that name,
read, and remove it live here so both sides share one convention rather than two
almost-identical ones.

Content is not loaded here. The server owns an ``EngineState`` and loads
configured packs on first use, with the same fall-back to the bundled slice
every other entry point takes — so a pack-defined terrain kind validates
identically however the engine was started, and there is no second copy of the
terrain table to fall out of step with a reconfiguration made over the API.

On SIGTERM the launcher shuts the server down gracefully and removes the state
file; the token is printed nowhere, and in particular never into a URL, because
shell history and browser history both outlive a launch. The served page
configures itself.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..paths import STATE_FILENAME, state_file_for
from ..service import maps as map_service
from ..service import replay as replay_service
from .http_server import EngineServer

__all__ = ["STATE_FILENAME", "main", "read_state", "state_file_for"]


def read_state(path: str | Path) -> dict[str, Any] | None:
    """The parsed state file, or ``None`` when missing, unreadable, or not JSON.

    Tolerant on purpose: a state file is a hint about a process that may have
    died, and every caller treats an unreadable one exactly like an absent one.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    """Bind, write the state file, announce the URL, and serve until told to stop."""
    parser = argparse.ArgumentParser(
        prog="fivee-sim-server",
        description="Serve the 5E-compatible simulation engine on localhost.",
    )
    parser.add_argument(
        "--maps-dir",
        default=None,
        help="directory this server reads and writes maps in "
        "(default: the configured maps root)",
    )
    parser.add_argument(
        "--replays-dir",
        default=None,
        help="directory the replay viewer plays bundles from, read-only "
        "(default: the configured replays root)",
    )
    parser.add_argument(
        "--port", type=int, default=0, help="port to bind on 127.0.0.1 (default: ephemeral)"
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="where to record {pid, port, token, maps_dir, started} once bound "
        "(default: fivee-sim-server.json next to the maps directory)",
    )
    args = parser.parse_args(argv)

    maps_dir = (
        Path(args.maps_dir).expanduser() if args.maps_dir else map_service.maps_root()
    )
    maps_dir.mkdir(parents=True, exist_ok=True)
    replays_dir = (
        Path(args.replays_dir).expanduser()
        if args.replays_dir
        else replay_service.replays_root()
    )
    state_path = (
        Path(args.state_file).expanduser() if args.state_file else state_file_for(maps_dir)
    )

    server = EngineServer(
        maps_dir=maps_dir,
        replays_dir=replays_dir,
        port=args.port,
    )

    # Written only after the bind succeeded, so a reader never finds a state
    # file describing a server that never came up. It carries the token, so it
    # is created 0600 from the first byte — a write-then-chmod would leave a
    # window where the default umask governs.
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.unlink(missing_ok=True)  # 0o600 below applies only at creation
    fd = os.open(state_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "pid": os.getpid(),
                "port": server.port,
                "token": server.token,
                "maps_dir": str(maps_dir),
                "replays_dir": str(replays_dir),
                # Taken from the server rather than read again here: one read of
                # the environment is what makes this record and the server's own
                # ping answer the same source, instead of two answers that agree
                # only as long as nobody edits one of them.
                "source_id": server.source_id,
                "started": datetime.now(UTC).isoformat(timespec="seconds"),
            },
            handle,
            indent=2,
        )
        handle.write("\n")

    def _on_sigterm(signum: int, frame: Any) -> None:
        # shutdown() waits for serve_forever to exit, and serve_forever cannot
        # run while the main thread sits in this handler — so hand the call to
        # a thread and return.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _on_sigterm)

    print(f"Serving the 5E-compatible engine on {server.url}")
    print(f"Pages: {server.url} (index), {server.url}editor, {server.url}viewer")
    print(f"API: {server.url}api/v1/operations")
    print("Open the index in a browser; each page configures its own access token.")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state_path.unlink(missing_ok=True)
        server.close()
    return 0
