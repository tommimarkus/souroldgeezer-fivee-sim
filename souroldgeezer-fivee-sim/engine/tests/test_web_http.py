"""The REST adapter, driven with a stdlib client over an ephemeral port.

What is pinned here is the HTTP contract: table-driven routing and the ``Allow``
header it builds, the token and Host guards, the config injection into served
pages, the sha256 ``ETag``/``If-Match`` rule on maps and the journal-head one on
encounters, ``Idempotency-Key`` replay, problem+json bodies carrying ``instance``
and a ``urn:fivee-sim:error:*`` ``type``, and that the published contract cannot
drift from the dispatch.

The *subjects* of these operations are pinned elsewhere — map behaviour in
test_map_service, fights in test_encounter, statistics in test_analytics. These
tests check the adapter's mapping, not the mapping's subject.
"""

from __future__ import annotations

import http.client
import io
import json
import re
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from fivee_sim import __version__
from fivee_sim.content import BuiltinMode
from fivee_sim.kernel.dice import Advantage
from fivee_sim.kernel.grid import DiagonalRule, MovementMode
from fivee_sim.model.encounter import HEALTH_BANDS, ActionKind, EncounterMode
from fivee_sim.paths import encounters_root
from fivee_sim.service import adventures as adventure_service
from fivee_sim.service import encounter_journal as journal_service
from fivee_sim.service import encounters as encounters_service
from fivee_sim.service import maps as map_service
from fivee_sim.service import replay as replay_service
from fivee_sim.service import views as views_service
from fivee_sim.service.common import sha256_of
from fivee_sim.web import openapi, routes
from fivee_sim.web.http_server import (
    _HANDLERS,
    CONFIG_MARKER,
    MAX_BODY_BYTES,
    SOURCE_ID_ENV,
    TOKEN_HEADER,
    EngineServer,
)

from . import api
from .conftest import mapless_fight

PROBLEM_TYPE = "application/problem+json"
STATIC = Path(str(resources.files("fivee_sim.web"))) / "static"
# The injected shape, and deliberately exact: this is also the substitution
# `test_each_path_serves_the_shipped_file_the_table_names` undoes to recover the
# bytes on disk, so a pattern loose enough to tolerate a stray field would let
# that identity check pass against a page carrying one. No `token` — it reaches
# the browser in the URL fragment now, and a body that still named it would be
# handing the launch's whole authority to every process that can reach the port.
CONFIG_RE = re.compile(
    r'window\.__FIVEE_EDITOR__ = \{apiBase: "/api/v1", version: "[^"]+"\};'
)

HERO: dict[str, Any] = {
    "name": "Thora",
    "team": "party",
    "ac": 16,
    "max_hp": 30,
    "position": [0, 0],
    "attacks": [
        {
            "name": "Longsword",
            "attack_bonus": 5,
            "damage": "1d8+3",
            "damage_type": "slashing",
            "kind": "melee",
        }
    ],
}
GOBLIN: dict[str, Any] = {
    "monster": "Goblin Warrior",
    "label": "Goblin",
    "team": "monsters",
    "position": [15, 0],
}


def payload() -> dict[str, Any]:
    """A small valid document, rebuilt fresh so tests may mutate freely."""
    return {
        "format": "fivee-sim-map",
        "format_version": 1,
        "name": "editor chamber",
        "grid": {"width": 5, "height": 4, "cell_feet": 5},
        "legend": {".": "floor", "#": "wall"},
        "tiles": [
            "#####",
            "#...#",
            "#...#",
            "#####",
        ],
        "features": [
            {
                "id": "door-1",
                "kind": "door",
                "at": [2, 3],
                "orientation": "horizontal",
                "state": "closed",
            },
        ],
        "provenance": {
            "generator": "hand",
            "seed": 7,
            "params": {"width": 5, "height": 4},
            "edited": False,
            "source": "Authored for the test suite; 5E-compatible original content",
        },
    }


def scene_payload() -> dict[str, Any]:
    """A whole scene: the roster of a fight nobody has started yet."""
    return {
        "name": "Ambush at the ford",
        "combatants": [dict(HERO), dict(GOBLIN)],
        "seed": 20260805,
        "movement_rule": "5-5-5",
    }


def replay_bundle() -> dict[str, Any]:
    """A real v2 bundle, built by the exporter that writes them for real.

    Hand-rolling one here would mean hand-rolling its integrity hashes, and a
    fixture whose hashes were computed by the test is a fixture that agrees
    with the test rather than with the format.
    """
    encounter_id = mapless_fight(seed=91)
    exported = api.replay_export(encounter_id)
    # Asserted, not assumed: `bundle` is only present on the inline branch, so
    # a v2 envelope that grew past the size gate would otherwise take every
    # test in TestReplays down with a KeyError in setup rather than a failure
    # anyone could read.
    assert "bundle" in exported, (
        f"the fixture fight no longer fits the inline branch "
        f"({exported.get('bytes')} bytes); read the written path instead"
    )
    bundle: dict[str, Any] = exported["bundle"]
    return bundle


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


@dataclass
class Editor:
    """A running server plus the client every test drives it with."""

    server: EngineServer
    thread: threading.Thread
    maps_dir: Path
    replays_dir: Path
    log: io.StringIO = field(default_factory=io.StringIO)

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        token: bool | str = True,
        headers: Mapping[str, str] | None = None,
        host: str | None = None,
    ) -> Response:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        try:
            sent = dict(headers or {})
            if token is True:
                sent[TOKEN_HEADER] = self.server.token
            elif isinstance(token, str):
                sent[TOKEN_HEADER] = token
            data = (
                json.dumps(json_body).encode("utf-8") if json_body is not None else None
            )
            if host is not None:
                connection.putrequest(method, path, skip_host=True)
                connection.putheader("Host", host)
                for key, value in sent.items():
                    connection.putheader(key, value)
                if data is not None:
                    connection.putheader("Content-Length", str(len(data)))
                connection.endheaders(data)
            else:
                connection.request(method, path, body=data, headers=sent)
            response = connection.getresponse()
            return Response(
                status=response.status,
                headers=dict(response.getheaders()),
                body=response.read(),
            )
        finally:
            connection.close()

    def put_map(self, map_id: str, document: dict[str, Any], if_match: str = "*") -> Response:
        return self.request(
            "PUT", f"/api/v1/maps/{map_id}", json_body=document, headers={"If-Match": if_match}
        )

    def put_scene(
        self, scene_id: str, document: dict[str, Any], if_match: str = "*"
    ) -> Response:
        return self.request(
            "PUT",
            f"/api/v1/scenes/{scene_id}",
            json_body=document,
            headers={"If-Match": if_match},
        )

    def file_of(self, map_id: str) -> Path:
        return self.maps_dir / f"{map_id}.json"

    def put_replay(self, replay_id: str, bundle: Mapping[str, Any]) -> Path:
        """Drop a bundle where the engine would have written one.

        There is no REST route that writes a replay, and there deliberately is
        not: the engine records fights, the browser only plays them back. So a
        test that needs one on disk puts it there itself.
        """
        self.replays_dir.mkdir(parents=True, exist_ok=True)
        target = self.replays_dir / f"{replay_id}.json"
        target.write_text(json.dumps(bundle), encoding="utf-8")
        return target


@pytest.fixture()
def editor(tmp_path: Path) -> Iterator[Editor]:
    log = io.StringIO()
    server = EngineServer(
        maps_dir=tmp_path / "maps",
        replays_dir=tmp_path / "replays",
        log=log,
    )
    # A short poll interval, because shutdown() blocks until the serve loop next
    # wakes: at the stdlib default this fixture spent half a second per test
    # dying, which was most of the file's runtime and most of the suite's.
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    yield Editor(
        server=server,
        thread=thread,
        maps_dir=tmp_path / "maps",
        replays_dir=tmp_path / "replays",
        log=log,
    )
    server.shutdown()
    server.close()
    thread.join(timeout=5)


def test_the_server_serves_and_stops_under_a_short_poll_interval(tmp_path: Path) -> None:
    """``serve_forever`` takes the interval its shutdown latency is bound by.

    ``ThreadingHTTPServer`` polls at half a second by default and ``shutdown()``
    blocks until the loop next wakes, so every test using the ``editor`` fixture
    spent that half second dying — 21 of this file's 22 seconds. The parameter
    exists for the fixture above; ``editor/cli.py`` keeps the stdlib default,
    where a shutdown happens once and its latency is nobody's problem.
    """
    log = io.StringIO()
    server = EngineServer(maps_dir=tmp_path / "maps", log=log)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        try:
            connection.request("GET", "/api/v1/ping", headers={TOKEN_HEADER: server.token})
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read())["ok"] is True
        finally:
            connection.close()
    finally:
        # Only a running loop can be shut down: BaseServer.shutdown() waits on an
        # event that serve_forever sets on its way out, so calling it after the
        # thread has died blocks for ever. Guarding it keeps a regression here a
        # failure rather than a hang.
        if thread.is_alive():
            server.shutdown()
        server.close()
        thread.join(timeout=5)
    assert not thread.is_alive(), "a short poll interval must still stop the server"


def test_the_ping_names_the_source_the_launch_had_not_the_one_asked_for_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running server answers for the source it started from, and only that.

    Read once at construction, the id is a fact about this process. Re-read per
    request it would follow whatever the environment says *now*, and the
    client's whole question — does this process run what I am editing — cannot
    be answered by a value that tracks the asker instead of the process.

    Every other case in the suite fixes the environment for a process's whole
    life, which makes the two readings indistinguishable; this is the one that
    separates them, and without it a handler reverted to `os.environ.get` passes
    the entire suite.
    """
    monkeypatch.setenv(SOURCE_ID_ENV, "a" * 64)
    log = io.StringIO()
    server = EngineServer(maps_dir=tmp_path / "maps", log=log)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    try:
        monkeypatch.setenv(SOURCE_ID_ENV, "b" * 64)
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        try:
            connection.request("GET", "/api/v1/ping", headers={TOKEN_HEADER: server.token})
            answer = json.loads(connection.getresponse().read())
        finally:
            connection.close()
    finally:
        if thread.is_alive():
            server.shutdown()
        server.close()
        thread.join(timeout=5)

    assert answer["source_id"] == "a" * 64, (
        "the ping followed the environment rather than reporting the launch"
    )


def assert_problem(response: Response, status: int, fragment: str = "") -> dict[str, Any]:
    """Every error is RFC-9457 problem+json with the status repeated in the body.

    ``fragment`` reads as optional and is not: a status-only assertion passes
    against a server with the branch under test deleted, because a neighbour
    answers with the same status. ``tests/test_assertion_discipline.py`` fails
    the suite for a call that omits it, and carries the reasoning.

    ``type`` and ``instance`` are asserted on every problem rather than in one
    case of their own, because they are properties of the *family*: a branch
    that grew its own response and forgot either would otherwise pass.
    """
    assert response.status == status
    assert response.headers["Content-Type"].startswith(PROBLEM_TYPE)
    problem = response.json()
    assert problem["type"] == routes.error_type(status)
    assert problem["type"].startswith("urn:fivee-sim:error:")
    assert problem["status"] == status
    assert problem["title"]
    assert problem["instance"]
    if fragment:
        assert fragment in problem["detail"]
    result: dict[str, Any] = problem
    return result


class TestRefusalsAreBounded:
    """A refusal names what was asked for, and a caller controls what that is.

    Every refusal in ``service/`` echoes the identifier it was given — ``no
    ruling with code 'x'``, ``no scene 'y'`` — which is a deliberate convention
    and the reason a refusal is useful. It also means the response grows with
    the request, so a caller can make the server quote 100 kB back at itself.
    Bounded once where the problem document is built, rather than at each of the
    forty sites that would each have to remember.
    """

    def test_a_refused_identifier_is_quoted_but_not_in_full(self, editor: Editor) -> None:
        problem = assert_problem(
            editor.request("GET", f"/api/v1/rulings?code={'a' * 20000}"),
            404,
            "no ruling with code",
        )
        assert len(problem["detail"]) < 20000, "the whole identifier came back"
        assert problem["detail"].endswith("…"), "a shortened detail must say it was shortened"

    def test_a_short_identifier_is_still_echoed_whole(self, editor: Editor) -> None:
        """The convention survives; only the runaway case is trimmed."""
        problem = assert_problem(
            editor.request("GET", "/api/v1/rulings?code=no_such_ruling"),
            404,
            "no ruling with code 'no_such_ruling'",
        )
        assert "…" not in problem["detail"]

    def test_an_unauthenticated_caller_cannot_amplify_through_instance(
        self, editor: Editor
    ) -> None:
        """The sharper half: this path answers before the token is checked.

        ``instance`` is the request target, so it echoes the query string to a
        caller who has not authenticated at all.
        """
        response = editor.request("GET", f"/api/v1/rulings?code={'b' * 20000}", token=False)
        problem = assert_problem(response, 401, "X-Fivee-Editor-Token")
        assert len(problem["instance"]) < 20000, "the whole request target came back"
        assert len(response.body) < 20000, "the 401 body grows with the request"

    def test_an_ordinary_request_target_is_reported_intact(self, editor: Editor) -> None:
        problem = assert_problem(
            editor.request("GET", "/api/v1/rulings?code=nope", token=False),
            401,
            "X-Fivee-Editor-Token",
        )
        assert problem["instance"] == "/api/v1/rulings?code=nope"


class TestGuards:
    def test_ping_answers_with_the_token(self, editor: Editor) -> None:
        response = editor.request("GET", "/api/v1/ping")
        assert response.status == 200
        answer = response.json()
        assert answer["ok"] is True
        assert answer["version"]
        assert answer["maps_dir"] == str(editor.maps_dir)

    def test_a_missing_token_is_401(self, editor: Editor) -> None:
        assert_problem(editor.request("GET", "/api/v1/ping", token=False), 401, TOKEN_HEADER)

    def test_a_wrong_token_is_401(self, editor: Editor) -> None:
        assert_problem(
            editor.request("GET", "/api/v1/ping", token="not-the-token"), 401, TOKEN_HEADER
        )

    def test_the_401_says_where_the_token_actually_comes_from(self, editor: Editor) -> None:
        """The refusal's only job beyond the status: point at the fix.

        It used to say the served page carries the token, which was both the
        advice and the vulnerability — the page carries no token now, so the
        sentence would send a reader to look for one that is not there. The
        URL is where it lives, and losing the fragment off a copied link is
        exactly how a caller arrives here.
        """
        problem = assert_problem(
            editor.request("GET", "/api/v1/ping", token=False), 401, TOKEN_HEADER
        )
        assert "fragment" in problem["detail"]
        assert "fivee serve" in problem["detail"]
        # And it does not send anyone back to the page, which has none to give.
        assert "served editor page" not in problem["detail"]

    def test_a_foreign_host_header_is_403_even_with_the_token(self, editor: Editor) -> None:
        response = editor.request("GET", "/api/v1/ping", host="evil.example")
        assert_problem(response, 403, "evil.example")

    def test_a_local_host_header_with_port_passes(self, editor: Editor) -> None:
        response = editor.request("GET", "/api/v1/ping", host=f"localhost:{editor.server.port}")
        assert response.status == 200

    def test_a_missing_host_header_is_403(self, editor: Editor) -> None:
        # HTTP/1.1 requires Host, so its absence is already a broken request;
        # the guard treats it as the empty name and refuses it like any other
        # host that is not ours, rather than defaulting to trust.
        connection = http.client.HTTPConnection("127.0.0.1", editor.server.port, timeout=10)
        try:
            connection.putrequest("GET", "/api/v1/ping", skip_host=True)
            connection.putheader(TOKEN_HEADER, editor.server.token)
            connection.endheaders()
            response = connection.getresponse()
            wrapped = Response(
                status=response.status,
                headers=dict(response.getheaders()),
                body=response.read(),
            )
        finally:
            connection.close()
        assert_problem(wrapped, 403, "host '' is not this server")

    def test_a_differently_cased_host_header_passes(self, editor: Editor) -> None:
        # RFC 9110 §7.2 inherits URI host semantics, in which the host is
        # case-insensitive, so LOCALHOST names this editor exactly as localhost
        # does and a browser is free to send either.
        assert editor.request("GET", "/api/v1/ping", host="LOCALHOST").status == 200
        response = editor.request("GET", "/api/v1/ping", host=f"LocalHost:{editor.server.port}")
        assert response.status == 200

    def test_an_unknown_route_is_404(self, editor: Editor) -> None:
        # "no route for" is what separates this 404 from the map ones, which all
        # say "no map ..."; the status alone would not.
        assert_problem(
            editor.request("GET", "/api/v1/nothing"), 404, "no route for /api/v1/nothing"
        )

    def test_a_method_mismatch_is_405_and_names_what_is_allowed(
        self, editor: Editor
    ) -> None:
        refused = editor.request("POST", "/api/v1/ping")
        assert_problem(refused, 405, "POST is not supported on /api/v1/ping")
        assert refused.headers["Allow"] == "GET"

    def test_the_allow_header_comes_from_the_table_not_a_branch(
        self, editor: Editor
    ) -> None:
        # A hand-kept branch is what this replaced, and the header is the part
        # a hand-kept branch got wrong: /api/v1/maps/{id} answers two methods,
        # /api/v1/encounters two different ones, and both lists come from the
        # same table the OpenAPI document is rendered from.
        refused = editor.request("DELETE", "/api/v1/maps/editor-chamber")
        assert_problem(refused, 405, "DELETE is not supported")
        assert refused.headers["Allow"] == "GET, PUT"

        collection = editor.request("DELETE", "/api/v1/encounters")
        assert_problem(collection, 405, "DELETE is not supported")
        assert collection.headers["Allow"] == "GET, POST"

    def test_a_literal_sub_resource_wins_the_method_it_declares(
        self, editor: Editor
    ) -> None:
        # /maps/generate and /maps/{id} both match that path, and the order in
        # the table decides: POST is the generator, GET is still a map lookup —
        # which answers 404 rather than 405, because it really is routed.
        assert editor.request("POST", "/api/v1/maps/generate",
                              json_body={"kind": "caves"}).status == 200
        assert_problem(
            editor.request("GET", "/api/v1/maps/generate"), 404, "no map 'generate'"
        )

    def test_a_malformed_body_is_400(self, editor: Editor) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", editor.server.port, timeout=10)
        try:
            connection.request(
                "POST",
                "/api/v1/maps/validate",
                body=b"{not json",
                headers={TOKEN_HEADER: editor.server.token},
            )
            response = connection.getresponse()
            wrapped = Response(
                status=response.status,
                headers=dict(response.getheaders()),
                body=response.read(),
            )
        finally:
            connection.close()
        assert_problem(wrapped, 400, "not valid JSON")

    def test_an_oversized_body_is_413_before_reading(self, editor: Editor) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", editor.server.port, timeout=10)
        try:
            connection.putrequest("PUT", "/api/v1/maps/big")
            connection.putheader(TOKEN_HEADER, editor.server.token)
            connection.putheader("If-Match", "*")
            connection.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
            connection.endheaders()
            response = connection.getresponse()
            body = json.loads(response.read())
        finally:
            connection.close()
        # The detail, not only the status: this test builds its own connection
        # so it can send a Content-Length without the body, which puts it
        # outside assert_problem's reach and so outside the AST check that
        # would otherwise have caught a status-only assertion here. Naming the
        # refusal is what distinguishes "the size guard fired" from "something
        # else answered 413", which is the whole reason that check exists.
        assert response.status == 413
        assert body["status"] == 413
        assert str(MAX_BODY_BYTES) in body["detail"], body["detail"]
        assert "over the" in body["detail"], body["detail"]

    def test_a_non_numeric_content_length_is_400(self, editor: Editor) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", editor.server.port, timeout=10)
        try:
            connection.putrequest("POST", "/api/v1/maps/validate")
            connection.putheader(TOKEN_HEADER, editor.server.token)
            connection.putheader("Content-Length", "twelve")
            connection.endheaders()
            response = connection.getresponse()
            wrapped = Response(
                status=response.status,
                headers=dict(response.getheaders()),
                body=response.read(),
            )
        finally:
            connection.close()
        assert_problem(wrapped, 400, "Content-Length is not a number")

    def test_a_negative_content_length_is_400(self, editor: Editor) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", editor.server.port, timeout=10)
        try:
            connection.putrequest("POST", "/api/v1/maps/validate")
            connection.putheader(TOKEN_HEADER, editor.server.token)
            connection.putheader("Content-Length", "-1")
            connection.endheaders()
            response = connection.getresponse()
            wrapped = Response(
                status=response.status,
                headers=dict(response.getheaders()),
                body=response.read(),
            )
        finally:
            connection.close()
        assert_problem(wrapped, 400, "Content-Length is negative")

    def test_a_body_that_is_not_an_object_is_400_naming_the_valid_keys(
        self, editor: Editor
    ) -> None:
        response = editor.request("POST", "/api/v1/maps/generate", json_body=["caves"])
        problem = assert_problem(response, 400, "request body must be a JSON object")
        assert "kind, name, params, save_as, seed" in problem["detail"]

    def test_an_unknown_key_is_400_naming_it_and_the_valid_keys(self, editor: Editor) -> None:
        response = editor.request(
            "POST", "/api/v1/maps/generate", json_body={"kind": "caves", "kinds": "caves"}
        )
        problem = assert_problem(response, 400, "unknown key(s): 'kinds'")
        assert "Valid keys: kind, name, params, save_as, seed" in problem["detail"]


class TestStaticPages:
    def test_the_editor_page_carries_the_config_exactly_once(self, editor: Editor) -> None:
        response = editor.request("GET", "/editor", token=False)
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/html")
        assert response.headers["Cache-Control"] == "no-store"
        assert CONFIG_MARKER not in response.text
        assert len(CONFIG_RE.findall(response.text)) == 1
        assert editor.server.token not in response.text

    def test_the_configured_page_names_the_running_version(self, editor: Editor) -> None:
        # Not merely well-shaped: the page must be told the version of the
        # engine actually serving it, so a stale install is visible in the UI
        # rather than only in a /ping nobody reads. Anchored to the real
        # __version__ so a hardcoded or last-release string fails here.
        response = editor.request("GET", "/editor", token=False)
        assert f'version: "{__version__}"' in response.text

    def test_the_table_puts_the_index_at_the_root_and_the_editor_below_it(self) -> None:
        """Where each page lives — the one claim that is about the table itself.

        Its neighbour below checks that the server honours ``routes.PAGES``,
        which is a different claim and, on its own, a circular one: swap two
        entries in the table and a server that faithfully serves the swap still
        passes it. This is the anchor, so "the editor moved off the root" is
        asserted somewhere that a table edit cannot satisfy by agreeing with
        itself.
        """
        assert routes.PAGES["/"][0] == "home.html"
        assert routes.PAGES["/editor"][0] == "editor.html"
        assert routes.PAGES["/viewer"][0] == "viewer.html"

    @pytest.mark.parametrize("path", sorted(routes.PAGES))
    def test_each_path_serves_the_shipped_file_the_table_names(
        self, editor: Editor, path: str
    ) -> None:
        """Which document each path serves, by identity against the shipped file.

        This is the assertion that pins the editor's move, and it is deliberately
        an identity rather than a sample of element ids. Both HTML pages answer
        200 with the same content type and both carry an injected config, so
        anything weaker stays green against a server that never moved the
        editor — and an id-sampling version would also have to be re-picked
        every time either page was reorganised, which is how a check quietly
        stops discriminating.

        Undoing the injection recovers the file on disk exactly, so the oracle
        is the shipped byte stream and nothing is typed here by hand. Driven off
        ``routes.PAGES``, so a page added or moved is covered without a new case.
        """
        filename, content_type, injected = routes.PAGES[path]
        response = editor.request("GET", path, token=False)
        assert response.status == 200
        assert response.headers["Content-Type"] == content_type
        shipped = (STATIC / filename).read_text(encoding="utf-8")
        recovered = CONFIG_RE.sub(CONFIG_MARKER, response.text) if injected else response.text
        assert recovered == shipped, f"GET {path} did not serve {filename}"

    @pytest.mark.parametrize("path", sorted(routes.PAGES))
    def test_no_page_hands_the_launch_token_to_an_unauthenticated_client(
        self, editor: Editor, path: str
    ) -> None:
        """No served body carries the token, for every page the table serves.

        A page is served without the token — being the thing that *delivers*
        the token is exactly why it has to be — so a body holding it hands the
        launch's whole authority to any local process that can reach the port,
        with no filesystem access and no need to read the ``0600`` state file.
        That authority is not confined to ``.fivee-sim/``: ``map.uvtt`` and
        ``encounter.replay`` write to any path the caller names, so the harvest
        is an arbitrary local file write as the launching user. The token
        travels in the URL fragment instead, which a browser never sends.

        Driven off ``routes.PAGES`` rather than the three paths that exist
        today, so a page added later is covered without anybody remembering
        this case. ``sorted`` only to keep the ids stable.
        """
        # Vacuity guard: an empty token is in every string, so a launch that
        # somehow had none would make the assertion below unfalsifiable.
        assert editor.server.token
        response = editor.request("GET", path, token=False)
        assert response.status == 200
        assert editor.server.token not in response.text, (
            f"GET {path} served the launch token to an unauthenticated client"
        )

    def test_the_landing_page_is_configured_like_any_served_page(self, editor: Editor) -> None:
        # It fetches the operations index with the launch token, so it needs
        # the same injection the other two get — and needs it exactly once.
        response = editor.request("GET", "/", token=False)
        assert CONFIG_MARKER not in response.text
        assert len(CONFIG_RE.findall(response.text)) == 1
        assert editor.server.token not in response.text
        assert f'version: "{__version__}"' in response.text

    def test_the_viewer_page_is_configured_and_keeps_its_data_slot(self, editor: Editor) -> None:
        response = editor.request("GET", "/viewer", token=False)
        assert response.status == 200
        assert len(CONFIG_RE.findall(response.text)) == 1
        assert '<script type="application/json" id="embedded-data">null</script>' in (
            response.text
        )

    def test_the_renderer_is_served_untouched(self, editor: Editor) -> None:
        response = editor.request("GET", "/assets/renderer.js", token=False)
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/javascript")
        assert response.headers["Cache-Control"] == "no-store"
        assert "__FIVEE_EDITOR__" not in response.text

    def test_a_served_page_refuses_to_be_framed(self, editor: Editor) -> None:
        """The one browser-side gap the auth model did not already close.

        Cross-origin *reads* are already impossible three times over: no CORS
        header is ever sent, the token header is not CORS-safelisted so a
        preflight fails before the real request, and the page is a full HTML
        document so ``<script src>`` inclusion cannot parse it. None of that
        stops UI redress — an attacker page framing the real editor, token and
        all, and clicking its real buttons. These two headers do.
        """
        for path in ("/", "/editor", "/viewer"):
            response = editor.request("GET", path, token=False)
            assert response.status == 200, path
            assert response.headers["X-Frame-Options"] == "DENY", path
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"], path

    def test_every_response_forbids_content_type_sniffing(self, editor: Editor) -> None:
        """Including the API's, whose problem+json a sniffer could read as HTML."""
        for path, token in (
            ("/", False),
            ("/editor", False),
            ("/assets/renderer.js", False),
            ("/api/v1/ping", True),
        ):
            response = editor.request("GET", path, token=token)
            assert response.headers["X-Content-Type-Options"] == "nosniff", path


