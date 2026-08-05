#!/usr/bin/env python3
"""End-to-end check of the plugin's HTTP engine and the ``fivee`` command.

This is the repository's automated end-to-end gate. It replaces the MCP
handshake check that went with the MCP server, and it makes the same kind of
claim the handshake made: not "the units pass" but "the thing a host actually
runs, runs".

Four properties are checked that the in-process suite structurally cannot:

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

    def json_call(self, method: str, path: str, body: Any = None) -> Any:
        """A request that must succeed, reduced to its payload."""
        status, payload, _ = self.call(method, path, body)
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

        # -- 6. the contract cannot drift ------------------------------------
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

        # -- 6b. and the examples that make an object-valued argument callable --
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

        # -- 7. and it all stops ---------------------------------------------
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
