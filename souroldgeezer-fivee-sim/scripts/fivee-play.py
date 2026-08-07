#!/usr/bin/env python3
"""Deterministic, file-first staging for the packaged play workflow.

This helper is deliberately stdlib-only.  It prepares private play artifacts
and drives the public ``fivee`` command; it does not import engine internals.
Machine stdout is always one compact JSON object and never contains adventure,
scene, map, content-pack, or character bodies.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

INDEXER_VERSION = 1
ROSTER_VERSION = 2
MANIFEST_VERSION = 1
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ATX_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_SETEXT = re.compile(r"^[ \t]*(=+|-+)[ \t]*$")
_LINK = re.compile(r"(?<!!)\[[^]]*]\(([^)]+)\)")
_PREFIX = re.compile(
    r"^(?P<prefix>chapter|act|scene|encounter|combat|interlude)\b\s*[:.\-–—]?\s*(?P<title>.*)$",
    re.IGNORECASE,
)


def _inherited_file_mode() -> int:
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


_DEFAULT_FILE_MODE = _inherited_file_mode()


class PlaySetupError(ValueError):
    """A deterministic setup invariant was refused."""


class PrepRequired(PlaySetupError):
    """The source needs one bounded semantic preparation pass."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PlaySetupError(f"{label} is not readable JSON: {path}: {error}") from error
    if not isinstance(value, dict):
        raise PlaySetupError(f"{label} must be one JSON object: {path}")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _atomic_write_text(path: Path, text: str) -> None:
    """Publish one regular file by rename, including the directory entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        existing_mode = path.stat().st_mode if path.exists() else None
        os.chmod(
            temporary,
            stat.S_IMODE(existing_mode) if existing_mode is not None else _DEFAULT_FILE_MODE,
        )
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_text(path, _canonical_json(value))


def select_party(path: Path, party_id: str, names: list[str] | None = None) -> list[dict[str, Any]]:
    document = _json_object(path.resolve(), "party file")
    parties = document.get("parties")
    if not isinstance(parties, dict) or party_id not in parties:
        available = sorted(str(key) for key in parties) if isinstance(parties, dict) else []
        raise PlaySetupError(
            f"party id {party_id!r} is absent; available party ids: {available or 'none'}"
        )
    party = parties[party_id]
    members = party.get("members") if isinstance(party, dict) else None
    if not isinstance(members, list) or not members:
        raise PlaySetupError(f"party id {party_id!r} must contain a non-empty members list")

    by_name: dict[str, dict[str, Any]] = {}
    for position, value in enumerate(members, start=1):
        if not isinstance(value, dict) or not isinstance(value.get("sheet"), dict):
            raise PlaySetupError(f"party member {position} must contain one sheet object")
        name = value["sheet"].get("name")
        if not isinstance(name, str) or not name.strip():
            raise PlaySetupError(f"party member {position} sheet needs a non-blank name")
        if name in by_name:
            raise PlaySetupError(f"duplicate member name {name!r} in party id {party_id!r}")
        by_name[name] = value

    chosen = list(by_name) if names is None else names
    if not chosen:
        raise PlaySetupError("selected party must contain at least one member")
    if len(set(chosen)) != len(chosen):
        raise PlaySetupError("selected member names must be unique")
    unknown = [name for name in chosen if name not in by_name]
    if unknown:
        raise PlaySetupError(
            f"selected member names are absent from party id {party_id!r}: {unknown}"
        )
    return [copy.deepcopy(by_name[name]) for name in chosen]


def project_party(
    members: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, dict[str, Any]]]:
    engine: list[dict[str, Any]] = []
    game_master_members: list[dict[str, Any]] = []
    seats: dict[str, dict[str, Any]] = {}
    for member in members:
        sheet = copy.deepcopy(member["sheet"])
        name = str(sheet["name"])
        engine.append(sheet)
        shared = {
            "identity": name,
            "class": copy.deepcopy(member.get("class")),
            "species": copy.deepcopy(member.get("species")),
            "background": copy.deepcopy(member.get("background")),
            "gear": copy.deepcopy(member.get("gear", [])),
            "rules": copy.deepcopy(member.get("rules", {})),
        }
        game_master_members.append(shared)
        seats[name] = {
            "identity": name,
            "sheet": sheet,
            "gear": copy.deepcopy(member.get("gear", [])),
            "rules": copy.deepcopy(member.get("rules", {})),
            "temperament": copy.deepcopy(member.get("temperament")),
            "voice": copy.deepcopy(member.get("voice")),
        }
    return engine, {"members": game_master_members}, seats


def _slug(text: str) -> str:
    value = text.strip().casefold()
    value = re.sub(r"[^\w\s-]", "", value)
    return re.sub(r"[\s_-]+", "-", value).strip("-")


def _headings(lines: list[str]) -> list[dict[str, Any]]:
    headings: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        match = _ATX_HEADING.match(lines[index])
        if match:
            headings.append(
                {"line": index + 1, "level": len(match.group(1)), "raw": match.group(2).strip()}
            )
            index += 1
            continue
        if (
            index + 1 < len(lines)
            and lines[index].strip()
            and (underline := _SETEXT.match(lines[index + 1]))
        ):
            headings.append(
                {
                    "line": index + 1,
                    "level": 1 if underline.group(1).startswith("=") else 2,
                    "raw": lines[index].strip(),
                }
            )
            index += 2
            continue
        index += 1
    return headings


def _entry_title(raw: str) -> tuple[str, str] | None:
    match = _PREFIX.match(raw)
    if match is None:
        return None
    prefix = match.group("prefix").casefold()
    title = match.group("title").strip() or raw.strip()
    kind = "encounter" if prefix in {"encounter", "combat"} else "scene"
    return kind, title


def index_markdown(path: Path) -> dict[str, Any]:
    source = path.resolve()
    if source.suffix.casefold() not in {".md", ".markdown"}:
        raise PrepRequired("deterministic indexing supports structured Markdown only")
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise PrepRequired(f"adventure source is not readable Markdown: {error}") from error
    found = _headings(lines)
    if not found:
        raise PrepRequired("adventure needs structured Markdown headings")

    entries: list[dict[str, Any]] = []
    heading_to_id: dict[int, str] = {}
    slug_to_ids: dict[str, list[str]] = {}
    for heading_index, heading in enumerate(found):
        classified = _entry_title(str(heading["raw"]))
        if classified is None:
            continue
        kind, title = classified
        entry_id = f"m{len(entries) + 1:04d}"
        end = (
            (found[heading_index + 1]["line"] - 1) if heading_index + 1 < len(found) else len(lines)
        )
        entry = {
            "id": entry_id,
            "kind": kind,
            "title": title,
            "locator": {"line_start": heading["line"], "line_end": end},
            "related_ids": [],
        }
        entries.append(entry)
        heading_to_id[heading_index] = entry_id
        for candidate in (str(heading["raw"]), title):
            slug_to_ids.setdefault(_slug(candidate), []).append(entry_id)

    if not entries:
        raise PrepRequired(
            "adventure headings need explicit Chapter, Act, Scene, Encounter, "
            "Combat, or Interlude prefixes"
        )

    by_id = {entry["id"]: entry for entry in entries}
    for heading_index, entry_id in heading_to_id.items():
        start = int(found[heading_index]["line"]) - 1
        end = int(by_id[entry_id]["locator"]["line_end"])
        related: list[str] = []
        for target in _LINK.findall("\n".join(lines[start:end])):
            target = target.strip().split(maxsplit=1)[0].strip("<>")
            if target.startswith("#"):
                fragment = target[1:]
            else:
                file_part, separator, fragment = target.partition("#")
                if not separator or Path(file_part).name not in {source.name, ""}:
                    continue
            candidates = list(dict.fromkeys(slug_to_ids.get(_slug(fragment), [])))
            if len(candidates) != 1:
                reason = "ambiguous" if candidates else "unresolved"
                raise PrepRequired(f"{reason} local link {target!r} in {source}")
            if candidates[0] != entry_id and candidates[0] not in related:
                related.append(candidates[0])
        by_id[entry_id]["related_ids"] = related

    return {
        "schema_version": 1,
        "source_path": str(source),
        "source_sha256": sha256_file(source),
        "source_format": "markdown",
        "entries": entries,
    }


def validate_module_index(document: dict[str, Any], source: Path) -> None:
    if document.get("schema_version") != 1:
        raise PlaySetupError("module index schema_version must be 1")
    digest = document.get("source_sha256")
    if digest != sha256_file(source.resolve()):
        raise PlaySetupError("module index source digest does not match the adventure")
    if not isinstance(digest, str) or _HEX_DIGEST.fullmatch(digest) is None:
        raise PlaySetupError("module index source digest must be lowercase SHA-256")
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise PlaySetupError("module index entries must be a non-empty list")
    ids: list[str] = []
    previous_line = 0
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise PlaySetupError(f"module index entry {position} must be an object")
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            raise PlaySetupError(f"module index entry {position} needs an id")
        if entry_id in ids:
            raise PlaySetupError(f"module index contains duplicate id {entry_id!r}")
        ids.append(entry_id)
        if not isinstance(entry.get("title"), str) or not entry["title"].strip():
            raise PlaySetupError(f"module index entry {entry_id!r} needs a title")
        if not isinstance(entry.get("kind"), str) or not entry["kind"].strip():
            raise PlaySetupError(f"module index entry {entry_id!r} needs a kind")
        locator = entry.get("locator")
        if not isinstance(locator, dict):
            raise PlaySetupError(f"module index entry {entry_id!r} needs a locator")
        if "line_start" in locator:
            start = locator.get("line_start")
            end = locator.get("line_end")
            if type(start) is not int or type(end) is not int or start < 1 or end < start:
                raise PlaySetupError(f"module index entry {entry_id!r} has an invalid line locator")
            if start <= previous_line:
                raise PlaySetupError("module index entries are not in source order")
            previous_line = start
        elif type(locator.get("page")) is not int or locator["page"] < 1:
            raise PlaySetupError(f"module index entry {entry_id!r} needs a line or page locator")
        if not isinstance(entry.get("related_ids"), list) or not all(
            isinstance(value, str) for value in entry["related_ids"]
        ):
            raise PlaySetupError(f"module index entry {entry_id!r} related_ids must be strings")
    known = set(ids)
    for entry in entries:
        for related in entry["related_ids"]:
            if related not in known:
                raise PlaySetupError(
                    f"module index entry {entry['id']!r} references unknown related id {related!r}"
                )


def load_or_build_index(source: Path, cache_dir: Path) -> tuple[dict[str, Any], str]:
    digest = sha256_file(source.resolve())
    cache_path = cache_dir / f"{digest}-v{INDEXER_VERSION}.json"
    if cache_path.is_file():
        cached = _json_object(cache_path, "module index cache")
        try:
            validate_module_index(cached, source)
        except PlaySetupError:
            pass
        else:
            return cached, "cached"
    built = index_markdown(source)
    validate_module_index(built, source)
    _atomic_write_json(cache_path, built)
    return built, "built"


def _safe_child(root: Path, relative: str, label: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise PlaySetupError(f"{label} escapes its staging directory: {relative!r}") from error
    return candidate


def publish_prep(
    staging_dir: Path, manifest_path: Path, destination_dir: Path, source: Path
) -> dict[str, Any]:
    staging = staging_dir.resolve()
    manifest = _json_object(manifest_path.resolve(), "prep manifest")
    if manifest.get("schema_version") != MANIFEST_VERSION or manifest.get("complete") is not True:
        raise PlaySetupError("prep manifest must be schema_version 1 and complete: true")
    if manifest.get("source_sha256") != sha256_file(source.resolve()):
        raise PlaySetupError("prep manifest source digest does not match the adventure")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise PlaySetupError("prep manifest files must be a non-empty list")

    prepared: list[tuple[Path, Path, str]] = []
    names: set[str] = set()
    for position, record in enumerate(files, start=1):
        if not isinstance(record, dict):
            raise PlaySetupError(f"prep manifest file {position} must be an object")
        relative = record.get("path")
        publish_as = record.get("publish_as")
        expected = record.get("sha256")
        if not isinstance(relative, str) or not relative.endswith(".partial"):
            raise PlaySetupError(f"prep manifest file {position} must name a private .partial file")
        if not isinstance(publish_as, str) or Path(publish_as).name != publish_as:
            raise PlaySetupError(f"prep manifest file {position} publish_as must be one filename")
        if publish_as in names:
            raise PlaySetupError(f"prep manifest publishes {publish_as!r} more than once")
        names.add(publish_as)
        source_file = _safe_child(staging, relative, "prep file")
        if not source_file.is_file():
            raise PlaySetupError(f"prep manifest file is missing: {relative}")
        actual = sha256_file(source_file)
        if expected != actual:
            raise PlaySetupError(f"prep manifest digest mismatch for {relative}")
        value = _json_object(source_file, f"prepared {record.get('kind', 'artifact')}")
        if record.get("kind") == "module-index":
            validate_module_index(value, source)
        elif record.get("kind") == "playtest-inventory":
            if value.get("source_sha256") != sha256_file(source.resolve()):
                raise PlaySetupError(
                    "playtest inventory source digest does not match the adventure"
                )
        prepared.append((source_file, destination_dir.resolve() / publish_as, actual))

    statuses: list[str] = []
    for source_file, destination, digest in prepared:
        if destination.is_file():
            if sha256_file(destination) != digest:
                raise PlaySetupError(f"published prep artifact differs: {destination.name}")
            statuses.append("reused")
            continue
        _atomic_write_text(destination, source_file.read_text(encoding="utf-8"))
        statuses.append("published")
    return {
        "schema_version": MANIFEST_VERSION,
        "status": "reused" if all(value == "reused" for value in statuses) else "published",
        "count": len(prepared),
        "source_sha256": manifest["source_sha256"],
        "paths": [str(destination) for _, destination, _ in prepared],
    }


def _referenced_json(run_dir: Path, relative: str) -> dict[str, Any]:
    path = _safe_child(run_dir, relative, "roster reference")
    if not path.is_file():
        raise PlaySetupError(f"roster reference is missing: {relative}")
    return _json_object(path, "roster reference")


def load_roster(path: Path) -> dict[str, Any]:
    roster = _json_object(path.resolve(), "roster")
    version = roster.get("schema_version", 1)
    if version == 1:
        return roster
    if version != ROSTER_VERSION:
        raise PlaySetupError(f"unsupported roster schema_version {version!r}")
    loaded = copy.deepcopy(roster)
    run_dir = path.resolve().parent
    for field in ("party_engine", "party_gm"):
        relative = loaded.get(field)
        if isinstance(relative, str):
            loaded[f"{field}_data"] = _referenced_json(run_dir, relative)
    gm = loaded.get("game_master")
    if isinstance(gm, dict) and isinstance(gm.get("input"), str):
        gm["input_data"] = _referenced_json(run_dir, gm["input"])
    seats = loaded.get("seats")
    if not isinstance(seats, list):
        raise PlaySetupError("roster v2 seats must be a list")
    for seat in seats:
        if not isinstance(seat, dict) or not isinstance(seat.get("input"), str):
            raise PlaySetupError("each roster v2 seat needs an input reference")
        seat["input_data"] = _referenced_json(run_dir, seat["input"])
    return loaded


def _configured_path(config_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else config_dir / path).resolve()


def _one_or_many_paths(config_dir: Path, value: Any, label: str) -> tuple[Path, ...]:
    values = [value] if isinstance(value, str) else value
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(item, str) and item.strip() for item in values)
    ):
        raise PlaySetupError(f"{label} must be one path or a non-empty path list")
    return tuple(_configured_path(config_dir, item) for item in values)


def validate_final_inputs(config_path: Path) -> dict[str, Any]:
    path = config_path.resolve()
    if not path.is_file():
        raise PlaySetupError(f"configuration must be a regular file: {path}")
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PlaySetupError(f"configuration is not readable TOML: {path}: {error}") from error
    if document.get("format_version") != 1 or type(document.get("format_version")) is not int:
        raise PlaySetupError("configuration format_version must be the integer 1")
    content = document.get("content", {})
    storage = document.get("storage", {})
    if not isinstance(content, dict) or not isinstance(storage, dict):
        raise PlaySetupError("configuration content and storage settings must be tables")
    config_dir = path.parent
    configured_content = content.get("paths")
    content_paths: tuple[Path, ...]
    if configured_content is None:
        default_content = config_dir / "content"
        content_paths = (default_content.resolve(),) if default_content.is_dir() else ()
    else:
        content_paths = _one_or_many_paths(config_dir, configured_content, "content.paths")
    map_paths = _one_or_many_paths(config_dir, storage.get("maps", "maps"), "storage.maps")
    scenes_value = storage.get("scenes", "scenes")
    if not isinstance(scenes_value, str) or not scenes_value.strip():
        raise PlaySetupError("storage.scenes must be one non-blank path")
    scenes_dir = _configured_path(config_dir, scenes_value)
    for content_path in content_paths:
        if not content_path.is_dir():
            raise PlaySetupError(
                f"configured content directory is not in its final location: {content_path}"
            )
    for map_path in map_paths:
        if not map_path.is_dir():
            raise PlaySetupError(
                f"configured maps directory is not in its final location: {map_path}"
            )
    if not scenes_dir.is_dir():
        raise PlaySetupError(
            f"configured scenes directory is not in its final location: {scenes_dir}"
        )
    return {
        "path": path,
        "project_dir": config_dir.parent.resolve(),
        "content_paths": content_paths,
        "map_paths": map_paths,
        "scenes_dir": scenes_dir,
    }


def _required_strings(document: dict[str, Any], fields: tuple[str, ...], operation: str) -> None:
    missing = [
        field for field in fields if not isinstance(document.get(field), str) or not document[field]
    ]
    if missing:
        raise PlaySetupError(f"{operation} succeeded without required selected fields: {missing}")


class FiveeRunner:
    """The only subprocess boundary; results remain private until projected."""

    def __init__(self, launcher: Path, config_path: Path, jq_path: Path) -> None:
        self.launcher = launcher.resolve()
        self.config_path = config_path.resolve()
        self.jq_path = jq_path.resolve()

    def _base(self) -> list[str]:
        return [sys.executable, str(self.launcher), "--config", str(self.config_path)]

    def run(self, tokens: list[str]) -> dict[str, Any]:
        command = [*self._base(), *tokens, "--compact"]
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode:
            detail = completed.stderr.strip() or f"exit status {completed.returncode}"
            raise PlaySetupError(f"fivee command failed ({tokens[0]}): {detail}")
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise PlaySetupError(
                f"fivee command returned invalid JSON ({tokens[0]}): {error}"
            ) from error
        if not isinstance(value, dict):
            raise PlaySetupError(f"fivee command returned a non-object ({tokens[0]})")
        return value

    def opening_chapter(
        self,
        *,
        adventure_id: str,
        adventure_version: str,
        scene_id: str,
        party_engine_path: Path,
        selected_names: list[str],
        seed: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        scene_command = [
            *self._base(),
            "--run",
            adventure_id,
            "scene.get",
            scene_id,
            "--compact",
        ]
        jq_program = r"""
          ($party[0] | map(.name)) as $selected
          | del(.name, .content_paths)
          | .combatants = (
              $party[0]
              + [.combatants[]
                 | (.name // .label // "") as $name
                 | select(($selected | index($name)) == null)]
            )
        """
        jq_command = [
            str(self.jq_path),
            "-e",
            "--slurpfile",
            "party",
            str(party_engine_path.resolve()),
            jq_program,
        ]
        encounter_command = [
            *self._base(),
            "--run",
            adventure_id,
            "adventure.encounter",
            adventure_id,
            "--if-match",
            adventure_version,
            "--idempotency-key",
            idempotency_key,
            "--seed",
            str(seed),
            "--json",
            "-",
            "--select",
            "adventure_id=/adventure_id",
            "--select",
            "encounter_id=/encounter_id",
            "--select",
            "index=/index",
            "--select",
            "version=/version",
            "--select",
            "state_sha256=/encounter/state_sha256",
            "--select",
            "map_sha256=/encounter/map_source/sha256",
            "--compact",
        ]
        with (
            tempfile.TemporaryFile() as scene_error,
            tempfile.TemporaryFile() as jq_error,
            tempfile.TemporaryFile() as encounter_error,
        ):
            scene = subprocess.Popen(scene_command, stdout=subprocess.PIPE, stderr=scene_error)
            assert scene.stdout is not None
            jq = subprocess.Popen(
                jq_command, stdin=scene.stdout, stdout=subprocess.PIPE, stderr=jq_error
            )
            scene.stdout.close()
            assert jq.stdout is not None
            encounter = subprocess.Popen(
                encounter_command,
                stdin=jq.stdout,
                stdout=subprocess.PIPE,
                stderr=encounter_error,
            )
            jq.stdout.close()
            output, _ = encounter.communicate()
            jq_status = jq.wait()
            scene_status = scene.wait()
            statuses = (scene_status, jq_status, encounter.returncode)
            errors: list[str] = []
            for label, status, stream in zip(
                ("scene.get", "jq", "adventure.encounter"),
                statuses,
                (scene_error, jq_error, encounter_error),
                strict=True,
            ):
                stream.seek(0)
                detail = stream.read().decode("utf-8", errors="replace").strip()
                if status:
                    errors.append(f"{label}: {detail or f'exit status {status}'}")
            if errors:
                raise PlaySetupError("opening scene pipeline failed: " + "; ".join(errors))
        try:
            value = json.loads(output)
        except json.JSONDecodeError as error:
            raise PlaySetupError(
                f"opening scene pipeline returned invalid JSON: {error}"
            ) from error
        if not isinstance(value, dict):
            raise PlaySetupError("opening scene pipeline returned a non-object")
        _required_strings(value, ("adventure_id", "encounter_id", "version"), "opening scene")
        return value


def _operation_key(label: str, values: dict[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json(values).encode("utf-8")).hexdigest()
    return f"fivee-play-{label}-{digest[:32]}"


def _seat_filename(name: str) -> str:
    found = _slug(name)
    if not found:
        raise PlaySetupError(f"seat name cannot form an artifact filename: {name!r}")
    return found


def _matching_inventory(path: Path, source: Path) -> dict[str, Any]:
    inventory = _json_object(path.resolve(), "playtest inventory")
    if inventory.get("source_sha256") != sha256_file(source.resolve()):
        raise PlaySetupError("playtest inventory source digest does not match the adventure")
    return inventory


def init_play(
    *,
    config_path: Path,
    adventure_path: Path,
    mode: str,
    seed: int,
    gm_kind: str,
    seat_kinds: dict[str, str],
    party_file: Path,
    party_id: str,
    selected_names: list[str] | None,
    prepared_index: Path | None,
    playtest_inventory: Path | None,
    opening_scene: str | None,
    runner: Any,
    jq_path: Path,
) -> dict[str, Any]:
    if mode not in {"play", "playtest"}:
        raise PlaySetupError("mode must be play or playtest")
    if gm_kind not in {"agent", "human"}:
        raise PlaySetupError("game-master kind must be agent or human")
    if type(seed) is not int:
        raise PlaySetupError("seed must be a whole number")
    if not jq_path.is_file():
        raise PlaySetupError("jq is required for play setup; install jq and retry")
    configuration = validate_final_inputs(config_path)
    adventure = adventure_path.resolve()
    if not adventure.is_file():
        raise PlaySetupError(f"adventure must be a regular file: {adventure}")
    members = select_party(party_file, party_id, selected_names)
    party_engine, party_gm, seats = project_party(members)
    unknown_seats = sorted(set(seat_kinds) - set(seats))
    if unknown_seats:
        raise PlaySetupError(f"seat kinds name unselected members: {unknown_seats}")
    invalid_kinds = sorted(
        f"{name}={kind}" for name, kind in seat_kinds.items() if kind not in {"agent", "human"}
    )
    if invalid_kinds:
        raise PlaySetupError(f"seat kinds must be agent or human: {invalid_kinds}")

    cache_dir = configuration["project_dir"] / ".fivee-sim" / "cache" / "play-indexes"
    if prepared_index is None:
        module_index, _ = load_or_build_index(adventure, cache_dir)
    else:
        module_index = _json_object(prepared_index.resolve(), "prepared module index")
        validate_module_index(module_index, adventure)
    inventory: dict[str, Any] | None = None
    if mode == "playtest":
        if playtest_inventory is None:
            raise PrepRequired(
                "playtest requires a matching semantic inventory from adventure-prep"
            )
        inventory = _matching_inventory(playtest_inventory, adventure)

    runner.run(
        [
            "serve",
            "--select",
            "runtime_dir=/runtime_dir",
            "--select",
            "already_running=/already_running",
        ]
    )
    content = runner.run(["content.status"])
    if content.get("startup_error"):
        raise PlaySetupError(f"configured content failed to load: {content['startup_error']}")
    source_digest = sha256_file(adventure)
    names = list(seats)
    create_key = _operation_key(
        "create",
        {
            "source_sha256": source_digest,
            "mode": mode,
            "seed": seed,
            "party_id": party_id,
            "members": names,
        },
    )
    created = runner.run(
        [
            "adventure.create",
            "--name",
            adventure.stem,
            "--idempotency-key",
            create_key,
            "--select",
            "adventure_id=/id",
            "--select",
            "version=/version",
            "--select",
            "status=/status",
        ]
    )
    _required_strings(created, ("adventure_id", "version"), "adventure.create")
    adventure_id = created["adventure_id"]
    adventure_version = created["version"]
    run_dir = configuration["project_dir"] / ".fivee-sim" / "plays" / adventure_id
    roster_path = run_dir / "roster.json"
    if roster_path.is_file():
        existing = _json_object(roster_path, "existing roster")
        if existing.get("schema_version", 1) != ROSTER_VERSION:
            raise PlaySetupError("existing v1 play is resume-only and will not be rewritten")
        if existing.get("source_sha256") != source_digest:
            raise PlaySetupError(
                "existing play source digest differs; refusing to rewrite saved run"
            )
        existing_kinds = {
            seat.get("name"): seat.get("kind")
            for seat in existing.get("seats", [])
            if isinstance(seat, dict)
        }
        expected_kinds = {name: seat_kinds.get(name, "agent") for name in names}
        if (
            existing.get("mode") != mode
            or existing.get("seed") != seed
            or existing.get("selected_names") != names
            or existing.get("game_master", {}).get("kind") != gm_kind
            or existing_kinds != expected_kinds
        ):
            raise PlaySetupError("existing play setup differs; refusing to rewrite saved run")
        module_path = run_dir / str(existing.get("module_index", "module-index.json"))
        checkpoint_path = run_dir / "checkpoint.json"
        reused_result: dict[str, Any] = {
            "schema_version": 1,
            "status": "reused",
            "mode": mode,
            "adventure_id": adventure_id,
            "artifact_id": adventure_id,
            "adventure_version": existing.get("adventure_version", adventure_version),
            "source_sha256": source_digest,
            "module_index_sha256": sha256_file(module_path),
            "content_generation": content.get("generation"),
            "content_counts": content.get("counts", {}),
            "seat_count": len(names),
            "paths": {
                "play": str(run_dir),
                "roster": str(roster_path),
                "checkpoint": str(checkpoint_path),
            },
        }
        if existing.get("current_encounter") is not None:
            reused_result.update(
                {
                    "encounter_id": existing["current_encounter"],
                    "state_sha256": existing.get("state_sha256"),
                    "map_sha256": existing.get("map_sha256"),
                }
            )
        return reused_result

    inputs = run_dir / "inputs"
    _atomic_write_json(inputs / "party-engine.json", party_engine)
    _atomic_write_json(inputs / "party-gm.json", party_gm)
    roster_seats: list[dict[str, Any]] = []
    for name, seat in seats.items():
        filename = _seat_filename(name)
        input_relative = f"inputs/seats/{filename}.json"
        memory_relative = f"seats/{filename}.md"
        _atomic_write_json(run_dir / input_relative, seat)
        _atomic_write_text(run_dir / memory_relative, "")
        roster_seats.append(
            {
                "name": name,
                "kind": seat_kinds.get(name, "agent"),
                "input": input_relative,
                "memory": memory_relative,
            }
        )
    _atomic_write_json(run_dir / "module-index.json", module_index)
    if inventory is not None:
        _atomic_write_json(run_dir / "run-sheet.json", inventory)
    _atomic_write_text(run_dir / "transcript.md", "")
    _atomic_write_json(run_dir / "council.json", {"schema_version": 1, "status": "empty"})
    _atomic_write_json(run_dir / "brief-cursors.json", {"schema_version": 1, "seats": {}})

    encounter_id: str | None = None
    state_sha256: str | None = None
    map_sha256: str | None = None
    if opening_scene is not None:
        opening_key = _operation_key(
            "opening",
            {
                "adventure_id": adventure_id,
                "scene_id": opening_scene,
                "seed": seed,
                "members": names,
            },
        )
        opened = runner.opening_chapter(
            adventure_id=adventure_id,
            adventure_version=adventure_version,
            scene_id=opening_scene,
            party_engine_path=inputs / "party-engine.json",
            selected_names=names,
            seed=seed,
            idempotency_key=opening_key,
        )
        _required_strings(opened, ("encounter_id", "version"), "opening scene")
        encounter_id = opened["encounter_id"]
        adventure_version = opened["version"]
        state_sha256 = opened.get("state_sha256")
        map_sha256 = opened.get("map_sha256")

    roster = {
        "schema_version": ROSTER_VERSION,
        "mode": mode,
        "seed": seed,
        "adventure_id": adventure_id,
        "source_path": str(adventure),
        "source_sha256": source_digest,
        "current_encounter": encounter_id,
        "adventure_version": adventure_version,
        "state_sha256": state_sha256,
        "map_sha256": map_sha256,
        "selected_names": names,
        "party_engine": "inputs/party-engine.json",
        "party_gm": "inputs/party-gm.json",
        "game_master": {"kind": gm_kind, "input": "inputs/party-gm.json"},
        "seats": roster_seats,
        "module_index": "module-index.json",
    }
    _atomic_write_json(roster_path, roster)
    _atomic_write_json(
        run_dir / "checkpoint.json",
        {
            "schema_version": 1,
            "status": "ready",
            "position": {"adventure_id": adventure_id, "encounter_id": encounter_id},
            "resolved_beats": 0,
            "write_lease": "root",
        },
    )
    module_digest = sha256_file(run_dir / "module-index.json")
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "ready",
        "mode": mode,
        "adventure_id": adventure_id,
        "artifact_id": adventure_id,
        "adventure_version": adventure_version,
        "source_sha256": source_digest,
        "module_index_sha256": module_digest,
        "content_generation": content.get("generation"),
        "content_counts": content.get("counts", {}),
        "seat_count": len(seats),
        "paths": {
            "play": str(run_dir),
            "roster": str(roster_path),
            "checkpoint": str(run_dir / "checkpoint.json"),
        },
    }
    if encounter_id is not None:
        result.update(
            {
                "encounter_id": encounter_id,
                "state_sha256": state_sha256,
                "map_sha256": map_sha256,
            }
        )
    return result


def _compact_error(error: Exception) -> dict[str, Any]:
    status = "prep_required" if isinstance(error, PrepRequired) else "refused"
    return {"schema_version": 1, "status": status, "detail": str(error)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fivee-play")
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish-prep")
    publish.add_argument("--staging-dir", type=Path, required=True)
    publish.add_argument("--manifest", type=Path, required=True)
    publish.add_argument("--destination-dir", type=Path, required=True)
    publish.add_argument("--adventure", type=Path, required=True)
    init = commands.add_parser("init")
    init.add_argument("--config", type=Path, required=True)
    init.add_argument("--adventure", type=Path, required=True)
    init.add_argument("--mode", choices=("play", "playtest"), default="play")
    init.add_argument("--seed", type=int, required=True)
    init.add_argument("--gm-kind", choices=("agent", "human"), default="agent")
    init.add_argument("--seat-kind", action="append", default=[], metavar="NAME=KIND")
    init.add_argument("--party-file", type=Path, required=True)
    init.add_argument("--party-id", required=True)
    init.add_argument("--member", action="append", default=[])
    init.add_argument("--prepared-index", type=Path)
    init.add_argument("--playtest-inventory", type=Path)
    init.add_argument("--opening-scene")
    return parser


def _seat_kinds(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        name, separator, kind = value.partition("=")
        if not separator or not name.strip() or not kind.strip():
            raise PlaySetupError(f"--seat-kind takes NAME=agent|human, not {value!r}")
        if name in parsed:
            raise PlaySetupError(f"--seat-kind repeats {name!r}")
        parsed[name] = kind
    return parsed


def main(argv: list[str]) -> int:
    try:
        arguments = _parser().parse_args(argv)
        if arguments.command == "publish-prep":
            result = publish_prep(
                arguments.staging_dir,
                arguments.manifest,
                arguments.destination_dir,
                arguments.adventure,
            )
        elif arguments.command == "init":
            jq = shutil.which("jq")
            if jq is None:
                raise PlaySetupError("jq is required for play setup; install jq and retry")
            result = init_play(
                config_path=arguments.config,
                adventure_path=arguments.adventure,
                mode=arguments.mode,
                seed=arguments.seed,
                gm_kind=arguments.gm_kind,
                seat_kinds=_seat_kinds(arguments.seat_kind),
                party_file=arguments.party_file,
                party_id=arguments.party_id,
                selected_names=arguments.member or None,
                prepared_index=arguments.prepared_index,
                playtest_inventory=arguments.playtest_inventory,
                opening_scene=arguments.opening_scene,
                runner=FiveeRunner(
                    Path(__file__).with_name("fivee.py"), arguments.config, Path(jq)
                ),
                jq_path=Path(jq),
            )
        else:  # pragma: no cover - argparse owns this refusal
            raise PlaySetupError(f"unsupported command {arguments.command!r}")
    except PlaySetupError as error:
        sys.stdout.write(_canonical_json(_compact_error(error)))
        return 3
    sys.stdout.write(_canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
