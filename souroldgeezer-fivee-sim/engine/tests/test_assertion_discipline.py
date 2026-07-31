"""An error-branch test must say *which* refusal it got, not just the status.

Nine unexercised request-validation branches in ``editor/http_server.py`` were
given tests by deleting each branch and rerunning: nine of nine failed. But
**four of those mutants still returned HTTP 400** — the non-object body, the
non-list ``operations``, the non-string ``kind``, and the non-object ``params``.
A test asserting only the status code would have passed against a server with
the check removed. ``'kind'`` is the sharpest: the guard and the service layer's
own ``ValueError`` both answer 400 with a message naming ``caves, dungeon,
overland``, so only the ``detail`` text tells the two apart.

So the rule, which these tests enforce over the suite's own source:

* every ``assert_problem(...)`` call passes a non-empty ``detail`` fragment;
* every ``pytest.raises(api.ToolError)`` passes a ``match=``.

The second is the same weakness in the MCP lane — a bare ``raises`` proves a
refusal happened, not which one. It already held everywhere when the rule was
written; enforcing it keeps it holding.

A fragment must also be *discriminating*, which no parser can check: it has to
be text the neighbouring branches sharing that status do not produce. See
``test_editor_http.test_a_traversal_id_is_404_from_the_grammar_not_the_index``
for a case where the obvious fragment is a prefix of the fall-through message
and so needed a second assertion to bite.

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


def test_every_tool_error_assertion_matches_the_message_not_only_the_type() -> None:
    offenders: list[str] = []
    checked = 0
    for path, holder, call in _suite_calls():
        if _dotted(call.func) not in ("pytest.raises", "raises"):
            continue
        if not call.args or not _dotted(call.args[0]).endswith("ToolError"):
            continue
        checked += 1
        if not any(keyword.arg == "match" for keyword in call.keywords):
            offenders.append(_site(path, holder, call))
    assert checked, "no ToolError raises found at all — has the exception been renamed?"
    assert not offenders, (
        "A ToolError test asserts the message, never the type alone: "
        "pytest.raises(api.ToolError) with no match= is the MCP lane's status-only "
        "assertion, proving a refusal happened but not which one. Give each of "
        "these a match= naming text that branch alone produces:\n  "
        + "\n  ".join(offenders)
    )