class TestMapsRoundTrip:
    def test_list_is_empty_before_anything_is_saved(self, editor: Editor) -> None:
        response = editor.request("GET", "/api/v1/maps")
        assert response.status == 200
        assert response.json() == {"maps": []}

    def test_put_creates_gets_match_and_the_listing_names_it(self, editor: Editor) -> None:
        created = editor.put_map("editor-chamber", payload())
        assert created.status == 201
        answer = created.json()
        assert answer["saved"] is True
        assert answer["map_id"] == "editor-chamber"
        assert answer["warnings"] == []
        assert answer["provenance"]["generator"] == "hand"
        sha256 = answer["sha256"]
        assert created.headers["ETag"] == f'"{sha256}"'
        assert editor.file_of("editor-chamber").exists()
        assert sha256_of(editor.file_of("editor-chamber").read_text()) == sha256

        fetched = editor.request("GET", "/api/v1/maps/editor-chamber")
        assert fetched.status == 200
        assert fetched.headers["ETag"] == f'"{sha256}"'
        assert fetched.json()["name"] == "editor chamber"

        listing = editor.request("GET", "/api/v1/maps").json()
        assert [entry["id"] for entry in listing["maps"]] == ["editor-chamber"]
        assert listing["maps"][0]["name"] == "editor chamber"

    def test_ground_height_survives_a_fetch_and_save(self, editor: Editor) -> None:
        # The page paints heights now, but the older promise still stands: a
        # map loaded and saved untouched round-trips its relief byte-for-byte.
        raised = payload()
        raised["elevation"] = {"default": 0, "squares": [[2, 2, 20]]}
        sha256 = editor.put_map("editor-chamber", raised).json()["sha256"]
        fetched = editor.request("GET", "/api/v1/maps/editor-chamber").json()
        assert fetched["elevation"] == {"default": 0, "squares": [[2, 2, 20]]}

        saved = editor.put_map("editor-chamber", fetched, if_match=f'"{sha256}"')
        assert saved.status == 200
        assert saved.json()["sha256"] == sha256  # unchanged bytes, unchanged digest
        assert editor.request("GET", "/api/v1/maps/editor-chamber").json()["elevation"] == (
            {"default": 0, "squares": [[2, 2, 20]]}
        )

    def test_a_canonical_height_layer_is_a_server_fixed_point(self, editor: Editor) -> None:
        # Characterization of the server contract the client canonicalizer
        # mirrors: a height layer already in canonical shape — non-zero datum,
        # negative feet, squares sorted row then column, none equal to the
        # datum — comes back verbatim and re-saves to the identical digest.
        # GREEN by design; it pins the shape the page must emit.
        terraced = payload()
        terraced["elevation"] = {"default": 5, "squares": [[3, 1, 20], [1, 2, -10]]}
        sha256 = editor.put_map("editor-chamber", terraced).json()["sha256"]
        fetched = editor.request("GET", "/api/v1/maps/editor-chamber").json()
        assert fetched["elevation"] == {"default": 5, "squares": [[3, 1, 20], [1, 2, -10]]}

        saved = editor.put_map("editor-chamber", fetched, if_match=f'"{sha256}"')
        assert saved.status == 200
        assert saved.json()["sha256"] == sha256  # a fixed point: same bytes, same digest

    def test_a_noncanonical_height_layer_converges_to_the_canonical_form(
        self, editor: Editor
    ) -> None:
        # The other half of the fixed-point claim: unsorted squares and a
        # square sitting at the datum are accepted and come back reduced —
        # sorted row then column, the datum-equal square dropped — after which
        # the layer is stable. A broken page canonicalizer lands here.
        jumbled = payload()
        jumbled["elevation"] = {"default": 0, "squares": [[3, 1, 20], [2, 2, 0], [1, 1, 20]]}
        editor.put_map("editor-chamber", jumbled)
        fetched = editor.request("GET", "/api/v1/maps/editor-chamber").json()
        assert fetched["elevation"] == {"default": 0, "squares": [[1, 1, 20], [3, 1, 20]]}

        sha256 = editor.request("GET", "/api/v1/maps/editor-chamber").headers["ETag"].strip('"')
        saved = editor.put_map("editor-chamber", fetched, if_match=f'"{sha256}"')
        assert saved.status == 200
        assert saved.json()["sha256"] == sha256  # converged, now a fixed point

    def test_a_canonical_palette_is_a_server_fixed_point(self, editor: Editor) -> None:
        # The shape the page's color picker must emit: lowercase six-digit hex,
        # kinds sorted, a pair only where the themes differ. Comes back verbatim
        # and re-saves to the identical digest.
        colored = payload()
        colored["palette"] = {
            "floor": "#d2440f",
            "wall": {"light": "#a9c6ce", "dark": "#1f3a44"},
        }
        sha256 = editor.put_map("editor-chamber", colored).json()["sha256"]
        fetched = editor.request("GET", "/api/v1/maps/editor-chamber").json()
        assert fetched["palette"] == {
            "floor": "#d2440f",
            "wall": {"light": "#a9c6ce", "dark": "#1f3a44"},
        }

        saved = editor.put_map("editor-chamber", fetched, if_match=f'"{sha256}"')
        assert saved.status == 200
        assert saved.json()["sha256"] == sha256  # a fixed point: same bytes, same digest

    def test_a_noncanonical_palette_converges_to_the_canonical_form(
        self, editor: Editor
    ) -> None:
        # The other half: shorthand and uppercase hex, kinds out of order, and a
        # pair whose halves match are all accepted and come back reduced, after
        # which the layer is stable. A page that hand-rolls the shape lands here.
        jumbled = payload()
        jumbled["palette"] = {
            "wall": "#ABC",
            "floor": {"light": "#D2440F", "dark": "#d2440f"},
        }
        editor.put_map("editor-chamber", jumbled)
        fetched = editor.request("GET", "/api/v1/maps/editor-chamber").json()
        assert list(fetched["palette"].items()) == [("floor", "#d2440f"), ("wall", "#aabbcc")]

        sha256 = editor.request("GET", "/api/v1/maps/editor-chamber").headers["ETag"].strip('"')
        saved = editor.put_map("editor-chamber", fetched, if_match=f'"{sha256}"')
        assert saved.status == 200
        assert saved.json()["sha256"] == sha256  # converged, now a fixed point

    def test_an_empty_palette_is_no_palette(self, editor: Editor) -> None:
        # What the page sends after clearing its last color, and why it must
        # delete the key rather than send {}: the digest has to return to the
        # one an uncolored map had, or clearing would read as an edit.
        plain = editor.put_map("editor-chamber", payload()).json()["sha256"]
        emptied = payload()
        emptied["palette"] = {}
        assert editor.put_map(
            "editor-chamber", emptied, if_match=f'"{plain}"'
        ).json()["sha256"] == plain
        assert "palette" not in editor.request("GET", "/api/v1/maps/editor-chamber").json()

    def test_a_bad_color_is_refused_by_the_seam(self, editor: Editor) -> None:
        broken = payload()
        broken["palette"] = {"floor": "url(https://example.invalid/x.png)"}
        assert_problem(editor.put_map("editor-chamber", broken), 422, "must be a hex color")

    def test_an_edit_does_not_discard_terrain_colors(self, editor: Editor) -> None:
        colored = payload()
        colored["palette"] = {"floor": "#d2440f"}
        editor.put_map("editor-chamber", colored)
        response = editor.request(
            "POST",
            "/api/v1/maps/editor-chamber/edits",
            json_body={"operations": [{"op": "set_name", "name": "renamed"}]},
        )
        assert response.status == 200
        assert response.json()["document"]["palette"] == {"floor": "#d2440f"}

    def test_an_edit_does_not_flatten_ground_height(self, editor: Editor) -> None:
        raised = payload()
        raised["elevation"] = {"default": 0, "squares": [[2, 2, 20]]}
        editor.put_map("editor-chamber", raised)
        response = editor.request(
            "POST",
            "/api/v1/maps/editor-chamber/edits",
            json_body={"operations": [{"op": "set_name", "name": "renamed"}]},
        )
        assert response.status == 200
        assert response.json()["document"]["elevation"]["squares"] == [[2, 2, 20]]

    def test_put_with_the_current_etag_replaces(self, editor: Editor) -> None:
        sha256 = editor.put_map("editor-chamber", payload()).json()["sha256"]
        renamed = payload()
        renamed["name"] = "renamed chamber"
        replaced = editor.put_map("editor-chamber", renamed, if_match=f'"{sha256}"')
        assert replaced.status == 200
        assert replaced.json()["sha256"] != sha256
        assert editor.request("GET", "/api/v1/maps/editor-chamber").json()["name"] == (
            "renamed chamber"
        )

    def test_put_with_a_stale_etag_is_409_and_changes_nothing(self, editor: Editor) -> None:
        editor.put_map("editor-chamber", payload())
        before = editor.file_of("editor-chamber").read_bytes()
        renamed = payload()
        renamed["name"] = "should not land"
        response = editor.put_map("editor-chamber", renamed, if_match='"not-the-sha"')
        assert_problem(response, 409, "has advanced since you read it")
        assert editor.file_of("editor-chamber").read_bytes() == before

    def test_put_without_if_match_is_428(self, editor: Editor) -> None:
        response = editor.request(
            "PUT", "/api/v1/maps/editor-chamber", json_body=payload()
        )
        assert_problem(response, 428, "If-Match")

    def test_a_new_id_needs_if_match_star(self, editor: Editor) -> None:
        response = editor.put_map("brand-new", payload(), if_match='"some-sha"')
        assert_problem(response, 409, "If-Match: *")
        assert not editor.file_of("brand-new").exists()

    def test_an_invalid_document_is_422_with_the_diagnostics(self, editor: Editor) -> None:
        broken = payload()
        broken["tiles"][0] = "###"
        broken["legend"]["?"] = "no-such-terrain"
        response = editor.put_map("editor-chamber", broken)
        problem = assert_problem(response, 422, "map error")
        assert isinstance(problem["diagnostics"], list)
        assert len(problem["diagnostics"]) >= 2
        assert not editor.file_of("editor-chamber").exists()

    def test_an_unknown_id_is_404(self, editor: Editor) -> None:
        assert_problem(editor.request("GET", "/api/v1/maps/never-saved"), 404, "never-saved")

    def test_a_traversal_id_is_404_from_the_grammar_not_the_index(self, editor: Editor) -> None:
        # Both refusals come from the id grammar, before the maps directory is
        # read at all — hence the second assertion on each. The fragment alone
        # would not bite: "no map '../escape'" is a *prefix* of what _entry_for
        # says for an id it cannot find ("no map '../escape'; maps here: none"),
        # so deleting the grammar guard leaves the fragment matching. The absence
        # of "maps here" is what proves the id never reached the index.
        fetched = assert_problem(
            editor.request("GET", "/api/v1/maps/%2e%2e%2fescape"), 404, "no map '../escape'"
        )
        assert "maps here" not in fetched["detail"]
        response = editor.request(
            "PUT", "/api/v1/maps/%2e%2e%2fescape", json_body=payload(),
            headers={"If-Match": "*"},
        )
        written = assert_problem(response, 404, "no map '../escape'")
        assert "maps here" not in written["detail"]
        assert not (editor.maps_dir.parent / "escape.json").exists()


class TestEdits:
    def test_edits_apply_persist_and_report_the_new_hash(self, editor: Editor) -> None:
        first = editor.put_map("editor-chamber", payload()).json()["sha256"]
        response = editor.request(
            "POST",
            "/api/v1/maps/editor-chamber/edits",
            json_body={"operations": [{"op": "paint", "cells": [[1, 1]], "terrain": "wall"}]},
        )
        assert response.status == 200
        answer = response.json()
        assert answer["saved"] is True
        assert answer["map_id"] == "editor-chamber"
        assert answer["applied"] == 1
        assert answer["edited"] is True
        assert answer["sha256"] != first
        assert answer["document"]["tiles"][1] == "##..#"
        assert sha256_of(editor.file_of("editor-chamber").read_text()) == answer["sha256"]

    def test_a_bad_operation_names_its_index_and_the_file_is_untouched(
        self, editor: Editor
    ) -> None:
        editor.put_map("editor-chamber", payload())
        before = editor.file_of("editor-chamber").read_bytes()
        response = editor.request(
            "POST",
            "/api/v1/maps/editor-chamber/edits",
            json_body={
                "operations": [
                    {"op": "paint", "cells": [[1, 1]], "terrain": "wall"},
                    {"op": "paint", "cells": [[99, 99]], "terrain": "wall"},
                ]
            },
        )
        assert_problem(response, 400, "operation #1")
        assert editor.file_of("editor-chamber").read_bytes() == before

    def test_edits_on_an_unknown_map_are_404(self, editor: Editor) -> None:
        # "maps here" is the index's refusal, so this is the mirror of the
        # traversal case above: that one must not name the index, this one must.
        response = editor.request(
            "POST", "/api/v1/maps/never-saved/edits", json_body={"operations": []}
        )
        assert_problem(response, 404, "no map 'never-saved'; maps here")

    def test_a_non_list_operations_value_is_400(self, editor: Editor) -> None:
        editor.put_map("editor-chamber", payload())
        before = editor.file_of("editor-chamber").read_bytes()
        response = editor.request(
            "POST",
            "/api/v1/maps/editor-chamber/edits",
            json_body={"operations": {"op": "set_name", "name": "renamed"}},
        )
        assert_problem(response, 400, "'operations' must be a list")
        assert editor.file_of("editor-chamber").read_bytes() == before


class TestGenerateAndValidate:
    def test_generate_reports_its_seed_and_persists_nothing(self, editor: Editor) -> None:
        response = editor.request(
            "POST",
            "/api/v1/maps/generate",
            json_body={"kind": "caves", "seed": 11, "params": {"width": 16, "height": 12}},
        )
        assert response.status == 200
        answer = response.json()
        assert answer["seed"] == 11
        assert answer["document"]["format"] == "fivee-sim-map"
        assert answer["document"]["provenance"]["seed"] == 11
        assert not editor.maps_dir.exists() or not any(editor.maps_dir.iterdir())

    def test_generate_without_a_seed_still_reports_one(self, editor: Editor) -> None:
        response = editor.request(
            "POST",
            "/api/v1/maps/generate",
            json_body={"kind": "caves", "params": {"width": 16, "height": 12}},
        )
        assert isinstance(response.json()["seed"], int)

    def test_an_unknown_kind_is_400_with_the_valid_list(self, editor: Editor) -> None:
        response = editor.request("POST", "/api/v1/maps/generate", json_body={"kind": "maze"})
        assert_problem(response, 400, "caves, dungeon, overland")

    def test_a_non_string_kind_is_400_before_the_service_sees_it(self, editor: Editor) -> None:
        # A string kind reaches the service and comes back as its ValueError
        # naming the generators; a kind that is not text is refused by the
        # schema, so the two are distinguished by their detail rather than by
        # the status they share.
        response = editor.request("POST", "/api/v1/maps/generate", json_body={"kind": 7})
        problem = assert_problem(response, 400, "'kind' must be text")
        assert "caves, dungeon, overland" not in problem["detail"]

    def test_a_non_object_params_value_is_400(self, editor: Editor) -> None:
        response = editor.request(
            "POST", "/api/v1/maps/generate", json_body={"kind": "caves", "params": "wide"}
        )
        assert_problem(response, 400, "'params' must be an object or null")

    def test_a_seed_that_is_not_a_whole_number_is_400(self, editor: Editor) -> None:
        # true is an int in Python, so a bare isinstance(seed, int) would accept
        # it and seed the generator with 1; the boolean case is the one that
        # proves the guard, not the string one.
        boolean = editor.request(
            "POST", "/api/v1/maps/generate", json_body={"kind": "caves", "seed": True}
        )
        assert_problem(boolean, 400, "'seed' must be a whole number or null")
        text = editor.request(
            "POST", "/api/v1/maps/generate", json_body={"kind": "caves", "seed": "eleven"}
        )
        assert_problem(text, 400, "'seed' must be a whole number or null")

    def test_a_name_that_is_not_text_is_400(self, editor: Editor) -> None:
        response = editor.request(
            "POST", "/api/v1/maps/generate", json_body={"kind": "caves", "name": 7}
        )
        assert_problem(response, 400, "'name' must be text or null")

    def test_validate_answers_ok_for_a_good_document(self, editor: Editor) -> None:
        response = editor.request("POST", "/api/v1/maps/validate", json_body=payload())
        assert response.status == 200
        assert response.json() == {"ok": True, "errors": [], "warnings": []}

    def test_validate_collects_every_error_without_raising(self, editor: Editor) -> None:
        broken = payload()
        broken["tiles"][0] = "###"
        del broken["provenance"]
        response = editor.request("POST", "/api/v1/maps/validate", json_body=broken)
        assert response.status == 200
        answer = response.json()
        assert answer["ok"] is False
        assert len(answer["errors"]) >= 2


