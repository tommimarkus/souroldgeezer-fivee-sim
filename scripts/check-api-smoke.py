#!/usr/bin/env python3
"""End-to-end check of the plugin's HTTP engine and the ``fivee`` command.

This is the repository's automated end-to-end gate. It replaces the MCP
handshake check that went with the MCP server, and it makes the same kind of
claim the handshake made: not "the units pass" but "the thing a host actually
runs, runs".

Seven properties are checked that the in-process suite structurally cannot:

* **The real launcher boots.** Everything here goes through
  ``souroldgeezer-fivee-sim/scripts/fivee.py`` — never ``python -m
  fivee_sim.web``, never the development venv directly. If the launcher cannot
  resolve the engine source, this check is what says so.
* **A seeded fight is reproducible across processes.** Two independent servers
  run the same fight at the same seed and must produce identical results, down
  to the integrity hashes of the exported replay. Reproducibility under a seed
  is what every other test in this repository rests on, and nothing else checks
  it across process boundaries.
* **The REST surface is complete.** The same fight is then run a third time
  through the ``fivee`` binary as a subprocess. ``fivee_sim.client`` is pinned
  by ``tests/test_layering.py`` to import nothing from the engine but
  ``fivee_sim.paths``, so it can reach the engine only over HTTP — which makes a
  pass here evidence about the *contract*, not about the tests.
* **The contract cannot drift.** ``GET /api/v1/operations``, ``GET
  /api/v1/openapi.json`` and ``fivee help`` are rendered from one route table,
  and all three are checked against that table's own source here, outside
  pytest. That now includes the worked examples the table declares for its
  object-valued arguments: the source names which operations have one, the
  served document must carry one for each, and the line ``fivee help`` prints
  for one of them is pasted straight back into ``fivee`` and must come back
  zero. An example is the one piece of the contract whose only failure mode is
  being *wrong* rather than missing, so nothing short of running it is a check.
* **State outlives a fight.** A two-encounter adventure is then run whole: two
  wolves clear a room, the survivors are carried into the next one, both fights
  are finalized, and the run is composed into a single replay and closed. Every
  other case here begins and ends inside one encounter, so nothing else says
  that what a fight *ended* at is what the next one *starts* from — which is
  the only claim that makes a run of fights an adventure rather than a list.
* **A run can be more than its fights.** A second adventure is then run whole,
  and it opens on an **interlude**: the party walks across the mill's squares
  with no initiative rolled and nobody holding the floor, somebody speaks a line
  and somebody rolls a check that both land in that chapter's journal, and the
  ambush that follows starts on the squares the walk ended on, on the map it
  carried, with the party surprised by a condition the table imposed a chapter
  earlier. Only ``check-api-smoke`` runs all three chapters against the shipped
  surface and then composes them into one replay — and only here does the
  boundary between an interlude and a fight get crossed by a real party rather
  than by a fixture.
* **A saved fight starts.** A scene is written under an id, read back, listed,
  and then posted to ``encounter.create`` to start the fight it describes — the
  one thing that makes a scene a scene, since there is deliberately no
  ``scene.play`` and Play is that post. Whether the *stored* document is an
  ``encounter.create`` body is asked of ``routes.py``'s own declaration rather
  than of a list kept here, so the keys it will not take are named by the check
  instead of assumed by it, and the fight is then swung at once: a scene that
  round-trips and cannot be played has round-tripped nothing.

Standard library only, and deliberately so: it must run in an environment where
nothing has been built at all, which since the launcher stopped creating virtual
environments is every environment. It never imports the engine either —
importing it would test the copy on ``sys.path`` rather than the one the
launcher resolved.

Every server it starts is pointed at a fresh scratch project root, so a run
writes nothing into the repository's ``.fivee-sim/``, and every server and
scratch directory is removed even when a case fails. A leaked detached server
would make the next run lie.

Usage: python3 scripts/check-api-smoke.py
Exit code 0 means every case passed.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "souroldgeezer-fivee-sim"
LAUNCHER = PLUGIN_ROOT / "scripts" / "fivee.py"
ROUTES_SOURCE = PLUGIN_ROOT / "engine" / "src" / "fivee_sim" / "web" / "routes.py"

#: Wire protocol, shared with the server by agreement rather than by import —
#: the same reason ``fivee_sim.client.discovery`` copies it.
TOKEN_HEADER = "X-Fivee-Editor-Token"
API_PREFIX = "/api/v1"

#: A cold start may build the virtual environment, which the launcher bounds at
#: 300 seconds; every later call is talking to a built environment.
COLD_TIMEOUT = 420.0
WARM_TIMEOUT = 180.0

# --- the fight ---------------------------------------------------------------
#: One seed, one pair of SRD creatures, one scripted exchange. The constants
#: below are what this seed produces; they are golden values, so a change to the
#: rules or to the dice stream is meant to turn this red rather than pass
#: quietly.
SEED = 20260805
COMBATANTS: list[dict[str, Any]] = [
    {"monster": "Goblin Warrior", "label": "Goblin", "position": 5},
    {"monster": "Wolf", "label": "Wolf", "team": "party", "position": 0},
]
EXPECTED_ORDER = ["Goblin", "Wolf"]
EXPECTED_INITIATIVE = {"Goblin": 19, "Wolf": 7}
EXPECTED_MISS = "Scimitar: d20 [6] +4 = 10 vs AC 12 -> miss"
EXPECTED_HIT = "Bite: d20 [18] +4 = 22 vs AC 15 -> hit; damage [4] +2 = 6"
EXPECTED_DAMAGE = 6
EXPECTED_GOBLIN_HP = 4
EXPECTED_WOLF_HP = 11
EXPECTED_EVENTS = 11
EXPECTED_ATTACKS = ["Scimitar", "Bite"]

#: The confidentiality scenario ``encounter.brief`` exists to satisfy, written
#: out because this script may not import the engine. These seven are the
#: *scenario* — an opposing creature's own sheet, named as the design brief
#: names it — not a copy of the projection's bucket, which
#: ``tests/test_player_brief.py`` derives from the model and holds on its own.
WITHHELD_FROM_A_PLAYER = ("hp", "max_hp", "ac", "spell_slots", "items", "attacks", "spells")

#: The replay-v2 integrity hashes that two runs of one seeded fight must share.
#:
#: ``events`` and ``checkpoints`` are deliberately absent, and this is the one
#: place the check is narrower than "byte for byte". A v2 bundle stamps every
#: event and every checkpoint with ``datetime.now(UTC)``, because it is an audit
#: record of a fight that happened rather than a description of one that could —
#: so the whole-bundle sha256 is not reproducible and comparing it would be a
#: check that fails for the wrong reason. What remains covers the initial state,
#: every action taken, the final state, the map, and the content the fight ran
#: under: everything the seed is supposed to determine.
COMPARABLE_HASHES = ("map", "initial", "actions", "latest_state", "content")

# --- the adventure -----------------------------------------------------------
#: The second scenario, and the only one here that spans more than one fight.
#: Two seeds, four creatures, and a party that walks out of the first room into
#: the next one. Everything below is golden for these two seeds in a scratch
#: root nothing else has touched — which is also what makes ``adv-1``, ``enc-1``
#: and ``enc-2`` the ids this run is allocated.
ADVENTURE_NAME = "The Smoke Test Run"
ADVENTURE_SEEDS = (20260806, 20260807)
#: Two wolves against two goblins, and then whatever the survivors meet next.
#: Both fights are run to a conclusion rather than scripted to a fixed number of
#: swings, because what the second one starts from is what the first one *ended*
#: at — a fight stopped halfway would carry a party nobody had finished hurting.
ADVENTURE_ROSTER: list[dict[str, Any]] = [
    {"monster": "Wolf", "label": "Fang", "team": "party", "position": 0},
    {"monster": "Wolf", "label": "Scar", "team": "party", "position": 0},
    {"monster": "Goblin Warrior", "label": "Goblin", "position": 5},
    {"monster": "Goblin Warrior", "label": "Sneak", "position": 5},
]
ADVENTURE_NEWCOMER: list[dict[str, Any]] = [
    {"monster": "Skeleton", "label": "Skeleton", "position": 5}
]
EXPECTED_ADVENTURE_ID = "adv-1"
EXPECTED_CHAPTER_IDS = ["enc-1", "enc-2"]
EXPECTED_FIRST_ORDER = ["Goblin", "Fang", "Sneak", "Scar"]
EXPECTED_FIRST_INITIATIVE = {"Goblin": 16, "Fang": 14, "Sneak": 14, "Scar": 11}
EXPECTED_FIRST_ROUND = 3
EXPECTED_FIRST_HP = {"Goblin": 0, "Fang": 6, "Sneak": 0, "Scar": 11}
EXPECTED_FIRST_EVENTS = 35
#: Who the first fight leaves standing, and therefore who the second one is
#: handed. ``Fang`` is the whole point of the run: a party rebuilt from its
#: creation specs would arrive at ``WOLF_MAX_HP`` instead, so the case below
#: holds the arriving hit points against the *ending* ones rather than against a
#: second copy of the number, and refuses to pass on a party nobody hurt.
EXPECTED_CARRIED = ["Fang", "Scar"]
WOLF_MAX_HP = 11
EXPECTED_SECOND_ORDER = ["Skeleton", "Scar", "Fang"]
EXPECTED_SECOND_ROUND = 2
EXPECTED_SECOND_HP = {"Skeleton": 0, "Scar": 7, "Fang": 6}
EXPECTED_SECOND_EVENTS = 23

# --- the run that opens on an interlude --------------------------------------
#: The second adventure here, and the only scenario anywhere that records a beat
#: with no fight in it. A run does not have to open on a fight: this one opens on
#: a walk across the mill floor, is ambushed on the squares that walk ended on,
#: and closes standing over what is left — walk, fight, walk, in the order they
#: happened and in one composed replay.
#:
#: It is deliberately the *opening* chapter that is an interlude. An adventure
#: that could only start with initiative would make the arrival at the mill
#: something a table narrates outside the engine, which is the gap this whole
#: feature exists to close.
#:
#: Golden for these seeds in a scratch root nothing else has touched, which is
#: what makes ``adv-1`` and ``enc-1``..``enc-3`` the ids this run is allocated
#: and ``the-mill`` the only map ``carry_map`` could resolve to.
INTERLUDE_ADVENTURE_NAME = "A Night at the Drowned Mill"
INTERLUDE_SEEDS = (20260809, 20260820, 20260811)
MILL_MAP_ID = "the-mill"
#: Open floor and nothing else: the interlude's claim is about squares, and a
#: wall or a fixture would put a second reason in the way of a move that failed.
MILL_MAP: dict[str, Any] = {
    "format": "fivee-sim-map",
    "format_version": 1,
    "name": "the drowned mill",
    "grid": {"width": 8, "height": 8, "cell_feet": 5},
    "legend": {".": "normal"},
    "tiles": ["." * 8 for _ in range(8)],
    "provenance": {
        "generator": "hand",
        "seed": 1,
        "params": {},
        "edited": False,
        "source": "hand-authored for the end-to-end gate; 5E-compatible original content",
    },
}
#: The party, on squares rather than at a scalar distance, because a chapter
#: that carries the ground has to carry somewhere to stand on it.
INTERLUDE_PARTY: list[dict[str, Any]] = [
    {"monster": "Wolf", "label": "Fang", "team": "party", "position": [5, 5]},
    {"monster": "Wolf", "label": "Scar", "team": "party", "position": [5, 15]},
]
#: Where each of them walks, named per creature: these two squares are the whole
#: claim the opening chapter makes, since they are where the fight then starts.
INTERLUDE_WALK: dict[str, list[int]] = {"Fang": [25, 25], "Scar": [25, 15]}
#: Waiting on the square between the two of them, so the ambush is in reach of
#: both without anybody moving — this gate scripts swings, not tactics.
AMBUSHERS: list[dict[str, Any]] = [
    {"monster": "Goblin Boss", "label": "Stalker", "position": [30, 20]}
]
#: Surprise is a ruling rather than a mechanism: the condition that costs a
#: creature its Initiative roll, imposed by the table while the interlude is
#: still running — so it crosses the boundary the way every other condition
#: does — and lifted once the roll it cost them has been rolled, because the
#: roll is all it costs.
SURPRISE = "incapacitated"
NOTE_TEXT = "The wheel has stopped, and the water behind it is still."
NOTE_CATEGORY = "dialogue"
NOTE_SPEAKER = "Fang"
#: One audited check, scoped to the interlude. A roll made with no
#: ``encounter_id`` is a roll the record never hears about, which is exactly the
#: failure the skills are being taught out of.
INTERLUDE_CHECK: dict[str, Any] = {
    "modifier": 3, "dc": 12, "seed": 20260812, "skill": "perception",
}
EXPECTED_INTERLUDE_ADVENTURE_ID = "adv-1"
EXPECTED_INTERLUDE_CHAPTERS = ["enc-1", "enc-2", "enc-3"]
EXPECTED_INTERLUDE_MODES = ["exploration", "combat", "exploration"]
#: No initiative was rolled, so the roster is in the only order left: its own
#: names. ``turn`` is null beside it, and neither is a fight's answer.
EXPECTED_WALK_ORDER = ["Fang", "Scar"]
#: The check fails, and that is the run rather than a wrinkle in it: nobody
#: spots what is waiting, so the party is surprised on the next page.
EXPECTED_CHECK_DETAIL = "d20 [2] +3 = 5 vs DC 12"
#: What the ambush's initiative comes to with the party surprised, and the one
#: golden block here worth reading twice. Both wolves rolled at Disadvantage
#: because they were Surprised and the ambusher did not, which is the whole of
#: "surprise" in this engine. Recalibrating it means re-running the control that
#: chose this seed: at ``20260820`` an *unsurprised* party has Scar acting first
#: on a 21, and the ambusher last on a 10. A seed where the order comes out the
#: same either way would leave this case passing without saying anything.

EXPECTED_AMBUSH_ORDER = ["Stalker", "Fang", "Scar"]
EXPECTED_AMBUSH_INITIATIVE = {"Stalker": 22, "Fang": 18, "Scar": 10}
EXPECTED_AMBUSH_ROUND = 2
EXPECTED_AMBUSH_HP = {"Stalker": 0, "Fang": 5, "Scar": 11}
#: Who walks out of the mill, and therefore who the closing chapter is handed.
EXPECTED_SURVIVORS = ["Fang", "Scar"]

# --- the scene ---------------------------------------------------------------
#: The third scenario, and the only durable document here that is *input*. An
#: adventure and a replay are what a fight left behind; a scene is what one is
#: started from — a stored ``encounter.create`` body with a label to list it by.
#: So the claim this case makes is that the two agree: a scene read back off
#: disk starts the fight it describes, over the shipped surface, with nothing
#: between the two but a projection onto the keys the contract declares.
#:
#: Golden for this seed in a scratch root nothing else has touched, which is
#: also what makes ``enc-1`` the id this fight is allocated and ``the-ford`` the
#: only map the scene's ``map_id`` could resolve to.
SCENE_MAP_ID = "the-ford"
#: Small on purpose: a walled chamber with a strip of floor, big enough to stand
#: two creatures a square apart and nothing more. The scene names it by id
#: rather than carrying it inline, which is what the editor does with a map the
#: server already has — and it is the only reason ``map.put`` is called here.
SCENE_MAP: dict[str, Any] = {
    "format": "fivee-sim-map",
    "format_version": 1,
    "name": "the ford",
    "grid": {"width": 6, "height": 4, "cell_feet": 5},
    "legend": {".": "floor", "#": "wall"},
    "tiles": ["######", "#....#", "#....#", "######"],
    "provenance": {
        "generator": "hand",
        "seed": 1,
        "params": {},
        "edited": False,
        "source": "hand-authored for the end-to-end gate; 5E-compatible original content",
    },
}
SCENE_ID = "ambush-at-the-ford"
SCENE_NAME = "Ambush at the Ford"
SCENE_SEED = 20260808
#: Positions are feet along the axes rather than cells, so these two stand in
#: the chamber's second and third columns — a square apart, which is what makes
#: the opening bite a melee attack rather than a walk.
SCENE: dict[str, Any] = {
    "name": SCENE_NAME,
    "combatants": [
        {"monster": "Wolf", "label": "Wolf", "team": "party", "position": [5, 5]},
        {"monster": "Goblin Warrior", "label": "Goblin", "position": [10, 5]},
    ],
    "seed": SCENE_SEED,
    "map_id": SCENE_MAP_ID,
}
#: What a stored scene carries that ``encounter.create`` does not declare, and
#: therefore the whole difference between the two documents. Written out as the
#: golden value it is: the *set* is derived from ``routes.py`` at run time, and
#: this is what that derivation is held against, so a key that starts or stops
#: being an encounter's business turns this red rather than passing quietly.
SCENE_LABELS = ["name"]
EXPECTED_SCENE_ORDER = ["Wolf", "Goblin"]
EXPECTED_SCENE_INITIATIVE = {"Wolf": 20, "Goblin": 4}
EXPECTED_SCENE_PLACEMENT = {"Wolf": [5, 5], "Goblin": [10, 5]}
EXPECTED_SCENE_HIT = "Bite: d20 [12] +4 = 16 vs AC 15 -> hit; damage [6] +2 = 8"
EXPECTED_SCENE_HP = {"Wolf": 11, "Goblin": 2}
#: An envelope that is wrong in a way only the scene layer can see: the roster is
#: there and is not a roster. ``encounter.create`` would refuse it too, later and
#: in its own words, which is the point — this one never gets that far.
MALFORMED_SCENE_ID = "half-a-thought"
MALFORMED_SCENE: dict[str, Any] = {
    "name": "half a thought",
    "combatants": "the whole tavern",
}
MALFORMED_PROBLEM = "'combatants' must be a list of specs"

failures: list[str] = []


def report(ok: bool, label: str, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)
        if detail:
            print(f"          | {detail}")
    return ok


class SmokeError(RuntimeError):
    """Something the check needed did not work; the message says what."""


class Aborted(Exception):
    """A phase failed, was reported, and the cases after it cannot run."""


def phase(label: str, action: Callable[[], T]) -> T:
    """Run one phase of the check, reporting a failure under its own name.

    Without this a refusal in the middle of the fight would surface as one
    anonymous traceback, and the case that would have named it never runs. The
    phase is still fatal — every case after it depends on it — but it fails as
    itself.
    """
    try:
        result = action()
    except Exception as error:
        report(False, label, f"{type(error).__name__}: {error}")
        raise Aborted from None
    report(True, label)
    return result


class Engine:
    """One engine server, started through the real launcher, in its own root.

    Each instance owns a scratch project directory, so its maps, replays,
    encounter journals and launch state file are all inside a directory this
    check created and will remove. ``tempfile`` honours ``TMPDIR``, which is the
    only thing that makes this runnable where ``/tmp`` is read-only.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.root = Path(tempfile.mkdtemp(prefix=f"fivee-smoke-{name}-"))
        self.env = {**os.environ, "FIVEE_SIM_PROJECT_DIR": str(self.root)}
        self.port: int | None = None
        self.token: str | None = None
        self.stopped = False

    # -- the launcher, which is the only way anything here is started --------
    def launcher(
        self, *arguments: str, timeout: float = WARM_TIMEOUT
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - a fixed script with fixed arguments
            [sys.executable, str(LAUNCHER), *arguments],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=timeout,
            check=False,
        )

    def fivee(self, *arguments: str, timeout: float = WARM_TIMEOUT) -> Any:
        """One ``fivee`` command, as a shell would run it: stdout parsed as JSON."""
        done = self.launcher(*arguments, "--compact", timeout=timeout)
        if done.returncode != 0:
            raise SmokeError(
                f"`fivee {' '.join(arguments)}` exited {done.returncode}: "
                f"{done.stderr.strip()[-400:]}"
            )
        try:
            return json.loads(done.stdout)
        except json.JSONDecodeError as error:
            raise SmokeError(
                f"`fivee {' '.join(arguments)}` did not put JSON on stdout "
                f"({error}); stdout was {done.stdout[:200]!r}"
            ) from None

    @property
    def state_file(self) -> Path:
        return self.root / ".fivee-sim" / "fivee-sim-server.json"

    def adopt(self) -> None:
        """Read the port and token of whatever server now serves this root.

        The token is in the launch state file and nowhere else — it is printed
        into no URL and no log — so a caller speaking raw HTTP reads it the same
        way the client does.
        """
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        port, token = state.get("port"), state.get("token")
        if not isinstance(port, int) or not isinstance(token, str):
            raise SmokeError(f"the state file at {self.state_file} names no port and token")
        self.port, self.token = port, token

    def start(self, timeout: float = COLD_TIMEOUT) -> Any:
        """``fivee serve``: build the environment if need be, then bind."""
        served = self.fivee("serve", timeout=timeout)
        self.adopt()
        return served

    # -- raw HTTP, which is the point ---------------------------------------
    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> tuple[int, Any, dict[str, str]]:
        """One request against this server; a refusal is a status, not a raise."""
        if self.port is None or self.token is None:
            raise SmokeError("no server has been started for this root yet")
        data = None if body is None else json.dumps(body).encode("utf-8")
        sending = {TOKEN_HEADER: self.token}
        if data is not None:
            sending["Content-Type"] = "application/json"
        sending.update(headers or {})
        request = urllib.request.Request(  # noqa: S310 - a literal loopback URL
            f"http://127.0.0.1:{self.port}{API_PREFIX}{path}",
            data=data,
            headers=sending,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return _answer(response.status, response.read(), dict(response.headers))
        except urllib.error.HTTPError as error:
            return _answer(error.code, error.read(), dict(error.headers))
        except OSError as error:
            raise SmokeError(f"{method} {path} did not reach the server: {error}") from None

    def page(self, path: str, timeout: float = 30.0) -> tuple[int, str, dict[str, str]]:
        """One served page, fetched the way a browser would: no prefix, no token.

        Deliberately not ``call``: the pages sit outside ``/api/v1`` and outside
        the token guard, so reaching them through the API helper would prove
        neither. A browser opening the printed URL sends exactly this.
        """
        if self.port is None:
            raise SmokeError("no server has been started for this root yet")
        request = urllib.request.Request(  # noqa: S310 - a literal loopback URL
            f"http://127.0.0.1:{self.port}{path}", method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return (
                    response.status,
                    response.read().decode("utf-8", "replace"),
                    dict(response.headers),
                )
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace"), dict(error.headers)
        except OSError as error:
            raise SmokeError(f"GET {path} did not reach the server: {error}") from None

    def json_call(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> Any:
        """A request that must succeed, reduced to its payload."""
        status, payload, _ = self.call(method, path, body, headers)
        if not 200 <= status < 300:
            raise SmokeError(f"{method} {path} answered {status}: {json.dumps(payload)[:300]}")
        return payload

    # -- cleanup, which must happen whatever else did ------------------------
    def cleanup(self) -> None:
        """Stop the server and remove the scratch root. Safe to call twice."""
        if not self.stopped:
            self.stopped = True
            try:
                self.launcher("stop", "--compact", timeout=60.0)
            except (OSError, subprocess.SubprocessError):
                pass
            self._kill_leftover()
        shutil.rmtree(self.root, ignore_errors=True)

    def _kill_leftover(self) -> None:
        """A backstop for a server that did not answer its own shutdown."""
        state = self.state_file
        try:
            pid = json.loads(state.read_text(encoding="utf-8")).get("pid")
        except (OSError, ValueError):
            return
        if isinstance(pid, int):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        state.unlink(missing_ok=True)

    def answers(self) -> bool:
        """Whether anything still answers on this server's port."""
        if self.port is None or self.token is None:
            return False
        request = urllib.request.Request(  # noqa: S310 - a literal loopback URL
            f"http://127.0.0.1:{self.port}{API_PREFIX}/ping", headers={TOKEN_HEADER: self.token}
        )
        try:
            with urllib.request.urlopen(request, timeout=3.0):  # noqa: S310
                return True
        except (OSError, ValueError):
            return False


def _answer(status: int, raw: bytes, headers: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = raw.decode("utf-8", "replace")
    return status, payload, headers


# --- the fight, run two ways -------------------------------------------------
def _exported(bundle_export: dict[str, Any], written: dict[str, Any]) -> dict[str, Any]:
    """The two replay exports reduced to the parts a rerun must reproduce."""
    bundle = bundle_export.get("bundle")
    if not isinstance(bundle, dict):
        raise SmokeError("encounter.replay returned no inline bundle")
    integrity = bundle.get("integrity")
    if not isinstance(integrity, dict):
        raise SmokeError("the exported replay bundle carries no integrity block")
    return {
        "summary": {
            key: value for key, value in bundle_export.items()
            # ``sha256`` hashes the serialised bundle, timestamps and all.
            if key not in ("bundle", "sha256")
        },
        "integrity": {key: integrity.get(key) for key in COMPARABLE_HASHES},
        "engine_version": bundle.get("engine_version"),
        "format_version": bundle.get("format_version"),
        "written_to": Path(str(written.get("path", ""))).name,
        "bundle": bundle,
        "written": written,
    }


def attack_plan(state: Mapping[str, Any]) -> tuple[str, str]:
    """What the creature whose turn it is swings, and at whom.

    Read from the live state rather than written into the script, and that is
    deliberate: a defect that changed who wins initiative would otherwise make
    the fight illegal and abort the run, when what this check wants to say is
    *the fight came out differently*. The golden constants still pin the
    weapon, the roll and the damage, so a changed plan is caught — it is caught
    as a wrong result rather than as a crash.
    """
    actor = str(state["turn"])
    by_name = {str(one["name"]): one for one in state["combatants"]}
    attack = str(by_name[actor]["attacks"][0])
    target = next(str(name) for name in state["order"] if str(name) != actor)
    return attack, target


def fight_over_http(engine: Engine) -> dict[str, Any]:
    """Create, act, advance, act, advance, read, log, export — over plain HTTP."""
    status, created, headers = engine.call(
        "POST", "/encounters", {"combatants": COMBATANTS, "seed": SEED}
    )
    if status != 201:
        raise SmokeError(f"encounter.create answered {status}: {json.dumps(created)[:300]}")
    encounter_id = str(created["encounter_id"])
    base = f"/encounters/{encounter_id}"

    def swing(state: Mapping[str, Any]) -> Any:
        attack, target = attack_plan(state)
        return engine.json_call(
            "POST", f"{base}/actions", {"kind": "attack", "attack": attack, "target": target}
        )

    fight: dict[str, Any] = {
        "created": created,
        "created_status": status,
        "created_etag": headers.get("ETag", ""),
        "encounter_id": encounter_id,
    }
    fight["attack_missed"] = swing(created["state"])
    fight["advanced"] = engine.json_call("POST", f"{base}/advance", {})
    fight["attack_hit"] = swing(fight["advanced"]["state"])
    fight["advanced_again"] = engine.json_call("POST", f"{base}/advance", {})
    fight["state"] = engine.json_call("GET", base)
    fight["log"] = engine.json_call("GET", f"{base}/log")
    fight["replay"] = _exported(
        engine.json_call("POST", f"{base}/replay", {}),
        engine.json_call("POST", f"{base}/replay", {"embed": True}),
    )
    return fight


def fight_through_the_command(engine: Engine) -> dict[str, Any]:
    """The same fight, driven entirely by the ``fivee`` binary as a subprocess."""
    created = engine.fivee(
        "encounter.create",
        "--seed",
        str(SEED),
        "--json",
        json.dumps({"combatants": COMBATANTS}),
        timeout=COLD_TIMEOUT,
    )
    encounter_id = str(created["encounter_id"])

    def swing(state: Mapping[str, Any]) -> Any:
        attack, target = attack_plan(state)
        return engine.fivee(
            "encounter.act", encounter_id,
            "--kind", "attack", "--attack", attack, "--target", target,
        )

    fight: dict[str, Any] = {"created": created, "encounter_id": encounter_id}
    fight["attack_missed"] = swing(created["state"])
    fight["advanced"] = engine.fivee("encounter.advance", encounter_id)
    fight["attack_hit"] = swing(fight["advanced"]["state"])
    fight["advanced_again"] = engine.fivee("encounter.advance", encounter_id)
    fight["state"] = engine.fivee("encounter.state", encounter_id)
    fight["log"] = engine.fivee("encounter.log", encounter_id)
    fight["replay"] = _exported(
        engine.fivee("encounter.replay", encounter_id),
        engine.fivee("encounter.replay", encounter_id, "--embed"),
    )
    return fight


#: The keys both routes to the engine produce, and which are therefore
#: comparable. ``created_status`` and ``created_etag`` are HTTP-only facts, and
#: the replay contributes only the reproducible part of itself.
SHARED_KEYS = (
    "created",
    "attack_missed",
    "advanced",
    "attack_hit",
    "advanced_again",
    "state",
    "log",
)


def fingerprint(fight: dict[str, Any], engine: Engine) -> str:
    """One fight reduced to what two runs of it should share, exactly.

    Two things are genuinely allowed to differ between runs and are normalised
    away: the encounter's id, and the scratch directory the run happened in.
    Everything else — every roll, every hit point, every integrity hash the seed
    determines — is compared verbatim.
    """
    comparable = {key: fight[key] for key in SHARED_KEYS}
    comparable["replay"] = {
        key: fight["replay"][key]
        for key in ("summary", "integrity", "engine_version", "format_version", "written_to")
    }
    text = json.dumps(comparable, sort_keys=True)
    return text.replace(str(engine.root), "<root>").replace(fight["encounter_id"], "<encounter>")


def first_difference(left: str, right: str) -> str:
    """Where two fingerprints part company, with enough either side to read it."""
    for index, (one, other) in enumerate(zip(left, right, strict=False)):
        if one != other:
            start, end = max(0, index - 60), index + 60
            return f"...{left[start:end]}\n       vs ...{right[start:end]}"
    return f"one run is {len(left)} characters, the other {len(right)}"


# --- the adventure, run whole ------------------------------------------------
def swing_plan(state: Mapping[str, Any]) -> tuple[str, str] | None:
    """What the creature whose turn it is swings, and at which enemy.

    ``attack_plan``'s rule does not survive a fight of four: "whoever else is in
    the order" would have a wolf biting its own packmate. The plan here is the
    dumbest one that still finishes a fight — the first conscious enemy in
    initiative order — and it is deliberately not tactics, because the golden
    constants pin what this plan produces and cleverness would make a rules
    change read as a differently clever fight rather than as a wrong one.

    ``None`` when the actor cannot swing: a combatant at 0 hit points still
    takes turns, and what happens on that turn is a death save the stepper makes
    when the turn is advanced.
    """
    actor = str(state["turn"])
    by_name = {str(one["name"]): one for one in state["combatants"]}
    if not by_name[actor]["conscious"]:
        return None
    team = by_name[actor]["team"]
    target = next(
        (
            str(name)
            for name in state["order"]
            if by_name[str(name)]["team"] != team and by_name[str(name)]["conscious"]
        ),
        None,
    )
    return None if target is None else (str(by_name[actor]["attacks"][0]), target)


def fight_to_a_finish(
    engine: Engine, encounter_id: str, opening: Mapping[str, Any], limit: int = 40
) -> dict[str, Any]:
    """Swing and advance until one side has nobody left; answer the ending state.

    The scripted exchange above stops after two swings, which is all a
    determinism fingerprint needs. A chapter of an adventure has to actually
    *end*: ``encounter.finalize`` freezes whatever it finds, and the next
    chapter starts from exactly that. ``limit`` is a defect guard rather than a
    policy — a fight still going after it is one that is not going to stop.
    """
    base = f"/encounters/{encounter_id}"
    state: dict[str, Any] = dict(opening)
    for _turn in range(limit):
        if state["over"]:
            return state
        plan = swing_plan(state)
        if plan is not None:
            attack, target = plan
            state = engine.json_call(
                "POST", f"{base}/actions", {"kind": "attack", "attack": attack, "target": target}
            )["state"]
            if state["over"]:
                return state
        state = engine.json_call("POST", f"{base}/advance", {})["state"]
    raise SmokeError(f"the fight in {encounter_id} had not ended after {limit} turns")


def adventure_over_http(engine: Engine) -> dict[str, Any]:
    """A whole run: start it, link a fight, finish it, link the next, compose, close.

    Every write carries the ``ETag`` the previous one answered with, which is
    what a client holding a version does: ``If-Match`` is *required* on an
    adventure rather than optional, because the document is rewritten whole and
    two callers each told they linked would leave one fight in a run that
    acknowledged it.
    """
    status, created, headers = engine.call("POST", "/adventures", {"name": ADVENTURE_NAME})
    if status != 201:
        raise SmokeError(f"adventure.create answered {status}: {json.dumps(created)[:300]}")
    adventure_id = str(created["id"])
    base = f"/adventures/{adventure_id}"
    run: dict[str, Any] = {
        "created": created,
        "created_status": status,
        "adventure_id": adventure_id,
    }

    version = headers.get("ETag", "")
    first_body = {"combatants": ADVENTURE_ROSTER, "seed": ADVENTURE_SEEDS[0]}
    # The same link, sent with no version at all. Refused by the adapter before
    # the service is reached, so it starts no fight — and if it ever stopped
    # being refused, the run below would find the ids it expects already taken.
    run["unguarded"] = engine.call("POST", f"{base}/encounters", first_body)[:2]

    status, first, headers = engine.call(
        "POST", f"{base}/encounters", first_body, headers={"If-Match": version}
    )
    if status != 201:
        raise SmokeError(f"the first link answered {status}: {json.dumps(first)[:300]}")
    first_id = str(first["encounter_id"])
    run["first_link"], run["first_link_status"] = first, status
    run["first_opening"] = first["encounter"]["state"]
    run["first_ending"] = fight_to_a_finish(engine, first_id, run["first_opening"])
    run["first_log"] = engine.json_call("GET", f"/encounters/{first_id}/log")
    run["first_frozen"] = engine.json_call("POST", f"/encounters/{first_id}/finalize", {})

    # Who comes forward is read from the fight that just ended rather than
    # written here, so the constants stay a claim about the fight instead of an
    # instruction to it: whoever the party still has standing walks next door.
    standing = [
        str(one["name"])
        for one in run["first_ending"]["combatants"]
        if one["team"] == "party" and one["conscious"]
    ]
    run["standing"] = standing
    version = headers.get("ETag", "")
    status, second, headers = engine.call(
        "POST",
        f"{base}/encounters",
        {"combatants": ADVENTURE_NEWCOMER, "carry": standing, "seed": ADVENTURE_SEEDS[1]},
        headers={"If-Match": version},
    )
    if status != 201:
        raise SmokeError(f"the second link answered {status}: {json.dumps(second)[:300]}")
    second_id = str(second["encounter_id"])
    run["second_link"], run["second_link_status"] = second, status
    run["arrival"] = second["encounter"]["state"]
    run["second_ending"] = fight_to_a_finish(engine, second_id, run["arrival"])
    run["second_log"] = engine.json_call("GET", f"/encounters/{second_id}/log")
    run["second_frozen"] = engine.json_call("POST", f"/encounters/{second_id}/finalize", {})

    composed = engine.json_call("POST", f"{base}/replay", {})
    run["composed"] = composed
    # Read back off the disk it named, because there is nowhere else to read it:
    # a composition always writes a file and never inlines its envelope, one v2
    # bundle already exceeding the ceiling an inline export is answered under.
    run["envelope"] = json.loads(Path(str(composed["path"])).read_text(encoding="utf-8"))
    run["validated"] = engine.json_call("POST", "/replays/validate", {"bundle": run["envelope"]})

    # Still the version the *second link* answered with: composing a run reads
    # frozen files and writes a new one, so it must not have moved the document.
    run["closed"] = engine.json_call(
        "POST", f"{base}/finalize", {}, headers={"If-Match": headers.get("ETag", "")}
    )
    run["listed_active"] = engine.json_call("GET", "/adventures")
    run["listed_finalized"] = engine.json_call("GET", "/adventures?status=finalized")
    return run


# --- the run that opens on an interlude --------------------------------------
def interlude_run(engine: Engine) -> dict[str, Any]:
    """Walk, fight, walk: three chapters, one map, one party, one composed replay.

    Everything an interlude adds is exercised here in the order a table would
    reach for it — a chapter created in exploration mode, a beat opened by
    naming its actor, a line attributed to a speaker, a check audited against
    the chapter it was rolled in, and a boundary that carries both the ground
    and the squares the party was standing on when it was crossed.

    Nothing here decides whether a case passed. Every answer is collected and
    the reports below read them, which is what keeps a refusal in the middle of
    the run from being reported as the case after it.
    """
    run: dict[str, Any] = {}
    status, stored_map, _ = engine.call(
        "PUT", f"/maps/{MILL_MAP_ID}", MILL_MAP, headers={"If-Match": "*"}
    )
    if status != 201:
        raise SmokeError(f"map.put answered {status}: {json.dumps(stored_map)[:300]}")

    status, created, headers = engine.call(
        "POST", "/adventures", {"name": INTERLUDE_ADVENTURE_NAME}
    )
    if status != 201:
        raise SmokeError(f"adventure.create answered {status}: {json.dumps(created)[:300]}")
    adventure_id = str(created["id"])
    base = f"/adventures/{adventure_id}"
    run["adventure_id"] = adventure_id

    # -- chapter one: the walk ---------------------------------------------
    version = headers.get("ETag", "")
    status, opening, headers = engine.call(
        "POST",
        f"{base}/encounters",
        {
            "combatants": INTERLUDE_PARTY,
            "seed": INTERLUDE_SEEDS[0],
            "mode": "exploration",
            "map_id": MILL_MAP_ID,
        },
        headers={"If-Match": version},
    )
    if status != 201:
        raise SmokeError(f"the opening interlude answered {status}: {json.dumps(opening)[:300]}")
    walk_id = str(opening["encounter_id"])
    run["opening"], run["opening_status"] = opening, status
    run["opening_state"] = opening["encounter"]["state"]
    # Read back as well as answered, because ``map_source`` is the read's to
    # report: it says which saved file this chapter is standing on, and the
    # whole of ``carry_map`` is that the next chapter answers the same one.
    run["opening_read"] = engine.json_call("GET", f"/encounters/{walk_id}")
    run["started_on"] = {
        str(one["name"]): one["position"]
        for one in run["opening_state"]["combatants"]
    }

    # Each act names its own actor, because nothing rolled an order for them to
    # take turns in. Both moves are real moves across the map's squares: terrain
    # cost, occupancy and bounds all apply, which is what makes crossing the
    # mill floor a walk rather than a note about one.
    run["walked"] = [
        engine.json_call(
            "POST",
            f"/encounters/{walk_id}/actions",
            {"kind": "move", "actor": name, "to_position": square},
        )
        for name, square in INTERLUDE_WALK.items()
    ]
    run["note"] = engine.json_call(
        "POST",
        f"/encounters/{walk_id}/notes",
        {"text": NOTE_TEXT, "category": NOTE_CATEGORY, "speaker": NOTE_SPEAKER},
    )
    run["check"] = engine.json_call(
        "POST", "/dice/checks", {**INTERLUDE_CHECK, "encounter_id": walk_id}
    )

    # The ambush is sprung while the interlude is still the chapter running, so
    # the condition it imposes is carried across the boundary rather than
    # declared on the far side of it.
    run["surprised"] = [
        engine.json_call(
            "POST", f"/encounters/{walk_id}/conditions", {"target": name, "condition": SURPRISE}
        )
        for name in INTERLUDE_WALK
    ]
    run["walk_ending"] = engine.json_call("GET", f"/encounters/{walk_id}")
    run["walk_frozen"] = engine.json_call("POST", f"/encounters/{walk_id}/finalize", {})

    # -- chapter two: the ambush -------------------------------------------
    # Who crosses is read from the chapter that just ended, not written here:
    # the constants stay a claim about the run instead of an instruction to it.
    walkers = [
        str(one["name"])
        for one in run["walk_ending"]["combatants"]
        if one["team"] == "party" and one["conscious"]
    ]
    run["walkers"] = walkers
    version = headers.get("ETag", "")
    status, ambush, headers = engine.call(
        "POST",
        f"{base}/encounters",
        {
            "combatants": AMBUSHERS,
            "carry": walkers,
            "seed": INTERLUDE_SEEDS[1],
            "mode": "combat",
            "carry_map": True,
        },
        headers={"If-Match": version},
    )
    if status != 201:
        raise SmokeError(f"the ambush answered {status}: {json.dumps(ambush)[:300]}")
    ambush_id = str(ambush["encounter_id"])
    run["ambush"], run["ambush_status"] = ambush, status
    # The read rather than the link's own answer, for ``map_source`` again —
    # and taken before the surprise is lifted, because how the fight *started*
    # is what the boundary carried.
    run["arrival"] = engine.json_call("GET", f"/encounters/{ambush_id}")

    # Surprise has now cost them the one roll it costs. Lifted before the fight
    # is swung so that the party can act in it — and so that what the golden
    # initiative above records is a Disadvantaged roll rather than a fight two
    # creatures sat out.
    run["recovered"] = [
        engine.json_call(
            "POST",
            f"/encounters/{ambush_id}/conditions",
            {"target": name, "condition": SURPRISE, "applied": False},
        )
        for name in walkers
    ]
    run["ambush_ending"] = fight_to_a_finish(
        engine, ambush_id, engine.json_call("GET", f"/encounters/{ambush_id}")
    )
    run["ambush_frozen"] = engine.json_call("POST", f"/encounters/{ambush_id}/finalize", {})

    # -- chapter three: the aftermath --------------------------------------
    survivors = [
        str(one["name"])
        for one in run["ambush_ending"]["combatants"]
        if one["team"] == "party" and one["conscious"]
    ]
    run["survivors"] = survivors
    version = headers.get("ETag", "")
    status, closing, headers = engine.call(
        "POST",
        f"{base}/encounters",
        {
            # Nobody new: a chapter can be entirely the party the last one left
            # behind, which is what an aftermath is.
            "carry": survivors,
            "seed": INTERLUDE_SEEDS[2],
            "mode": "exploration",
            "carry_map": True,
        },
        headers={"If-Match": version},
    )
    if status != 201:
        raise SmokeError(f"the closing interlude answered {status}: {json.dumps(closing)[:300]}")
    closing_id = str(closing["encounter_id"])
    run["closing"], run["closing_status"] = closing, status
    run["aftermath"] = engine.json_call("GET", f"/encounters/{closing_id}")
    run["closing_frozen"] = engine.json_call("POST", f"/encounters/{closing_id}/finalize", {})

    composed = engine.json_call("POST", f"{base}/replay", {})
    run["composed"] = composed
    run["envelope"] = json.loads(Path(str(composed["path"])).read_text(encoding="utf-8"))
    run["validated"] = engine.json_call("POST", "/replays/validate", {"bundle": run["envelope"]})
    run["state"] = engine.json_call("GET", base)
    return run


# --- the scene, saved and then played ----------------------------------------
def scene_round_trip(engine: Engine) -> dict[str, Any]:
    """Save a map, save the fight that runs on it, read it back, and play it.

    The map goes first because a scene that names one is a scene whose
    ``map_id`` has to resolve, and a run that saved neither would validate
    against a directory it does not describe.

    Nothing here decides whether a case passed; every answer is collected and
    the reports below read them. Two of the calls are refusals asked for on
    purpose — a write with no version, and a write from a stale read — because
    a document two clients can both hold is only as safe as what it says no to.
    """
    run: dict[str, Any] = {}
    status, stored_map, _ = engine.call(
        "PUT", f"/maps/{SCENE_MAP_ID}", SCENE_MAP, headers={"If-Match": "*"}
    )
    if status != 201:
        raise SmokeError(f"map.put answered {status}: {json.dumps(stored_map)[:300]}")
    run["map_saved"] = stored_map

    # The same write, sent with no version at all. Refused by the adapter before
    # the service is reached, so it stores nothing — and if it ever stopped
    # being refused, the create below would find the id it expects already taken.
    run["unguarded"] = engine.call("PUT", f"/scenes/{SCENE_ID}", SCENE)[:2]

    status, saved, headers = engine.call(
        "PUT", f"/scenes/{SCENE_ID}", SCENE, headers={"If-Match": "*"}
    )
    if status != 201:
        raise SmokeError(f"scene.put answered {status}: {json.dumps(saved)[:300]}")
    run["saved"], run["saved_status"] = saved, status
    run["saved_etag"] = headers.get("ETag", "")

    status, document, headers = engine.call("GET", f"/scenes/{SCENE_ID}")
    if status != 200:
        raise SmokeError(f"scene.get answered {status}: {json.dumps(document)[:300]}")
    run["document"], run["document_etag"] = document, headers.get("ETag", "")

    # A version that was never this file's. Sent after the read rather than
    # before it, so the run holds the real one and this one is unambiguously
    # somebody else's.
    run["stale"] = engine.call(
        "PUT", f"/scenes/{SCENE_ID}", SCENE, headers={"If-Match": '"0"'}
    )[:2]
    run["listed"] = engine.json_call("GET", "/scenes")

    # Play, and the load-bearing step: a scene is a saved ``encounter.create``
    # body, so what starts the fight is what came back off the disk. The whole
    # document goes first — if the two are the same document, that is a 201 and
    # the projection below is a no-op; if they are not, this is where the
    # difference is named rather than quietly stepped around.
    run["posted_whole"] = engine.call("POST", "/encounters", document)[:2]
    declared = declared_body_keys("encounter.create")
    posted = {key: value for key, value in document.items() if key in declared}
    run["declared"] = sorted(declared)
    run["dropped"] = sorted(set(document) - set(posted))
    status, created, _ = engine.call("POST", "/encounters", posted)
    if status != 201:
        raise SmokeError(
            f"the stored scene would not start a fight: {status} {json.dumps(created)[:300]}"
        )
    run["started"], run["started_status"] = created, status
    encounter_id = str(created["encounter_id"])
    run["encounter_id"] = encounter_id

    # One swing, so the round trip is proved live rather than merely stored: a
    # fight that was created and never acted in has not shown that the roster a
    # file held is a roster the stepper can resolve.
    attack, target = attack_plan(created["state"])
    run["swing"] = engine.json_call(
        "POST",
        f"/encounters/{encounter_id}/actions",
        {"kind": "attack", "attack": attack, "target": target},
    )

    # And the envelope that is wrong, reported by one operation and refused by
    # the other. ``scene.validate`` is a report rather than a refusal — it
    # answers 200 with the problems it found, the shape ``map.validate`` uses —
    # so the problem+json refusal is ``scene.put``'s, and both are read.
    run["diagnosed"] = engine.json_call("POST", "/scenes/validate", MALFORMED_SCENE)
    run["refused"] = engine.call(
        "PUT", f"/scenes/{MALFORMED_SCENE_ID}", MALFORMED_SCENE, headers={"If-Match": "*"}
    )[:2]
    run["listed_after"] = engine.json_call("GET", "/scenes")
    return run


# --- the contract, read from its own source ----------------------------------
def repository_state() -> list[str]:
    """Every path under the repository's own ``.fivee-sim``, as a sorted list.

    A run that leaked a fight into the developer's game state would add entries
    here. Existence alone would not catch it: a checkout that has ever served
    the engine already has the directory.
    """
    root = REPO_ROOT / ".fivee-sim"
    if not root.exists():
        return []
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def declared_operations() -> set[str]:
    """Every contract operation the route table declares, parsed as source.

    Parsed rather than imported on purpose: this check must not put the engine
    on ``sys.path``, or it would be describing the copy it imported instead of
    the one the launcher built and is serving.
    """
    tree = ast.parse(ROUTES_SOURCE.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "Route":
            continue
        contract = True
        for keyword in node.keywords:
            if keyword.arg == "contract" and isinstance(keyword.value, ast.Constant):
                contract = bool(keyword.value.value)
        if not contract or len(node.args) < 3:
            continue
        name = node.args[2]
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            found.add(name.value)
    return found


def declared_examples() -> set[str]:
    """Every contract operation whose ``Route`` declares a worked example body.

    Read as source for the same reason the operations are, and read as keyword
    *presence* rather than as a value: the examples reference module constants
    that no literal evaluator can resolve, and the claim worth checking here is
    which operations have one — the value itself is the served document's to
    publish and the launcher's to run.
    """
    tree = ast.parse(ROUTES_SOURCE.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "Route":
            continue
        if not any(keyword.arg == "example" for keyword in node.keywords):
            continue
        if len(node.args) >= 3 and isinstance(node.args[2], ast.Constant):
            found.add(str(node.args[2].value))
    return found


def declared_body_keys(operation: str) -> set[str]:
    """The request-body keys one contract operation declares, parsed as source.

    Read the same way the operations are, and for a reason the scene case turns
    on: *is what a scene stored an ``encounter.create`` body?* is a question
    about the declaration the server was built from. Answering it from a list
    kept here would make the check agree with itself — a key added to
    ``encounter.create`` and not to a scene would be dropped from every fight a
    scene starts, silently, and the copy would still say the two agreed.

    An operation with no body, or one whose schema declares no ``properties``,
    yields the empty set; the caller reports that rather than proceeding, since
    an empty projection is indistinguishable from a parse that found nothing.
    """
    tree = ast.parse(ROUTES_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "Route":
            continue
        if len(node.args) < 3 or not isinstance(node.args[2], ast.Constant):
            continue
        if node.args[2].value != operation:
            continue
        for keyword in node.keywords:
            if keyword.arg != "body_schema" or not isinstance(keyword.value, ast.Dict):
                continue
            for key, value in zip(keyword.value.keys, keyword.value.values, strict=True):
                if not isinstance(key, ast.Constant) or key.value != "properties":
                    continue
                if not isinstance(value, ast.Dict):
                    continue
                return {
                    str(name.value)
                    for name in value.keys
                    if isinstance(name, ast.Constant)
                }
    return set()


def declared_pages() -> dict[str, tuple[str, str]]:
    """The ``PAGES`` table as source: served path -> (filename, content type).

    Read the same way as the operations, and for the same reason — the copy the
    launcher is serving is the only one worth checking. Deriving it also means a
    page added or moved is covered here without anyone remembering to add a
    case.
    """
    tree = ast.parse(ROUTES_SOURCE.read_text(encoding="utf-8"))
    found: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        target = getattr(node, "target", None)
        if not isinstance(node, ast.AnnAssign) or not isinstance(target, ast.Name):
            continue
        if target.id != "PAGES" or not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values, strict=True):
            if not isinstance(key, ast.Constant) or not isinstance(value, ast.Tuple):
                continue
            parts = [
                element.value for element in value.elts if isinstance(element, ast.Constant)
            ]
            if len(parts) >= 2:
                found[str(key.value)] = (str(parts[0]), str(parts[1]))
    return found


def interpreter_already_has_the_engine() -> bool:
    """Whether this interpreter can import ``fivee_sim`` without the launcher's help.

    The launcher's whole job is putting the engine on the import path, so an
    interpreter that already has it makes every case below pass against a
    launcher that resolved nothing — the same false green that a subprocess test
    using the development venv would give. Run this with a plain ``python3``.
    """
    probe = subprocess.run(  # noqa: S603 - this interpreter, one fixed argument
        [sys.executable, "-c", "import fivee_sim"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


def main() -> int:
    if not LAUNCHER.is_file():
        print(f"launcher not found at {LAUNCHER}")
        return 1
    if not ROUTES_SOURCE.is_file():
        print(f"route table not found at {ROUTES_SOURCE}")
        return 1
    if interpreter_already_has_the_engine():
        print(
            f"{sys.executable} already imports fivee_sim, so this check would pass\n"
            "whether or not the launcher resolves the engine. Run it with a plain\n"
            "python3, not from the development virtual environment."
        )
        return 1

    engines: list[Engine] = []
    repo_state_before = repository_state()
    started = time.monotonic()
    print("=== the launcher, the engine, and the fivee command ===")
    try:
        # -- 1. the launcher boots something that answers --------------------
        primary = Engine("primary")
        engines.append(primary)
        served = phase(
            "the launcher builds or finds its environment and binds a port", primary.start
        )
        report(
            isinstance(served.get("port"), int) and not served.get("already_running"),
            "the server it started is a fresh one, not one already running",
            json.dumps(served)[:300],
        )
        ping = primary.json_call("GET", "/ping")
        report(
            str(ping.get("maps_dir", "")).startswith(str(primary.root)),
            "the engine serves the scratch project root it was pointed at",
            f"maps_dir={ping.get('maps_dir')!r} root={primary.root}",
        )

        # -- 2. a token is not optional --------------------------------------
        status, refused, _ = primary.call("GET", "/ping", headers={TOKEN_HEADER: "wrong"})
        report(
            status == 401 and "type" in refused,
            "an unauthenticated request is refused as problem+json",
            f"status={status} body={json.dumps(refused)[:200]}",
        )

        # -- 3. the fight, over plain HTTP -----------------------------------
        reference = phase(
            "the scripted fight runs end to end over plain HTTP",
            lambda: fight_over_http(primary),
        )
        created = reference["created"]
        report(
            reference["created_status"] == 201
            and created["seed"] == SEED
            and created["state"]["order"] == EXPECTED_ORDER
            and {
                one["name"]: one["initiative"] for one in created["state"]["combatants"]
            } == EXPECTED_INITIATIVE,
            "encounter.create rolls the initiative this seed determines",
            json.dumps(created.get("state", {}).get("order")) + " " + json.dumps(
                {one["name"]: one["initiative"] for one in created["state"]["combatants"]}
            ),
        )
        report(
            bool(reference["created_etag"]),
            "the fight's version comes back as an ETag a write can be guarded with",
            f"etag={reference['created_etag']!r}",
        )

        missed = reference["attack_missed"]["events"][0]
        report(
            missed["detail"] == EXPECTED_MISS
            and missed["data"]["hit"] is False
            and missed["data"]["natural"] == 6,
            "the first attack resolves to the roll the seed determines, and misses",
            f"detail={missed['detail']!r}",
        )
        hit = reference["attack_hit"]["events"][0]
        report(
            hit["detail"] == EXPECTED_HIT
            and hit["data"]["hit"] is True
            and hit["data"]["damage"] == EXPECTED_DAMAGE,
            "the second attack hits and lands the damage the seed determines",
            f"detail={hit['detail']!r}",
        )
        report(
            reference["advanced"]["state"]["turn"] == "Wolf"
            and reference["advanced_again"]["state"]["round"] == 2,
            "advancing ends the turn, begins the next, and rolls the round over",
            f"after one={reference['advanced']['state']['turn']!r} "
            f"round after two={reference['advanced_again']['state']['round']}",
        )

        state = reference["state"]
        hp = {one["name"]: one["hp"] for one in state["combatants"]}
        report(
            state["round"] == 2
            and state["turn"] == "Goblin"
            and hp == {"Goblin": EXPECTED_GOBLIN_HP, "Wolf": EXPECTED_WOLF_HP},
            "encounter.state is authoritative about the damage that was taken",
            f"round={state['round']} turn={state['turn']!r} hp={hp}",
        )

        # The same fight from the other chair. ``tests/test_web_http.py`` owns
        # this claim against the true wire bytes; what this adds is that the
        # redaction is in the engine the *launcher* resolved, rather than in a
        # copy on some sys.path — the reason every other case here is run twice.
        # The payload is re-serialised rather than read raw, which costs the
        # check nothing: neither a nested key name nor a weapon's name can be
        # created or destroyed by json.dumps.
        seat = primary.json_call(
            "GET", f"/encounters/{reference['encounter_id']}/brief?as=Wolf"
        )
        rendered = json.dumps(seat, sort_keys=True)
        # Read defensively: a route wired to the wrong service function answers
        # a payload with none of these keys, and this case has to *report* that
        # rather than raise on it — a traceback names a dict key, not the
        # operation whose promise was broken.
        opposing = list(seat.get("enemies", []))
        leaked = sorted(
            key for one in opposing for key in WITHHELD_FROM_A_PLAYER if key in one
        )
        # A distinctive string, not a small integer: the goblin's 4 hit points
        # would collide with a position, an initiative or a round, and an
        # absence check that can be satisfied by accident is not one.
        weapon = EXPECTED_ATTACKS[0]
        report(
            seat.get("as") == "Wolf"
            and bool(opposing)
            and all("health" in one for one in opposing)
            and not leaked
            and weapon not in rendered
            and weapon in json.dumps(state),
            "encounter.brief briefs the seat and withholds the sheet state reports",
            f"opposing={[one['name'] for one in opposing]} leaked={leaked} "
            f"{weapon}_in_brief={weapon in rendered}",
        )

        log = reference["log"]
        landed = [
            entry["action"]["attack"]
            for entry in log["actions"]
            if entry.get("action") and entry["action"].get("attack")
        ]
        report(
            log["seed"] == SEED
            and log["total_events"] == EXPECTED_EVENTS
            and len(log["events"]) == EXPECTED_EVENTS
            and landed == EXPECTED_ATTACKS,
            "encounter.log replays every event and the actions that made them",
            f"seed={log['seed']} total_events={log['total_events']} attacks={landed}",
        )

        replay = reference["replay"]
        written = Path(str(replay["written"]["path"]))
        report(
            replay["summary"]["format"] == "fivee-sim-replay"
            and replay["summary"]["events"] == EXPECTED_EVENTS
            and replay["format_version"] == 2
            and written.is_file()
            and str(written).startswith(str(primary.root)),
            "encounter.replay exports the fight, inline and to a file in the scratch root",
            json.dumps(replay["summary"])[:200] + f" written={written}",
        )
        validated = primary.json_call("POST", "/replays/validate", {"bundle": replay["bundle"]})
        report(
            validated.get("valid") is True and not validated.get("errors"),
            "the exported bundle verifies its own integrity hashes",
            json.dumps(validated)[:300],
        )

        # -- 4. determinism, across two separate server processes ------------
        second = Engine("determinism")
        engines.append(second)

        def repeat() -> dict[str, Any]:
            second.start(timeout=WARM_TIMEOUT)
            return fight_over_http(second)

        repeated = phase("a second, independent server runs the same fight end to end", repeat)
        reference_print = fingerprint(reference, primary)
        repeated_print = fingerprint(repeated, second)
        report(
            reference_print == repeated_print,
            "the same fight at the same seed is identical in a second server",
            first_difference(reference_print, repeated_print),
        )
        report(
            reference["replay"]["integrity"] == repeated["replay"]["integrity"],
            "the exported replay hashes the same fight in both, hash for hash",
            f"{json.dumps(reference['replay']['integrity'])} vs "
            f"{json.dumps(repeated['replay']['integrity'])}",
        )

        # -- 5. the same fight through the binary ----------------------------
        commanded = Engine("command")
        engines.append(commanded)
        driven = phase(
            "the fivee command runs the same fight end to end, as a subprocess",
            lambda: fight_through_the_command(commanded),
        )
        driven_print = fingerprint(driven, commanded)
        report(
            reference_print == driven_print,
            "the fivee command produces the identical fight over the REST surface",
            first_difference(reference_print, driven_print),
        )

        # -- 6. a whole adventure: two fights, and the party between them ----
        adventuring = Engine("adventure")
        engines.append(adventuring)

        def whole_run() -> dict[str, Any]:
            adventuring.start(timeout=WARM_TIMEOUT)
            return adventure_over_http(adventuring)

        run = phase(
            "an adventure of two linked fights runs end to end over plain HTTP", whole_run
        )
        opened = run["created"]
        report(
            run["created_status"] == 201
            and opened["id"] == EXPECTED_ADVENTURE_ID
            and opened["format"] == "fivee-sim-adventure"
            and opened["name"] == ADVENTURE_NAME
            and opened["status"] == "active"
            and opened["members"] == []
            and bool(opened["version"]),
            "adventure.create starts an empty, active run in the scratch root",
            json.dumps({key: opened.get(key) for key in ("id", "name", "status", "members")}),
        )
        refused_status, refused_body = run["unguarded"]
        report(
            refused_status == 428
            and "If-Match is required" in str(refused_body.get("detail", "")),
            "a link that names no version of the run is refused as problem+json",
            f"status={refused_status} body={json.dumps(refused_body)[:200]}",
        )

        first = run["first_link"]
        opening = run["first_opening"]
        report(
            run["first_link_status"] == 201
            and first["index"] == 0
            and first["carried"] == []
            and first["encounter_id"] == EXPECTED_CHAPTER_IDS[0]
            and first["encounter"]["seed"] == ADVENTURE_SEEDS[0]
            and opening["order"] == EXPECTED_FIRST_ORDER
            and {
                one["name"]: one["initiative"] for one in opening["combatants"]
            } == EXPECTED_FIRST_INITIATIVE,
            "the run's first fight is linked at index 0 with nobody carried into it",
            f"index={first['index']} carried={first['carried']} "
            f"id={first['encounter_id']!r} order={opening['order']}",
        )
        ending = run["first_ending"]
        ending_hp = {str(one["name"]): one["hp"] for one in ending["combatants"]}
        report(
            ending["over"] is True
            and ending["winner"] == "party"
            and ending["round"] == EXPECTED_FIRST_ROUND
            and ending_hp == EXPECTED_FIRST_HP
            and run["first_log"]["total_events"] == EXPECTED_FIRST_EVENTS,
            "the first fight runs to the conclusion this seed determines",
            f"round={ending['round']} winner={ending['winner']!r} hp={ending_hp} "
            f"events={run['first_log']['total_events']}",
        )
        frozen = Path(str(run["first_frozen"]["replay_path"]))
        report(
            run["first_frozen"]["status"] == "finalized"
            and frozen.is_file()
            and str(frozen).startswith(str(adventuring.root)),
            "encounter.finalize freezes that fight's replay beside its own journal",
            json.dumps({key: run["first_frozen"].get(key) for key in ("status", "bytes")})
            + f" at {frozen}",
        )

        # The claim that makes a run of fights an adventure rather than two
        # fights. Held against what the *previous* fight ended at rather than
        # against a second copy of the number, and refusing to pass on a party
        # nobody hurt: a roster rebuilt from its creation specs would arrive
        # whole, which is exactly what a carried one must not do.
        arrival = run["arrival"]
        arrived = {
            str(one["name"]): one["hp"]
            for one in arrival["combatants"]
            if str(one["name"]) in EXPECTED_CARRIED
        }
        maxima = {
            str(one["name"]): one["max_hp"]
            for one in arrival["combatants"]
            if str(one["name"]) in EXPECTED_CARRIED
        }
        walked_out_on = {name: ending_hp[name] for name in EXPECTED_CARRIED if name in ending_hp}
        report(
            run["second_link_status"] == 201
            and run["second_link"]["index"] == 1
            and run["second_link"]["carried"] == EXPECTED_CARRIED
            and run["standing"] == EXPECTED_CARRIED
            and arrived == walked_out_on
            and maxima == dict.fromkeys(EXPECTED_CARRIED, WOLF_MAX_HP)
            and min(arrived.values(), default=WOLF_MAX_HP) < WOLF_MAX_HP,
            "the second fight starts its party where the first left them, not whole",
            f"carried={run['second_link']['carried']} arrived={arrived} "
            f"walked out on={walked_out_on} max={maxima}",
        )
        second_ending = run["second_ending"]
        second_hp = {str(one["name"]): one["hp"] for one in second_ending["combatants"]}
        report(
            second_ending["over"] is True
            and second_ending["winner"] == "party"
            and run["second_link"]["encounter"]["seed"] == ADVENTURE_SEEDS[1]
            and second_ending["round"] == EXPECTED_SECOND_ROUND
            and arrival["order"] == EXPECTED_SECOND_ORDER
            and second_hp == EXPECTED_SECOND_HP
            and run["second_log"]["total_events"] == EXPECTED_SECOND_EVENTS,
            "the second fight rolls its own initiative and reaches its own end",
            f"order={arrival['order']} round={second_ending['round']} hp={second_hp} "
            f"events={run['second_log']['total_events']}",
        )

        composed, envelope = run["composed"], run["envelope"]
        chapters = envelope.get("chapters", [])
        written = Path(str(composed["path"]))
        report(
            composed["format"] == "fivee-sim-adventure-replay"
            and composed["chapters"] == len(EXPECTED_CHAPTER_IDS)
            and composed["encounters"] == EXPECTED_CHAPTER_IDS
            and "bundle" not in composed
            and written.is_file()
            and str(written).startswith(str(adventuring.root))
            # The file names the run it composed, and says so in its own words:
            # every other case here reads the *result*, which would say all of
            # this about an envelope that had been written empty.
            and envelope.get("format") == composed["format"]
            and envelope.get("adventure", {}).get("id") == EXPECTED_ADVENTURE_ID
            and envelope.get("adventure", {}).get("name") == ADVENTURE_NAME,
            "adventure.replay writes the whole run to one file in the scratch root",
            json.dumps({key: composed.get(key) for key in ("format", "chapters", "encounters")})
            + f" written={written}",
        )
        report(
            [chapter.get("index") for chapter in chapters] == [0, 1]
            and [chapter.get("encounter_id") for chapter in chapters] == EXPECTED_CHAPTER_IDS
            and [chapter.get("carried") for chapter in chapters] == [[], EXPECTED_CARRIED]
            and [chapter.get("replay", {}).get("format") for chapter in chapters]
            == ["fivee-sim-replay"] * len(EXPECTED_CHAPTER_IDS)
            and [len(chapter.get("replay", {}).get("events", [])) for chapter in chapters]
            == [EXPECTED_FIRST_EVENTS, EXPECTED_SECOND_EVENTS],
            "its chapters are the two fights it named, in the order the run linked them",
            json.dumps(
                [
                    {key: chapter.get(key) for key in ("index", "encounter_id", "carried")}
                    for chapter in chapters
                ]
            )[:250],
        )
        report(
            run["validated"].get("valid") is True
            and not run["validated"].get("diagnostics")
            and run["validated"].get("error_count") == 0,
            "the composed run verifies its own hashes, and every chapter's with it",
            json.dumps(run["validated"])[:300],
        )

        closed = run["closed"]
        still_open = [str(entry["adventure_id"]) for entry in run["listed_active"]["adventures"]]
        finished = run["listed_finalized"]["adventures"]
        report(
            closed["status"] == "finalized"
            and EXPECTED_ADVENTURE_ID not in still_open
            and [str(entry["adventure_id"]) for entry in finished] == [EXPECTED_ADVENTURE_ID]
            and [entry["encounters"] for entry in finished] == [len(EXPECTED_CHAPTER_IDS)],
            "adventure.finalize closes the run and the listing moves it across",
            f"active={still_open} finalized={json.dumps(finished)[:200]}",
        )

        # -- 6b. a run that opens on an interlude: walk, ambush, aftermath ---
        exploring = Engine("interlude")
        engines.append(exploring)

        def walked_run() -> dict[str, Any]:
            exploring.start(timeout=WARM_TIMEOUT)
            return interlude_run(exploring)

        walk = phase(
            "a run of interlude, fight and interlude runs end to end over plain HTTP",
            walked_run,
        )
        opening_state = walk["opening_state"]
        report(
            walk["opening_status"] == 201
            and walk["opening"]["index"] == 0
            and walk["opening"]["encounter_id"] == EXPECTED_INTERLUDE_CHAPTERS[0]
            and opening_state["mode"] == "exploration"
            # The three absences that make it an interlude, asserted as
            # absences: no initiative was rolled, nobody holds the floor, and
            # the chapter is not over on arrival even though one team is all
            # there is. A party alone in a fight is a finished fight.
            and opening_state["turn"] is None
            and opening_state["order"] == EXPECTED_WALK_ORDER
            and opening_state["over"] is False
            and walk["opening_read"]["map_source"]["map_id"] == MILL_MAP_ID,
            "an adventure opens on a chapter with no initiative, no turn and no end",
            f"mode={opening_state.get('mode')!r} turn={opening_state.get('turn')!r} "
            f"order={opening_state.get('order')} over={opening_state.get('over')!r}",
        )

        walked_to = {
            str(one["name"]): one["position"]
            for one in walk["walk_ending"]["combatants"]
        }
        report(
            all(answer["state"]["mode"] == "exploration" for answer in walk["walked"])
            and walked_to == INTERLUDE_WALK
            and walk["started_on"] != walked_to
            and walk["walk_ending"]["over"] is False,
            "each act names its own actor, and the party crosses the mill's real squares",
            f"started on {walk['started_on']} walked to {walked_to}",
        )

        chapters = walk["envelope"].get("chapters", [])
        frozen_walk = chapters[0].get("replay", {}) if chapters else {}
        attempts = {
            str(attempt.get("operation")): attempt
            for attempt in frozen_walk.get("attempts", [])
        }
        spoken = attempts.get("encounter_note", {})
        rolled = attempts.get("check", {})
        report(
            walk["note"]["speaker"] == NOTE_SPEAKER
            and walk["check"]["encounter_id"] == EXPECTED_INTERLUDE_CHAPTERS[0]
            and walk["check"]["detail"] == EXPECTED_CHECK_DETAIL
            # And both of them in the frozen artifact, which is the only place
            # that says the record survives the chapter: a note the engine
            # answered and did not journal would read identically above.
            #
            # What survives is the *call*, not its answer. This line used to
            # read the check's own ``result.detail`` back out of the frozen
            # attempt; journal_version 2 keeps a result only for a call that
            # passed ``request_id`` and bought idempotency, and neither of
            # these did. A fight's outcomes are re-derived by replaying its
            # actions, so nothing is lost there — but a *primitive* is not
            # replayed, so this roll's face is now recorded nowhere and the
            # narrower claim below is all this artifact can still support.
            and spoken.get("arguments", {}).get("speaker") == NOTE_SPEAKER
            and spoken.get("arguments", {}).get("text") == NOTE_TEXT
            and rolled.get("arguments", {}).get("dc") == INTERLUDE_CHECK["dc"]
            and rolled.get("arguments", {}).get("seed") == INTERLUDE_CHECK["seed"]
            and rolled.get("status") == "success",
            "the line somebody spoke and the check somebody rolled freeze with the chapter",
            f"speaker={walk['note'].get('speaker')!r} check={walk['check'].get('detail')!r} "
            f"frozen={sorted(attempts)}",
        )

        arrival = walk["arrival"]
        arrived_on = {str(one["name"]): one["position"] for one in arrival["combatants"]}
        carried_conditions = {
            str(one["name"]): sorted(one["conditions"])
            for one in arrival["combatants"]
            if str(one["name"]) in walk["walkers"]
        }
        report(
            walk["ambush_status"] == 201
            and walk["ambush"]["carried"] == walk["walkers"]
            # The claim the opening chapter exists to make, held against the
            # interlude's *live* ending state rather than a second copy of the
            # squares: the party is ambushed exactly where it stopped walking.
            and {name: arrived_on[name] for name in walked_to} == walked_to
            and arrived_on["Stalker"] == AMBUSHERS[0]["position"]
            and arrival["map_source"]["map_id"] == MILL_MAP_ID
            and carried_conditions == dict.fromkeys(walk["walkers"], [SURPRISE])
            and arrival["order"] == EXPECTED_AMBUSH_ORDER
            and {
                one["name"]: one["initiative"] for one in arrival["combatants"]
            } == EXPECTED_AMBUSH_INITIATIVE,
            "the fight starts on the squares the walk ended on, with the party surprised",
            f"arrived on {arrived_on} conditions={carried_conditions} "
            f"order={arrival.get('order')} initiative="
            + json.dumps({
                str(one["name"]): one["initiative"] for one in arrival["combatants"]
            }),
        )

        ambush_ending = walk["ambush_ending"]
        ambush_hp = {str(one["name"]): one["hp"] for one in ambush_ending["combatants"]}
        aftermath = walk["aftermath"]
        aftermath_hp = {
            str(one["name"]): one["hp"]
            for one in aftermath["combatants"]
            if str(one["name"]) in walk["survivors"]
        }
        walked_out_on = {name: ambush_hp[name] for name in walk["survivors"]}
        report(
            ambush_ending["over"] is True
            and ambush_ending["winner"] == "party"
            and ambush_ending["round"] == EXPECTED_AMBUSH_ROUND
            and ambush_hp == EXPECTED_AMBUSH_HP
            and walk["survivors"] == EXPECTED_SURVIVORS
            # The second boundary, and the one where hit points are what
            # crosses: the aftermath starts the party at what the fight left
            # them at, and somebody in it is provably not whole.
            and aftermath_hp == walked_out_on
            and min(aftermath_hp.values(), default=WOLF_MAX_HP) < WOLF_MAX_HP
            and aftermath["mode"] == "exploration"
            and aftermath["turn"] is None
            and aftermath["map_source"]["map_id"] == MILL_MAP_ID,
            "the ambush ends, and the closing interlude starts the party where it left them",
            f"round={ambush_ending['round']} hp={ambush_hp} "
            f"aftermath={aftermath_hp} walked out on={walked_out_on}",
        )

        composed = walk["composed"]
        report(
            composed["chapters"] == len(EXPECTED_INTERLUDE_CHAPTERS)
            and composed["encounters"] == EXPECTED_INTERLUDE_CHAPTERS
            and [chapter.get("index") for chapter in chapters] == [0, 1, 2]
            and [chapter.get("mode") for chapter in chapters] == EXPECTED_INTERLUDE_MODES
            # The chapter record and the frozen bundle inside it must agree
            # about which kind of chapter it was — the record copies the bundle
            # rather than re-deriving it, and this is what says so.
            and [
                chapter.get("replay", {}).get("encounter", {}).get("mode")
                for chapter in chapters
            ] == EXPECTED_INTERLUDE_MODES
            and [chapter.get("carried") for chapter in chapters]
            == [[], walk["walkers"], walk["survivors"]]
            and walk["validated"].get("valid") is True
            and walk["validated"].get("error_count") == 0,
            "the composed run is three chapters, and each of them says which kind it is",
            json.dumps([
                {key: chapter.get(key) for key in ("index", "encounter_id", "mode")}
                for chapter in chapters
            ])[:250] + " " + json.dumps(walk["validated"])[:120],
        )
        report(
            [member["mode"] for member in walk["state"]["members"]]
            == EXPECTED_INTERLUDE_MODES
            and [str(member["encounter_id"]) for member in walk["state"]["members"]]
            == EXPECTED_INTERLUDE_CHAPTERS,
            "the run's own state reports the shape of it without opening a chapter",
            json.dumps(
                [
                    {key: member.get(key) for key in ("encounter_id", "mode")}
                    for member in walk["state"]["members"]
                ]
            )[:250],
        )

        # -- 7. a scene: the fight a table saved, read back and played -------
        staging = Engine("scene")
        engines.append(staging)

        def saved_fight() -> dict[str, Any]:
            staging.start(timeout=WARM_TIMEOUT)
            return scene_round_trip(staging)

        scene = phase(
            "a scene is saved, read back, and started as a fight over plain HTTP",
            saved_fight,
        )
        stored = scene["saved"]
        scene_file = Path(str(stored.get("path", "")))
        report(
            scene["saved_status"] == 201
            and stored["saved"] is True
            and stored["scene_id"] == SCENE_ID
            and stored["name"] == SCENE_NAME
            and stored["combatants"] == len(SCENE["combatants"])
            and stored["warnings"] == []
            and scene_file.is_file()
            and str(scene_file).startswith(str(staging.root))
            and scene["saved_etag"] == f'"{stored["sha256"]}"',
            "scene.put stores the fight under an id and answers its version as an ETag",
            json.dumps({key: stored.get(key) for key in ("scene_id", "name", "combatants")})
            + f" etag={scene['saved_etag']!r} written={scene_file}",
        )
        unversioned_status, unversioned_body = scene["unguarded"]
        report(
            unversioned_status == 428
            and "If-Match is required" in str(unversioned_body.get("detail", "")),
            "a scene write that names no version of it is refused as problem+json",
            f"status={unversioned_status} body={json.dumps(unversioned_body)[:200]}",
        )
        report(
            scene["document"] == SCENE and scene["document_etag"] == scene["saved_etag"],
            "scene.get returns the document that was stored, under that same version",
            f"read back {json.dumps(scene['document'], sort_keys=True)[:200]} at "
            f"{scene['document_etag']!r}, stored at {scene['saved_etag']!r}",
        )
        stale_status, stale_body = scene["stale"]
        report(
            stale_status == 409
            and "has advanced since you read it" in str(stale_body.get("detail", "")),
            "a scene write from a stale read is refused rather than merged",
            f"status={stale_status} body={json.dumps(stale_body)[:200]}",
        )
        rows = scene["listed"]["scenes"]
        report(
            [str(row["id"]) for row in rows] == [SCENE_ID]
            and rows[0]["name"] == SCENE_NAME
            and rows[0]["seed"] == SCENE_SEED
            and rows[0]["map_id"] == SCENE_MAP_ID
            and rows[0]["combatants"] == len(SCENE["combatants"])
            and rows[0]["inline_map"] is False,
            "scene.list names the saved scene, its seed, and the map it runs on",
            json.dumps(rows)[:250],
        )

        # The seam the whole design rests on, and the reason this case exists:
        # a scene is a saved ``encounter.create`` body plus the label it is
        # listed by. The label is the *only* difference, and that is asserted in
        # both directions — the whole document is refused and names it, and the
        # projection onto what the route table declares drops nothing else. A
        # key that quietly stopped being an encounter's business would widen
        # ``dropped`` and fail here rather than vanish from every saved fight.
        whole_status, whole_body = scene["posted_whole"]
        detail = str(whole_body.get("detail", ""))
        report(
            bool(scene["declared"])
            and scene["dropped"] == SCENE_LABELS
            and whole_status == 400
            and "unknown key(s)" in detail
            and all(repr(label) in detail for label in SCENE_LABELS),
            "the label a scene is listed by is the only key encounter.create will not take",
            f"declared={scene['declared']} dropped={scene['dropped']} "
            f"status={whole_status} detail={detail[:200]}",
        )

        launched = scene["started"]
        opening = launched["state"]
        source = launched.get("map_source") or {}
        placed = {str(one["name"]): one["position"] for one in opening["combatants"]}
        rolled = {str(one["name"]): one["initiative"] for one in opening["combatants"]}
        report(
            scene["started_status"] == 201
            and launched["seed"] == SCENE_SEED
            and source.get("map_id") == SCENE_MAP_ID
            and source.get("sha256") == scene["map_saved"]["sha256"]
            and opening["order"] == EXPECTED_SCENE_ORDER
            and rolled == EXPECTED_SCENE_INITIATIVE
            and placed == EXPECTED_SCENE_PLACEMENT,
            "the stored scene starts the fight it describes, on the map it named",
            f"order={opening['order']} map={source.get('map_id')!r} placed={placed} "
            f"initiative={rolled}",
        )
        swung = scene["swing"]["events"][0]
        after = {str(one["name"]): one["hp"] for one in scene["swing"]["state"]["combatants"]}
        report(
            swung["detail"] == EXPECTED_SCENE_HIT
            and swung["data"]["hit"] is True
            and after == EXPECTED_SCENE_HP,
            "that fight takes its first action, at the seed the scene saved",
            f"detail={swung['detail']!r} hp={after}",
        )

        diagnosed = scene["diagnosed"]
        errors = diagnosed.get("errors", [])
        report(
            diagnosed.get("ok") is False
            and [str(one.get("problem", "")) for one in errors] == [MALFORMED_PROBLEM]
            and [str(one.get("field", "")) for one in errors] == ["combatants"]
            and [str(one.get("field", "")) for one in diagnosed.get("warnings", [])] == ["seed"],
            "scene.validate reports what an envelope got wrong without storing it",
            json.dumps(diagnosed)[:300],
        )
        refused_status, refused_scene = scene["refused"]
        refusal = str(refused_scene.get("detail", ""))
        report(
            refused_status == 400
            and f"scene {MALFORMED_SCENE_ID!r} cannot be saved" in refusal
            and MALFORMED_PROBLEM in refusal
            and [str(row["id"]) for row in scene["listed_after"]["scenes"]] == [SCENE_ID],
            "a malformed scene is refused as problem+json and never reaches the listing",
            f"status={refused_status} detail={refusal[:200]} "
            f"listed={[row['id'] for row in scene['listed_after']['scenes']]}",
        )

        # -- 8. the contract cannot drift ------------------------------------
        declared = declared_operations()
        index = primary.json_call("GET", "/operations")
        listed = {str(entry["operation"]) for entry in index["operations"]}
        report(
            bool(declared) and listed == declared,
            f"GET /operations lists every one of the route table's {len(declared)} operations",
            f"missing={sorted(declared - listed)} unexpected={sorted(listed - declared)}",
        )
        report(
            index.get("count") == len(listed) and index.get("base") == API_PREFIX,
            "the operations index counts itself correctly and names its version prefix",
            json.dumps({"count": index.get("count"), "base": index.get("base")}),
        )

        document = primary.json_call("GET", "/openapi.json")
        described = {
            str(operation.get("operationId", ""))
            for methods in document.get("paths", {}).values()
            for operation in methods.values()
        }
        expected_ids = {_operation_id(name) for name in declared}
        report(
            document.get("openapi", "").startswith("3.1") and described == expected_ids,
            "GET /openapi.json describes every one of them and nothing else",
            f"openapi={document.get('openapi')!r} "
            f"missing={sorted(expected_ids - described)} "
            f"unexpected={sorted(described - expected_ids)}",
        )

        helped = primary.launcher("help")
        unrendered = sorted(name for name in declared if name not in helped.stdout)
        report(
            helped.returncode == 0 and not unrendered,
            "fivee help renders every operation the server serves",
            f"exit={helped.returncode} unrendered={unrendered}",
        )

        # -- 8b. and the examples that make an object-valued argument callable --
        exemplified = declared_examples()
        by_id = {
            str(operation.get("operationId", "")): operation
            for methods in document.get("paths", {}).values()
            for operation in methods.values()
        }
        unpublished = sorted(
            name
            for name in exemplified
            if by_id.get(_operation_id(name), {})
            .get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("example")
            is None
        )
        report(
            bool(exemplified) and not unpublished,
            f"the served document carries the worked example all "
            f"{len(exemplified)} of those operations declare",
            f"declared={sorted(exemplified)} unpublished={unpublished}",
        )

        # The claim an example exists to make, and the only way to check it:
        # take the line off the printed page and run it. Anything less would
        # pass against an example that is merely present.
        shown = primary.launcher("help", "encounter.create")
        printed = [
            line.strip()
            for line in shown.stdout.splitlines()
            if line.strip().startswith("fivee encounter.create ")
        ]
        pasted = printed[-1].partition("--json ")[2].strip().strip("'") if printed else ""
        ran = primary.launcher("encounter.create", "--json", pasted, "--compact")
        report(
            "encounter.create" in exemplified
            and bool(pasted)
            and ran.returncode == 0
            and bool(_parsed(ran.stdout).get("encounter_id")),
            "the example fivee help prints is pasted back into fivee and answered",
            f"example={pasted[:180]!r} exit={ran.returncode} "
            f"stderr={ran.stderr.strip()[-200:]!r}",
        )

        pages = declared_pages()
        # Before walking the table, prove the table was read. `declared_pages`
        # parses source, and a parse that quietly returns nothing would run
        # zero page cases and report nothing at all — a gate that covers
        # nothing looks exactly like a gate that passed.
        report(
            {"/", "/editor", "/viewer"} <= set(pages),
            "the route table declares the three pages this check walks",
            f"declared: {sorted(pages)}",
        )
        answered = {}
        for path, (filename, content_type) in sorted(pages.items()):
            status, text, headers = primary.page(path)
            answered[path] = (status, headers.get("Content-Type", ""), text)
            report(
                status == 200 and headers.get("Content-Type", "") == content_type,
                f"GET {path} serves {filename}",
                f"status={status} content-type={headers.get('Content-Type')!r}",
            )
        # Fails closed: an absent page is an empty body, and two empty bodies
        # must not read as "two different documents".
        root_body = answered.get("/", (0, "", ""))[2]
        editor_body = answered.get("/editor", (0, "", ""))[2]
        report(
            bool(root_body) and bool(editor_body) and root_body != editor_body,
            "the root page and the editor are two different documents",
            "identical bytes" if root_body == editor_body else "one of them served nothing",
        )
        report(
            "__FIVEE_EDITOR__" in root_body
            and primary.token is not None
            and primary.token in root_body,
            "the landing page is configured with this launch's own token",
            "the injected config is absent from GET /",
        )

        # -- 9. and it all stops ---------------------------------------------
        for engine in engines:
            engine.cleanup()
        report(
            not any(engine.answers() for engine in engines),
            "every server this check started stops answering when told to",
            f"still up: {[engine.name for engine in engines if engine.answers()]}",
        )
        repo_state_after = repository_state()
        report(
            repo_state_after == repo_state_before,
            "nothing was written into the repository's own .fivee-sim",
            f"appeared: {sorted(set(repo_state_after) - set(repo_state_before))[:8]}",
        )
    except Aborted:
        pass  # already reported under the phase that failed
    except Exception as error:  # a broken case must still clean up and tally
        report(False, "the check ran to completion", f"{type(error).__name__}: {error}")
    finally:
        for engine in engines:
            engine.cleanup()

    print()
    print(f"{len(failures)} of the cases above failed. ({time.monotonic() - started:.0f}s)"
          if failures else f"all cases passed. ({time.monotonic() - started:.0f}s)")
    if failures:
        for label in failures:
            print(f"  - {label}")
        return 1
    return 0


def _parsed(text: str) -> dict[str, Any]:
    """One command's stdout as JSON, or an empty document when it is not.

    A failing command prints nothing on stdout by design, and a check that
    raised on that would report a decode error where the interesting fact is
    the exit code its own case is already reading.
    """
    try:
        found = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return found if isinstance(found, dict) else {}


def _operation_id(operation: str) -> str:
    """``encounter.act`` -> ``encounterAct``; the same rule the server applies."""
    words = [word for word in operation.replace("-", ".").replace("_", ".").split(".") if word]
    return words[0] + "".join(word[:1].upper() + word[1:] for word in words[1:])


if __name__ == "__main__":
    sys.exit(main())
