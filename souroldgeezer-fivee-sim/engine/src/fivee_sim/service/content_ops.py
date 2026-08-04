"""Loading, checking and switching the content a session runs on.

Everything here changes when :mod:`fivee_sim.content` changes and at no other
time. The one rule worth restating: a reconfiguration builds a *new* registry
and swaps it in rather than mutating the live one, so a fight already in
progress finishes under the content it started with — switching to ``exclude``
mid-fight would otherwise strip the creature currently taking its turn. What
that costs is visibility, which :func:`status` pays back by naming every
encounter still resolving on an older generation.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..content import (
    BUILTIN_ENV,
    CONTENT_ENV,
    BuiltinMode,
    ContentError,
    ContentRegistry,
    environment_paths,
    load_packs,
)
from ..content import validate as validate_content
from . import sessions
from .errors import RequestError

__all__ = [
    "configure",
    "content_snapshot",
    "parse_builtin_mode",
    "status",
    "validate",
]


def content_snapshot(registry: ContentRegistry) -> dict[str, Any]:
    """The exact content records and provenance an encounter captured."""
    records: dict[str, dict[str, Any]] = {}
    for section in ("spells", "conditions", "terrain", "items"):
        records[section] = {
            name: {
                "record": deepcopy(record),
                "source": registry.source_of(section, name),
            }
            for name, record in sorted(registry.records_for(section).items())
        }
    return {
        "builtin": registry.builtin.value,
        "packs": [pack.as_dict() for pack in registry.packs],
        "retained_conditions": list(registry.retained_conditions),
        "records": records,
    }


def parse_builtin_mode(value: str | None, *, default: BuiltinMode) -> BuiltinMode:
    if value is None:
        return default
    try:
        return BuiltinMode(value.strip().casefold())
    except ValueError as error:
        allowed = ", ".join(item.value for item in BuiltinMode)
        raise RequestError(f"builtin must be one of: {allowed}") from error


def status(state: sessions.EngineState) -> dict[str, Any]:
    """What content is loaded, from where, and which fights predate it."""
    content = sessions.active_content(state)
    stale = [
        {"encounter_id": name, "content_generation": session.content_generation}
        for name, session in sorted(state.sessions.items())
        if session.content_generation != content.generation
    ]
    reported: dict[str, Any] = {
        "generation": content.generation,
        "configured_paths": list(content.configured),
        "environment": {
            CONTENT_ENV: environment_paths() or None,
            BUILTIN_ENV: content.registry.builtin.value,
        },
        **content.registry.summary(),
    }
    if stale:
        # A fight keeps the content it started with, so this is not a fault — it is
        # the divergence made visible, which is the only way narration stays honest.
        reported["encounters_on_older_content"] = stale
    if content.startup_error:
        reported["startup_error"] = content.startup_error
    return reported


def validate(
    state: sessions.EngineState,
    paths: list[str] | None = None,
    builtin: str | None = None,
) -> dict[str, Any]:
    content = sessions.active_content(state)
    candidates = list(paths) if paths is not None else list(content.configured)
    diagnostics = validate_content(
        candidates, builtin=parse_builtin_mode(builtin, default=content.registry.builtin)
    )
    errors = [d for d in diagnostics if d.severity.value == "error"]
    warnings = [d for d in diagnostics if d.severity.value == "warning"]
    return {
        "checked": candidates,
        "builtin": parse_builtin_mode(builtin, default=content.registry.builtin).value,
        "ok": not errors,
        "errors": [d.as_dict() for d in errors],
        "warnings": [d.as_dict() for d in warnings],
        "summary": (
            "no problems found" if not diagnostics
            else f"{len(errors)} error(s), {len(warnings)} warning(s)"
        ),
    }


def configure(
    state: sessions.EngineState,
    paths: list[str] | None = None,
    builtin: str | None = None,
    add: bool = False,
) -> dict[str, Any]:
    content = sessions.active_content(state)
    if paths is None and builtin is None:
        raise RequestError("give 'paths', 'builtin', or both — there is nothing to change")
    mode = parse_builtin_mode(builtin, default=content.registry.builtin)
    if paths is None:
        configured = list(content.configured)
    elif add:
        configured = [*content.configured, *paths]
    else:
        configured = list(paths)

    try:
        registry = load_packs(configured, builtin=mode)
    except ContentError as error:
        raise RequestError(
            f"content not changed. {error}"
        ) from error

    state.content = sessions.Content(
        registry=registry,
        generation=content.generation + 1,
        configured=tuple(configured),
        startup_error="",
    )
    return {"changed": True, **status(state)}