class TestReplays:
    """The read-only half of the surface: what the served viewer plays from.

    The editor writes maps; nothing over REST writes a replay. That asymmetry
    is the contract, so the method guard is pinned alongside the happy path —
    a replay route that quietly accepted a PUT would let a browser overwrite a
    fight's audit record, which is the one thing the format exists to prevent.
    """

    def test_the_list_is_empty_when_no_fight_has_been_exported(
        self, editor: Editor
    ) -> None:
        response = editor.request("GET", "/api/v1/replays")
        assert response.status == 200
        assert response.json() == {"replays": []}

    def test_a_bundle_on_disk_is_listed_under_the_id_the_get_route_takes(
        self, editor: Editor
    ) -> None:
        editor.put_replay("gatehouse-brawl", replay_bundle())

        listed = editor.request("GET", "/api/v1/replays").json()["replays"]

        assert [row["id"] for row in listed] == ["gatehouse-brawl"]
        assert editor.request("GET", "/api/v1/replays/gatehouse-brawl").status == 200

    def test_the_row_carries_what_a_chooser_shows_without_a_second_request(
        self, editor: Editor
    ) -> None:
        bundle = replay_bundle()
        editor.put_replay("gatehouse-brawl", bundle)

        (row,) = editor.request("GET", "/api/v1/replays").json()["replays"]

        assert row["name"] == bundle["name"]
        assert row["seed"] == bundle["seed"]
        assert row["events"] == len(bundle["events"])
        # Read off the bundle like every line above it rather than spelled: the
        # claim is that the row reports the file's own version, and a literal
        # here would only pin whichever version the fixture happened to write.
        assert row["format_version"] == bundle["format_version"]

    def test_listing_needs_the_token_like_every_other_api_route(
        self, editor: Editor
    ) -> None:
        assert_problem(
            editor.request("GET", "/api/v1/replays", token=False), 401, TOKEN_HEADER
        )

    def test_fetching_one_needs_the_token_too(self, editor: Editor) -> None:
        editor.put_replay("gatehouse-brawl", replay_bundle())
        assert_problem(
            editor.request("GET", "/api/v1/replays/gatehouse-brawl", token=False),
            401,
            TOKEN_HEADER,
        )

    def test_the_bundle_comes_back_whole_with_its_hash_as_an_etag(
        self, editor: Editor
    ) -> None:
        target = editor.put_replay("gatehouse-brawl", replay_bundle())

        response = editor.request("GET", "/api/v1/replays/gatehouse-brawl")

        assert response.status == 200
        assert response.json() == json.loads(target.read_text(encoding="utf-8"))
        assert response.headers["ETag"] == f'"{sha256_of(target.read_text("utf-8"))}"'

    def test_an_unknown_id_is_404_and_names_what_is_actually_there(
        self, editor: Editor
    ) -> None:
        editor.put_replay("gatehouse-brawl", replay_bundle())
        problem = assert_problem(
            editor.request("GET", "/api/v1/replays/no-such-fight"), 404, "no replay"
        )
        assert "gatehouse-brawl" in problem["detail"]

    def test_a_traversal_id_is_simply_an_unknown_replay(self, editor: Editor) -> None:
        assert_problem(
            editor.request("GET", "/api/v1/replays/..%2f..%2fetc%2fpasswd"),
            404,
            "no replay",
        )

    def test_a_corrupt_bundle_is_422_with_the_validator_diagnostics(
        self, editor: Editor
    ) -> None:
        broken = replay_bundle()
        del broken["events"]
        editor.put_replay("broken-fight", broken)

        problem = assert_problem(
            editor.request("GET", "/api/v1/replays/broken-fight"),
            422,
            "not a playable replay bundle",
        )

        assert problem["diagnostics"]
        assert {"path", "problem"} <= set(problem["diagnostics"][0])

    def test_a_corrupt_bundle_is_still_listed_so_the_user_can_see_which_it_is(
        self, editor: Editor
    ) -> None:
        broken = replay_bundle()
        del broken["events"]
        editor.put_replay("broken-fight", broken)

        listed = editor.request("GET", "/api/v1/replays").json()["replays"]

        assert [row["id"] for row in listed] == ["broken-fight"]

    def test_replays_are_read_only_over_rest(self, editor: Editor) -> None:
        editor.put_replay("gatehouse-brawl", replay_bundle())
        assert_problem(
            editor.request(
                "PUT", "/api/v1/replays/gatehouse-brawl", json_body=replay_bundle()
            ),
            405,
            "PUT is not supported",
        )

    def test_the_collection_refuses_writes_too(self, editor: Editor) -> None:
        assert_problem(
            editor.request("POST", "/api/v1/replays", json_body={}),
            405,
            "POST is not supported",
        )


class TestShutdown:
    def test_shutdown_is_202_and_the_server_stops(self, editor: Editor) -> None:
        response = editor.request("POST", "/api/v1/shutdown")
        assert response.status == 202
        assert response.json() == {"stopping": True}
        editor.thread.join(timeout=5)
        assert not editor.thread.is_alive()

    def test_shutdown_still_needs_the_token(self, editor: Editor) -> None:
        assert_problem(
            editor.request("POST", "/api/v1/shutdown", token=False), 401, TOKEN_HEADER
        )
        assert editor.thread.is_alive()


