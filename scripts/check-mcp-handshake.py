#!/usr/bin/env python3
"""End-to-end check of the plugin's MCP stdio server.

Spawns the real launcher and speaks raw newline-delimited JSON-RPC to it, which
checks two things the in-process tests cannot:

* the launcher resolves its environment and execs the server successfully;
* **stdout carries protocol traffic only**. Any stray print, warning, or progress
  line on stdout corrupts the stream, and the failure mode is a server that looks
  broken rather than merely noisy. Every line read here must parse as JSON.

Lives outside souroldgeezer-fivee-sim/ deliberately: it is a development check, not part of the
plugin payload that ships to installs.

Usage: python3 scripts/check-mcp-handshake.py
Exit code 0 means the handshake, tool listing, and sample calls all succeeded.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "souroldgeezer-fivee-sim" / "scripts" / "fivee-sim-mcp.sh"
PROTOCOL_VERSION = "2025-06-18"

EXPECTED_TOOLS = {
    "roll",
    "check",
    "save",
    "lookup_rule",
    "encounter_create",
    "encounter_state",
    "encounter_log",
    "encounter_act",
    "encounter_advance",
    "encounter_finalize",
    "encounter_list",
    "encounter_note",
    "encounter_resume",
    "replay_export",
    "replay_validate",
    "map_generate",
    "map_load",
    "map_save",
    "map_render",
    "map_edit",
    "map_query",
    "map_editor_serve",
    "map_editor_stop",
    "uvtt_export",
    "simulate_rounds",
    "simulate_dpr",
    "scenario_timing",
    "content_status",
    "content_configure",
    "content_validate",
}

failures: list[str] = []


def report(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)
        if detail:
            print(f"          | {detail}")


class Server:
    def __init__(self) -> None:
        self.process = subprocess.Popen(
            ["bash", str(LAUNCHER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def read(self) -> dict[str, Any]:
        """Read one line and require it to be JSON — this is the stdout purity check."""
        assert self.process.stdout is not None
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"server closed stdout. stderr:\n{self.stderr()}")
        try:
            parsed: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"non-JSON line on stdout: {line!r}") from error
        return parsed

    def request(self, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = self.read()
            if message.get("id") == request_id:
                return message

    def stderr(self) -> str:
        assert self.process.stderr is not None
        self.process.stderr.flush()
        return ""

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


def main() -> int:
    if not LAUNCHER.is_file():
        print(f"launcher not found at {LAUNCHER}")
        return 1

    print("=== MCP stdio handshake ===")
    server = Server()
    try:
        initialized = server.request(
            1,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "handshake-check", "version": "0"},
            },
        )
        result = initialized.get("result", {})
        report("serverInfo" in result, "initialize returns serverInfo", str(initialized)[:400])
        report(
            result.get("serverInfo", {}).get("name") == "souroldgeezer-fivee-sim",
            "server identifies as souroldgeezer-fivee-sim",
            str(result.get("serverInfo")),
        )

        server.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        listed = server.request(2, "tools/list", {})
        tools = {tool["name"] for tool in listed.get("result", {}).get("tools", [])}
        report(
            tools == EXPECTED_TOOLS,
            f"tools/list returns the {len(EXPECTED_TOOLS)} expected tools",
            f"missing={sorted(EXPECTED_TOOLS - tools)} unexpected={sorted(tools - EXPECTED_TOOLS)}",
        )
        described = [
            tool
            for tool in listed.get("result", {}).get("tools", [])
            if tool.get("description")
        ]
        report(
            len(described) == len(tools),
            "every tool carries a description",
            f"{len(tools) - len(described)} without one",
        )

        called = server.request(
            3,
            "tools/call",
            {"name": "lookup_rule", "arguments": {"topic": "prone"}},
        )
        payload = json.dumps(called.get("result", {}))
        report("attacked_with_advantage_in_melee" in payload, "lookup_rule('prone') resolves",
               payload[:300])

        rolled = server.request(
            4,
            "tools/call",
            {"name": "roll", "arguments": {"expression": "2d6+3", "seed": 42}},
        )
        payload = json.dumps(rolled.get("result", {}))
        report('"seed": 42' in payload or "'seed': 42" in payload,
               "roll echoes the seed it used", payload[:300])

        fight = server.request(
            5,
            "tools/call",
            {
                "name": "encounter_create",
                "arguments": {
                    "combatants": [
                        {"monster": "Goblin Warrior", "label": "Goblin", "position": 5},
                        {"monster": "Wolf", "label": "Wolf", "team": "party", "position": 0},
                    ],
                    "seed": 7,
                },
            },
        )
        payload = json.dumps(fight.get("result", {}))
        report("encounter_id" in payload, "encounter_create starts a fight", payload[:300])

        refused = server.request(
            6,
            "tools/call",
            {"name": "lookup_rule", "arguments": {"topic": "Beholder"}},
        )
        text = json.dumps(refused)
        report(
            "content_status" in text or refused.get("result", {}).get("isError"),
            "an unloaded lookup is refused rather than invented",
            text[:300],
        )

        status = server.request(
            7, "tools/call", {"name": "content_status", "arguments": {}}
        )
        status_text = json.dumps(status.get("result", {}))
        report(
            '"builtin": "include"' in status_text,
            "content_status reports the bundled slice by default",
            status_text[:300],
        )
    finally:
        server.close()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all handshake checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
