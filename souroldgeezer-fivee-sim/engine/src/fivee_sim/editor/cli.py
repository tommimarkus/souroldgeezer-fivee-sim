"""The ``fivee-sim-editor`` launcher and its state-file conventions.

Two callers start the editor: a developer at a shell, and the MCP server's
``map_editor_serve`` tool spawning ``python -m fivee_sim.editor`` detached.
Both find the running server the same way — through the **state file**, a small
JSON record ``{pid, port, token, maps_dir, started}`` written *after* the
socket is bound, next to the maps directory. The helpers that name, read, and
remove it live here so both sides share one convention rather than two
almost-identical ones.

The launcher loads content exactly as the MCP server does — configured packs
with a fall-back to the bundled slice — so a pack-defined terrain kind
validates identically over REST and over MCP. On SIGTERM it shuts the server
down gracefully and removes the state file; the token is printed nowhere, and
in particular never into a URL, because shell history and browser history both
outlive a launch. The served page configures itself.
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

from ..content import ContentError, builtin_mode, builtin_registry, load_packs
from ..kernel.grid import TerrainTable
from ..service import maps as map_service
from .http_server import EditorServer

__all__ = ["STATE_FILENAME", "main", "read_state", "state_file_for"]

#: The state file's name; it lives next to the maps directory (for the default
#: ``<project>/.fivee-sim/maps`` that means ``<project>/.fivee-sim/``).
STATE_FILENAME = "editor-server.json"


def state_file_for(maps_dir: str | Path) -> Path:
    """Where the launch state file for ``maps_dir`` lives: next to the maps dir."""
    return Path(maps_dir).expanduser().parent / STATE_FILENAME


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


def _terrain_table() -> TerrainTable:
    """The active terrain table, loaded the way the MCP server loads content.

    A pack the environment names but that will not load must not stop the
    editor from starting: the bundled slice loads instead and the failure goes
    to the log, mirroring the server's own fall-back.
    """
    try:
        return load_packs(builtin=builtin_mode()).terrain_effects
    except ContentError as error:
        print(f"fivee-sim-editor: falling back to bundled content: {error}", file=sys.stderr)
        return builtin_registry().terrain_effects


def main(argv: Sequence[str] | None = None) -> int:
    """Bind, write the state file, announce the URL, and serve until told to stop."""
    parser = argparse.ArgumentParser(
        prog="fivee-sim-editor",
        description="Serve the 5E-compatible map editor on localhost.",
    )
    parser.add_argument(
        "--maps-dir",
        default=None,
        help="directory the editor reads and writes maps in "
        "(default: the configured maps root)",
    )
    parser.add_argument(
        "--port", type=int, default=0, help="port to bind on 127.0.0.1 (default: ephemeral)"
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="where to record {pid, port, token, maps_dir, started} once bound "
        "(default: editor-server.json next to the maps directory)",
    )
    args = parser.parse_args(argv)

    maps_dir = (
        Path(args.maps_dir).expanduser() if args.maps_dir else map_service.maps_root()
    )
    maps_dir.mkdir(parents=True, exist_ok=True)
    state_path = (
        Path(args.state_file).expanduser() if args.state_file else state_file_for(maps_dir)
    )

    server = EditorServer(maps_dir=maps_dir, terrain=_terrain_table(), port=args.port)

    # Written only after the bind succeeded, so a reader never finds a state
    # file describing a server that never came up. It carries the token, so it
    # is not world-readable.
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "port": server.port,
                "token": server.token,
                "maps_dir": str(maps_dir),
                "started": datetime.now(UTC).isoformat(timespec="seconds"),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(state_path, 0o600)

    def _on_sigterm(signum: int, frame: Any) -> None:
        # shutdown() waits for serve_forever to exit, and serve_forever cannot
        # run while the main thread sits in this handler — so hand the call to
        # a thread and return.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _on_sigterm)

    print(f"Serving the map editor on {server.url}")
    print("Open it in a browser; the page configures its own access token.")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state_path.unlink(missing_ok=True)
        server.close()
    return 0