class TestConcurrentEdits:
    """The editor is a threading server, so its own handlers race each other.

    ``PUT`` has required ``If-Match`` since it was written; ``POST /edits`` is
    a read-modify-write that had no precondition at all, so two browser tabs
    editing one map silently dropped whichever edit landed first.
    """

    def test_no_simultaneous_edit_is_acknowledged_and_then_lost(
        self, editor: Editor
    ) -> None:
        """Every edit told it succeeded survives; a loser is refused, not dropped.

        Asserting a strict one-winner-one-refusal split would be flaky, and was:
        the barrier synchronises the two *requests*, not the two handlers, so a
        run where the first finishes before the second loads the document ends
        200/200 and is perfectly correct. Measured at roughly one run in twelve.
        What holds under every interleaving is the property the bug violated —
        an acknowledged edit is never silently discarded — so that is what this
        asserts. The deterministic refusal branch is pinned at the service layer
        by test_map_service's TestConcurrentWrites.
        """
        editor.put_map("editor-chamber", payload())
        start = threading.Barrier(2)
        results: list[tuple[int, Response]] = []
        guard = threading.Lock()

        def paint(row: int) -> None:
            start.wait(timeout=10)
            response = editor.request(
                "POST",
                "/api/v1/maps/editor-chamber/edits",
                json_body={
                    "operations": [{"op": "paint", "cells": [[1, row]], "terrain": "wall"}]
                },
            )
            with guard:
                results.append((row, response))

        threads = [threading.Thread(target=paint, args=(row,)) for row in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert len(results) == 2
        assert {response.status for _row, response in results} <= {200, 409}
        acknowledged = [(row, r) for row, r in results if r.status == 200]
        assert acknowledged, "at least one writer must make progress"
        for _row, refused in ((row, r) for row, r in results if r.status == 409):
            assert_problem(refused, 409, "has advanced")

        final = json.loads(editor.file_of("editor-chamber").read_text())
        for row, response in acknowledged:
            assert final["tiles"][row][1] == "#", (
                f"row {row} was acknowledged with sha {response.json()['sha256']} "
                "but its paint is missing from the saved file"
            )
        assert sha256_of(editor.file_of("editor-chamber").read_text()) in {
            response.json()["sha256"] for _row, response in acknowledged
        }


class TestTheContract:
    """The table, the OpenAPI document and the operations index are one thing.

    §7's "an added endpoint has no contract entry" cannot happen if the entry
    *is* the endpoint, so what these check is the correspondence itself: a
    route that is routed but undocumented, or documented but unroutable, fails
    here rather than being discovered by a client.
    """

    def test_every_routed_operation_appears_in_the_openapi_document(
        self, editor: Editor
    ) -> None:
        document = editor.request("GET", "/api/v1/openapi.json").json()
        described = {
            (path, method.upper())
            for path, operations in document["paths"].items()
            for method in operations
        }
        assert described == {
            (route.path, route.method) for route in routes.api_routes()
        }

    def test_every_routed_operation_appears_in_the_operations_index(
        self, editor: Editor
    ) -> None:
        index = editor.request("GET", "/api/v1/operations").json()
        assert index["count"] == len(routes.api_routes())
        assert {entry["operation"] for entry in index["operations"]} == {
            route.operation for route in routes.api_routes()
        }
        assert index["base"] == "/api/v1"
        assert index["openapi"] == "/api/v1/openapi.json"

    def test_the_two_faces_agree_on_every_operation_id(self, editor: Editor) -> None:
        # The index is what an agent calls an operation by; the document is what
        # a generator names the client method. One drifting from the other is
        # exactly the failure a single table exists to prevent.
        document = editor.request("GET", "/api/v1/openapi.json").json()
        index = editor.request("GET", "/api/v1/operations").json()
        from_document = {
            operation["operationId"]
            for operations in document["paths"].values()
            for operation in operations.values()
        }
        assert from_document == {entry["operation_id"] for entry in index["operations"]}
        assert len(from_document) == len(routes.api_routes())  # and every one unique

    def test_the_document_is_a_well_formed_openapi_3_1_object(
        self, editor: Editor
    ) -> None:
        response = editor.request("GET", "/api/v1/openapi.json")
        assert response.status == 200
        document = response.json()
        assert document["openapi"].startswith("3.1.")
        assert document["info"]["title"] and document["info"]["version"] == __version__
        assert document["paths"]
        for path, operations in document["paths"].items():
            assert path.startswith("/api/v1/"), path
            for method, operation in operations.items():
                assert method in ("get", "post", "put", "delete"), (path, method)
                assert operation["operationId"]
                assert operation["summary"]
                assert operation["responses"], (path, method)
                for status, answer in operation["responses"].items():
                    assert status.isdigit()
                    assert answer["description"]
                    assert answer["content"]

    def test_the_document_publishes_the_error_type_registry(self, editor: Editor) -> None:
        schemas = editor.request("GET", "/api/v1/openapi.json").json()["components"][
            "schemas"
        ]
        assert schemas["Problem"]["required"] == [
            "type", "title", "status", "detail", "instance"
        ]
        published = set(schemas["ErrorType"]["enum"])
        assert published == {
            routes.error_type(status) for status in routes.ERROR_TYPES
        }
        assert "urn:fivee-sim:error:stale-write" in published
        assert not any(name.startswith("http") for name in published)

    def test_the_document_declares_the_launch_token_as_the_scheme(
        self, editor: Editor
    ) -> None:
        document = editor.request("GET", "/api/v1/openapi.json").json()
        scheme = document["components"]["securitySchemes"]["launchToken"]
        assert (scheme["type"], scheme["in"], scheme["name"]) == (
            "apiKey", "header", TOKEN_HEADER
        )
        assert document["security"] == [{"launchToken": []}]

    def test_every_route_names_a_handler_that_exists(self) -> None:
        # The join between the table and the registry, checked from both ends:
        # a route with no handler cannot be served, and a handler no route names
        # is dead code nothing can reach.
        assert {route.handler for route in routes.ROUTES} == set(_HANDLERS)

    def test_the_operations_index_is_the_table_rendered_not_a_second_list(self) -> None:
        rendered = openapi.operations_index()
        by_name = {entry["operation"]: entry for entry in rendered["operations"]}
        for route in routes.api_routes():
            entry = by_name[route.operation]
            assert (entry["method"], entry["path"]) == (route.method, route.path)
            assert entry["summary"] == route.summary
            expected_body = sorted(route.body_schema.get("properties", {})) if (
                route.body_schema
            ) else []
            assert entry["body"] == expected_body


#: The JSON types that describe themselves. Anything else is a shape a caller
#: has to be *shown*, which is the whole criterion below.
SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean", "null"})


def type_names(schema: Mapping[str, Any]) -> frozenset[str]:
    declared = schema.get("type")
    if declared is None:
        return frozenset()
    if isinstance(declared, str):
        return frozenset({declared})
    return frozenset(str(name) for name in declared)


def is_shapeless(schema: Mapping[str, Any]) -> bool:
    """True where a property's schema does not spell its own shape out.

    An object says nothing but "object"; an array of objects says nothing but
    "array". An array of *scalars* does say what it holds, and so needs no
    example — ``targets`` is a list of creature names and reads as one.
    """
    names = type_names(schema)
    if "object" in names:
        return True
    if "array" not in names:
        return False
    items = schema.get("items")
    if not isinstance(items, Mapping):
        return True
    return not type_names(items) <= SCALAR_TYPES


def routes_needing_an_example() -> list[routes.Route]:
    """Every contract route whose input includes a shape a schema cannot show.

    Derived from the schemas rather than listed, so a route added with an
    object-valued argument is covered here the day it lands rather than the day
    somebody remembers to extend a list.
    """
    found: list[routes.Route] = []
    for route in routes.api_routes():
        schema = route.body_schema
        if schema is None:
            continue
        # No `properties` is the "any JSON" body — a map document, a replay
        # bundle. The whole body is the undocumented shape.
        if "properties" not in schema or any(
            is_shapeless(one) for one in schema["properties"].values()
        ):
            found.append(route)
    return found


class TestDeclaredExamples:
    """An object-valued argument is undiscoverable unless something shows it.

    ``fivee help <operation>`` used to synthesise its example from the schema
    alone, which for an object meant ``{}`` and for an array meant ``[]``: the
    combatant shape, the build shape and the map document appeared nowhere on
    the surface an agent actually reads. The route table now declares a whole
    runnable body for each, and these are the three claims that keeps honest —
    that every such route has one, that it is a body its own route accepts, and
    that the engine really answers it.
    """

    def test_every_operation_with_a_shapeless_argument_declares_an_example(self) -> None:
        needed = routes_needing_an_example()
        assert needed, (
            "the criterion matched no route at all, which would make every case "
            "in this class pass without checking anything"
        )
        missing = sorted(route.operation for route in needed if route.example is None)
        assert not missing, (
            "these operations take an object, an array of objects, or a whole "
            "document, so their help has no shape to show unless the route "
            "declares one: " + ", ".join(missing)
        )

    def test_no_declared_example_names_an_argument_its_route_does_not_have(self) -> None:
        offenders: list[str] = []
        for route in routes.api_routes():
            if route.example is None:
                continue
            schema = route.body_schema or {}
            if "properties" not in schema:
                continue  # a free body: the document's own keys are its own
            unknown = sorted(set(route.example) - set(schema["properties"]))
            missing = sorted(set(schema.get("required", [])) - set(route.example))
            if unknown:
                offenders.append(f"{route.operation} passes unknown key(s) {unknown}")
            if missing:
                offenders.append(f"{route.operation} omits required key(s) {missing}")
        assert not offenders, "\n  ".join(offenders)

    def test_no_declared_example_carries_a_quote_the_help_would_break_on(self) -> None:
        """``fivee help`` wraps the example in single quotes, so it may hold none.

        A silent one would render an example that a shell splits into pieces —
        the exact failure a declared example exists to remove.
        """
        for route in routes.api_routes():
            for value in (route.example, *(param.example for param in route.params)):
                if value is None:
                    continue
                assert "'" not in json.dumps(value), route.operation

    def test_every_declared_example_is_a_call_this_engine_answers(
        self, editor: Editor
    ) -> None:
        """The proof, and the only one that counts: each example is sent.

        An example that does not run is worse than the empty one it replaces,
        because it reads as authoritative. So every declared body goes over the
        wire to the route that declared it, and a 4xx here is a failure of the
        declaration rather than of the engine.
        """
        creation = next(
            route for route in routes.api_routes()
            if route.operation == "encounter.create"
        )
        created = editor.request(
            "POST", "/api/v1/encounters", json_body=creation.example
        )
        assert created.status == 201, created.body
        # A map for `map.edit` to edit, put there directly so the cases below
        # do not depend on each other's order.
        editor.put_map("saved-map", payload())
        # An empty adventure for `adventure.encounter` to start its first fight
        # in: the example carries a whole roster, which is exactly what a run
        # with nothing to carry forward from needs.
        adventure = editor.request(
            "POST", "/api/v1/adventures", json_body={"name": "Declared Examples"}
        )
        assert adventure.status == 201, adventure.body
        subjects = {
            "encounter.act": created.json()["encounter_id"],
            "encounter.correct": created.json()["encounter_id"],
            "adventure.encounter": adventure.json()["id"],
            "map.put": "example-map",
            "map.edit": "saved-map",
            "scene.put": "example-scene",
        }
        needed = routes_needing_an_example()
        templated = {route.operation for route in needed if "{" in route.path}
        assert templated <= set(subjects), (
            "no subject id for " + ", ".join(sorted(templated - set(subjects)))
        )
        for route in needed:
            assert route.example is not None
            path = route.path.replace("{id}", subjects.get(route.operation, ""))
            headers = {
                param.name: param.example
                for param in route.params
                if param.example is not None
            }
            response = editor.request(
                route.method, path, json_body=route.example, headers=headers
            )
            assert response.status < 400, (
                f"the example {route.operation} declares is refused by the "
                f"operation that declares it: {response.status} "
                f"{response.body.decode('utf-8', 'replace')[:400]}"
            )

    def test_the_openapi_document_publishes_every_declared_example(
        self, editor: Editor
    ) -> None:
        """Declared once, carried by the renderer — the client reads it there.

        ``fivee help`` builds its line out of ``GET /api/v1/openapi.json``, so
        an example the document drops is an example no caller ever sees.
        """
        document = editor.request("GET", "/api/v1/openapi.json").json()
        for route in routes.api_routes():
            operation = document["paths"][route.path][route.method.lower()]
            media = operation.get("requestBody", {}).get("content", {}).get(
                "application/json", {}
            )
            assert media.get("example") == route.example, route.operation
            published = {
                one["name"]: one.get("example") for one in operation["parameters"]
            }
            for param in route.params:
                assert published[param.name] == param.example, (
                    route.operation, param.name
                )

    def test_the_document_carries_an_example_for_every_shapeless_argument(
        self, editor: Editor
    ) -> None:
        """The finding, restated as a test: the document used to hold zero.

        Read with ``.get`` at every step rather than subscripted. Subscripting
        made a missing example a ``KeyError`` raised while *building* the list,
        which meant both assertions below were unreachable — the case still
        failed, but on a traceback that named a dict key instead of the
        operation whose help had nothing to show.
        """
        document = editor.request("GET", "/api/v1/openapi.json").json()
        undescribed = sorted(
            route.operation
            for route in routes_needing_an_example()
            if document["paths"][route.path][route.method.lower()]
            .get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("example")
            is None
        )
        assert not undescribed, (
            "the served document publishes no example for these operations, so "
            "`fivee help` has nothing to print for an argument whose shape the "
            "schema cannot show: " + ", ".join(undescribed)
        )


class TestDeclaredBounds:
    """A length bound the dispatcher enforces and the service also owns.

    Same trade as ``TestDeclaredEnums`` below and for the same reason: the route
    table may not import ``service/``, so the note bound is written in both
    places and held equal here rather than derived. Neither number is written
    into this file — that would pin each declaration against a literal instead
    of against the other, and both could drift together.

    The duplication is not redundancy. ``audited_primitive`` journals an
    attempt's arguments *before* the operation body runs, so only the schema copy
    can refuse an oversized payload before it reaches the disk; and only the
    service copy guards ``tests/api.py``, which calls these functions with no
    adapter in front of them.
    """

    def test_the_note_bound_is_the_same_number_on_both_sides(self) -> None:
        route = routes.find("POST", f"{routes.API_PREFIX}/encounters/enc-1/notes")
        assert route is not None
        schema = route[0].body_schema or {}
        declared = schema["properties"]["text"]["maxLength"]
        assert declared == encounters_service.MAX_NOTE_TEXT

    def test_the_reason_bound_is_the_same_number_on_both_sides(self) -> None:
        route = routes.find("POST", f"{routes.API_PREFIX}/encounters/enc-1/corrections")
        assert route is not None
        schema = route[0].body_schema or {}
        declared = schema["properties"]["reason"]["maxLength"]
        assert declared == encounters_service.MAX_REASON_TEXT

    def test_every_journalled_string_argument_is_bounded(self) -> None:
        # Derived from the table rather than listed: an audited operation added
        # tomorrow with an unbounded string is caught without editing this test.
        # Free-form bodies (``{}``) validate in the service and are exempt.
        unbounded: list[str] = []
        for route in routes.api_routes():
            if "/encounters/" not in route.path or route.method != "POST":
                continue
            for name, schema in (route.body_schema or {}).get("properties", {}).items():
                types = schema.get("type")
                types = [types] if isinstance(types, str) else list(types or [])
                if "string" not in types:
                    continue
                # An ``enum`` is already a bound, and one the dispatcher applies
                # in the same pass: a value outside a closed set is refused
                # whatever its length, so it cannot reach the journal either.
                if "enum" in schema or "maxLength" in schema:
                    continue
                unbounded.append(f"{route.operation}.{name}")
        assert unbounded == [], (
            "these journalled string arguments reach the durable journal before "
            f"any length check: {unbounded}"
        )


class TestDeclaredDefaults:
    """A default in the table is an answer the service never gets asked for.

    Third sibling to ``TestDeclaredBounds`` above and ``TestDeclaredEnums``
    below, and a separate class because a default is neither a bound nor a
    closed set — it is the value a caller receives for *saying nothing*. The
    trade is the same one both of those make: the route table may not import
    ``service/``, so the number is written in both places and held equal here
    rather than derived, and neither number appears in this file.

    This one is easy to mistake for decoration, which is why it is worth a
    class of its own. ``map_ops.replay_export``'s signature default is
    ``LATEST_FORMAT_VERSION`` and looks authoritative, but no HTTP caller ever
    reaches it: ``_checked_body`` fills an absent key from the schema, so the
    literal in the table is what arrives. A phase bumping the writer without
    touching the table would leave ``encounter.finalize`` — which passes the
    constant explicitly — writing one version while ``encounter.replay``
    quietly kept writing the old one, with the whole suite green.
    """

    def declared_default(self, method: str, path: str, field: str) -> Any:
        """The default a caller gets, reached through the table's own structure.

        Navigated rather than eyeballed, and every step that can come back
        empty says which one did: a reshaped declaration must fail this loudly
        instead of quietly finding nothing left to disagree with.
        """
        found = routes.find(method, path)
        assert found is not None, f"no route answers {method} {path} any more"
        properties = (found[0].body_schema or {}).get("properties", {})
        assert field in properties, (
            f"{found[0].operation} no longer declares a {field!r} body field; "
            "this test can no longer reach the default it exists to pin"
        )
        schema = properties[field]
        assert "default" in schema, (
            f"{found[0].operation}.{field} declares no default any more, so a "
            "caller who omits it gets whatever the handler does — decide which"
        )
        return schema["default"]

    def test_the_replay_export_default_is_the_version_the_engine_writes(self) -> None:
        assert (
            self.declared_default(
                "POST", f"{routes.API_PREFIX}/encounters/enc-1/replay", "format_version"
            )
            == replay_service.LATEST_FORMAT_VERSION
        )


class TestDeclaredEnums:
    """A closed set the help does not print is one you learn by guessing wrong.

    Every enum in the route table names a set some other module is the
    authority on. Three are derived from that authority outright, so drift is
    impossible; the two the map service keeps as private module constants and
    the adventure service's ``LIST_STATUSES`` are written into the contract and
    held against the constant here, which is the same guarantee arrived at the
    long way — the route table may not import ``service/``. What is never
    acceptable is a literal expected set written into this file: that pins the
    table against itself.
    """

    def enums_declared(self) -> dict[str, list[Any]]:
        found: dict[str, list[Any]] = {}
        for route in routes.api_routes():
            for param in route.params:
                if "enum" in param.schema:
                    found[f"{route.operation}.{param.name}"] = list(param.schema["enum"])
            for name, schema in (route.body_schema or {}).get("properties", {}).items():
                if "enum" in schema:
                    found[f"{route.operation}.{name}"] = list(schema["enum"])
        return found

    #: Each declared enum against whatever module decides what is accepted.
    #: Written as a mapping from the *route's* label to the authority's own
    #: value, so neither side is spelled out twice and either one moving is a
    #: failure here. ``map.*`` reaches through an underscore deliberately: the
    #: constants are private, and a test may know that where the published
    #: contract may not.
    def authorities(self) -> dict[str, set[Any]]:
        movement_rules = {rule.value for rule in DiagonalRule}
        advantages = {state.value for state in Advantage}
        builtins = {mode.value for mode in BuiltinMode} | {None}
        return {
            "encounter.act.kind": {kind.value for kind in ActionKind},
            "encounter.act.movement_mode": (
                {mode.value for mode in MovementMode} | {None}
            ),
            "encounter.create.mode": {mode.value for mode in EncounterMode},
            "encounter.create.movement_rule": movement_rules,
            "adventure.encounter.mode": {mode.value for mode in EncounterMode},
            "adventure.encounter.movement_rule": movement_rules,
            "adventure.list.status": set(adventure_service.LIST_STATUSES),
            "analytics.rounds.movement_rule": movement_rules,
            "content.validate.builtin": builtins,
            "content.configure.builtin": builtins,
            "dice.roll.advantage": advantages,
            "dice.check.advantage": advantages,
            "dice.save.advantage": advantages,
            "map.generate.kind": set(map_service._PARAM_TYPES),
            "map.query.query": set(map_service._QUERIES),
            "encounter.create.view": set(views_service.VIEWS),
            "encounter.act.view": set(views_service.VIEWS),
            "encounter.advance.view": set(views_service.VIEWS),
            "encounter.resume.view": set(views_service.VIEWS),
        }

    def test_every_declared_enum_is_the_set_its_authority_accepts(self) -> None:
        declared = self.enums_declared()
        authorities = self.authorities()
        assert "encounter.act.kind" in declared, (
            "the action kinds are the closed set the finding names; without an "
            "enum here the help prints none of them and this class is vacuous"
        )
        unheld = sorted(set(declared) - set(authorities) - {"encounter.list.status"})
        assert not unheld, (
            "a closed set declared here with nothing to hold it against is one "
            "that can drift from the validator in silence. Name its authority "
            "above, or pin it the way encounter.list.status is: " + ", ".join(unheld)
        )
        for label, expected in sorted(authorities.items()):
            assert label in declared, f"{label} no longer declares an enum"
            assert set(declared[label]) == expected, label

    def test_the_one_set_with_no_constant_behind_it_is_pinned_to_its_own_refusal(
        self, editor: Editor
    ) -> None:
        """``status`` predates this and its authority is an inline literal.

        There is no constant to derive from, so the engine's own answer is used
        instead: every declared value is accepted, and a value outside the set
        is refused with a message that names exactly the declared ones. That is
        a weaker guarantee than the derivations above — it would not notice the
        service growing a fourth status the table never heard of — and it is
        recorded as weaker rather than dressed up.
        """
        declared = self.enums_declared()["encounter.list.status"]
        for value in declared:
            answered = editor.request("GET", f"/api/v1/encounters?status={value}")
            assert answered.status == 200, (value, answered.body)
        problem = assert_problem(
            editor.request("GET", "/api/v1/encounters?status=archived"),
            400,
            "must be one of: ",
        )
        assert all(str(value) in problem["detail"] for value in declared)


class TestProblemShape:
    def test_a_problem_names_the_request_it_describes(self, editor: Editor) -> None:
        # `instance` is the correlation handle this server offers instead of a
        # trace context: one process, no outbound calls, nothing else to join on.
        problem = assert_problem(
            editor.request("GET", "/api/v1/maps/never-saved"), 404, "no map 'never-saved'"
        )
        assert problem["instance"] == "/api/v1/maps/never-saved"

    def test_the_instance_keeps_the_query_that_produced_the_problem(
        self, editor: Editor
    ) -> None:
        problem = assert_problem(
            editor.request("GET", "/api/v1/catalog/search?query=goblin&since=-1"),
            400,
            "since must be a non-negative whole number",
        )
        assert problem["instance"] == "/api/v1/catalog/search?query=goblin&since=-1"

    def test_each_refusal_carries_its_own_type_not_about_blank(
        self, editor: Editor
    ) -> None:
        missing = assert_problem(
            editor.request("GET", "/api/v1/encounters/enc-nope"), 404, "unknown encounter"
        )
        malformed = assert_problem(
            editor.request("POST", "/api/v1/dice/rolls", json_body={}),
            400,
            "'expression' is required",
        )
        assert missing["type"] == "urn:fivee-sim:error:not-found"
        assert malformed["type"] == "urn:fivee-sim:error:invalid-parameter"


class TestDiceAndRules:
    def test_a_roll_reports_the_seed_it_used_and_reproduces_under_it(
        self, editor: Editor
    ) -> None:
        response = editor.request(
            "POST", "/api/v1/dice/rolls", json_body={"expression": "2d6+3", "seed": 11}
        )
        assert response.status == 200
        answer = response.json()
        assert answer["seed"] == 11
        assert answer["expression"] == "2d6+3"
        again = editor.request(
            "POST", "/api/v1/dice/rolls", json_body={"expression": "2d6+3", "seed": 11}
        ).json()
        assert again["total"] == answer["total"]

    def test_a_roll_without_a_seed_still_reports_one(self, editor: Editor) -> None:
        answer = editor.request(
            "POST", "/api/v1/dice/rolls", json_body={"expression": "d20"}
        ).json()
        assert isinstance(answer["seed"], int)

    def test_an_advantage_outside_the_enum_is_refused_by_the_schema(
        self, editor: Editor
    ) -> None:
        # The schema the contract publishes is the schema the dispatcher
        # enforces, so this refusal names the choices rather than reaching the
        # kernel and coming back as something vaguer.
        assert_problem(
            editor.request(
                "POST",
                "/api/v1/dice/rolls",
                json_body={"expression": "d20", "advantage": "lucky"},
            ),
            400,
            "'advantage' must be one of: none, advantage, disadvantage",
        )

    def test_a_check_and_a_save_answer_their_own_shapes(self, editor: Editor) -> None:
        check = editor.request(
            "POST", "/api/v1/dice/checks", json_body={"modifier": 5, "dc": 10, "seed": 3}
        ).json()
        assert set(check) >= {"seed", "natural", "total", "dc", "success", "detail"}
        saved = editor.request(
            "POST",
            "/api/v1/dice/saves",
            json_body={"modifier": 2, "dc": 15, "auto_fail": True, "seed": 3},
        ).json()
        assert saved["auto_failed"] is True and saved["success"] is False

    def test_a_rules_lookup_answers_from_the_loaded_content(self, editor: Editor) -> None:
        answer = editor.request("GET", "/api/v1/rules?topic=Prone").json()
        assert answer["kind"] == "condition"
        assert answer["name"] == "prone"

    def test_an_empty_topic_summarises_what_is_loaded(self, editor: Editor) -> None:
        answer = editor.request("GET", "/api/v1/rules").json()
        assert answer["counts"]["conditions"] >= 1
        assert answer["builtin"] == "include"

    def test_a_topic_nothing_has_loaded_is_404_not_400(self, editor: Editor) -> None:
        # The distinction the NotFoundError subclass exists for: a miss is an
        # id that is not there, not an argument the caller got wrong.
        assert_problem(
            editor.request("GET", "/api/v1/rules?topic=Beholder"),
            404,
            "nothing loaded for 'Beholder'",
        )

    def test_an_undeclared_query_parameter_is_refused_not_ignored(
        self, editor: Editor
    ) -> None:
        # A misspelled parameter that is silently ignored is a wrong answer
        # nobody can see, which is the same argument the body's unknown-key
        # refusal makes.
        assert_problem(
            editor.request("GET", "/api/v1/rules?subject=Prone"),
            400,
            "unknown query parameter(s): 'subject'",
        )


class TestRulings:
    """The register over HTTP, which is the surface the ``fivee`` command reads."""

    def test_the_whole_register_is_served(self, editor: Editor) -> None:
        answer = editor.request("GET", "/api/v1/rulings").json()
        assert answer["count"] == len(answer["rulings"]) >= 1
        codes = {entry["code"] for entry in answer["rulings"]}
        assert "loading_capped_per_turn" in codes

    def test_an_entry_carries_the_trigger_that_would_overturn_it(
        self, editor: Editor
    ) -> None:
        answer = editor.request("GET", "/api/v1/rulings?code=loading_capped_per_turn").json()
        entry = answer["rulings"][0]
        assert entry["kind"] == "approximation"
        assert "Bonus Action attack" in entry["revisit"]

    def test_a_kind_narrows_the_register(self, editor: Editor) -> None:
        answer = editor.request("GET", "/api/v1/rulings?kind=srd_silent").json()
        assert answer["count"] >= 1
        assert {entry["kind"] for entry in answer["rulings"]} == {"srd_silent"}

    def test_an_unknown_code_is_404_not_400(self, editor: Editor) -> None:
        # Same distinction the rules lookup draws: a miss is an id that is not
        # there, not an argument the caller got wrong.
        assert_problem(
            editor.request("GET", "/api/v1/rulings?code=no_such_ruling"),
            404,
            "no ruling with code 'no_such_ruling'",
        )

    def test_an_unknown_kind_is_400_and_lists_the_legal_ones(self, editor: Editor) -> None:
        assert_problem(
            editor.request("GET", "/api/v1/rulings?kind=probably"),
            400,
            "unknown ruling kind 'probably'",
        )


class TestCatalogAndContent:
    def test_a_search_pages_and_reports_where_the_next_page_starts(
        self, editor: Editor
    ) -> None:
        page = editor.request("GET", "/api/v1/catalog/search?query=goblin&limit=1").json()
        assert page["limit"] == 1
        assert len(page["results"]) <= 1
        assert page["since"] == 0

    def test_a_search_without_its_required_parameter_is_refused(
        self, editor: Editor
    ) -> None:
        assert_problem(
            editor.request("GET", "/api/v1/catalog/search"),
            400,
            "query parameter 'query' is required",
        )

    def test_a_non_numeric_page_offset_names_the_parameter(self, editor: Editor) -> None:
        assert_problem(
            editor.request("GET", "/api/v1/catalog/search?query=goblin&since=soon"),
            400,
            "query parameter 'since' must be a whole number, not 'soon'",
        )

    def test_a_record_comes_back_whole_with_both_of_its_provenances(
        self, editor: Editor
    ) -> None:
        # The 404 below was this route's only exercise over the wire, and a
        # miss proves nothing about a hit: an adapter that had stopped passing
        # the id, or serialised the record's nested facts wrongly, would still
        # 404 for an id that is not there.
        response = editor.request(
            "GET", "/api/v1/catalog/records/1800-15-157-goblin-warrior"
        )

        assert response.status == 200
        record = response.json()
        assert record["id"] == "1800-15-157-goblin-warrior"
        assert (record["kind"], record["name"]) == ("creature", "Goblin Warrior")
        assert record["provenance"] == "SRD 5.2.1"
        assert record["facts"]["armor_class"] == 15
        assert record["facts"]["actions"]["Scimitar"]["attack_bonus"] == 4
        # Catalog identity and executable content are separate provenances and
        # the record keeps them apart, which is the whole point of the field.
        assert record["content_ref"] == {"section": "creatures", "name": "Goblin Warrior"}
        assert record["sources"] == {
            "catalog": "bundled:catalog-15-monsters-a-z.json",
            "executable": "bundled:catalog-15-monsters-a-z.json",
        }

    def test_a_table_answers_a_bounded_window_and_where_the_next_starts(
        self, editor: Editor
    ) -> None:
        response = editor.request(
            "GET", "/api/v1/catalog/tables/006-ability-modifiers?since=2&limit=3"
        )

        assert response.status == 200
        page = response.json()
        assert page["id"] == "006-ability-modifiers"
        assert page["name"] == "Ability Modifiers"
        assert [column["id"] for column in page["columns"]] == [
            "score", "modifier", "score-2", "modifier-2"
        ]
        # The window is the claim: three of eight rows from the third, and the
        # offset the caller pages from next rather than a page number.
        assert (page["since"], page["limit"], page["total"]) == (2, 3, 8)
        assert page["next_since"] == 5
        assert len(page["rows"]) == 3
        assert page["rows"][0]["cells"][1] == {"value": -3, "numeric_value": -3}

        whole = editor.request("GET", "/api/v1/catalog/tables/006-ability-modifiers").json()
        assert len(whole["rows"]) == whole["total"] == 8
        assert whole["next_since"] is None  # nothing left to page to

    def test_an_unknown_record_id_is_404_and_names_what_is_available(
        self, editor: Editor
    ) -> None:
        problem = assert_problem(
            editor.request("GET", "/api/v1/catalog/records/srd:nothing:here"),
            404,
            "no catalog record with id 'srd:nothing:here'",
        )
        assert "Available:" in problem["detail"]

    def test_an_unknown_table_id_is_404(self, editor: Editor) -> None:
        assert_problem(
            editor.request("GET", "/api/v1/catalog/tables/srd:table:nope"),
            404,
            "no catalog table with id 'srd:table:nope'",
        )

    def test_content_status_reports_the_bundled_slice(self, editor: Editor) -> None:
        answer = editor.request("GET", "/api/v1/content").json()
        assert answer["builtin"] == "include"
        assert answer["generation"] >= 1
        assert answer["counts"]["conditions"] >= 1

    def test_configuring_nothing_is_refused_with_the_reason(
        self, editor: Editor
    ) -> None:
        assert_problem(
            editor.request("POST", "/api/v1/content/configuration", json_body={}),
            400,
            "there is nothing to change",
        )

    def test_excluding_the_bundled_slice_moves_what_the_engine_reports(
        self, editor: Editor
    ) -> None:
        # And it moves the *server's* registry, not a copy: the terrain table
        # map validation uses comes from the same place, which is why this
        # adapter no longer holds one of its own.
        configured = editor.request(
            "POST", "/api/v1/content/configuration", json_body={"builtin": "exclude"}
        ).json()
        assert configured["changed"] is True
        assert configured["builtin"] == "exclude"
        assert editor.request("GET", "/api/v1/content").json()["builtin"] == "exclude"

    def test_validating_a_pack_path_that_is_not_there_reports_it(
        self, editor: Editor, tmp_path: Path
    ) -> None:
        answer = editor.request(
            "POST",
            "/api/v1/content/validations",
            json_body={"paths": [str(tmp_path / "no-such-pack.json")]},
        ).json()
        assert answer["ok"] is False
        assert answer["errors"]


class TestAnalytics:
    def test_a_batch_reports_one_outcome_per_iteration(self, editor: Editor) -> None:
        result = editor.request(
            "POST",
            "/api/v1/analytics/rounds",
            json_body={
                "combatants": [HERO, GOBLIN],
                "iterations": 5,
                "seed": 7,
                "max_rounds": 10,
            },
        ).json()
        assert sum(result["wins"].values()) == 5

    def test_a_dpr_run_answers_a_damage_figure(self, editor: Editor) -> None:
        result = editor.request(
            "POST",
            "/api/v1/analytics/dpr",
            json_body={"build": HERO, "target_ac": 15, "rounds": 2, "iterations": 20},
        ).json()
        assert result["damage_per_round"] > 0

    def test_a_zero_iteration_batch_is_refused_by_the_service(
        self, editor: Editor
    ) -> None:
        assert_problem(
            editor.request(
                "POST",
                "/api/v1/analytics/rounds",
                json_body={"combatants": [HERO, GOBLIN], "iterations": 0},
            ),
            400,
            "at least 1",
        )

    def test_a_missing_required_field_names_it_and_the_valid_keys(
        self, editor: Editor
    ) -> None:
        problem = assert_problem(
            editor.request("POST", "/api/v1/analytics/dpr", json_body={"build": HERO}),
            400,
            "'target_ac' is required",
        )
        assert "Valid keys: build, distance, iterations, rounds, seed, target_ac" in (
            problem["detail"]
        )

    def test_scenario_timing_answers_the_traveller_alone_without_a_response(
        self, editor: Editor
    ) -> None:
        result = editor.request(
            "POST",
            "/api/v1/analytics/scenario-timing",
            json_body={"distance_feet": 120, "speed_feet": 30},
        ).json()
        assert result["traveller"]["travel_rounds"] == 4
        assert result["traveller"]["arrival_after_rounds"] == 4


def combatants() -> list[dict[str, Any]]:
    return [dict(HERO), dict(GOBLIN)]


class TestEncountersOverHttp:
    """A fight, start to finish, over the transport the engine now speaks."""

    def create(self, editor: Editor, **body: Any) -> Response:
        return editor.request(
            "POST", "/api/v1/encounters",
            json_body={"combatants": combatants(), "seed": 11, **body},
        )

    def test_creating_a_fight_answers_201_with_where_to_find_it(
        self, editor: Editor
    ) -> None:
        created = self.create(editor)
        assert created.status == 201
        answer = created.json()
        encounter_id = answer["encounter_id"]
        assert answer["seed"] == 11
        assert created.headers["Location"] == f"/api/v1/encounters/{encounter_id}"
        assert created.headers["ETag"]

    def test_the_state_is_readable_and_carries_the_journal_head_as_an_etag(
        self, editor: Editor
    ) -> None:
        created = self.create(editor)
        encounter_id = created.json()["encounter_id"]
        state = editor.request("GET", f"/api/v1/encounters/{encounter_id}")
        assert state.status == 200
        assert {row["name"] for row in state.json()["combatants"]} == {"Thora", "Goblin"}
        assert state.headers["ETag"] == created.headers["ETag"]

    def test_the_log_pages_from_the_sequence_it_is_given(self, editor: Editor) -> None:
        encounter_id = self.create(editor).json()["encounter_id"]
        whole = editor.request("GET", f"/api/v1/encounters/{encounter_id}/log").json()
        assert whole["total_events"] >= 1
        paged = editor.request(
            "GET", f"/api/v1/encounters/{encounter_id}/log?limit=1&include_actions=false"
        ).json()
        assert len(paged["events"]) == 1
        assert "actions" not in paged

    def test_advancing_moves_the_turn_and_moves_the_etag(self, editor: Editor) -> None:
        created = self.create(editor)
        encounter_id = created.json()["encounter_id"]
        before = created.json()["state"]["turn"]
        advanced = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/advance?view=full"
        )
        assert advanced.status == 200
        assert advanced.json()["state"]["turn"] != before
        assert advanced.headers["ETag"] != created.headers["ETag"]

    def test_a_note_is_created_and_carries_its_own_timestamp(
        self, editor: Editor
    ) -> None:
        encounter_id = self.create(editor).json()["encounter_id"]
        written = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/notes",
            json_body={"text": "the goblin flinches", "category": "narration"},
        )
        assert written.status == 201
        assert written.json()["category"] == "narration"
        assert written.json()["timestamp"]

    def test_a_note_names_its_speaker_and_an_unknown_one_is_the_surface_404(
        self, editor: Editor
    ) -> None:
        encounter_id = self.create(editor).json()["encounter_id"]
        spoken = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/notes",
            json_body={"text": "hold there", "speaker": "Thora"},
        )
        assert spoken.status == 201
        assert spoken.json()["speaker"] == "Thora"
        assert_problem(
            editor.request(
                "POST", f"/api/v1/encounters/{encounter_id}/notes",
                json_body={"text": "hold there", "speaker": "Kettle"},
            ),
            404,
            "no combatant named 'Kettle' in this encounter",
        )

    def test_a_blank_note_is_refused_by_the_service(self, editor: Editor) -> None:
        encounter_id = self.create(editor).json()["encounter_id"]
        assert_problem(
            editor.request(
                "POST", f"/api/v1/encounters/{encounter_id}/notes",
                json_body={"text": "   "},
            ),
            400,
            "note text must not be blank",
        )

    def test_an_unsaved_editor_buffer_posts_inline_as_the_document_it_is(
        self, editor: Editor
    ) -> None:
        """Play on a buffer that has never been saved, over the real transport.

        The editor sends ``map_id`` for a map the server already has and the
        whole buffer for one it does not, and a buffer is a ``fivee-sim-map``
        document. The spec parser used to answer that with ``unknown map key
        'format'`` — a 400 naming the very key that identifies the format — so
        Play on an unsaved map was broken end to end. The unit cases in
        ``tests/test_encounter_map_input.py`` own the dispatch; this is the
        claim the browser depends on.
        """
        created = editor.request(
            "POST", "/api/v1/encounters",
            json_body={
                # Feet, not squares: the chamber is five squares wide and its
                # only floor is the 3x2 interior, so (5, 5) and (15, 10).
                "combatants": [
                    {**HERO, "position": [5, 5]}, {**GOBLIN, "position": [15, 10]}
                ],
                "seed": 11,
                "map": payload(),
            },
        )

        assert created.status == 201, created.body
        state = editor.request(
            "GET", f"/api/v1/encounters/{created.json()['encounter_id']}"
        ).json()
        assert state["map"]["name"] == "editor chamber"
        assert state["map_source"] is None, "an inline map has no file to source"

    def test_an_inline_document_that_does_not_parse_is_422_with_its_diagnostics(
        self, editor: Editor
    ) -> None:
        """The refusal a *saved* map gets, for a map that was never saved.

        422 and a diagnostic list rather than a bare 400: an inline document is
        parsed by the map service, so it is refused in the map service's own
        words. ``source`` says ``inline map`` so a client knows the complaint is
        about the body it sent and not some file on the host.
        """
        broken = {**payload(), "tiles": ["##", "##"]}

        problem = assert_problem(
            editor.request(
                "POST", "/api/v1/encounters",
                json_body={
                    "combatants": [{**HERO, "position": [5, 5]}, dict(GOBLIN)],
                    "seed": 11,
                    "map": broken,
                },
            ),
            422,
            "the grid is 5 squares wide",
        )
        assert [one["source"] for one in problem["diagnostics"]] == ["inline map"] * 3

    def test_an_inline_map_that_is_neither_shape_is_refused_as_a_spec(
        self, editor: Editor
    ) -> None:
        """No ``format``, so it is read as a spec and told which keys a spec has.

        The complement of the case above, and the reason the dispatch is on the
        key's *presence*: an object that never claimed to be a document is still
        judged by the spec parser, whose refusal lists the spec's own keys.
        """
        assert_problem(
            self.create(editor, map={"grid": {"width": 5, "height": 4}}),
            400,
            "unknown map key 'grid'",
        )

    def attempts(self, editor: Editor, encounter_id: str) -> list[dict[str, Any]]:
        exported = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/replay",
            json_body={"format_version": 2},
        )
        assert exported.status == 200
        return list(exported.json()["bundle"]["attempts"])

    def test_an_oversized_name_is_refused_before_anything_is_journalled(
        self, editor: Editor
    ) -> None:
        # The journal records an attempt's arguments *before* the operation body
        # validates them, so a refusal that happens in the body has already
        # written whatever it was handed. Bounding the field in the route schema
        # is what moves the refusal to the other side of that write; a check
        # inside the service cannot, however early it runs.
        encounter_id = self.create(editor).json()["encounter_id"]
        before = len(self.attempts(editor, encounter_id))

        assert_problem(
            editor.request(
                "POST", f"/api/v1/encounters/{encounter_id}/conditions",
                json_body={"target": "Thora", "condition": "x" * 5000},
            ),
            400,
            "'condition' must be at most",
        )

        assert len(self.attempts(editor, encounter_id)) == before

    def test_a_refusal_from_the_operation_body_is_still_journalled(
        self, editor: Editor
    ) -> None:
        # The contrast that gives the test above its meaning: a name of legal
        # length but no such combatant is refused by the body, and that attempt
        # *is* recorded — as it should be, since it is a real call against a real
        # fight. Only unbounded input is kept off the disk.
        encounter_id = self.create(editor).json()["encounter_id"]

        assert_problem(
            editor.request(
                "POST", f"/api/v1/encounters/{encounter_id}/conditions",
                json_body={"target": "Nobody", "condition": "poisoned"},
            ),
            400,
            "no combatant named 'Nobody'",
        )

        assert self.attempts(editor, encounter_id)[-1]["status"] == "refused"

    def test_an_oversized_note_is_refused_by_the_schema(self, editor: Editor) -> None:
        encounter_id = self.create(editor).json()["encounter_id"]
        assert_problem(
            editor.request(
                "POST", f"/api/v1/encounters/{encounter_id}/notes",
                json_body={"text": "x" * 5000},
            ),
            400,
            "'text' must be at most",
        )

    def test_an_oversized_correction_reason_is_refused_by_the_schema(
        self, editor: Editor
    ) -> None:
        encounter_id = self.create(editor).json()["encounter_id"]
        assert_problem(
            editor.request(
                "POST", f"/api/v1/encounters/{encounter_id}/corrections",
                json_body={"state": {"Thora": {"ac": 11}}, "reason": "x" * 5000},
            ),
            400,
            "'reason' must be at most",
        )

    def test_an_unknown_encounter_is_404_and_names_the_active_ones(
        self, editor: Editor
    ) -> None:
        assert_problem(
            editor.request("GET", "/api/v1/encounters/enc-404"),
            404,
            "unknown encounter 'enc-404'",
        )

    def test_the_listing_finds_the_fight_on_disk(self, editor: Editor) -> None:
        encounter_id = self.create(editor).json()["encounter_id"]
        listed = editor.request("GET", "/api/v1/encounters").json()
        assert [entry["encounter_id"] for entry in listed["encounters"]] == [encounter_id]
        assert listed["encounters"][0]["status"] == "active"

    def test_pruning_lists_the_stranded_ids_and_then_reclaims_them(
        self, editor: Editor
    ) -> None:
        """The route and its handler, which nothing else exercises.

        Every other case for ``encounter.prune`` calls the service body, and
        ``tests/api.py`` reaches that without a transport — so a path nothing
        routes to, a body key spelled one way in the schema and another in the
        handler, or a handler nobody registered would all ship green. The id is
        stranded by hand because no *request* can strand one:
        ``create`` claims after the roster is built and refuses before that, so
        what leaves a claim behind is a failed blob write or a dead process.
        """
        kept = self.create(editor).json()["encounter_id"]
        assert journal_service.claim("enc-9001") is True

        listed = editor.request("POST", "/api/v1/encounters/prune")
        assert listed.status == 200
        assert listed.json() == {"applied": False, "encounters": ["enc-9001"]}
        assert (encounters_root() / "enc-9001").is_dir()

        applied = editor.request(
            "POST", "/api/v1/encounters/prune", json_body={"apply": True}
        )
        assert applied.status == 200
        assert applied.json() == {"applied": True, "encounters": ["enc-9001"]}
        assert not (encounters_root() / "enc-9001").exists()
        assert (encounters_root() / kept).is_dir()

    def test_a_prune_that_cannot_finish_is_a_refusal_rather_than_a_bug_report(
        self, editor: Editor
    ) -> None:
        """The refusal path of a destructive operation, over the transport.

        The happy path above is the only place this route was exercised, which
        is how it went unnoticed that ``encounters.prune`` translated nothing:
        ``JournalError`` is a bare ``ValueError`` rather than a ``RequestError``,
        so it reached ``_dispatch``'s catch-all and an operator asking to reclaim
        stranded ids was told the engine had a defect. A lock that will not open
        is a refusal about a file, and it is answered as one.
        """
        assert journal_service.claim("enc-9001") is True
        guard = encounters_root() / "enc-9001" / "journal.jsonl.lock"
        guard.symlink_to(encounters_root() / "elsewhere")

        refused = editor.request(
            "POST", "/api/v1/encounters/prune", json_body={"apply": True}
        )

        assert_problem(refused, 400, "cannot lock")

    def test_an_unknown_status_filter_names_the_three_that_work(
        self, editor: Editor
    ) -> None:
        assert_problem(
            editor.request("GET", "/api/v1/encounters?status=lively"),
            400,
            "query parameter 'status' must be one of: active, finalized, all",
        )

    def test_finalizing_writes_the_replay_and_links_into_this_viewer(
        self, editor: Editor
    ) -> None:
        encounter_id = self.create(editor).json()["encounter_id"]
        finalized = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/finalize"
        ).json()
        assert finalized["status"] == "finalized"
        assert Path(finalized["replay_path"]).exists()
        # And the fight is then closed to writes, which is what finalized means.
        assert_problem(
            editor.request("POST", f"/api/v1/encounters/{encounter_id}/advance"),
            400,
            "is finalized",
        )

    def test_an_exported_replay_comes_back_inline_and_names_this_viewer(
        self, editor: Editor
    ) -> None:
        encounter_id = self.create(editor).json()["encounter_id"]
        exported = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/replay",
            json_body={"path": str(editor.replays_dir / "brawl.json")},
        ).json()
        assert Path(exported["path"]).exists()
        # The fragment is the token, and it goes after the query: that is the
        # whole of how the page is told one, now that the served body no longer
        # names it. A browser never sends a fragment, so handing this link
        # around is not the disclosure that injecting it into the body was.
        assert exported["viewer_url"] == (
            f"http://127.0.0.1:{editor.server.port}/viewer"
            f"?replay=brawl#{editor.server.token}"
        )
        # The bundle it just wrote is the one the viewer would play.
        assert editor.request("GET", "/api/v1/replays/brawl").status == 200

    def test_a_bundle_written_outside_the_served_root_gets_no_viewer_link(
        self, editor: Editor
    ) -> None:
        """A link this server could not honour is worse than no link at all.

        The viewer plays what is under the replays root this launch serves, so
        a bundle written anywhere else would 404 in the user's browser and be
        blamed on the export. The absence is the contract, not an oversight.
        """
        encounter_id = self.create(editor).json()["encounter_id"]
        exported = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/replay",
            json_body={"path": str(editor.replays_dir.parent / "stray" / "away.json")},
        ).json()
        assert Path(exported["path"]).exists()
        assert "viewer_url" not in exported

    def test_resuming_reads_the_fight_back_from_its_journal(
        self, editor: Editor
    ) -> None:
        encounter_id = self.create(editor).json()["encounter_id"]
        editor.request("POST", f"/api/v1/encounters/{encounter_id}/advance")
        # Drop the in-memory copy: what comes back must come from the journal.
        editor.server.state.sessions.clear()
        resumed = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/resume"
        ).json()
        assert resumed["recovered"] is True
        assert resumed["state"]["round"] >= 1


