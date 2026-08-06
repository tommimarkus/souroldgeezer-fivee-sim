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
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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
    "SCENES_ENV",
    "SCENES_SUBDIR",
    "SOURCE_ID_ENV",
    "STATE_FILENAME",
    "adventures_root",
    "blobs_root",
    "encounters_root",
    "environment_replay_roots",
    "environment_roots",
    "maps_root",
    "project_root",
    "read_state",
    "replays_root",
    "scenes_root",
    "source_id",
    "state_file_for",
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

#: The editor's launch state file; it lives next to the maps directory (for the
#: default ``<project>/.fivee-sim/maps`` that means ``<project>/.fivee-sim/``).
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
