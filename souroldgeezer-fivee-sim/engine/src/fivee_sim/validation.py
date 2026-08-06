"""Structured validation: located diagnostics, and a reader that collects them.

Extracted from :mod:`fivee_sim.content` so that map documents — single files
with a lifecycle, not merged pack registries — can validate under the same
idiom without importing the pack machinery. The idiom is the point and it is
shared, not copied:

**Every problem is collected, never just the first.** A file with three
mistakes should report three, not send the author round the loop three times.
:class:`Reader` reads one record's fields and appends a :class:`Diagnostic`
for each thing wrong, and the caller raises once, at the end, with all of them.

**A diagnostic locates its problem.** Source, section, record, field — enough
for the author to go straight to the line, because the consumer is a person
debugging their own JSON and "invalid file" tells them nothing they can act on.

**Unknown keys are errors.** A mistyped key would be silently dropped, so it is
refused, and the refusal lists what would have been valid.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from .kernel.dice import Dice, DiceError

__all__ = ["ContentError", "Diagnostic", "Reader", "Severity"]


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One problem, located precisely enough for the author to go and fix it."""

    source: str
    problem: str
    section: str = ""
    record: str = ""
    field: str = ""
    severity: Severity = Severity.ERROR

    def describe(self) -> str:
        where = self.source
        if self.section:
            where += f" [{self.section}]"
        if self.record:
            where += f" {self.record!r}"
        if self.field:
            where += f".{self.field}"
        return f"{self.severity.value}: {where}: {self.problem}"

    def as_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity.value,
            "source": self.source,
            "section": self.section,
            "record": self.record,
            "field": self.field,
            "problem": self.problem,
        }


class ContentError(ValueError):
    """One or more packs could not be loaded. Carries every diagnostic, not the first."""

    def __init__(self, diagnostics: Sequence[Diagnostic]) -> None:
        self.diagnostics = tuple(diagnostics)
        errors = [d for d in self.diagnostics if d.severity is Severity.ERROR]
        super().__init__(
            f"{len(errors)} content error(s):\n"
            + "\n".join(f"  {d.describe()}" for d in errors)
        )


_E = TypeVar("_E", bound=StrEnum)


class Reader:
    """Reads one record's fields, collecting every problem instead of raising.

    A record with three mistakes should report three, not send the author round the
    loop three times.
    """

    def __init__(
        self,
        record: Mapping[str, Any],
        diagnostics: list[Diagnostic],
        *,
        source: str,
        section: str,
        name: str,
    ) -> None:
        self._record = record
        self._diagnostics = diagnostics
        self._source = source
        self._section = section
        self._name = name
        self.ok = True

    def fail(self, field: str, problem: str) -> None:
        self.ok = False
        self._diagnostics.append(
            Diagnostic(
                source=self._source, section=self._section, record=self._name,
                field=field, problem=problem,
            )
        )

    def warn(self, field: str, problem: str) -> None:
        self._diagnostics.append(
            Diagnostic(
                source=self._source, section=self._section, record=self._name,
                field=field, problem=problem, severity=Severity.WARNING,
            )
        )

    def unknown_keys(self, allowed: frozenset[str]) -> None:
        for key in sorted(set(self._record) - allowed):
            self.fail(
                key,
                f"unknown key; a mistyped key would be silently dropped, so it is "
                f"refused. Valid keys: {', '.join(sorted(allowed))}",
            )

    def string(self, key: str, *, required: bool = False, default: str = "") -> str:
        value = self._record.get(key)
        if value is None:
            if required:
                self.fail(key, "required")
            return default
        if not isinstance(value, str):
            self.fail(key, f"must be text, got {type(value).__name__}")
            return default
        return value

    def integer(
        self, key: str, *, required: bool = False, default: int = 0,
        minimum: int | None = None,
    ) -> int:
        value = self._record.get(key)
        if value is None:
            if required:
                self.fail(key, "required")
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            self.fail(key, f"must be a whole number, got {value!r}")
            return default
        if minimum is not None and value < minimum:
            self.fail(key, f"must be at least {minimum}, got {value}")
            return default
        return value

    def optional_integer(self, key: str, *, minimum: int | None = None) -> int | None:
        """Like :meth:`integer`, but ``None`` when the key is absent.

        For a field where "not stated" and "stated as zero" are different
        facts — a printed Initiative bonus, say — and a defaulted ``0`` would
        erase that distinction.
        """
        value = self._record.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            self.fail(key, f"must be a whole number, got {value!r}")
            return None
        if minimum is not None and value < minimum:
            self.fail(key, f"must be at least {minimum}, got {value}")
            return None
        return value

    def boolean(self, key: str, *, default: bool = False) -> bool:
        value = self._record.get(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            self.fail(key, f"must be true or false, got {value!r}")
            return default
        return value

    def dice(self, key: str) -> Dice | None:
        value = self._record.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            self.fail(key, f"must be a dice expression such as \"2d6+3\", got {value!r}")
            return None
        try:
            return Dice.parse(value)
        except DiceError as error:
            self.fail(key, str(error))
            return None

    def enum(self, key: str, enum_class: type[_E]) -> _E | None:
        value = self._record.get(key)
        if value is None:
            return None
        allowed = ", ".join(member.value for member in enum_class)
        if not isinstance(value, str):
            self.fail(key, f"must be one of: {allowed}")
            return None
        try:
            return enum_class(value)
        except ValueError:
            self.fail(key, f"{value!r} is not valid; must be one of: {allowed}")
            return None

    def string_list(self, key: str) -> list[str]:
        value = self._record.get(key)
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            self.fail(key, "must be a list of names")
            return []
        return list(value)

    def mapping(self, key: str) -> Mapping[str, Any]:
        value = self._record.get(key)
        if value is None:
            return {}
        if not isinstance(value, dict):
            self.fail(key, f"must be an object, got {type(value).__name__}")
            return {}
        return value

    def sequence(self, key: str) -> list[Any]:
        value = self._record.get(key)
        if value is None:
            return []
        if not isinstance(value, list):
            self.fail(key, f"must be a list, got {type(value).__name__}")
            return []
        return value

    def enum_keyed_ints(self, key: str, enum_class: type[_E]) -> None:
        """Validate a mapping such as ``abilities`` without rebuilding it."""
        allowed = ", ".join(member.value for member in enum_class)
        for name, value in self.mapping(key).items():
            try:
                enum_class(name)
            except ValueError:
                self.fail(key, f"{name!r} is not valid; keys must be one of: {allowed}")
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                self.fail(key, f"{name} must be a whole number, got {value!r}")

    def string_keyed_ints(self, key: str) -> None:
        """Validate a plain string-keyed integer mapping without rebuilding it.

        Unlike :meth:`enum_keyed_ints`, a key here is not checked against a
        closed set — used for a field like ``skill_bonuses``, where the key
        names a skill and this engine treats a skill as an open string, the
        same as a condition.
        """
        for name, value in self.mapping(key).items():
            if isinstance(value, bool) or not isinstance(value, int):
                self.fail(key, f"{name} must be a whole number, got {value!r}")

    def enum_list(self, key: str, enum_class: type[_E]) -> None:
        allowed = ", ".join(member.value for member in enum_class)
        for value in self.sequence(key):
            if not isinstance(value, str):
                self.fail(key, f"entries must be one of: {allowed}")
                continue
            try:
                enum_class(value)
            except ValueError:
                self.fail(key, f"{value!r} is not valid; must be one of: {allowed}")