#: One party member, standing somewhere. An interlude's whole roster can be
#: this: nobody is opposing anybody, so the two-sides rule a fight is built on
#: has nothing to say about it.
SCOUT: dict[str, Any] = {
    "name": "Kettle",
    "team": "party",
    "ac": 13,
    "max_hp": 9,
    "position": [0, 0],
}

#: A saved map for the interlude cases below: open floor, and an id, because
#: carrying a map is carrying an id.
MILL_DOCUMENT: dict[str, Any] = {
    "format": "fivee-sim-map",
    "format_version": 1,
    "name": "The Drowned Mill",
    "grid": {"width": 12, "height": 12, "cell_feet": 5},
    "legend": {".": "normal"},
    "tiles": ["." * 12 for _ in range(12)],
    "features": [],
    "provenance": {
        "generator": "hand",
        "seed": 0,
        "params": {},
        "edited": False,
        "source": "synthetic test fixture, not SRD content",
    },
}


class TestInterludesOverHttp:
    """The chapter with no fight in it, driven the way a caller drives it.

    ``model/encounter.py`` owns what an interlude *is* and pins it in
    ``tests/test_encounter.py``. What is checked here is the half a caller can
    reach: that the mode crosses the wire, that the acting seam takes a name,
    and that each refusal arrives as the sentence naming the mode that refused
    rather than as a bare status.
    """

    def interlude(self, editor: Editor, **body: Any) -> Response:
        return editor.request(
            "POST", "/api/v1/encounters",
            json_body={
                "combatants": [dict(HERO), dict(SCOUT)],
                "seed": 11,
                "mode": "exploration",
                **body,
            },
        )

    def test_an_interlude_reports_the_mode_and_holds_nobody_s_turn(
        self, editor: Editor
    ) -> None:
        created = self.interlude(editor)
        assert created.status == 201
        state = created.json()["state"]
        assert state["mode"] == "exploration"
        # Nobody holds the floor between beats, so there is no turn to name and
        # no round for one to belong to.
        assert state["turn"] is None

    def test_a_fight_is_still_the_mode_a_caller_gets_without_asking(
        self, editor: Editor
    ) -> None:
        created = editor.request(
            "POST", "/api/v1/encounters",
            json_body={"combatants": combatants(), "seed": 11},
        )
        assert created.json()["state"]["mode"] == "combat"
        assert created.json()["state"]["turn"]

    def test_one_combatant_is_a_whole_interlude(self, editor: Editor) -> None:
        created = self.interlude(editor, combatants=[dict(SCOUT)])
        assert created.status == 201
        assert [one["name"] for one in created.json()["state"]["combatants"]] == [
            "Kettle"
        ]

    def test_a_fight_still_needs_two_sides_worth_of_combatants(
        self, editor: Editor
    ) -> None:
        assert_problem(
            editor.request(
                "POST", "/api/v1/encounters",
                json_body={"combatants": [dict(SCOUT)], "seed": 11},
            ),
            400,
            "an encounter needs at least two combatants",
        )

    def test_an_interlude_still_needs_somebody_to_stand_somewhere(
        self, editor: Editor
    ) -> None:
        assert_problem(
            self.interlude(editor, combatants=[]),
            400,
            "an interlude needs at least one combatant",
        )

    def test_a_reinforcement_is_refused_where_no_round_could_bring_them(
        self, editor: Editor
    ) -> None:
        """``arrival_round`` is inert here, so it is refused rather than ignored.

        Nothing arrives when no round turns over, so a combatant scheduled for
        round 2 would be refused every act for the life of the chapter with
        "does not arrive until round 2" — a fight's sentence, in a chapter that
        has no rounds. The refusal belongs at the spec.
        """
        assert_problem(
            self.interlude(
                editor,
                combatants=[dict(SCOUT), {**HERO, "arrival_round": 2}],
            ),
            400,
            "an interlude has no rounds, so combatant 'Thora' cannot arrive",
        )

    def test_an_act_names_who_takes_it(self, editor: Editor) -> None:
        encounter_id = self.interlude(editor).json()["encounter_id"]
        acted = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/actions?view=full",
            json_body={"kind": "move", "to_position": [10, 0], "actor": "Kettle"},
        )
        assert acted.status == 200
        moved = next(
            one for one in acted.json()["state"]["combatants"] if one["name"] == "Kettle"
        )
        assert moved["position"] == [10, 0]

    def test_an_act_that_names_nobody_is_refused_by_the_mode(
        self, editor: Editor
    ) -> None:
        encounter_id = self.interlude(editor).json()["encounter_id"]
        assert_problem(
            editor.request(
                "POST", f"/api/v1/encounters/{encounter_id}/actions",
                json_body={"kind": "dodge"},
            ),
            400,
            "an interlude has no initiative, so an act must name its actor",
        )

    def test_a_fight_refuses_an_act_that_names_an_actor(self, editor: Editor) -> None:
        created = editor.request(
            "POST", "/api/v1/encounters",
            json_body={"combatants": combatants(), "seed": 11},
        )
        encounter_id = created.json()["encounter_id"]
        assert_problem(
            editor.request(
                "POST", f"/api/v1/encounters/{encounter_id}/actions",
                json_body={"kind": "dodge", "actor": "Thora"},
            ),
            400,
            "initiative decides who acts",
        )

    def test_an_unknown_actor_is_refused_in_the_surface_s_one_sentence(
        self, editor: Editor
    ) -> None:
        """The seat refusal, from a fifth door and with the same status.

        Word for word what ``encounter.brief`` answers a name this cast does not
        hold, and a ``404`` for the same reason: an actor is a thing that is or
        is not in this chapter. It names nobody else, so guessing wrong tells a
        guesser nothing about who is really here.
        """
        encounter_id = self.interlude(editor).json()["encounter_id"]
        problem = assert_problem(
            editor.request(
                "POST", f"/api/v1/encounters/{encounter_id}/actions",
                json_body={"kind": "dodge", "actor": "Nobody"},
            ),
            404,
            "no combatant named 'Nobody' in this encounter",
        )
        assert "Kettle" not in problem["detail"]

    def test_an_interlude_has_no_round_to_advance(self, editor: Editor) -> None:
        encounter_id = self.interlude(editor).json()["encounter_id"]
        assert_problem(
            editor.request("POST", f"/api/v1/encounters/{encounter_id}/advance"),
            400,
            "an interlude has no rounds to advance",
        )

    def test_a_seat_s_brief_names_the_mode_and_nobody_s_turn(
        self, editor: Editor
    ) -> None:
        encounter_id = self.interlude(editor).json()["encounter_id"]
        brief = editor.request(
            "GET", f"/api/v1/encounters/{encounter_id}/brief?as=Kettle"
        ).json()
        assert brief["mode"] == "exploration"
        # Null rather than false: "not your turn" is a fact about a fight, and
        # this chapter has no turns for it to be a fact about.
        assert brief["turn"] is None
        assert brief["your_turn"] is None

    def test_a_fight_s_brief_still_answers_whose_turn_it_is(
        self, editor: Editor
    ) -> None:
        created = editor.request(
            "POST", "/api/v1/encounters",
            json_body={"combatants": combatants(), "seed": 11},
        )
        encounter_id = created.json()["encounter_id"]
        brief = editor.request(
            "GET", f"/api/v1/encounters/{encounter_id}/brief?as=Thora"
        ).json()
        assert brief["mode"] == "combat"
        assert brief["your_turn"] in (True, False)
        assert brief["turn"] is not None


#: A fight with something to hide: a foe whose sheet carries unmistakable
#: values, and a reinforcement who is in the initiative order but not on the
#: battlefield. The numbers are chosen so that finding one in a response is
#: proof of where it came from — no position, initiative, round or distance in
#: this fixture can produce them.
BRIEFING_FOE_AC = 4093
BRIEFING_FOE_MAX_HP = 4400
BRIEFING_FOE_HP = 3300
BRIEFING_FOE_ATTACK = "Rustcleaver"
BRIEFING_AMBUSHER = "Zzaxil"


def briefing_roster() -> list[dict[str, Any]]:
    return [
        dict(HERO),
        {
            "name": "Grelk",
            "team": "monsters",
            "ac": BRIEFING_FOE_AC,
            "max_hp": BRIEFING_FOE_MAX_HP,
            "hp": BRIEFING_FOE_HP,
            "items": {"Emberflask": 5},
            "spell_slots": {"3": 2},
            "attacks": [
                {
                    "name": BRIEFING_FOE_ATTACK,
                    "attack_bonus": 6,
                    "damage": "2d6+4",
                    "damage_type": "slashing",
                    "kind": "melee",
                }
            ],
            "position": [35, 0],
        },
        {
            "name": BRIEFING_AMBUSHER,
            "team": "monsters",
            "ac": 12,
            "max_hp": 9,
            "position": [60, 0],
            "arrival_round": 9,
        },
    ]


#: The foe a *write* can actually hurt. Grelk's AC is 4093 and it stands 35 feet
#: off — both deliberate, both right for the brief above, and both fatal to a
#: write case: the leak those exist to catch rides in on a ``damage`` event, and
#: a swing that cannot reach and could never land emits none. So the writes add
#: one more monster, next to the hero and hittable, whose hit points are as
#: unmistakable as everything else in this fixture.
BRIEFING_DUMMY = "Skrit"
BRIEFING_DUMMY_MAX_HP = 7700
BRIEFING_DUMMY_HP = 6600


def briefing_brawl() -> list[dict[str, Any]]:
    return [
        *briefing_roster(),
        {
            "name": BRIEFING_DUMMY,
            "team": "monsters",
            "ac": 5,
            "max_hp": BRIEFING_DUMMY_MAX_HP,
            "hp": BRIEFING_DUMMY_HP,
            "position": [5, 0],
        },
    ]


