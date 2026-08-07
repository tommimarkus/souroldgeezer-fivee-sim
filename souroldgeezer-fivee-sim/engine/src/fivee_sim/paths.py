"""Where the engine reads and writes, resolved in one place.

Maps, replays, scenes, encounter journals and blobs each answer the same
question — *which directory* — with the same three-step rule: the surface's own
environment variable wins outright, then the project directory, then the current
one. That rule was written out four times, in :mod:`fivee_sim.service.maps`,
:mod:`fivee_sim.service.replay`, :mod:`fivee_sim.service.encounter_journal` and
:mod:`fivee_sim.web.cli`, which is three chances for one of them to drift
from the others.

It lives here instead, beside the other cross-cutting root modules: this is
neither a rules primitive nor creature state, and it is imported by
``service/`` and ``editor/`` alike while no rules layer touches it. The old
names remain importable from the modules that used to define them, so a caller
that already knows where to ask keeps working.

The launch state file follows the same ownership: its name, location and
tolerant JSON reader live together here. The client and server launcher
re-export the reader, preserving their existing surfaces without maintaining
two copies of the file convention.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .configuration import Configuration
from .content import CLAUDE_PROJECT_ENV, PROJECT_ENV

__all__ = [
    "ADVENTURES_ENV",
    "ADVENTURES_SUBDIR",
    "BLOBS_ENV",
    "BLOBS_SUBDIR",
    "ENCOUNTERS_ENV",
    "ENCOUNTERS_SUBDIR",
    "MAPS_ENV",
    "MAPS_SUBDIR",
    "REPLAYS_ENV",
    "REPLAYS_SUBDIR",
    "RUNS_ENV",
    "RUNS_SUBDIR",
    "SCENES_ENV",
    "SCENES_SUBDIR",
    "SOURCE_ID_ENV",
    "STATE_FILENAME",
    "RunSelectionError",
    "StorageLayout",
    "adventures_root",
    "blobs_root",
    "encounters_root",
    "environment_replay_roots",
    "environment_roots",
    "maps_root",
    "project_root",
    "read_state",
    "replays_root",
    "runs_root",
    "scenes_root",
    "source_id",
    "state_file_for",
    "storage_layout",
]

#: Environment variable holding an ``os.pathsep``-separated list of map files
#: or directories — the maps analogue of ``FIVEE_SIM_CONTENT``.
MAPS_ENV = "FIVEE_SIM_MAPS"
#: Where maps live inside a project when nothing else is configured.
MAPS_SUBDIR = Path(".fivee-sim") / "maps"

#: Environment variable holding an ``os.pathsep``-separated list of replay
#: bundles or directories, rooted independently of the maps.
REPLAYS_ENV = "FIVEE_SIM_REPLAYS"
#: Where replays live inside a project when nothing else is configured.
REPLAYS_SUBDIR = Path(".fivee-sim") / "replays"

#: Environment variable naming the directory saved scenes are kept in. One
#: directory rather than a search path, like the encounters below and unlike
#: maps and replays: a scene is written as often as it is read, and a list of
#: roots leaves "which one does a write land in" to be guessed.
SCENES_ENV = "FIVEE_SIM_SCENES"
#: Where scenes live inside a project when nothing else is configured.
SCENES_SUBDIR = Path(".fivee-sim") / "scenes"

#: Environment variable naming the directory encounter journals are kept in.
ENCOUNTERS_ENV = "FIVEE_SIM_ENCOUNTERS"
#: Where encounter journals live inside a project when nothing else is configured.
#: One directory per fight underneath it: a journal, the lock guarding it, its
#: crash tail and its frozen replay are one fight's artifacts and belong
#: together, which is also what gives ``encounter.prune`` something to remove.
ENCOUNTERS_SUBDIR = Path(".fivee-sim") / "encounters"

#: Environment variable naming the directory adventure documents are kept in.
ADVENTURES_ENV = "FIVEE_SIM_ADVENTURES"
#: Where adventure documents live inside a project when nothing else is
#: configured. Its own root rather than a corner of the encounters one: an
#: adventure used to sit beside the journals, kept apart from them by two id
#: grammars and two globs that had to agree in four places, and a separate root
#: is the same guarantee with nothing to keep in step.
ADVENTURES_SUBDIR = Path(".fivee-sim") / "adventures"

#: Environment variable naming the directory content-addressed blobs are kept
#: in. One directory, like the two above: a blob is addressed by its own digest,
#: so a search path would be answering a question the name has already settled.
BLOBS_ENV = "FIVEE_SIM_BLOBS"
#: Where blobs live inside a project when nothing else is configured. A sibling
#: of the journals rather than a child, because the point of a blob is to be
#: shared by every journal that names one — but the two roots move
#: independently, so a journal carried to another project without its blobs
#: names payloads that are not there, and recovery says so.
BLOBS_SUBDIR = Path(".fivee-sim") / "blobs"

#: Environment variable naming the root of isolated adventure-run workspaces.
RUNS_ENV = "FIVEE_SIM_RUNS"
#: Where isolated adventure-run workspaces live by default.
RUNS_SUBDIR = Path(".fivee-sim") / "runs"

#: The editor's launch state filename; selector-specific directories beneath
#: ``.fivee-sim/runtime`` keep control, legacy and adventure processes apart.
STATE_FILENAME = "fivee-sim-server.json"

#: Environment variable naming the engine source this launch was started from,
#: as a sha256 hex digest. The launcher exports it only when it was asked to
#: watch the source for changes; an ordinary launch leaves it unset.
#:
#: It lives here for the reason the roots above do: ``web/http_server.py``
#: answers for it on ``ping`` and ``service/encounters.py`` writes it into a
#: creation record, and one misspelling between two copies would be *quiet* —
#: nothing raises, the id simply reads as absent for ever.
#: ``client/discovery.py`` keeps a copy on purpose and is the documented
#: exception: the client imports nothing of the engine but this module's
#: functions, and that boundary is worth more than the third copy costs.
SOURCE_ID_ENV = "FIVEE_SIM_SOURCE_ID"
_SAFE_RUN_ID = re.compile(r"^adv-[A-Za-z0-9_-]+$")


class RunSelectionError(ValueError):
    """A requested adventure run is unsafe or does not exist."""


@dataclass(frozen=True, slots=True)
class StorageLayout:
    """All storage roots owned by one engine process.

    ``run_id`` is an ``adv-*`` workspace, ``legacy`` for the pre-run mutable
    roots, or ``None`` for the read/control process. Shared maps, scenes and
    replays remain explicit inputs; an adventure run writes only below its
    own :attr:`run_root`.
    """

    run_id: str | None
    runs_dir: Path
    runtime_dir: Path
    shared_map_paths: tuple[Path, ...]
    shared_replay_paths: tuple[Path, ...]
    shared_scenes_dir: Path
    legacy_encounters_dir: Path
    legacy_adventures_dir: Path
    legacy_blobs_dir: Path

    @property
    def run_root(self) -> Path | None:
        if self.run_id is None or self.run_id == "legacy":
            return None
        return self.runs_dir / self.run_id

    @property
    def maps_dir(self) -> Path:
        return self.run_root / "maps" if self.run_root else self.shared_map_paths[0]

    @property
    def replays_dir(self) -> Path:
        return self.run_root / "replays" if self.run_root else self.shared_replay_paths[0]

    @property
    def scenes_dir(self) -> Path:
        return self.run_root / "scenes" if self.run_root else self.shared_scenes_dir

    @property
    def encounters_dir(self) -> Path:
        return (
            self.run_root / "encounters"
            if self.run_root
            else self.legacy_encounters_dir
        )

    @property
    def adventures_dir(self) -> Path:
        return (
            self.run_root / "adventures"
            if self.run_root
            else self.legacy_adventures_dir
        )

    @property
    def blobs_dir(self) -> Path:
        return self.run_root / "blobs" if self.run_root else self.legacy_blobs_dir

    @property
    def state_path(self) -> Path:
        return self.runtime_dir / STATE_FILENAME

    @property
    def is_writable_run(self) -> bool:
        """Whether this process owns an isolated adventure workspace."""
        return self.run_root is not None

    def _scoped(self, run: Path, shared: tuple[Path, ...]) -> tuple[tuple[Path, str], ...]:
        if self.run_root is not None:
            return ((run, "run"), *((root, "shared") for root in shared))
        scope = "legacy" if self.run_id == "legacy" else "shared"
        return tuple((root, scope) for root in shared)

    @property
    def scoped_map_roots(self) -> tuple[tuple[Path, str], ...]:
        return self._scoped(self.maps_dir, self.shared_map_paths)

    @property
    def scoped_scene_roots(self) -> tuple[tuple[Path, str], ...]:
        return self._scoped(self.scenes_dir, (self.shared_scenes_dir,))

    @property
    def scoped_replay_roots(self) -> tuple[tuple[Path, str], ...]:
        return self._scoped(self.replays_dir, self.shared_replay_paths)


def source_id(env: Mapping[str, str] | None = None) -> str:
    """The engine source this launch was started from, or ``""``.

    Blank is *unset*, exactly as in :func:`project_root`: an exported-empty
    variable means the launcher had no opinion, not that the build has an id
    which happens to be nothing.
    """
    environ = os.environ if env is None else env
    return environ.get(SOURCE_ID_ENV, "").strip()


def project_root(env: Mapping[str, str] | None = None) -> str:
    """The project directory the environment names, or ``""``.

    Two variables answer this, in order: the engine's own
    ``FIVEE_SIM_PROJECT_DIR`` and the host-supplied ``CLAUDE_PROJECT_DIR``.
    Blank is *unset* — a variable exported empty must not resolve every root to
    the current directory's parent by accident.
    """
    environ = os.environ if env is None else env
    return (
        environ.get(PROJECT_ENV, "").strip()
        or environ.get(CLAUDE_PROJECT_ENV, "").strip()
    )


def _configured_roots(
    variable: str, subdir: Path, env: Mapping[str, str] | None
) -> list[str]:
    """The roots ``variable`` asks for, mirroring the content precedence.

    The variable wins outright when set; entries may be files or directories.
    Only when it is unset does the project directory apply.
    """
    environ = os.environ if env is None else env
    configured = environ.get(variable, "").strip()
    if configured:
        return [part for part in configured.split(os.pathsep) if part.strip()]
    project = project_root(environ)
    if project:
        return [str(Path(project) / subdir)]
    return []


def _single_root(
    variable: str, subdir: Path, env: Mapping[str, str] | None
) -> Path:
    """The one directory ``variable`` names, or the project's, or the here-and-now.

    The counterpart of :func:`_configured_roots` for the two surfaces that are
    written as much as read — scenes and encounter journals. They take one
    directory rather than a search path, so there is never a question of which
    entry a write lands in.
    """
    environ = os.environ if env is None else env
    configured = environ.get(variable, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(project_root(environ) or Path.cwd()) / subdir


def environment_roots(env: Mapping[str, str] | None = None) -> list[str]:
    """Map roots the environment asks for, mirroring the content precedence.

    ``FIVEE_SIM_MAPS`` wins outright when set; entries may be files or
    directories. Only when it is unset does the project directory apply.
    """
    return _configured_roots(MAPS_ENV, MAPS_SUBDIR, env)


def maps_root(env: Mapping[str, str] | None = None) -> Path:
    """Where maps are saved by default: the first configured root, or the
    project's ``.fivee-sim/maps``, or the same under the current directory."""
    roots = environment_roots(env)
    if roots:
        return Path(roots[0]).expanduser()
    return Path.cwd() / MAPS_SUBDIR


