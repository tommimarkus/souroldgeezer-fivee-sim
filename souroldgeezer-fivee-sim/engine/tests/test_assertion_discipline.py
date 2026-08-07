"""An error-branch test must say *which* refusal it got, not just the status.

Nine unexercised request-validation branches in ``web/http_server.py`` were
given tests by deleting each branch and rerunning: nine of nine failed. But
**four of those mutants still returned HTTP 400** — the non-object body, the
non-list ``operations``, the non-string ``kind``, and the non-object ``params``.
A test asserting only the status code would have passed against a server with
the check removed. ``'kind'`` is the sharpest: the schema guard and the service
layer's own ``ValueError`` both answer 400, so only the ``detail`` text tells
the two apart.

So the rule, which these tests enforce over the suite's own source:

* every ``assert_problem(...)`` call passes a non-empty ``detail`` fragment;
* every ``pytest.raises(...)`` on the **refusal family** passes a ``match=``.

The family is :data:`REFUSAL_ERRORS`, which is exactly what
``fivee_sim.service.errors`` exports:
``RequestError``/``NotFoundError``/``MapError``/``MapEditError``/``ReplayError``/
``StaleWriteError``. It started as a rule about the MCP adapter's ``ToolError``,
which flattened all of them into one class, and outlived it: every one of these
means *the caller asked for something the engine will not do*, and a bare
``raises`` proves only that some refusal happened. Which refusal is the whole
content of the test.

Two members are load-bearing in ways a status is not. ``NotFoundError`` is the
only thing separating a 404 from a 400 over HTTP, so a test that accepts either
would pass against an adapter that had stopped telling them apart.
``StaleWriteError`` carries the remedy — re-read and reapply — and a test
matching only the type would pass against a 400 that tells the caller nothing
they can act on.

A fragment must also be *discriminating*, which no parser can check: it has to
be text the neighbouring branches sharing that status do not produce. See
``test_web_http.test_a_traversal_id_is_404_from_the_grammar_not_the_index`` for
a case where the obvious fragment is a prefix of the fall-through message and so
needed a second assertion to bite.

There are no exemptions today, and :data:`FRAGMENT_EXEMPTIONS` is empty. One
would be warranted only where no fragment could distinguish a branch from
another producing the same status, and it must be named there with that
reasoning, so it stays reviewable rather than silently permitted.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

#: Call sites permitted to assert a status with no distinguishing fragment,
#: as ``"<file>::<enclosing test>"``. Empty; the docstring above sets the bar.
FRAGMENT_EXEMPTIONS: frozenset[str] = frozenset()

#: The refusal family: raising any of these means the caller asked for something
#: the engine will not do, so a test must say which refusal it got. Matched on
#: the trailing name, since the suite imports them by several routes.
REFUSAL_ERRORS: frozenset[str] = frozenset(
    {
        "RequestError",
        "IdempotencyConflictError",
        "NotFoundError",
        "MapError",
        "MapEditError",
        "ReplayError",
        "StaleWriteError",
    }
)


def _dotted(node: ast.expr) -> str:
    """``pytest.raises`` for an attribute chain, ``assert_problem`` for a name."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _walk(node: ast.AST, holder: str, found: list[tuple[str, ast.Call]]) -> None:
    """Collect every call under ``node``, tagged with the function enclosing it."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _walk(child, child.name, found)
        else:
            if isinstance(child, ast.Call):
                found.append((holder, child))
            _walk(child, holder, found)


def _suite_calls() -> Iterator[tuple[Path, str, ast.Call]]:
    """Every call in every test module, paired with the function enclosing it.

    Walking the tree rather than matching text is the point: a call whose
    arguments wrap across lines is invisible to a line-oriented search, and a
    long argument list is exactly what makes a call wrap.
    """
    for path in sorted(TESTS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls: list[tuple[str, ast.Call]] = []
        _walk(tree, "<module>", calls)
        for holder, call in calls:
            yield path, holder, call


def _site(path: Path, holder: str, call: ast.Call) -> str:
    return f"{path.relative_to(TESTS_DIR.parent)}:{call.lineno} in {holder}()"


def _fragment_argument(call: ast.Call) -> ast.expr | None:
    """``assert_problem``'s third parameter, given positionally or by keyword."""
    given: ast.expr | None = call.args[2] if len(call.args) >= 3 else None
    for keyword in call.keywords:
        if keyword.arg == "fragment":
            given = keyword.value
    return given


def test_every_problem_assertion_names_a_detail_fragment_not_only_a_status() -> None:
    offenders: list[str] = []
    checked = 0
    for path, holder, call in _suite_calls():
        if _dotted(call.func) != "assert_problem":
            continue
        checked += 1
        if f"{path.name}::{holder}" in FRAGMENT_EXEMPTIONS:
            continue
        fragment = _fragment_argument(call)
        if fragment is None or (isinstance(fragment, ast.Constant) and not fragment.value):
            offenders.append(_site(path, holder, call))
    assert checked, "no assert_problem calls found at all — has the helper been renamed?"
    assert not offenders, (
        "An error-branch test asserts the problem+json 'detail' fragment, never the "
        "status alone. A status-only assertion still passes against a server with "
        "the branch deleted, because a neighbouring branch answers with the same "
        "status: of the nine http_server guards this rule came from, four mutants "
        "still returned 400. Give each of these a fragment quoting what the server "
        "actually says for that branch, and check it is text the neighbours do not "
        "also produce:\n  " + "\n  ".join(offenders)
    )


def test_every_refusal_assertion_matches_the_message_not_only_the_type() -> None:
    offenders: list[str] = []
    seen: set[str] = set()
    for path, holder, call in _suite_calls():
        if _dotted(call.func) not in ("pytest.raises", "raises"):
            continue
        if not call.args:
            continue
        name = _dotted(call.args[0]).rpartition(".")[2]
        if name not in REFUSAL_ERRORS:
            continue
        seen.add(name)
        if not any(keyword.arg == "match" for keyword in call.keywords):
            offenders.append(_site(path, holder, call))
    assert seen, "no refusal raises found at all — has the family been renamed?"
    assert not offenders, (
        "A refusal test asserts the message, never the type alone: "
        "pytest.raises(RequestError) with no match= is the status-only assertion "
        "in another lane, proving a refusal happened but not which one. Give each "
        "of these a match= naming text that branch alone produces:\n  "
        + "\n  ".join(offenders)
    )


def test_the_refusal_family_is_the_one_the_service_layer_actually_raises() -> None:
    """A family listed here but gone from the source would enforce nothing.

    The rule above is a name match, so a renamed exception would silently stop
    being checked while the suite stayed green. This is the check on the check:
    :data:`REFUSAL_ERRORS` is neither more nor less than what
    ``fivee_sim.service.errors`` exports. Equality in both directions, because
    a name listed here and gone from the source enforces nothing, and a refusal
    exported there and missing here escapes the rule entirely.
    """
    from fivee_sim.service import errors

    assert REFUSAL_ERRORS <= set(errors.__all__), sorted(
        REFUSAL_ERRORS - set(errors.__all__)
    )
    assert REFUSAL_ERRORS == set(errors.__all__), (
        "service/errors.py exports a refusal this rule does not enforce: "
        f"{sorted(set(errors.__all__) - REFUSAL_ERRORS)}"
    )