class TestThePlayerBriefOverHttp:
    """``encounter.brief`` is the seat's view, and its promise is byte-level.

    The unit cases in ``tests/test_player_brief.py`` own the classification;
    what is checked here is the thing a client actually receives. The assertions
    read ``response.body`` rather than the parsed dictionary because the leak
    being guarded against is a nested one — a withheld value surviving inside
    some summary a key check never looks at.
    """

    def create(self, editor: Editor) -> str:
        created = editor.request(
            "POST", "/api/v1/encounters",
            json_body={"combatants": briefing_roster(), "seed": 11},
        )
        assert created.status == 201, created.body
        return str(created.json()["encounter_id"])

    def test_the_brief_answers_200_and_carries_the_encounters_etag(
        self, editor: Editor
    ) -> None:
        encounter_id = self.create(editor)

        brief = editor.request(
            "GET", f"/api/v1/encounters/{encounter_id}/brief?as=Thora"
        )

        assert brief.status == 200
        assert brief.json()["as"] == "Thora"
        state = editor.request("GET", f"/api/v1/encounters/{encounter_id}")
        assert brief.headers["ETag"] == state.headers["ETag"]

    def test_the_response_bytes_hold_no_part_of_an_opponents_sheet(
        self, editor: Editor
    ) -> None:
        encounter_id = self.create(editor)

        brief = editor.request(
            "GET", f"/api/v1/encounters/{encounter_id}/brief?as=Thora"
        )

        gm = editor.request("GET", f"/api/v1/encounters/{encounter_id}").body
        for secret in (
            str(BRIEFING_FOE_AC),
            str(BRIEFING_FOE_MAX_HP),
            str(BRIEFING_FOE_HP),
            BRIEFING_FOE_ATTACK,
            "Emberflask",
        ):
            # In the GM's payload by construction, and out of the player's.
            assert secret.encode("utf-8") in gm, secret
            assert secret.encode("utf-8") not in brief.body, secret

    def test_a_creature_who_has_not_arrived_is_named_nowhere_in_the_response(
        self, editor: Editor
    ) -> None:
        encounter_id = self.create(editor)

        brief = editor.request(
            "GET", f"/api/v1/encounters/{encounter_id}/brief?as=Thora"
        )

        gm = editor.request("GET", f"/api/v1/encounters/{encounter_id}").body
        assert BRIEFING_AMBUSHER.encode("utf-8") in gm
        assert BRIEFING_AMBUSHER.encode("utf-8") not in brief.body

    def test_the_askers_own_sheet_arrives_whole_and_the_foe_gets_a_band(
        self, editor: Editor
    ) -> None:
        encounter_id = self.create(editor)

        brief = editor.request(
            "GET", f"/api/v1/encounters/{encounter_id}/brief?as=Thora"
        ).json()

        seat = brief["you"]
        assert seat["hp"] == HERO["max_hp"]
        assert seat["ac"] == HERO["ac"]
        assert seat["attacks"] == ["Longsword"]
        foe = next(one for one in brief["enemies"] if one["name"] == "Grelk")
        assert foe["health"] in HEALTH_BANDS
        assert "hp" not in foe and "ac" not in foe

    def test_a_seat_the_fight_does_not_hold_is_404_and_names_no_one_else(
        self, editor: Editor
    ) -> None:
        encounter_id = self.create(editor)

        refused = editor.request(
            "GET", f"/api/v1/encounters/{encounter_id}/brief?as=Nobody"
        )

        problem = assert_problem(refused, 404, "no combatant named 'Nobody'")
        # The refusal must not become the disclosure: listing the cast would
        # hand a player-chair client the very name the projection withholds.
        assert BRIEFING_AMBUSHER not in json.dumps(problem)

    def test_the_seat_must_be_named_at_all(self, editor: Editor) -> None:
        encounter_id = self.create(editor)

        refused = editor.request("GET", f"/api/v1/encounters/{encounter_id}/brief")

        assert_problem(refused, 400, "query parameter 'as' is required")


class TestThePlayerBriefOnTheWrites:
    """``as=`` on the four operations that answer a caller with a fight's state.

    ``encounter.brief`` was this projection's only door for a release, and every
    *mutating* operation answered the seat that posted it with the GM's whole
    snapshot: an opponent's exact hit points, AC, items and weapons, in the body
    of the player's own action. ``web/static/play.js`` survived that by reading
    ``events`` out of the answer and discarding the rest — client-side hiding,
    which is the failure mode this projection was written to avoid, and which is
    inert against devtools, a proxy or an extension.

    So the four routes take the same ``as=`` ``encounter.brief`` does, they
    answer with the same projection in the same shape, and the same 404 refuses
    a name the fight does not hold. These cases read ``response.body`` for the
    reason the class above does: the leak being guarded against is a nested
    one.

    **The parameter is optional, and its absence is a promise.** The CLI, the
    ``encounter-sim`` skill and the ``play`` skill all read ``state`` from
    these answers, so a call that names no chair must get back exactly what it
    got before — which is a claim in its own right below, not an assumption.

    **The events are narrowed with it, and for a release they were not.** This
    docstring used to close by calling that a boundary of the fix rather than an
    oversight, and the cases below acted with ``dodge`` — whose events name
    nobody but the actor and carry no number worth hiding — so the byte
    assertions passed over the events channel without ever touching it. They
    were passing vacuously. ``events[].data`` was the same fight's own account
    with none of it withheld: a ``damage`` event answered a player's own swing
    with ``{"hp": 6594, "max_hp": 7700}`` beside a ``state`` that said "hurt",
    and an ``attack`` event's ``total`` bracketed the AC it was rolled against.
    So the cases below now swing, land, and hurt somebody.
    """

    #: Every value from the other side's sheets that finding in a response is
    #: proof of. The ambusher's *name* rides along because an undetected
    #: creature is omitted rather than unlabelled, and a write must omit them
    #: too. The hit points the swing actually moves are not written down here —
    #: :meth:`secrets_of` reads them back, because a constant would go stale the
    #: moment the blow landed and pass against the number the response holds.
    SECRETS = (
        str(BRIEFING_FOE_AC),
        str(BRIEFING_FOE_MAX_HP),
        str(BRIEFING_FOE_HP),
        BRIEFING_FOE_ATTACK,
        "Emberflask",
        BRIEFING_AMBUSHER,
        str(BRIEFING_DUMMY_MAX_HP),
        str(BRIEFING_DUMMY_HP),
    )

    def create(self, editor: Editor, seat: str = "") -> Response:
        return editor.request(
            "POST",
            "/api/v1/encounters" + (f"?as={seat}" if seat else ""),
            json_body={"combatants": briefing_brawl(), "seed": 11},
        )

    def ready(self, editor: Editor) -> str:
        """A fight standing at Thora's turn, so the next write is hers to make."""
        encounter_id = str(self.create(editor).json()["encounter_id"])
        for _ in range(12):
            state = editor.request("GET", f"/api/v1/encounters/{encounter_id}").json()
            if state["turn"] == "Thora":
                return encounter_id
            editor.request("POST", f"/api/v1/encounters/{encounter_id}/advance")
        raise AssertionError("Thora never got a turn")

    def swing(
        self, editor: Editor, encounter_id: str, seat: str = "", view: str = "full"
    ) -> Response:
        """The write that leaks: a real attack that lands on a real opponent.

        ``view="full"`` because this class classifies keys and wants them all.
        A delta over the same answer can only carry a subset of what the brief
        let through — that is :mod:`tests.test_state_views`'s claim, and it is
        made against the projection rather than re-made here.
        """
        query = "&".join(
            part for part in (f"as={seat}" if seat else "", f"view={view}") if part
        )
        return editor.request(
            "POST",
            f"/api/v1/encounters/{encounter_id}/actions?{query}",
            json_body={
                "kind": "attack", "target": BRIEFING_DUMMY, "attack": "Longsword"
            },
        )

    def secrets_of(self, editor: Editor, encounter_id: str) -> tuple[str, ...]:
        """:data:`SECRETS`, plus whatever hit points the fight now stands at."""
        state = editor.request("GET", f"/api/v1/encounters/{encounter_id}").json()
        hurt = next(
            one for one in state["combatants"] if one["name"] == BRIEFING_DUMMY
        )
        return (*self.SECRETS, str(hurt["hp"]))

    def assert_briefed(
        self, editor: Editor, response: Response, seat: str, encounter_id: str
    ) -> None:
        """This is that seat's brief, and holds no byte of anyone else's sheet."""
        assert response.status in (200, 201), response.body
        # The brief's own shape, not a relabelled snapshot: one projection
        # answers the read and the write, so a client has one thing to render.
        state = response.json()["state"]
        assert state["as"] == seat
        assert "combatants" not in state and "ongoing_effects" not in state
        for secret in self.secrets_of(editor, encounter_id):
            assert secret.encode("utf-8") not in response.body, secret

    def assert_whole(self, response: Response) -> None:
        """This is the GM's answer: the fight reported as it always was.

        The hit points are read off the answer's own ``state`` rather than
        supplied, because the swing moves them: a fixed number would make this
        assert that a *stale* value is present, which the response quite
        correctly no longer carries.
        """
        assert response.status in (200, 201), response.body
        state = response.json()["state"]
        assert "as" not in state
        hurt = next(
            one for one in state["combatants"] if one["name"] == BRIEFING_DUMMY
        )
        for secret in (*self.SECRETS[:-1], str(hurt["hp"])):
            assert secret.encode("utf-8") in response.body, secret

    def test_creating_from_a_seat_answers_that_seat_and_no_one_elses_sheet(
        self, editor: Editor
    ) -> None:
        created = self.create(editor, seat="Thora")

        self.assert_briefed(
            editor, created, "Thora", created.json()["encounter_id"]
        )

    def test_acting_from_a_seat_answers_that_seat_and_no_one_elses_sheet(
        self, editor: Editor
    ) -> None:
        encounter_id = self.ready(editor)

        acted = self.swing(editor, encounter_id, seat="Thora")

        # The events are the half this used to pass over: the fixture is built
        # so the swing lands, so there is a damage event here to be narrowed.
        assert [one["kind"] for one in acted.json()["events"]] == ["attack", "damage"]
        self.assert_briefed(editor, acted, "Thora", encounter_id)

    def test_a_briefed_action_still_reports_what_the_table_watched(
        self, editor: Editor
    ) -> None:
        """Redaction that emptied the events would pass the case above."""
        encounter_id = self.ready(editor)

        acted = self.swing(editor, encounter_id, seat="Thora").json()

        swung, wound = acted["events"]
        assert swung["data"]["hit"] is True
        assert wound["data"]["amount"] > 0
        # Her own swing is hers to know, arithmetic and all: a table that says
        # "fifteen hits" has told the player who rolled it exactly this.
        assert swung["data"]["total"] == swung["data"]["natural"] + 5
        assert swung["data"]["attack"] == "Longsword"
        # And what the projection took is the target's sheet, not the account.
        assert "hp" not in wound["data"] and "max_hp" not in wound["data"]
        assert "detail" not in swung and "detail" not in wound

    def test_advancing_from_a_seat_answers_that_seat_and_no_one_elses_sheet(
        self, editor: Editor
    ) -> None:
        encounter_id = self.ready(editor)
        self.swing(editor, encounter_id)

        advanced = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/advance?as=Thora"
        )

        self.assert_briefed(editor, advanced, "Thora", encounter_id)

    def test_resuming_from_a_seat_answers_that_seat_and_no_one_elses_sheet(
        self, editor: Editor
    ) -> None:
        encounter_id = self.ready(editor)
        self.swing(editor, encounter_id)
        # Off the journal rather than out of memory, which is the path a
        # player's client actually takes after the server it was talking to
        # went away — and the one that reads ``map_source`` back in.
        editor.server.state.sessions.clear()

        resumed = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/resume?as=Thora"
        )

        self.assert_briefed(editor, resumed, "Thora", encounter_id)

    def test_a_retried_creation_is_briefed_like_the_first_answer(
        self, editor: Editor
    ) -> None:
        """The one whose events are not ``events``.

        ``create`` answers with ``log``, and an idempotent retry answers with
        the *whole* log of a fight already in progress rather than the opening
        pair a fresh one has. A projection that narrowed only ``events`` would
        hand a player every round of it.
        """
        body = {"combatants": briefing_brawl(), "seed": 11}
        again = {"Idempotency-Key": "the-same-fight"}
        first = editor.request(
            "POST", "/api/v1/encounters", json_body=body, headers=again
        )
        encounter_id = str(first.json()["encounter_id"])
        for _ in range(12):
            if (
                editor.request("GET", f"/api/v1/encounters/{encounter_id}").json()[
                    "turn"
                ]
                == "Thora"
            ):
                break
            editor.request("POST", f"/api/v1/encounters/{encounter_id}/advance")
        self.swing(editor, encounter_id)

        retried = editor.request(
            "POST", "/api/v1/encounters?as=Thora", json_body=body, headers=again
        )

        assert retried.json()["encounter_id"] == encounter_id
        assert len(retried.json()["log"]) > 2, "the retry replayed no fight to narrow"
        self.assert_briefed(editor, retried, "Thora", encounter_id)

    def test_naming_no_seat_leaves_every_write_answering_the_fight_whole(
        self, editor: Editor
    ) -> None:
        # The additive promise, pinned rather than assumed: this is what the
        # CLI and both skills read, and the fix must not have moved it.
        created = self.create(editor)
        encounter_id = str(created.json()["encounter_id"])
        for _ in range(12):
            if (
                editor.request("GET", f"/api/v1/encounters/{encounter_id}").json()[
                    "turn"
                ]
                == "Thora"
            ):
                break
            editor.request("POST", f"/api/v1/encounters/{encounter_id}/advance")
        acted = self.swing(editor, encounter_id)
        # ``view=full`` on the two that changed default. The additive promise is
        # about ``as=``, and it is intact: this payload is the fight whole.
        advanced = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/advance?view=full"
        )
        editor.server.state.sessions.clear()
        resumed = editor.request("POST", f"/api/v1/encounters/{encounter_id}/resume")

        for whole in (created, acted, advanced, resumed):
            self.assert_whole(whole)
        # Including the account of what happened, sentence and numbers both.
        struck = acted.json()["events"][0]
        assert struck["detail"] and "vs AC" in struck["detail"]
        assert acted.json()["events"][1]["data"]["max_hp"] == BRIEFING_DUMMY_MAX_HP

    def test_a_seat_the_fight_does_not_hold_is_refused_by_every_write(
        self, editor: Editor
    ) -> None:
        encounter_id = self.ready(editor)
        base = f"/api/v1/encounters/{encounter_id}"

        for method, path, body in (
            ("POST", f"{base}/actions?as=Nobody", {"kind": "dodge"}),
            ("POST", f"{base}/advance?as=Nobody", None),
            ("POST", f"{base}/resume?as=Nobody", None),
        ):
            refused = editor.request(method, path, json_body=body)

            problem = assert_problem(refused, 404, "no combatant named 'Nobody'")
            # The refusal is not the disclosure, exactly as ``encounter.brief``'s
            # is not: listing the cast would name the creature it withholds.
            assert BRIEFING_AMBUSHER not in json.dumps(problem), path

    def test_creating_under_a_seat_the_roster_lacks_starts_no_fight(
        self, editor: Editor
    ) -> None:
        # The one refusal that has to land *before* the operation runs. Left to
        # the projection it would refuse after the fight had been created,
        # journaled and given an id — a 404 with an orphan encounter behind it.
        refused = self.create(editor, seat="Nobody")

        assert_problem(refused, 404, "no combatant named 'Nobody'")
        listed = editor.request("GET", "/api/v1/encounters?status=all").json()
        assert listed["encounters"] == [], listed

    def test_a_briefed_write_still_carries_the_encounters_etag(
        self, editor: Editor
    ) -> None:
        # A player's client polls on the same version the GM's does, so a chair
        # that writes must be handed the same journal head a chair that reads is.
        encounter_id = self.ready(editor)

        acted = self.swing(editor, encounter_id, seat="Thora")

        state = editor.request("GET", f"/api/v1/encounters/{encounter_id}")
        assert acted.headers["ETag"] == state.headers["ETag"]


