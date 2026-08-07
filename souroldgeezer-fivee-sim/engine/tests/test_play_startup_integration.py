"""One real temporary-project startup through fivee, jq, and carry state."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
HELPER = PLUGIN_ROOT / "scripts" / "fivee-play.py"
FIVEE = PLUGIN_ROOT / "scripts" / "fivee.py"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fivee serve is POSIX-only")


def _run(command: list[str], *, stdin: bytes | None = None) -> dict[str, Any]:
    completed = subprocess.run(command, input=stdin, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    value = json.loads(completed.stdout)
    assert isinstance(value, dict)
    return value


def _fivee(config: Path, *tokens: str) -> dict[str, Any]:
    return _run([sys.executable, str(FIVEE), "--config", str(config), *tokens, "--compact"])


def _scene(name: str, foe: str) -> dict[str, Any]:
    return {
        "name": name,
        "content_paths": [],
        "combatants": [
            {
                "name": "Thora",
                "team": "party",
                "ac": 9,
                "max_hp": 1,
                "position": [30, 30],
            },
            {
                "monster": "Goblin Warrior",
                "label": foe,
                "team": "monsters",
                "position": [10, 0],
            },
        ],
        "seed": 1,
        "mode": "combat",
    }


def test_file_first_startup_and_later_carry_need_pregens_only_once(tmp_path: Path) -> None:
    project = tmp_path / "project"
    control = project / ".fivee-sim"
    for name in ("content", "maps", "scenes"):
        (control / name).mkdir(parents=True, exist_ok=True)
    config = control / "config.toml"
    config.write_text(
        """format_version = 1
[content]
paths = ["content"]
builtin = "include"
[storage]
maps = "maps"
scenes = "scenes"
runs = "runs"
""",
        encoding="utf-8",
    )
    (control / "scenes" / "opening.json").write_text(
        json.dumps(_scene("Opening", "Opening Goblin")), encoding="utf-8"
    )
    (control / "scenes" / "second.json").write_text(
        json.dumps(_scene("Second", "Second Goblin")), encoding="utf-8"
    )
    adventure = project / "adventure.md"
    adventure.write_text("# Scene: Opening\n\n## Encounter: Second\n", encoding="utf-8")
    party = project / "party.json"
    party.write_text(
        json.dumps(
            {
                "parties": {
                    "fixture": {
                        "members": [
                            {
                                "class": "Fighter",
                                "species": "Human",
                                "background": "Guard",
                                "gear": [],
                                "rules": {},
                                "temperament": "bold",
                                "voice": "brief",
                                "sheet": {
                                    "name": "Thora",
                                    "team": "party",
                                    "ac": 16,
                                    "max_hp": 12,
                                    "position": [0, 0],
                                    "attacks": [
                                        {
                                            "name": "Sword",
                                            "attack_bonus": 4,
                                            "damage": "1d8+2",
                                            "damage_type": "slashing",
                                            "kind": "melee",
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    adventure_id = ""
    try:
        started = _run(
            [
                sys.executable,
                str(HELPER),
                "init",
                "--config",
                str(config),
                "--adventure",
                str(adventure),
                "--mode",
                "play",
                "--seed",
                "71",
                "--gm-kind",
                "agent",
                "--party-file",
                str(party),
                "--party-id",
                "fixture",
                "--opening-scene",
                "opening",
            ]
        )
        allowed = {
            "schema_version",
            "status",
            "mode",
            "adventure_id",
            "artifact_id",
            "adventure_version",
            "source_sha256",
            "module_index_sha256",
            "content_generation",
            "content_counts",
            "seat_count",
            "paths",
            "encounter_id",
            "state_sha256",
            "map_sha256",
        }
        assert set(started) == allowed
        adventure_id = started["adventure_id"]
        assert started["artifact_id"] == adventure_id

        opening = _fivee(
            config,
            "--run",
            adventure_id,
            "encounter.state",
            started["encounter_id"],
        )
        opening_by_name = {entry["name"]: entry for entry in opening["combatants"]}
        assert opening_by_name["Thora"]["max_hp"] == 12
        assert set(opening_by_name) == {"Thora", "Opening Goblin"}

        _fivee(
            config,
            "--run",
            adventure_id,
            "encounter.finalize",
            started["encounter_id"],
            "--if-match",
            "*",
        )
        version = _fivee(
            config,
            "--run",
            adventure_id,
            "adventure.state",
            adventure_id,
            "--select",
            "version=/version",
        )["version"]
        party.unlink()

        scene = subprocess.Popen(
            [
                sys.executable,
                str(FIVEE),
                "--config",
                str(config),
                "--run",
                adventure_id,
                "scene.get",
                "second",
                "--compact",
            ],
            stdout=subprocess.PIPE,
        )
        assert scene.stdout is not None
        jq = subprocess.run(
            [
                "jq",
                "-e",
                "--argjson",
                "selected",
                '["Thora"]',
                (
                    "del(.name,.content_paths) "
                    "| .combatants |= [.[] "
                    '| (.name // .label // "") as $name '
                    "| select(($selected | index($name)) == null)] "
                    "| .carry = $selected"
                ),
            ],
            stdin=scene.stdout,
            capture_output=True,
            check=False,
        )
        scene.stdout.close()
        assert scene.wait() == 0
        assert jq.returncode == 0, jq.stderr.decode()
        linked = _run(
            [
                sys.executable,
                str(FIVEE),
                "--config",
                str(config),
                "--run",
                adventure_id,
                "adventure.encounter",
                adventure_id,
                "--if-match",
                str(version),
                "--idempotency-key",
                "fixture-second",
                "--seed",
                "72",
                "--mode",
                "combat",
                "--json",
                "-",
                "--select",
                "encounter_id=/encounter_id",
                "--select",
                "carried=/carried",
                "--select",
                "version=/version",
                "--compact",
            ],
            stdin=jq.stdout,
        )
        assert linked["carried"] == ["Thora"]
        second = _fivee(config, "--run", adventure_id, "encounter.state", linked["encounter_id"])
        second_by_name = {entry["name"]: entry for entry in second["combatants"]}
        assert set(second_by_name) == {"Thora", "Second Goblin"}
        assert second_by_name["Thora"]["max_hp"] == 12
        state = _fivee(config, "--run", adventure_id, "adventure.state", adventure_id)
        assert len(state["members"]) == 2
    finally:
        if adventure_id:
            subprocess.run(
                [
                    sys.executable,
                    str(FIVEE),
                    "--config",
                    str(config),
                    "--run",
                    adventure_id,
                    "stop",
                ],
                capture_output=True,
                check=False,
            )
        subprocess.run(
            [sys.executable, str(FIVEE), "--config", str(config), "stop"],
            capture_output=True,
            check=False,
        )
