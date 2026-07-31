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

from fivee_sim.editor.http_server import CONFIG_MARKER, MAX_BODY_BYTES, TOKEN_HEADER, EditorServer
from fivee_sim.kernel.grid import TERRAIN
from fivee_sim.service.common import sha256_of

PROBLEM_TYPE = "application/problem+json"
CONFIG_RE = re.compile(r'window\.__FIVEE_EDITOR__ = \{token: "[^"]+", apiBase: "/api"\};')


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


@pytest.fixture()
def editor(tmp_path: Path) -> Iterator[Editor]:
    log = io.StringIO()
    server = EditorServer(maps_dir=tmp_path / "maps", terrain=TERRAIN, log=log)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield Editor(server=server, thread=thread, maps_dir=tmp_path / "maps", log=log)
    server.shutdown()
    server.close()
    thread.join(timeout=5)


def assert_problem(response: Response, status: int, fragment: str = "") -> dict[str, Any]:
    """Every error is RFC-9457 problem+json with the status repeated in the body."""
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
        assert_problem(editor.request("GET", "/api/ping", token="not-the-token"), 401)

    def test_a_foreign_host_header_is_403_even_with_the_token(self, editor: Editor) -> None:
        response = editor.request("GET", "/api/ping", host="evil.example")
        assert_problem(response, 403, "evil.example")

    def test_a_local_host_header_with_port_passes(self, editor: Editor) -> None:
        response = editor.request("GET", "/api/ping", host=f"localhost:{editor.server.port}")
        assert response.status == 200

    def test_an_unknown_route_is_404(self, editor: Editor) -> None:
        assert_problem(editor.request("GET", "/api/nothing"), 404)

    def test_a_method_mismatch_is_405(self, editor: Editor) -> None:
        assert_problem(editor.request("POST", "/api/ping"), 405)
        assert_problem(editor.request("GET", "/api/generate"), 405)

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


class TestStaticPages:
    def test_the_editor_page_carries_the_config_exactly_once(self, editor: Editor) -> None:
        response = editor.request("GET", "/", token=False)
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/html")
        assert response.headers["Cache-Control"] == "no-store"
        assert CONFIG_MARKER not in response.text
        assert len(CONFIG_RE.findall(response.text)) == 1
        assert editor.server.token in response.text

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
        # The page cannot paint heights, but it PUTs back the document it was
        # given — so a map with relief must not come home flat.
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

    def test_a_traversal_id_is_404(self, editor: Editor) -> None:
        assert_problem(editor.request("GET", "/api/maps/%2e%2e%2fescape"), 404)
        response = editor.request(
            "PUT", "/api/maps/%2e%2e%2fescape", json_body=payload(),
            headers={"If-Match": "*"},
        )
        assert_problem(response, 404)
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
        response = editor.request(
            "POST", "/api/maps/never-saved/edits", json_body={"operations": []}
        )
        assert_problem(response, 404)


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


class TestShutdown:
    def test_shutdown_is_202_and_the_server_stops(self, editor: Editor) -> None:
        response = editor.request("POST", "/api/shutdown")
        assert response.status == 202
        assert response.json() == {"stopping": True}
        editor.thread.join(timeout=5)
        assert not editor.thread.is_alive()

    def test_shutdown_still_needs_the_token(self, editor: Editor) -> None:
        assert_problem(editor.request("POST", "/api/shutdown", token=False), 401)
        assert editor.thread.is_alive()