class TestEncounterActions:
    """The widest body in the contract, and the only route that spends a turn.

    ``POST /encounters/{id}/actions`` declares eighteen fields and the adapter
    hands every one of them to ``service.encounters.act`` **positionally**. A
    transposition there — a ``center`` arriving where ``direction`` goes — is a
    different fight that still answers 200, so a status is not enough to know
    the plumbing is right: these read the engine's own answer back. Which
    creature swung, what the die showed, what the move cost, and what the
    journal recorded is what pins field to parameter.

    The dice figures are golden for seed 11 with this pair, where the Goblin
    wins initiative 20 to 15 and acts first. A rules or dice-stream change
    turns them red on purpose; reproduce, then recalibrate deliberately.
    """

    def start(self, editor: Editor, goblin_at: list[int]) -> Response:
        """A fight at seed 11 with the Goblin placed where the case needs it."""
        return editor.request(
            "POST",
            "/api/v1/encounters",
            json_body={
                "combatants": [dict(HERO), {**GOBLIN, "position": goblin_at}],
                "seed": 11,
            },
        )

    def log_of(self, editor: Editor, encounter_id: str) -> dict[str, Any]:
        answer: dict[str, Any] = editor.request(
            "GET", f"/api/v1/encounters/{encounter_id}/log"
        ).json()
        return answer

    def test_an_attack_names_its_weapon_and_lands_on_the_named_target(
        self, editor: Editor
    ) -> None:
        created = self.start(editor, [5, 0])
        encounter_id = created.json()["encounter_id"]
        assert created.json()["state"]["turn"] == "Goblin"

        struck = editor.request(
            "POST",
            f"/api/v1/encounters/{encounter_id}/actions?view=full",
            json_body={"kind": "attack", "target": "Thora", "attack": "Scimitar"},
        )

        assert struck.status == 200
        swing, wound = struck.json()["events"]
        # `attack` chose the weapon and `target` chose whom: a body whose two
        # string fields were swapped would resolve the Goblin's other weapon,
        # or nothing at all, and still answer 200.
        assert (swing["kind"], swing["actor"], swing["target"]) == (
            "attack", "Goblin", "Thora"
        )
        assert swing["data"]["attack"] == "Scimitar"
        assert (swing["data"]["natural"], swing["data"]["total"]) == (15, 19)
        assert swing["data"]["hit"] is True
        assert swing["data"]["damage"] == 6
        assert wound["kind"] == "damage" and wound["data"]["amount"] == 6
        # And the fight moved: the damage is on the creature, and the swing
        # spent the turn's one attack rather than being replayed as a preview.
        hit_points = {row["name"]: row["hp"] for row in struck.json()["state"]["combatants"]}
        assert hit_points["Thora"] == 30 - swing["data"]["damage"] == 24
        turn_state = struck.json()["state"]["turn_state"]
        assert turn_state["action_used"] is True and turn_state["attacks_left"] == 0
        assert struck.headers["ETag"] != created.headers["ETag"]

    def test_the_journal_records_the_action_that_was_asked_for(
        self, editor: Editor
    ) -> None:
        # "durably audit it" is what the route promises beyond taking the turn,
        # and the audit is of the *arguments*: a field the adapter dropped on
        # the way through would be missing here even though the swing landed.
        encounter_id = self.start(editor, [5, 0]).json()["encounter_id"]
        editor.request(
            "POST",
            f"/api/v1/encounters/{encounter_id}/actions",
            json_body={"kind": "attack", "target": "Thora", "attack": "Scimitar"},
        )

        log = self.log_of(editor, encounter_id)

        assert log["total_actions"] == 1
        (recorded,) = log["actions"]
        assert recorded["actor"] == "Goblin"
        assert recorded["action"] == {
            "kind": "attack",
            "target": "Thora",
            "attack": "Scimitar",
            "as_bonus_action": False,
        }

    def test_a_move_carries_the_creature_and_spends_only_its_speed(
        self, editor: Editor
    ) -> None:
        created = self.start(editor, [15, 0])
        encounter_id = created.json()["encounter_id"]

        moved = editor.request(
            "POST",
            f"/api/v1/encounters/{encounter_id}/actions?view=full",
            json_body={"kind": "move", "to_position": [10, 0]},
        )

        assert moved.status == 200
        (step,) = moved.json()["events"]
        assert step["kind"] == "move" and step["actor"] == "Goblin"
        assert step["data"]["origin"] == [15, 0]
        assert step["data"]["destination"] == [10, 0]
        assert step["data"]["cost"] == 5 and step["data"]["completed"] is True
        state = moved.json()["state"]
        assert {row["name"]: row["position"] for row in state["combatants"]} == {
            "Goblin": [10, 0],
            "Thora": [0, 0],
        }
        # A move is not the action: 5 of 30 feet gone, the action still in hand.
        assert state["turn_state"]["movement_left"] == 25
        assert state["turn_state"]["action_used"] is False

    def test_an_unknown_body_key_is_refused_naming_it_and_the_valid_keys(
        self, editor: Editor
    ) -> None:
        # The widest body is where a misspelling is likeliest and quietest: a
        # dropped 'target' would make an attack answer "needs a target", but a
        # dropped 'set_open' would flip a door the caller meant to open.
        encounter_id = self.start(editor, [5, 0]).json()["encounter_id"]

        refused = editor.request(
            "POST",
            f"/api/v1/encounters/{encounter_id}/actions",
            json_body={"kind": "dodge", "targetted": "Thora"},
        )

        problem = assert_problem(refused, 400, "unknown key(s): 'targetted'")
        # Derived from the route table rather than restated. A literal list
        # pinned the message against a copy of itself: adding a body key meant
        # editing this string, which is a test that only ever agrees with
        # whatever was last typed into it.
        act_route = next(r for r in routes.ROUTES if r.operation == "encounter.act")
        assert act_route.body_schema is not None
        expected = ", ".join(sorted(act_route.body_schema["properties"]))
        assert f"Valid keys: {expected}" in problem["detail"]
        assert self.log_of(editor, encounter_id)["total_actions"] == 0

    def test_a_wrong_typed_field_is_refused_by_the_schema_not_the_engine(
        self, editor: Editor
    ) -> None:
        # The schema the contract publishes is the schema the dispatcher
        # enforces, so a destination that is not a point never reaches
        # ``specs.parse_point`` — whose own refusal for the same field says
        # "feet along the x-axis", and whose absence is what tells the two
        # 400s apart.
        encounter_id = self.start(editor, [15, 0]).json()["encounter_id"]

        misplaced = assert_problem(
            editor.request(
                "POST",
                f"/api/v1/encounters/{encounter_id}/actions",
                json_body={"kind": "move", "to_position": "north"},
            ),
            400,
            "'to_position' must be a list, a whole number or null",
        )
        assert "feet along the x-axis" not in misplaced["detail"]
        assert_problem(
            editor.request(
                "POST",
                f"/api/v1/encounters/{encounter_id}/actions",
                json_body={"kind": "attack", "target": "Thora", "targets": "Thora"},
            ),
            400,
            "'targets' must be a list or null",
        )
        assert self.log_of(editor, encounter_id)["total_actions"] == 0

    def test_an_action_without_a_kind_is_refused_before_the_fight_is_touched(
        self, editor: Editor
    ) -> None:
        encounter_id = self.start(editor, [5, 0]).json()["encounter_id"]
        assert_problem(
            editor.request(
                "POST",
                f"/api/v1/encounters/{encounter_id}/actions",
                json_body={"target": "Thora", "attack": "Scimitar"},
            ),
            400,
            "'kind' is required",
        )
        assert self.log_of(editor, encounter_id)["total_actions"] == 0

    def test_an_unknown_kind_names_every_action_this_engine_takes(
        self, editor: Editor
    ) -> None:
        # The ten are derived from ActionKind rather than written out here: a
        # literal list would pin the refusal against a second copy of itself.
        # A kind that is text but not one of them is refused against the closed
        # set the route declares — which is the same set, and is also what
        # `fivee help encounter.act` prints. A kind that is not text fails the
        # type check before the set is ever consulted, so the two are told
        # apart by their detail rather than by the status they share.
        encounter_id = self.start(editor, [5, 0]).json()["encounter_id"]
        refusal = assert_problem(
            editor.request(
                "POST",
                f"/api/v1/encounters/{encounter_id}/actions",
                json_body={"kind": "yodel"},
            ),
            400,
            "'kind' must be one of: ",
        )
        assert all(kind.value in refusal["detail"] for kind in ActionKind)
        schema_refusal = assert_problem(
            editor.request(
                "POST",
                f"/api/v1/encounters/{encounter_id}/actions",
                json_body={"kind": 7},
            ),
            400,
            "'kind' must be text",
        )
        assert "must be one of" not in schema_refusal["detail"]

    def test_an_action_the_rules_forbid_is_refused_with_the_rule(
        self, editor: Editor
    ) -> None:
        encounter_id = self.start(editor, [5, 0]).json()["encounter_id"]
        editor.request(
            "POST",
            f"/api/v1/encounters/{encounter_id}/actions",
            json_body={"kind": "attack", "target": "Thora", "attack": "Scimitar"},
        )

        refused = editor.request(
            "POST",
            f"/api/v1/encounters/{encounter_id}/actions",
            json_body={"kind": "dodge"},
        )

        assert_problem(refused, 400, "Goblin has already taken an action this turn")
        # Refused, not adjusted and not recorded: the journal still holds one
        # action, so a reader of the audit sees the turn that happened.
        assert self.log_of(editor, encounter_id)["total_actions"] == 1

    def test_an_empty_quiver_is_refused_over_the_wire_and_names_both(
        self, editor: Editor
    ) -> None:
        # The model raises rather than emitting for this one, so the refusal
        # only reaches a caller if ``service.encounters.act`` translates it —
        # and a message naming neither the ammunition nor the weapon would
        # leave a player guessing which of two bows is dry.
        archer: dict[str, Any] = {
            "name": "Sylvi",
            "team": "party",
            "ac": 14,
            "max_hp": 20,
            "position": [0, 0],
            "items": {"Arrow": 0},
            "attacks": [
                {
                    "name": "Shortbow",
                    "attack_bonus": 5,
                    "damage": "1d6+3",
                    "damage_type": "piercing",
                    "kind": "ranged",
                    "normal_range": 80,
                    "long_range": 320,
                    "ammunition": "Arrow",
                }
            ],
        }
        created = editor.request(
            "POST",
            "/api/v1/encounters",
            json_body={
                "combatants": [archer, {**GOBLIN, "position": [15, 0]}],
                "seed": 11,
            },
        )
        encounter_id = created.json()["encounter_id"]
        for _ in range(4):
            state = editor.request(
                "GET", f"/api/v1/encounters/{encounter_id}"
            ).json()
            if state["turn"] == "Sylvi":
                break
            editor.request("POST", f"/api/v1/encounters/{encounter_id}/advance")
        else:
            raise AssertionError("Sylvi never got a turn")
        recorded = self.log_of(editor, encounter_id)["total_actions"]

        refused = editor.request(
            "POST",
            f"/api/v1/encounters/{encounter_id}/actions",
            json_body={"kind": "attack", "target": "Goblin", "attack": "Shortbow"},
        )

        assert_problem(refused, 400, "no Arrow left to fire Shortbow")
        # Refused rather than recorded: the audit gains nothing from a shot the
        # rules never let happen.
        assert self.log_of(editor, encounter_id)["total_actions"] == recorded

    def test_a_move_beyond_the_creatures_speed_names_what_it_would_cost(
        self, editor: Editor
    ) -> None:
        encounter_id = self.start(editor, [15, 0]).json()["encounter_id"]
        assert_problem(
            editor.request(
                "POST",
                f"/api/v1/encounters/{encounter_id}/actions",
                json_body={"kind": "move", "to_position": [200, 0]},
            ),
            400,
            "Goblin has 30 ft of movement, needs 185 ft",
        )
        assert self.log_of(editor, encounter_id)["total_actions"] == 0

    def test_a_retried_action_under_one_key_swings_once(self, editor: Editor) -> None:
        encounter_id = self.start(editor, [5, 0]).json()["encounter_id"]
        swing = {"kind": "attack", "target": "Thora", "attack": "Scimitar"}

        first = editor.request(
            "POST",
            f"/api/v1/encounters/{encounter_id}/actions?view=full",
            json_body=swing,
            headers={"Idempotency-Key": "swing-1"},
        )
        # No ``view`` on the retry: a retry is answered whole whatever it asked
        # for, because a caller retrying is one that did not hear the answer.
        again = editor.request(
            "POST",
            f"/api/v1/encounters/{encounter_id}/actions",
            json_body=swing,
            headers={"Idempotency-Key": "swing-1"},
        )

        assert first.status == again.status == 200
        assert again.json() == first.json()
        assert again.headers["ETag"] == first.headers["ETag"]
        # Not merely equal-looking. A second swing would have been refused for
        # having no attacks left, so equality alone could be a cached refusal;
        # one journalled action and 6 damage rather than 12 is what proves the
        # recorded result came back instead of the turn being taken twice.
        assert self.log_of(editor, encounter_id)["total_actions"] == 1
        state = editor.request("GET", f"/api/v1/encounters/{encounter_id}").json()
        assert {row["name"]: row["hp"] for row in state["combatants"]}["Thora"] == 24

    def test_an_action_from_a_version_the_fight_has_moved_past_is_409(
        self, editor: Editor
    ) -> None:
        created = self.start(editor, [5, 0])
        encounter_id = created.json()["encounter_id"]
        stale = created.headers["ETag"]
        editor.request(
            "POST",
            f"/api/v1/encounters/{encounter_id}/actions",
            json_body={"kind": "attack", "target": "Thora", "attack": "Scimitar"},
        )

        # A move, and legal: the precondition is the only thing standing
        # between this write and the journal, so a guard that stopped checking
        # would land it rather than fail for some second reason.
        refused = editor.request(
            "POST",
            f"/api/v1/encounters/{encounter_id}/actions",
            json_body={"kind": "move", "to_position": [5, 5]},
            headers={"If-Match": stale},
        )

        problem = assert_problem(refused, 409, "has advanced since you read it")
        assert f"encounter {encounter_id!r}" in problem["detail"]
        assert "read it again and reapply" in problem["detail"]
        # Refused rather than merged, and refused before the fight was touched:
        # two divergent copies of one turn produce a journal replaying as
        # neither, which is why nothing here may have happened.
        assert self.log_of(editor, encounter_id)["total_actions"] == 1
        state = editor.request("GET", f"/api/v1/encounters/{encounter_id}").json()
        assert {row["name"]: row["position"] for row in state["combatants"]}["Goblin"] == (
            [5, 0]
        )
        assert state["turn_state"]["movement_left"] == 30

    def test_an_action_matching_the_head_is_taken(self, editor: Editor) -> None:
        # The control for the case above: a guard that refused every If-Match
        # would pass it, so the version the caller really did read must work.
        created = self.start(editor, [5, 0])
        encounter_id = created.json()["encounter_id"]

        struck = editor.request(
            "POST",
            f"/api/v1/encounters/{encounter_id}/actions",
            json_body={"kind": "attack", "target": "Thora", "attack": "Scimitar"},
            headers={"If-Match": created.headers["ETag"]},
        )

        assert struck.status == 200
        assert struck.json()["events"][0]["data"]["hit"] is True
        assert self.log_of(editor, encounter_id)["total_actions"] == 1


class TestEncounterPreconditions:
    """``If-Match`` on a fight is its journal chain head, and it is honoured."""

    def create(self, editor: Editor) -> Response:
        return editor.request(
            "POST", "/api/v1/encounters",
            json_body={"combatants": combatants(), "seed": 11},
        )

    def test_a_write_matching_the_head_is_allowed(self, editor: Editor) -> None:
        created = self.create(editor)
        encounter_id = created.json()["encounter_id"]
        advanced = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/advance",
            headers={"If-Match": created.headers["ETag"]},
        )
        assert advanced.status == 200

    def test_a_write_from_a_version_the_fight_has_moved_past_is_409(
        self, editor: Editor
    ) -> None:
        # The refusal a merge would have hidden: two divergent copies of one
        # fight produce a journal that replays as neither.
        created = self.create(editor)
        encounter_id = created.json()["encounter_id"]
        stale = created.headers["ETag"]
        editor.request("POST", f"/api/v1/encounters/{encounter_id}/advance")
        refused = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/advance",
            headers={"If-Match": stale},
        )
        problem = assert_problem(refused, 409, "has advanced since you read it")
        assert f"encounter {encounter_id!r}" in problem["detail"]
        assert "read it again and reapply" in problem["detail"]

    def test_a_star_precondition_writes_regardless(self, editor: Editor) -> None:
        created = self.create(editor)
        encounter_id = created.json()["encounter_id"]
        editor.request("POST", f"/api/v1/encounters/{encounter_id}/advance")
        taken = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/advance",
            headers={"If-Match": "*"},
        )
        assert taken.status == 200

    def test_the_precondition_covers_notes_as_well_as_turns(
        self, editor: Editor
    ) -> None:
        created = self.create(editor)
        encounter_id = created.json()["encounter_id"]
        stale = created.headers["ETag"]
        editor.request("POST", f"/api/v1/encounters/{encounter_id}/advance")
        assert_problem(
            editor.request(
                "POST", f"/api/v1/encounters/{encounter_id}/notes",
                json_body={"text": "written from a stale read"},
                headers={"If-Match": stale},
            ),
            409,
            "has advanced since you read it",
        )

    def test_the_precondition_covers_a_correction(self, editor: Editor) -> None:
        created = self.create(editor)
        encounter_id = created.json()["encounter_id"]
        stale = created.headers["ETag"]
        editor.request("POST", f"/api/v1/encounters/{encounter_id}/advance")
        assert_problem(
            editor.request(
                "POST", f"/api/v1/encounters/{encounter_id}/corrections",
                json_body={"state": {"Thora": {"ac": 11}}, "reason": "mistyped stat block"},
                headers={"If-Match": stale},
            ),
            409,
            "has advanced since you read it",
        )


class TestIdempotencyKey:
    """The header that replaced ``request_id``, over the same replay cache."""

    def create(self, editor: Editor, key: str | None = None) -> Response:
        headers = {"Idempotency-Key": key} if key is not None else {}
        return editor.request(
            "POST", "/api/v1/encounters",
            json_body={"combatants": combatants(), "seed": 11},
            headers=headers,
        )

    def test_a_retried_turn_under_one_key_is_taken_once(self, editor: Editor) -> None:
        encounter_id = self.create(editor).json()["encounter_id"]
        first = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/advance?view=full",
            headers={"Idempotency-Key": "turn-1"},
        )
        again = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/advance",
            headers={"Idempotency-Key": "turn-1"},
        )
        assert first.status == again.status == 200
        assert again.json() == first.json()
        # Not merely equal-looking: the fight must not have moved. An identical
        # body could come back from a second turn that happened to land in the
        # same place, so the event log is what proves the turn was not taken.
        log = editor.request(
            "GET", f"/api/v1/encounters/{encounter_id}/log?include_actions=false"
        ).json()
        assert log["total_actions"] == 1

    def test_a_different_key_takes_the_next_turn(self, editor: Editor) -> None:
        encounter_id = self.create(editor).json()["encounter_id"]
        first = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/advance?view=full",
            headers={"Idempotency-Key": "turn-1"},
        ).json()
        second = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/advance?view=full",
            headers={"Idempotency-Key": "turn-2"},
        ).json()
        assert second["state"]["turn"] != first["state"]["turn"]

    def test_a_retried_creation_makes_one_fight(self, editor: Editor) -> None:
        first = self.create(editor, key="fight-1").json()
        again = self.create(editor, key="fight-1").json()
        assert again["encounter_id"] == first["encounter_id"]
        listed = editor.request("GET", "/api/v1/encounters").json()["encounters"]
        assert len(listed) == 1

    def test_a_key_on_a_roll_needs_a_fight_to_be_audited_against(
        self, editor: Editor
    ) -> None:
        # A replay key is a journal entry, and there is no journal without an
        # encounter; the refusal says so rather than silently ignoring it.
        assert_problem(
            editor.request(
                "POST", "/api/v1/dice/rolls",
                json_body={"expression": "d20"},
                headers={"Idempotency-Key": "roll-1"},
            ),
            400,
            "request_id requires encounter_id",
        )

    def test_a_keyed_roll_against_a_fight_replays_rather_than_rerolls(
        self, editor: Editor
    ) -> None:
        encounter_id = self.create(editor).json()["encounter_id"]
        body = {"expression": "d20", "encounter_id": encounter_id}
        first = editor.request(
            "POST", "/api/v1/dice/rolls", json_body=body,
            headers={"Idempotency-Key": "roll-1"},
        ).json()
        again = editor.request(
            "POST", "/api/v1/dice/rolls", json_body=body,
            headers={"Idempotency-Key": "roll-1"},
        ).json()
        # No seed was given, so a re-roll would pick a new one; equality here is
        # the recorded result coming back rather than a coincidence.
        assert again == first


class TestAdventuresOverHttp:
    """The adventure is a guarded document, so its adapter is map.put's, not act's.

    An encounter's ``If-Match`` is optional — one agent driving one fight is
    taking the engine's word for where it is. An adventure is a *document* two
    servers rewrite whole, so the precondition is required and its absence is a
    428, exactly as it is for a map.
    """

    def start(self, editor: Editor, name: str = "The Sunless Citadel") -> Response:
        return editor.request("POST", "/api/v1/adventures", json_body={"name": name})

    def test_starting_one_answers_201_with_where_to_find_it(self, editor: Editor) -> None:
        response = self.start(editor)

        assert response.status == 201, response.body
        body = response.json()
        assert body["id"] == "adv-1"
        assert body["status"] == "active"
        assert body["members"] == []
        assert response.headers["Location"] == "/api/v1/adventures/adv-1"
        assert response.headers["ETag"] == f'"{body["version"]}"'

    def test_reading_one_carries_its_version_as_an_etag(self, editor: Editor) -> None:
        created = self.start(editor)

        read = editor.request("GET", "/api/v1/adventures/adv-1")

        assert read.status == 200
        assert read.json()["name"] == "The Sunless Citadel"
        assert read.headers["ETag"] == created.headers["ETag"]

    def test_an_unknown_adventure_is_404_and_names_what_is_there(
        self, editor: Editor
    ) -> None:
        self.start(editor)

        assert_problem(
            editor.request("GET", "/api/v1/adventures/adv-9"),
            404,
            "no adventure 'adv-9'; adventures here: adv-1",
        )

    def test_linking_an_encounter_without_if_match_is_428(self, editor: Editor) -> None:
        self.start(editor)

        assert_problem(
            editor.request(
                "POST",
                "/api/v1/adventures/adv-1/encounters",
                json_body={"combatants": [HERO, GOBLIN], "seed": 5},
            ),
            428,
            "If-Match is required",
        )

    def test_linking_with_a_star_precondition_starts_the_fight(
        self, editor: Editor
    ) -> None:
        self.start(editor)

        linked = editor.request(
            "POST",
            "/api/v1/adventures/adv-1/encounters",
            json_body={"combatants": [HERO, GOBLIN], "seed": 5},
            headers={"If-Match": "*"},
        )

        assert linked.status == 201, linked.body
        body = linked.json()
        assert body["index"] == 0
        assert body["encounter"]["seed"] == 5
        assert linked.headers["Location"] == (
            f"/api/v1/encounters/{body['encounter_id']}"
        )
        assert linked.headers["ETag"] == f'"{body["version"]}"'

    def test_linking_from_a_version_the_run_has_moved_past_is_409(
        self, editor: Editor
    ) -> None:
        stale = self.start(editor).headers["ETag"]
        first = editor.request(
            "POST",
            "/api/v1/adventures/adv-1/encounters",
            json_body={"combatants": [HERO, GOBLIN], "seed": 5},
            headers={"If-Match": "*"},
        )
        assert first.status == 201, first.body

        assert_problem(
            editor.request(
                "POST",
                "/api/v1/adventures/adv-1/encounters",
                json_body={"combatants": [HERO, GOBLIN], "seed": 6},
                headers={"If-Match": stale},
            ),
            409,
            "the adventure 'adv-1' has advanced since you read it",
        )
        assert len(editor.request("GET", "/api/v1/adventures/adv-1").json()["members"]) == 1

    def test_the_listing_shows_the_run_and_its_status_filter_is_the_encounters_one(
        self, editor: Editor
    ) -> None:
        self.start(editor)
        self.start(editor, "Tomb of Horrors")
        finalized = editor.request(
            "POST",
            "/api/v1/adventures/adv-2/finalize",
            json_body={},
            headers={"If-Match": "*"},
        )
        assert finalized.status == 200, finalized.body

        active = editor.request("GET", "/api/v1/adventures").json()
        closed = editor.request("GET", "/api/v1/adventures?status=finalized").json()

        assert [entry["adventure_id"] for entry in active["adventures"]] == ["adv-1"]
        assert [entry["adventure_id"] for entry in closed["adventures"]] == ["adv-2"]

    def test_an_unknown_status_filter_names_the_three_that_work(
        self, editor: Editor
    ) -> None:
        assert_problem(
            editor.request("GET", "/api/v1/adventures?status=halfway"),
            400,
            "'status' must be one of: active, finalized, all",
        )

    def test_a_retried_link_under_one_idempotency_key_links_once(
        self, editor: Editor
    ) -> None:
        self.start(editor)
        body = {"combatants": [HERO, GOBLIN], "seed": 5}
        headers = {"If-Match": "*", "Idempotency-Key": "link-1"}

        first = editor.request(
            "POST", "/api/v1/adventures/adv-1/encounters", json_body=body, headers=headers
        )
        again = editor.request(
            "POST", "/api/v1/adventures/adv-1/encounters", json_body=body, headers=headers
        )

        assert first.status == 201 and again.status == 201, again.body
        assert again.json()["encounter_id"] == first.json()["encounter_id"]
        assert len(editor.request("GET", "/api/v1/adventures/adv-1").json()["members"]) == 1

    def test_a_finalized_run_refuses_a_further_encounter(self, editor: Editor) -> None:
        self.start(editor)
        editor.request(
            "POST",
            "/api/v1/adventures/adv-1/finalize",
            json_body={},
            headers={"If-Match": "*"},
        )

        assert_problem(
            editor.request(
                "POST",
                "/api/v1/adventures/adv-1/encounters",
                json_body={"combatants": [HERO, GOBLIN], "seed": 5},
                headers={"If-Match": "*"},
            ),
            400,
            "adventure 'adv-1' is finalized",
        )

    def link(self, editor: Editor, seed: int = 5) -> str:
        linked = editor.request(
            "POST",
            "/api/v1/adventures/adv-1/encounters",
            json_body={"combatants": [HERO, GOBLIN], "seed": seed},
            headers={"If-Match": "*"},
        )
        assert linked.status == 201, linked.body
        return str(linked.json()["encounter_id"])

    def test_composing_the_runs_replay_answers_the_file_it_wrote(
        self, editor: Editor
    ) -> None:
        self.start(editor)
        encounter_id = self.link(editor)
        finalized = editor.request(
            "POST", f"/api/v1/encounters/{encounter_id}/finalize", json_body={}
        )
        assert finalized.status == 200, finalized.body

        composed = editor.request(
            "POST", "/api/v1/adventures/adv-1/replay", json_body={}
        )

        assert composed.status == 200, composed.body
        body = composed.json()
        assert body["chapters"] == 1
        assert body["encounters"] == [encounter_id]
        written = json.loads(Path(body["path"]).read_text(encoding="utf-8"))
        assert written["format"] == "fivee-sim-adventure-replay"
        assert written["chapters"][0]["replay"]["encounter"]["id"] == encounter_id
        # Written into the replays directory and deliberately not offered as a
        # replay: the listing filters on the replay format, so a viewer link
        # handed out for this file would be a link to a 404.
        assert "viewer_url" not in body
        assert editor.request("GET", "/api/v1/replays").json() == {"replays": []}

    def test_composing_a_run_whose_fight_is_unfinished_is_400_and_names_it(
        self, editor: Editor
    ) -> None:
        self.start(editor)
        encounter_id = self.link(editor)

        assert_problem(
            editor.request("POST", "/api/v1/adventures/adv-1/replay", json_body={}),
            400,
            f"encounter '{encounter_id}' of adventure 'adv-1' has no finalized replay",
        )

    def test_composing_a_run_that_has_no_fights_yet_is_400(self, editor: Editor) -> None:
        self.start(editor)

        assert_problem(
            editor.request("POST", "/api/v1/adventures/adv-1/replay", json_body={}),
            400,
            "adventure 'adv-1' has no encounters to compose",
        )