def environment_replay_roots(env: Mapping[str, str] | None = None) -> list[str]:
    """Replay roots the environment asks for, mirroring the maps precedence.

    ``FIVEE_SIM_REPLAYS`` wins outright when set; entries may be files or
    directories. Only when it is unset does the project directory apply.
    """
    return _configured_roots(REPLAYS_ENV, REPLAYS_SUBDIR, env)


def replays_root(env: Mapping[str, str] | None = None) -> Path:
    """Where replays are written by default: the first configured root, or the
    project's ``.fivee-sim/replays``, or the same under the current directory."""
    roots = environment_replay_roots(env)
    if roots:
        return Path(roots[0]).expanduser()
    return Path.cwd() / REPLAYS_SUBDIR


def scenes_root(env: Mapping[str, str] | None = None) -> Path:
    """Where scenes live: ``FIVEE_SIM_SCENES``, else the project's
    ``.fivee-sim/scenes``, else the same under the current directory. One
    directory, not a list, for the reason :data:`SCENES_ENV` gives."""
    return _single_root(SCENES_ENV, SCENES_SUBDIR, env)


def encounters_root(env: Mapping[str, str] | None = None) -> Path:
    """Where encounter journals live: ``FIVEE_SIM_ENCOUNTERS``, else the
    project's ``.fivee-sim/encounters``, else the same under the current
    directory. Unlike maps and replays this names one directory, not a list."""
    return _single_root(ENCOUNTERS_ENV, ENCOUNTERS_SUBDIR, env)


