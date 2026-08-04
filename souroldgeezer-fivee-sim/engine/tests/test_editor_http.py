"""The REST editor adapter, driven with a stdlib client over an ephemeral port.

What is pinned here is the HTTP contract the editor page codes against: the
token and Host guards, the config injection into served pages, the sha256
ETag / If-Match concurrency rule, atomic edits, problem+json error bodies, and
that generate never persists. The map behaviour itself is the service layer's,
already pinned by test_map_service — these tests check the adapter's mapping,
not the mapping's subject.
"""

from __future__ import annotations

import http.client
import io
import json
import re
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from fivee_sim import __version__
from fivee_sim.editor.http_server import CONFIG_MARKER, MAX_BODY_BYTES, TOKEN_HEADER, EditorServer
from fivee_sim.kernel.grid import TERRAIN
from fivee_sim.mcp_server import server as api
from fivee_sim.service.common import sha256_of

from .conftest import mapless_fight

PROBLEM_TYPE = "application/problem+json"
CONFIG_RE = re.compile(
    r'window\.__FIVEE_EDITOR__ = \{token: "[^"]+", apiBase: "/api", version: "[^"]+"\};'
)


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


def replay_bundle() -> dict[str, Any]:
    """A real v2 bundle, built by the exporter that writes them for real.

    Hand-rolling one here would mean hand-rolling its integrity hashes, and a
    fixture whose hashes were computed by the test is a fixture that agrees
    with the test rather than with the format.
    """
    encounter_id = mapless_fight(seed=91)
    exported = api.replay_export(encounter_id)
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

    server: EditorServer
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
            "PUT", f"/api/maps/{map_id}", json_body=document, headers={"If-Match": if_match}
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
    server = EditorServer(
        maps_dir=tmp_path / "maps",
        replays_dir=tmp_path / "replays",
        terrain=TERRAIN,
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
    server = EditorServer(maps_dir=tmp_path / "maps", terrain=TERRAIN, log=log)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        try:
            connection.request("GET", "/api/ping", headers={TOKEN_HEADER: server.token})
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


def assert_problem(response: Response, status: int, fragment: str = "") -> dict[str, Any]:
    """Every error is RFC-9457 problem+json with the status repeated in the body.

    ``fragment`` reads as optional and is not: a status-only assertion passes
    against a server with the branch under test deleted, because a neighbour
    answers with the same status. ``tests/test_assertion_discipline.py`` fails
    the suite for a call that omits it, and carries the reasoning.
    """
    assert response.status == status
    assert response.headers["Content-Type"].startswith(PROBLEM_TYPE)
    problem = response.json()
    assert problem["type"] == "about:blank"
    assert problem["status"] == status
    assert problem["title"]
    if fragment:
        assert fragment in problem["detail"]
    result: dict[str, Any] = problem
    return result


class TestGuards:
    def test_ping_answers_with_the_token(self, editor: Editor) -> None:
        response = editor.request("GET", "/api/ping")
        assert response.status == 200
        answer = response.json()
        assert answer["ok"] is True
        assert answer["version"]
        assert answer["maps_dir"] == str(editor.maps_dir)

    def test_a_missing_token_is_401(self, editor: Editor) -> None:
        assert_problem(editor.request("GET", "/api/ping", token=False), 401, TOKEN_HEADER)

    def test_a_wrong_token_is_401(self, editor: Editor) -> None:
        assert_problem(
            editor.request("GET", "/api/ping", token="not-the-token"), 401, TOKEN_HEADER
        )

    def test_a_foreign_host_header_is_403_even_with_the_token(self, editor: Editor) -> None:
        response = editor.request("GET", "/api/ping", host="evil.example")
        assert_problem(response, 403, "evil.example")

    def test_a_local_host_header_with_port_passes(self, editor: Editor) -> None:
        response = editor.request("GET", "/api/ping", host=f"localhost:{editor.server.port}")
        assert response.status == 200

    def test_a_missing_host_header_is_403(self, editor: Editor) -> None:
        # HTTP/1.1 requires Host, so its absence is already a broken request;
        # the guard treats it as the empty name and refuses it like any other
        # host that is not ours, rather than defaulting to trust.
        connection = http.client.HTTPConnection("127.0.0.1", editor.server.port, timeout=10)
        try:
            connection.putrequest("GET", "/api/ping", skip_host=True)
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
        assert_problem(wrapped, 403, "host '' is not this editor")

    def test_a_differently_cased_host_header_passes(self, editor: Editor) -> None:
        # RFC 9110 §7.2 inherits URI host semantics, in which the host is
        # case-insensitive, so LOCALHOST names this editor exactly as localhost
        # does and a browser is free to send either.
        assert editor.request("GET", "/api/ping", host="LOCALHOST").status == 200
        response = editor.request("GET", "/api/ping", host=f"LocalHost:{editor.server.port}")
        assert response.status == 200

    def test_an_unknown_route_is_404(self, editor: Editor) -> None:
        # "no route for" is what separates this 404 from the map ones, which all
        # say "no map ..."; the status alone would not.
        assert_problem(editor.request("GET", "/api/nothing"), 404, "no route for /api/nothing")

    def test_a_method_mismatch_is_405(self, editor: Editor) -> None:
        assert_problem(
            editor.request("POST", "/api/ping"), 405, "POST is not supported on /api/ping"
        )
        assert_problem(
            editor.request("GET", "/api/generate"), 405, "GET is not supported on /api/generate"
        )

    def test_a_malformed_body_is_400(self, editor: Editor) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", editor.server.port, timeout=10)
        try:
            connection.request(
                "POST",
                "/api/validate",
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
            connection.putrequest("PUT", "/api/maps/big")
            connection.putheader(TOKEN_HEADER, editor.server.token)
            connection.putheader("If-Match", "*")
            connection.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
            connection.endheaders()
            response = connection.getresponse()
            body = json.loads(response.read())
        finally:
            connection.close()
        assert response.status == 413
        assert body["status"] == 413

    def test_a_non_numeric_content_length_is_400(self, editor: Editor) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", editor.server.port, timeout=10)
        try:
            connection.putrequest("POST", "/api/validate")
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
            connection.putrequest("POST", "/api/validate")
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
        response = editor.request("POST", "/api/generate", json_body=["caves"])
        problem = assert_problem(response, 400, "request body must be a JSON object")
        assert "kind, name, params, seed" in problem["detail"]

    def test_an_unknown_key_is_400_naming_it_and_the_valid_keys(self, editor: Editor) -> None:
        response = editor.request(
            "POST", "/api/generate", json_body={"kind": "caves", "kinds": "caves"}
        )
        problem = assert_problem(response, 400, "unknown key(s): 'kinds'")
        assert "Valid keys: kind, name, params, seed" in problem["detail"]


class TestStaticPages:
    def test_the_editor_page_carries_the_config_exactly_once(self, editor: Editor) -> None:
        response = editor.request("GET", "/", token=False)
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/html")
        assert response.headers["Cache-Control"] == "no-store"
        assert CONFIG_MARKER not in response.text
        assert len(CONFIG_RE.findall(response.text)) == 1
        assert editor.server.token in response.text

    def test_the_configured_page_names_the_running_version(self, editor: Editor) -> None:
        # Not merely well-shaped: the page must be told the version of the
        # engine actually serving it, so a stale install is visible in the UI
        # rather than only in a /ping nobody reads. Anchored to the real
        # __version__ so a hardcoded or last-release string fails here.
        response = editor.request("GET", "/", token=False)
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


class TestMapsRoundTrip:
    def test_list_is_empty_before_anything_is_saved(self, editor: Editor) -> None:
        response = editor.request("GET", "/api/maps")
        assert response.status == 200
        assert response.json() == {"maps": []}

    def test_put_creates_gets_match_and_the_listing_names_it(self, editor: Editor) -> None:
        created = editor.put_map("editor-chamber", payload())
        assert created.status == 201
        answer = created.json()
        assert answer["saved"] is True
        assert answer["warnings"] == []
        assert answer["provenance"]["generator"] == "hand"
        sha256 = answer["sha256"]
        assert created.headers["ETag"] == f'"{sha256}"'
        assert editor.file_of("editor-chamber").exists()
        assert sha256_of(editor.file_of("editor-chamber").read_text()) == sha256

        fetched = editor.request("GET", "/api/maps/editor-chamber")
        assert fetched.status == 200
        assert fetched.headers["ETag"] == f'"{sha256}"'
        assert fetched.json()["name"] == "editor chamber"

        listing = editor.request("GET", "/api/maps").json()
        assert [entry["id"] for entry in listing["maps"]] == ["editor-chamber"]
        assert listing["maps"][0]["name"] == "editor chamber"

    def test_ground_height_survives_a_fetch_and_save(self, editor: Editor) -> None:
        # The page paints heights now, but the older promise still stands: a
        # map loaded and saved untouched round-trips its relief byte-for-byte.
        raised = payload()
        raised["elevation"] = {"default": 0, "squares": [[2, 2, 20]]}
        sha256 = editor.put_map("editor-chamber", raised).json()["sha256"]
        fetched = editor.request("GET", "/api/maps/editor-chamber").json()
        assert fetched["elevation"] == {"default": 0, "squares": [[2, 2, 20]]}

        saved = editor.put_map("editor-chamber", fetched, if_match=f'"{sha256}"')
        assert saved.status == 200
        assert saved.json()["sha256"] == sha256  # unchanged bytes, unchanged digest
        assert editor.request("GET", "/api/maps/editor-chamber").json()["elevation"] == (
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
        fetched = editor.request("GET", "/api/maps/editor-chamber").json()
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
        fetched = editor.request("GET", "/api/maps/editor-chamber").json()
        assert fetched["elevation"] == {"default": 0, "squares": [[1, 1, 20], [3, 1, 20]]}

        sha256 = editor.request("GET", "/api/maps/editor-chamber").headers["ETag"].strip('"')
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
        fetched = editor.request("GET", "/api/maps/editor-chamber").json()
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
        fetched = editor.request("GET", "/api/maps/editor-chamber").json()
        assert list(fetched["palette"].items()) == [("floor", "#d2440f"), ("wall", "#aabbcc")]

        sha256 = editor.request("GET", "/api/maps/editor-chamber").headers["ETag"].strip('"')
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
        assert "palette" not in editor.request("GET", "/api/maps/editor-chamber").json()

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
            "/api/maps/editor-chamber/edits",
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
            "/api/maps/editor-chamber/edits",
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
        assert editor.request("GET", "/api/maps/editor-chamber").json()["name"] == (
            "renamed chamber"
        )

    def test_put_with_a_stale_etag_is_409_and_changes_nothing(self, editor: Editor) -> None:
        editor.put_map("editor-chamber", payload())
        before = editor.file_of("editor-chamber").read_bytes()
        renamed = payload()
        renamed["name"] = "should not land"
        response = editor.put_map("editor-chamber", renamed, if_match='"not-the-sha"')
        assert_problem(response, 409, "sha256")
        assert editor.file_of("editor-chamber").read_bytes() == before

    def test_put_without_if_match_is_428(self, editor: Editor) -> None:
        response = editor.request(
            "PUT", "/api/maps/editor-chamber", json_body=payload()
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
        assert_problem(editor.request("GET", "/api/maps/never-saved"), 404, "never-saved")

    def test_a_traversal_id_is_404_from_the_grammar_not_the_index(self, editor: Editor) -> None:
        # Both refusals come from the id grammar, before the maps directory is
        # read at all — hence the second assertion on each. The fragment alone
        # would not bite: "no map '../escape'" is a *prefix* of what _entry_for
        # says for an id it cannot find ("no map '../escape'; maps here: none"),
        # so deleting the grammar guard leaves the fragment matching. The absence
        # of "maps here" is what proves the id never reached the index.
        fetched = assert_problem(
            editor.request("GET", "/api/maps/%2e%2e%2fescape"), 404, "no map '../escape'"
        )
        assert "maps here" not in fetched["detail"]
        response = editor.request(
            "PUT", "/api/maps/%2e%2e%2fescape", json_body=payload(),
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
            "/api/maps/editor-chamber/edits",
            json_body={"operations": [{"op": "paint", "cells": [[1, 1]], "terrain": "wall"}]},
        )
        assert response.status == 200
        answer = response.json()
        assert answer["saved"] is True
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
            "/api/maps/editor-chamber/edits",
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
            "POST", "/api/maps/never-saved/edits", json_body={"operations": []}
        )
        assert_problem(response, 404, "no map 'never-saved'; maps here")

    def test_a_non_list_operations_value_is_400(self, editor: Editor) -> None:
        editor.put_map("editor-chamber", payload())
        before = editor.file_of("editor-chamber").read_bytes()
        response = editor.request(
            "POST",
            "/api/maps/editor-chamber/edits",
            json_body={"operations": {"op": "set_name", "name": "renamed"}},
        )
        assert_problem(response, 400, "'operations' must be a list of edit operations")
        assert editor.file_of("editor-chamber").read_bytes() == before


class TestGenerateAndValidate:
    def test_generate_reports_its_seed_and_persists_nothing(self, editor: Editor) -> None:
        response = editor.request(
            "POST",
            "/api/generate",
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
            "/api/generate",
            json_body={"kind": "caves", "params": {"width": 16, "height": 12}},
        )
        assert isinstance(response.json()["seed"], int)

    def test_an_unknown_kind_is_400_with_the_valid_list(self, editor: Editor) -> None:
        response = editor.request("POST", "/api/generate", json_body={"kind": "maze"})
        assert_problem(response, 400, "caves, dungeon, overland")

    def test_a_non_string_kind_is_400_before_the_service_sees_it(self, editor: Editor) -> None:
        # A string kind reaches the service and comes back as its ValueError; a
        # kind that is not text is refused here, so the two are distinguished by
        # their detail rather than by the status they share.
        response = editor.request("POST", "/api/generate", json_body={"kind": 7})
        assert_problem(response, 400, "'kind' must name a generator")

    def test_a_non_object_params_value_is_400(self, editor: Editor) -> None:
        response = editor.request(
            "POST", "/api/generate", json_body={"kind": "caves", "params": "wide"}
        )
        assert_problem(response, 400, "'params' must be an object")

    def test_a_seed_that_is_not_a_whole_number_is_400(self, editor: Editor) -> None:
        # true is an int in Python, so a bare isinstance(seed, int) would accept
        # it and seed the generator with 1; the boolean case is the one that
        # proves the guard, not the string one.
        boolean = editor.request(
            "POST", "/api/generate", json_body={"kind": "caves", "seed": True}
        )
        assert_problem(boolean, 400, "'seed' must be a whole number")
        text = editor.request(
            "POST", "/api/generate", json_body={"kind": "caves", "seed": "eleven"}
        )
        assert_problem(text, 400, "'seed' must be a whole number")

    def test_a_name_that_is_not_text_is_400(self, editor: Editor) -> None:
        response = editor.request(
            "POST", "/api/generate", json_body={"kind": "caves", "name": 7}
        )
        assert_problem(response, 400, "'name' must be text")

    def test_validate_answers_ok_for_a_good_document(self, editor: Editor) -> None:
        response = editor.request("POST", "/api/validate", json_body=payload())
        assert response.status == 200
        assert response.json() == {"ok": True, "errors": [], "warnings": []}

    def test_validate_collects_every_error_without_raising(self, editor: Editor) -> None:
        broken = payload()
        broken["tiles"][0] = "###"
        del broken["provenance"]
        response = editor.request("POST", "/api/validate", json_body=broken)
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
        response = editor.request("GET", "/api/replays")
        assert response.status == 200
        assert response.json() == {"replays": []}

    def test_a_bundle_on_disk_is_listed_under_the_id_the_get_route_takes(
        self, editor: Editor
    ) -> None:
        editor.put_replay("gatehouse-brawl", replay_bundle())

        listed = editor.request("GET", "/api/replays").json()["replays"]

        assert [row["id"] for row in listed] == ["gatehouse-brawl"]
        assert editor.request("GET", "/api/replays/gatehouse-brawl").status == 200

    def test_the_row_carries_what_a_chooser_shows_without_a_second_request(
        self, editor: Editor
    ) -> None:
        bundle = replay_bundle()
        editor.put_replay("gatehouse-brawl", bundle)

        (row,) = editor.request("GET", "/api/replays").json()["replays"]

        assert row["name"] == bundle["name"]
        assert row["seed"] == bundle["seed"]
        assert row["events"] == len(bundle["events"])
        assert row["format_version"] == 2

    def test_listing_needs_the_token_like_every_other_api_route(
        self, editor: Editor
    ) -> None:
        assert_problem(
            editor.request("GET", "/api/replays", token=False), 401, TOKEN_HEADER
        )

    def test_fetching_one_needs_the_token_too(self, editor: Editor) -> None:
        editor.put_replay("gatehouse-brawl", replay_bundle())
        assert_problem(
            editor.request("GET", "/api/replays/gatehouse-brawl", token=False),
            401,
            TOKEN_HEADER,
        )

    def test_the_bundle_comes_back_whole_with_its_hash_as_an_etag(
        self, editor: Editor
    ) -> None:
        target = editor.put_replay("gatehouse-brawl", replay_bundle())

        response = editor.request("GET", "/api/replays/gatehouse-brawl")

        assert response.status == 200
        assert response.json() == json.loads(target.read_text(encoding="utf-8"))
        assert response.headers["ETag"] == f'"{sha256_of(target.read_text("utf-8"))}"'

    def test_an_unknown_id_is_404_and_names_what_is_actually_there(
        self, editor: Editor
    ) -> None:
        editor.put_replay("gatehouse-brawl", replay_bundle())
        problem = assert_problem(
            editor.request("GET", "/api/replays/no-such-fight"), 404, "no replay"
        )
        assert "gatehouse-brawl" in problem["detail"]

    def test_a_traversal_id_is_simply_an_unknown_replay(self, editor: Editor) -> None:
        assert_problem(
            editor.request("GET", "/api/replays/..%2f..%2fetc%2fpasswd"),
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
            editor.request("GET", "/api/replays/broken-fight"),
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

        listed = editor.request("GET", "/api/replays").json()["replays"]

        assert [row["id"] for row in listed] == ["broken-fight"]

    def test_replays_are_read_only_over_rest(self, editor: Editor) -> None:
        editor.put_replay("gatehouse-brawl", replay_bundle())
        assert_problem(
            editor.request(
                "PUT", "/api/replays/gatehouse-brawl", json_body=replay_bundle()
            ),
            405,
            "PUT is not supported",
        )

    def test_the_collection_refuses_writes_too(self, editor: Editor) -> None:
        assert_problem(
            editor.request("POST", "/api/replays", json_body={}),
            405,
            "POST is not supported",
        )


class TestShutdown:
    def test_shutdown_is_202_and_the_server_stops(self, editor: Editor) -> None:
        response = editor.request("POST", "/api/shutdown")
        assert response.status == 202
        assert response.json() == {"stopping": True}
        editor.thread.join(timeout=5)
        assert not editor.thread.is_alive()

    def test_shutdown_still_needs_the_token(self, editor: Editor) -> None:
        assert_problem(
            editor.request("POST", "/api/shutdown", token=False), 401, TOKEN_HEADER
        )
        assert editor.thread.is_alive()