class TestLinkingAnInterludeOverHttp:
    """``mode`` and ``carry_map`` on the link, the way a caller reaches them.

    The two together are what makes a run a sequence of walks and fights rather
    than a sequence of fights: one says which kind of chapter to start, the
    other puts it on the ground the last one was on. Every refusal here names
    what it refused, because a 400 alone would pass against a server with the
    check deleted.
    """

    def start(self, editor: Editor) -> None:
        saved = editor.request(
            "PUT", "/api/v1/maps/mill",
            json_body=MILL_DOCUMENT,
            headers={"If-Match": "*"},
        )
        assert saved.status == 201, saved.body
        editor.request(
            "POST", "/api/v1/adventures", json_body={"name": "The Drowned Mill"}
        )

    def link(self, editor: Editor, **body: Any) -> Response:
        return editor.request(
            "POST", "/api/v1/adventures/adv-1/encounters",
            json_body=body,
            headers={"If-Match": "*"},
        )

    def test_a_run_can_open_with_an_interlude_on_a_saved_map(
        self, editor: Editor
    ) -> None:
        self.start(editor)

        linked = self.link(
            editor,
            combatants=[dict(SCOUT)],
            seed=5,
            map_id="mill",
            mode="exploration",
        )

        assert linked.status == 201, linked.body
        assert linked.json()["encounter"]["state"]["mode"] == "exploration"
        assert linked.json()["encounter"]["state"]["turn"] is None

    def test_the_run_reports_each_chapters_mode_from_both_doors(
        self, editor: Editor
    ) -> None:
        self.start(editor)
        interlude = self.link(
            editor, combatants=[dict(SCOUT)], seed=5, map_id="mill",
            mode="exploration",
        ).json()["encounter_id"]
        editor.request("POST", f"/api/v1/encounters/{interlude}/finalize")
        assert self.link(
            editor, combatants=[dict(GOBLIN)], seed=6, carry_map=True
        ).status == 201

        document = editor.request("GET", "/api/v1/adventures/adv-1").json()
        listed = editor.request("GET", "/api/v1/adventures?status=all").json()

        assert [member["mode"] for member in document["members"]] == [
            "exploration", "combat"
        ]
        assert listed["adventures"][0]["modes"] == ["exploration", "combat"]

    def test_a_carried_map_puts_the_next_chapter_on_the_same_ground(
        self, editor: Editor
    ) -> None:
        self.start(editor)
        self.link(
            editor, combatants=[dict(SCOUT)], seed=5, map_id="mill", mode="exploration"
        )

        linked = self.link(editor, combatants=[dict(GOBLIN)], seed=6, carry_map=True)

        assert linked.status == 201, linked.body
        assert linked.json()["encounter"]["map_source"]["map_id"] == "mill"

    def test_carrying_a_map_and_naming_one_is_400_and_says_which(
        self, editor: Editor
    ) -> None:
        self.start(editor)
        self.link(
            editor, combatants=[dict(SCOUT)], seed=5, map_id="mill", mode="exploration"
        )

        assert_problem(
            self.link(
                editor, combatants=[dict(GOBLIN)], seed=6, carry_map=True,
                map_id="mill",
            ),
            400,
            "carry_map cannot be given with 'map_id'",
        )

    def test_carrying_a_map_from_a_chapter_that_had_none_is_400(
        self, editor: Editor
    ) -> None:
        self.start(editor)
        self.link(editor, combatants=[dict(HERO), dict(GOBLIN)], seed=5)

        assert_problem(
            self.link(editor, seed=6, carry_map=True),
            400,
            "it was not on a map",
        )

    def test_a_mode_the_route_does_not_declare_is_refused_by_the_schema(
        self, editor: Editor
    ) -> None:
        self.start(editor)

        assert_problem(
            self.link(editor, combatants=[dict(SCOUT)], seed=5, mode="wandering"),
            400,
            "'mode' must be one of: combat, exploration",
        )


class TestMapOperationsOverHttp:
    """Maps without sessions: generate, look, keep, and address by id."""

    def test_generate_answers_the_document_and_saves_nothing(
        self, editor: Editor
    ) -> None:
        answer = editor.request(
            "POST", "/api/v1/maps/generate",
            json_body={"kind": "caves", "seed": 11, "params": {"width": 16, "height": 12}},
        ).json()
        assert answer["map_id"] is None
        assert answer["document"]["format"] == "fivee-sim-map"
        assert editor.request("GET", "/api/v1/maps").json() == {"maps": []}

    def test_generate_with_save_as_writes_it_under_that_id(self, editor: Editor) -> None:
        answer = editor.request(
            "POST", "/api/v1/maps/generate",
            json_body={
                "kind": "caves", "seed": 11,
                "params": {"width": 16, "height": 12}, "save_as": "hollow",
            },
        ).json()
        assert answer["map_id"] == "hollow"
        assert editor.file_of("hollow").exists()
        fetched = editor.request("GET", "/api/v1/maps/hollow")
        assert fetched.status == 200
        assert fetched.headers["ETag"] == f'"{answer["saved"]["sha256"]}"'

    def test_generate_into_an_id_already_in_use_is_refused(self, editor: Editor) -> None:
        editor.put_map("editor-chamber", payload())
        assert_problem(
            editor.request(
                "POST", "/api/v1/maps/generate",
                json_body={"kind": "caves", "save_as": "editor-chamber"},
            ),
            400,
            "already exists; supply the version you read",
        )

    def test_a_render_takes_a_saved_id(self, editor: Editor) -> None:
        editor.put_map("editor-chamber", payload())
        rendered = editor.request(
            "POST", "/api/v1/maps/render", json_body={"map_id": "editor-chamber"}
        ).json()
        assert rendered["rows"][0] == "#####"
        assert rendered["map_id"] == "editor-chamber"

    def test_a_render_takes_an_inline_document_instead(self, editor: Editor) -> None:
        # generate -> look -> tweak -> save, with nothing loaded in between:
        # this is what replaced the map session.
        rendered = editor.request(
            "POST", "/api/v1/maps/render", json_body={"document": payload()}
        ).json()
        assert rendered["rows"][0] == "#####"
        assert rendered["map_id"] is None

    def test_a_render_naming_neither_subject_is_refused(self, editor: Editor) -> None:
        assert_problem(
            editor.request("POST", "/api/v1/maps/render", json_body={}),
            400,
            "exactly one of 'map_id' (a saved map) or 'document' (inline)",
        )

    def test_a_render_naming_both_subjects_is_refused(self, editor: Editor) -> None:
        editor.put_map("editor-chamber", payload())
        assert_problem(
            editor.request(
                "POST", "/api/v1/maps/render",
                json_body={"map_id": "editor-chamber", "document": payload()},
            ),
            400,
            "exactly one of 'map_id' (a saved map) or 'document' (inline)",
        )

    def test_a_render_of_an_unsaved_id_is_404(self, editor: Editor) -> None:
        assert_problem(
            editor.request(
                "POST", "/api/v1/maps/render", json_body={"map_id": "never-saved"}
            ),
            404,
            "no map 'never-saved'; maps here",
        )

    def test_a_query_answers_geometry_over_either_subject(self, editor: Editor) -> None:
        editor.put_map("editor-chamber", payload())
        saved = editor.request(
            "POST", "/api/v1/maps/query",
            json_body={"map_id": "editor-chamber", "query": "distance",
                       "frm": [1, 1], "to": [3, 1]},
        ).json()
        inline = editor.request(
            "POST", "/api/v1/maps/query",
            json_body={"document": payload(), "query": "distance",
                       "frm": [1, 1], "to": [3, 1]},
        ).json()
        assert saved["feet"] == inline["feet"] == 10

    def test_an_unknown_query_kind_names_the_three_that_work(
        self, editor: Editor
    ) -> None:
        assert_problem(
            editor.request(
                "POST", "/api/v1/maps/query",
                json_body={"document": payload(), "query": "cover",
                           "frm": [0, 0], "to": [1, 1]},
            ),
            400,
            "distance, line_of_sight, path",
        )

    def test_a_uvtt_export_writes_a_file_for_either_subject(
        self, editor: Editor, tmp_path: Path
    ) -> None:
        target = tmp_path / "chamber.uvtt"
        result = editor.request(
            "POST", "/api/v1/maps/uvtt",
            json_body={
                "document": payload(), "path": str(target),
                "pixels_per_grid": 8, "include_image": False,
            },
        ).json()
        assert result["path"] == str(target)
        assert target.exists()
        assert result["portals"] == 1

    def test_an_inline_document_that_does_not_validate_is_422(
        self, editor: Editor
    ) -> None:
        broken = payload()
        broken["legend"]["?"] = "no-such-terrain"
        problem = assert_problem(
            editor.request("POST", "/api/v1/maps/render", json_body={"document": broken}),
            422,
            "map error",
        )
        assert problem["diagnostics"]

    def test_a_fight_runs_on_a_saved_map_and_reports_its_hash(
        self, editor: Editor
    ) -> None:
        saved = editor.put_map("editor-chamber", payload()).json()
        created = editor.request(
            "POST", "/api/v1/encounters",
            json_body={
                "combatants": [
                    {**HERO, "position": [5, 5]},
                    {**GOBLIN, "position": [15, 10]},
                ],
                "seed": 11,
                "map_id": "editor-chamber",
            },
        )
        assert created.status == 201
        source = created.json()["map_source"]
        assert source == {
            "map_id": "editor-chamber",
            "sha256": saved["sha256"],
            "current_sha256": saved["sha256"],
            "stale": False,
        }

    def test_editing_the_file_makes_a_running_fight_report_it_as_stale(
        self, editor: Editor
    ) -> None:
        # The fight keeps resolving on what it captured; what changes is that
        # the divergence is visible instead of silent — and it is measured
        # against the *file*, which is what every process on this host shares.
        editor.put_map("editor-chamber", payload())
        created = editor.request(
            "POST", "/api/v1/encounters",
            json_body={
                "combatants": [
                    {**HERO, "position": [5, 5]},
                    {**GOBLIN, "position": [15, 10]},
                ],
                "seed": 11,
                "map_id": "editor-chamber",
            },
        ).json()
        edited = editor.request(
            "POST", "/api/v1/maps/editor-chamber/edits",
            json_body={"operations": [{"op": "set_name", "name": "renamed"}]},
        ).json()
        state = editor.request(
            "GET", f"/api/v1/encounters/{created['encounter_id']}"
        ).json()
        assert state["map_source"]["stale"] is True
        assert state["map_source"]["current_sha256"] == edited["sha256"]
        assert state["map"]["width"] == 5  # still fighting on what it captured

    def test_a_fight_on_a_map_that_is_not_saved_is_404(self, editor: Editor) -> None:
        assert_problem(
            editor.request(
                "POST", "/api/v1/encounters",
                json_body={"combatants": combatants(), "map_id": "never-saved"},
            ),
            404,
            "no map 'never-saved'",
        )

    def test_a_render_overlays_the_fight_when_asked(self, editor: Editor) -> None:
        editor.put_map("editor-chamber", payload())
        created = editor.request(
            "POST", "/api/v1/encounters",
            json_body={
                "combatants": [
                    {**HERO, "position": [5, 5]},
                    {**GOBLIN, "position": [15, 10]},
                ],
                "seed": 11,
                "map_id": "editor-chamber",
            },
        ).json()
        rendered = editor.request(
            "POST", "/api/v1/maps/render",
            json_body={"map_id": "editor-chamber",
                       "encounter_id": created["encounter_id"]},
        ).json()
        assert set(rendered["tokens"].values()) == {"Thora", "Goblin"}

    def test_an_edit_refuses_a_version_the_file_has_moved_past(
        self, editor: Editor
    ) -> None:
        stale = editor.put_map("editor-chamber", payload()).json()["sha256"]
        editor.request(
            "POST", "/api/v1/maps/editor-chamber/edits",
            json_body={"operations": [{"op": "set_name", "name": "first"}]},
        )
        assert_problem(
            editor.request(
                "POST", "/api/v1/maps/editor-chamber/edits",
                json_body={"operations": [{"op": "set_name", "name": "second"}]},
                headers={"If-Match": f'"{stale}"'},
            ),
            409,
            "has advanced since you read it",
        )


class TestReplayValidation:
    def test_a_bundle_posted_for_validation_is_checked_not_stored(
        self, editor: Editor
    ) -> None:
        answer = editor.request(
            "POST", "/api/v1/replays/validate", json_body={"bundle": replay_bundle()}
        ).json()
        assert answer == {"valid": True, "error_count": 0, "diagnostics": []}
        assert editor.request("GET", "/api/v1/replays").json() == {"replays": []}

    def test_a_broken_bundle_reports_every_diagnostic_without_raising(
        self, editor: Editor
    ) -> None:
        broken = replay_bundle()
        del broken["events"]
        answer = editor.request(
            "POST", "/api/v1/replays/validate", json_body={"bundle": broken}
        ).json()
        assert answer["valid"] is False
        assert answer["diagnostics"]

    def test_validation_needs_the_bundle_it_is_named_for(self, editor: Editor) -> None:
        assert_problem(
            editor.request("POST", "/api/v1/replays/validate", json_body={}),
            400,
            "'bundle' is required",
        )

    def test_an_adventure_envelope_is_validated_as_an_envelope(
        self, editor: Editor
    ) -> None:
        """One route, two formats, dispatched on what the document says it is.

        Without the dispatch this posts an envelope to a validator that demands
        ``seed``, ``initial.creatures`` and ``events`` before it reaches its own
        version check, so the answer would be a wall of diagnostics about fields
        an adventure replay has never had.
        """
        started = editor.request(
            "POST", "/api/v1/adventures", json_body={"name": "Composed Run"}
        )
        assert started.status == 201, started.body
        linked = editor.request(
            "POST",
            "/api/v1/adventures/adv-1/encounters",
            json_body={"combatants": [HERO, GOBLIN], "seed": 5},
            headers={"If-Match": "*"},
        )
        assert linked.status == 201, linked.body
        editor.request(
            "POST",
            f"/api/v1/encounters/{linked.json()['encounter_id']}/finalize",
            json_body={},
        )
        composed = editor.request(
            "POST", "/api/v1/adventures/adv-1/replay", json_body={}
        )
        assert composed.status == 200, composed.body
        envelope = json.loads(
            Path(composed.json()["path"]).read_text(encoding="utf-8")
        )

        answer = editor.request(
            "POST", "/api/v1/replays/validate", json_body={"bundle": envelope}
        ).json()

        assert answer == {"valid": True, "error_count": 0, "diagnostics": []}


class TestScenesOverHttp:
    """A scene is a saved ``encounter.create`` body, addressed like a map.

    Same shape as the map routes on purpose — sha256 ``ETag``, ``If-Match`` on
    every write, ``*`` to create — because it is the same kind of thing: one
    document, rewritten whole, that two servers on a host can both be holding.

    What is deliberately absent is a ``scene.play``. The last case here is why:
    a stored scene starts a fight by being posted to ``encounter.create``, so
    there is exactly one code path into a fight and a scene cannot become a
    second, quieter one.
    """

    def test_list_is_empty_before_anything_is_saved(self, editor: Editor) -> None:
        response = editor.request("GET", "/api/v1/scenes")
        assert response.status == 200
        assert response.json() == {"scenes": []}

    def test_put_creates_gets_match_and_the_listing_names_it(
        self, editor: Editor
    ) -> None:
        created = editor.put_scene("ford", scene_payload())
        assert created.status == 201, created.body
        answer = created.json()
        assert answer["saved"] is True
        assert answer["scene_id"] == "ford"
        assert answer["warnings"] == []
        sha256 = answer["sha256"]
        assert created.headers["ETag"] == f'"{sha256}"'

        fetched = editor.request("GET", "/api/v1/scenes/ford")
        assert fetched.status == 200
        assert fetched.headers["ETag"] == f'"{sha256}"'
        assert fetched.json() == scene_payload()

        listing = editor.request("GET", "/api/v1/scenes").json()
        assert [entry["id"] for entry in listing["scenes"]] == ["ford"]
        assert listing["scenes"][0]["name"] == "Ambush at the ford"
        assert listing["scenes"][0]["combatants"] == 2

    def test_a_write_from_a_stale_read_is_refused_rather_than_merged(
        self, editor: Editor
    ) -> None:
        first = editor.put_scene("ford", scene_payload()).json()["sha256"]
        moved = {**scene_payload(), "name": "Ambush, second thoughts"}
        assert editor.put_scene("ford", moved, if_match=f'"{first}"').status == 200

        stale = editor.put_scene("ford", scene_payload(), if_match=f'"{first}"')
        assert_problem(stale, 409, "has advanced since you read it")
        assert editor.request("GET", "/api/v1/scenes/ford").json()["name"] == (
            "Ambush, second thoughts"
        )

    def test_a_put_with_no_if_match_is_refused_before_anything_is_written(
        self, editor: Editor
    ) -> None:
        response = editor.request(
            "PUT", "/api/v1/scenes/ford", json_body=scene_payload()
        )
        assert_problem(response, 428, "If-Match is required")
        assert editor.request("GET", "/api/v1/scenes").json() == {"scenes": []}

    def test_creating_under_a_hash_nobody_has_read_is_refused(
        self, editor: Editor
    ) -> None:
        response = editor.put_scene("ford", scene_payload(), if_match=f'"{"a" * 64}"')
        assert_problem(response, 409, "there is no saved scene 'ford' to match against")

    def test_an_invalid_envelope_is_refused_and_names_the_key(
        self, editor: Editor
    ) -> None:
        headless = scene_payload()
        del headless["combatants"]
        assert_problem(editor.put_scene("ford", headless), 400, "'combatants' is required")
        assert editor.request("GET", "/api/v1/scenes").json() == {"scenes": []}

    def test_an_unknown_scene_is_a_404_naming_what_is_there(
        self, editor: Editor
    ) -> None:
        editor.put_scene("ford", scene_payload())
        assert_problem(
            editor.request("GET", "/api/v1/scenes/crypt"), 404, "scenes here: ford"
        )

    def test_validation_reads_this_launchs_maps_and_stores_nothing(
        self, editor: Editor
    ) -> None:
        """The one check that needs a second directory, over the wire.

        The service takes the known map ids as an argument; this is the case
        that says the adapter passes *this server's* maps directory rather than
        whatever the process happens to be standing in.
        """
        named = {**scene_payload(), "map_id": "editor-chamber"}
        before = editor.request("POST", "/api/v1/scenes/validate", json_body=named)
        assert before.status == 200
        assert before.json()["ok"] is False
        assert any(
            "no saved map 'editor-chamber'" in entry["problem"]
            for entry in before.json()["errors"]
        )

        editor.put_map("editor-chamber", payload())
        after = editor.request("POST", "/api/v1/scenes/validate", json_body=named)
        assert after.json() == {"ok": True, "errors": [], "warnings": []}
        # Validation is not a write: nothing was stored on the way through.
        assert editor.request("GET", "/api/v1/scenes").json() == {"scenes": []}

    def test_a_combatant_spec_is_the_fights_business_not_the_scenes(
        self, editor: Editor
    ) -> None:
        """A draft that will not start is still a draft, and saves.

        ``encounter.create`` is the one owner of what a combatant is, so it is
        the one that refuses this — at Play, in its own words. The scene layer
        holding a second copy of that judgement is the duplication the design
        rejected.

        The chain is *walked* rather than described: the draft is PUT, read
        back over HTTP, and the roster posted to the fight is the one GET
        answered. Posting a second copy hand-built here would assert nothing
        about storage — a scene service that quietly rewrote what it stored
        would satisfy that test and fails this one.
        """
        broken = {"team": "party", "ac": 16}
        unbuildable = {**scene_payload(), "combatants": [broken, dict(GOBLIN)]}
        assert editor.put_scene("draft", unbuildable).status == 201

        stored = editor.request("GET", "/api/v1/scenes/draft").json()
        assert stored["combatants"] == [broken, dict(GOBLIN)], stored

        refused = editor.request(
            "POST", "/api/v1/encounters", json_body={"combatants": stored["combatants"]}
        )
        # Named rather than merely refused, and the stored roster is two long,
        # so what comes back is the *spec* being judged and not the arity.
        assert_problem(refused, 400, "combatant spec is missing 'name'")

    def test_a_saved_scene_starts_the_fight_it_describes(self, editor: Editor) -> None:
        """Play is a POST of the stored body, which is why there is no scene.play.

        Read back over HTTP and handed to ``encounter.create`` unchanged but for
        the label around it — one code path into a fight, and this is the proof
        that the body a scene stores is one that path accepts.
        """
        editor.put_scene("ford", scene_payload())
        stored = editor.request("GET", "/api/v1/scenes/ford").json()

        body = {
            key: stored[key]
            for key in ("combatants", "seed", "movement_rule")
            if key in stored
        }
        started = editor.request("POST", "/api/v1/encounters", json_body=body)
        assert started.status == 201, started.body
        assert started.json()["seed"] == stored["seed"]
        assert sorted(started.json()["state"]["order"]) == sorted(
            str(one["label"] if "label" in one else one["name"])
            for one in stored["combatants"]
        )

    def test_there_is_no_play_route_and_that_is_the_design(
        self, editor: Editor
    ) -> None:
        editor.put_scene("ford", scene_payload())
        assert_problem(
            editor.request("POST", "/api/v1/scenes/ford/play", json_body={}),
            404,
            "no route for /api/v1/scenes/ford/play",
        )
        assert not [
            route for route in routes.api_routes() if route.operation == "scene.play"
        ]