def adventures_root(env: Mapping[str, str] | None = None) -> Path:
    """Where adventure documents live: ``FIVEE_SIM_ADVENTURES``, else the
    project's ``.fivee-sim/adventures``, else the same under the current
    directory. One directory, not a list, for the reason
    :data:`ADVENTURES_ENV` gives."""
    return _single_root(ADVENTURES_ENV, ADVENTURES_SUBDIR, env)


def blobs_root(env: Mapping[str, str] | None = None) -> Path:
    """Where blobs live: ``FIVEE_SIM_BLOBS``, else the project's
    ``.fivee-sim/blobs``, else the same under the current directory. One
    directory, not a list, for the reason :data:`BLOBS_ENV` gives."""
    return _single_root(BLOBS_ENV, BLOBS_SUBDIR, env)


def runs_root(env: Mapping[str, str] | None = None) -> Path:
    """Where isolated adventure-run workspaces live."""
    return _single_root(RUNS_ENV, RUNS_SUBDIR, env)


def storage_layout(
    *,
    configuration: Configuration | None = None,
    run_id: str | None = None,
    maps_dir: str | Path | None = None,
    replays_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> StorageLayout:
    """Resolve one launch's immutable storage ownership.

    Adventure run selection is intentionally strict at the composition root:
    a misspelled id must not start a fresh process pointing at a directory that
    merely resembles a run. ``legacy`` is the explicit compatibility selector;
    ``None`` is the control/read process used to allocate a new run.
    """
    if configuration is not None:
        runs = configuration.runs_dir
        shared_maps = configuration.map_paths
        shared_replays = configuration.replay_paths
        shared_scenes = configuration.scenes_dir
        legacy_encounters = configuration.encounters_dir
        legacy_adventures = configuration.adventures_dir
        legacy_blobs = configuration.blobs_dir
        runtime_base = configuration.path.parent / "runtime"
    else:
        shared_maps = (
            (Path(maps_dir).expanduser(),)
            if maps_dir is not None
            else tuple(Path(path).expanduser() for path in environment_roots(env))
            or (maps_root(env),)
        )
        shared_replays = (
            (Path(replays_dir).expanduser(),)
            if replays_dir is not None
            else tuple(Path(path).expanduser() for path in environment_replay_roots(env))
            or (replays_root(env),)
        )
        shared_scenes = scenes_root(env)
        legacy_encounters = encounters_root(env)
        legacy_adventures = adventures_root(env)
        legacy_blobs = blobs_root(env)
        environ = os.environ if env is None else env
        if maps_dir is not None and not environ.get(RUNS_ENV, "").strip():
            runs = Path(maps_dir).expanduser().parent / "runs"
        else:
            runs = runs_root(env)
        runtime_base = runs.parent / "runtime"

    if run_id is not None and run_id != "legacy":
        if _SAFE_RUN_ID.fullmatch(run_id) is None:
            raise RunSelectionError(f"run {run_id!r} is not a safe adventure id")
        run = runs / run_id
        if not run.is_dir():
            raise RunSelectionError(f"run {run_id!r} does not exist under {runs}")
        required = {"maps", "scenes", "replays", "encounters", "adventures", "blobs"}
        missing = sorted(name for name in required if not (run / name).is_dir())
        document = run / "adventures" / f"{run_id}.json"
        if missing or not document.is_file():
            detail = f"missing {', '.join(missing)}" if missing else "missing adventure document"
            raise RunSelectionError(f"run {run_id!r} is incomplete: {detail}")

    selector = run_id if run_id is not None else "control"
    return StorageLayout(
        run_id=run_id,
        runs_dir=runs,
        runtime_dir=runtime_base / selector,
        shared_map_paths=shared_maps,
        shared_replay_paths=shared_replays,
        shared_scenes_dir=shared_scenes,
        legacy_encounters_dir=legacy_encounters,
        legacy_adventures_dir=legacy_adventures,
        legacy_blobs_dir=legacy_blobs,
    )


def state_file_for(maps_dir: str | Path) -> Path:
    """The legacy-config control rendezvous associated with ``maps_dir``."""
    return (
        Path(maps_dir).expanduser().parent
        / "runtime"
        / "control"
        / STATE_FILENAME
    )


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
