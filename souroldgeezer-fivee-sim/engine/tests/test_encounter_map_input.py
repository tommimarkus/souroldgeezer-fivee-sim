"""What an *inline* map may be, and how ``resolve_battle_map`` tells them apart.

``encounter.create`` takes either ``map`` or ``map_id``, and until now ``map``
meant one thing: the hand-written battle-map **spec** — ``width``, ``height``,
``rows`` and a ``legend``. The browser editor's Play button broke that
assumption. Its buffer is a map **document**, and an unsaved buffer has no id to
send, so Play posted the document inline and the spec parser refused it with
``unknown map key 'format'`` — the very key that names the format.

So the inline value is self-identifying, and this file pins the three answers:

* a **spec** still builds the map it always did;
* a **document** goes through the document path — the same parse, the same
  diagnostics, the same 422 a saved map gets — because an inline document that
  skipped validation would be a second and laxer way onto the same grid;
* an object that is **neither** is still refused as a malformed spec, naming the
  keys a spec takes.

The dispatch is presence of ``format`` and not its value, which is what makes
the refusals useful: an object claiming a format we do not speak is judged by
the document parser, which says *must be "fivee-sim-map"*, rather than by the
spec parser, which could only call the key unknown.
"""

from __future__ import annotations

from typing import Any

import pytest

from fivee_sim.map_document import FORMAT, MapError
from fivee_sim.service import sessions
from fivee_sim.service.errors import RequestError
from fivee_sim.service.specs import MAP_KEYS

from . import api
from .conftest import REPLAY_GOBLIN, REPLAY_HERO

FIXTURE = "Authored for the test suite; 5E-compatible original content"

#: The buffer shape the editor posts: a whole ``fivee-sim-map`` document. Wide
#: enough for the shared roster, whose goblin stands at (15, 15).
BUFFER_DOOR = "buffer-door"
BUFFER_DC = 17


def buffer_document() -> dict[str, Any]:
    return {
        "format": "fivee-sim-map",
        "format_version": 1,
        "name": "unsaved buffer",
        "grid": {"width": 20, "height": 20, "cell_feet": 5},
        "legend": {".": "normal", "#": "wall"},
        "tiles": ["." * 20 for _ in range(20)],
        "features": [
            {
                "id": BUFFER_DOOR,
                "kind": "door",
                "at": [10, 0],
                "orientation": "vertical",
                "state": "closed",
                "check": {"ability": "strength", "dc": BUFFER_DC},
            }
        ],
        "provenance": {
            "generator": "hand",
            "seed": 3,
            "params": {"width": 20, "height": 20},
            "edited": True,
            "source": FIXTURE,
        },
    }


#: The other inline shape, unchanged: the hand-authored battle-map spec.
def buffer_spec() -> dict[str, Any]:
    return {
        "name": "inline spec room",
        "width": 20,
        "height": 20,
        "default_terrain": "normal",
        "terrain": [{"kind": "wall", "squares": [[0, 0]]}],
    }


def roster() -> list[dict[str, Any]]:
    return [dict(REPLAY_HERO), dict(REPLAY_GOBLIN)]


class TestAnInlineMapIsWhicheverFormatItSaysItIs:
    def test_a_spec_still_builds_the_map_it_always_did(self) -> None:
        created = api.encounter_create(roster(), seed=11, map=buffer_spec())

        snapshot = api.encounter_state(str(created["encounter_id"]))
        assert snapshot["map"]["name"] == "inline spec room"
        assert snapshot["map"]["width"] == 20

    def test_a_document_is_accepted_and_read_as_a_document(self) -> None:
        """The Play case. ``format`` routes it, and the fight is on that map.

        The door's DC is the discriminator: it exists only in the document
        format, so a fight that knows it can only have been built by
        the document path. A test asserting the map's
        *name* would pass against a spec parser taught to ignore ``format``.
        """
        created = api.encounter_create(roster(), seed=11, map=buffer_document())

        snapshot = api.encounter_state(str(created["encounter_id"]))
        assert snapshot["map"]["name"] == "unsaved buffer"
        features = snapshot["map"]["features"]
        assert BUFFER_DOOR in features, features
        assert features[BUFFER_DOOR]["check"]["dc"] == BUFFER_DC

    def test_an_object_that_is_neither_is_refused_as_a_malformed_spec(self) -> None:
        """No ``format``, so it is a spec, and the spec parser names its keys."""
        with pytest.raises(RequestError, match="unknown map key 'grid'"):
            api.encounter_create(roster(), seed=11, map={"grid": {"width": 2}})

    def test_an_inline_document_gets_the_validation_a_saved_one_gets(self) -> None:
        """Same parse, same :class:`MapError`, same diagnostics — not a shortcut.

        ``MapError`` rather than ``RequestError`` is the whole point: the adapter
        answers it 422 with every diagnostic, which is what a saved map's refusal
        looks like. Routing an inline document through a laxer door would have
        let a buffer onto the grid that no saved file could reach.
        """
        broken = {**buffer_document(), "tiles": ["..", ".."]}
        with pytest.raises(MapError, match="tiles"):
            api.encounter_create(roster(), seed=11, map=broken)

    def test_a_format_we_do_not_speak_is_named_by_the_document_parser(self) -> None:
        """Presence dispatches; the value is the document parser's to judge.

        Dispatching on ``format == "fivee-sim-map"`` would send a future or
        mistyped format back to the spec parser, whose only available complaint
        is that ``format`` is not a spec key — which is exactly the unhelpful
        refusal this change exists to remove.
        """
        wrong = {**buffer_document(), "format": "some-other-map"}
        with pytest.raises(MapError, match='must be "fivee-sim-map"'):
            api.encounter_create(roster(), seed=11, map=wrong)

    def test_naming_both_an_inline_map_and_a_saved_one_is_still_refused(self) -> None:
        with pytest.raises(RequestError, match="not both"):
            api.encounter_create(
                roster(), seed=11, map=buffer_document(), map_id="chamber"
            )


class TestAnInlineDocumentHasNoFileToHaveMoved:
    def test_it_reports_no_map_source_because_there_is_no_map_to_source_it_from(
        self,
    ) -> None:
        """``map_source`` answers *has the file changed since?* — and there is none.

        A capture here would have to invent an id nothing resolves and a
        ``current_sha256`` nothing can be read back from, so ``stale`` could
        never be anything but ``False``. Absent is the honest answer, and it is
        the same answer a spec gets.
        """
        document = api.encounter_create(roster(), seed=11, map=buffer_document())
        spec = api.encounter_create(roster(), seed=11, map=buffer_spec())

        assert api.encounter_state(str(document["encounter_id"]))["map_source"] is None
        assert api.encounter_state(str(spec["encounter_id"]))["map_source"] is None

    def test_the_fight_still_says_which_map_it_is_on_by_carrying_it_whole(
        self,
    ) -> None:
        """No id, so the map travels by value — and as the document it arrived as.

        Rendering it back out of the battle map instead would drop everything
        a grid had no slot for: the provenance that says where the map came
        from, and any fixture the grid does not consult. That is the replay gap
        the missing ``map_source`` would otherwise leave, so it is closed here
        rather than papered over there.
        """
        created = api.encounter_create(roster(), seed=11, map=buffer_document())

        bundle = api.replay_export(
            str(created["encounter_id"]), format_version=2
        )["bundle"]

        assert bundle["map"] == buffer_document()

    def test_a_saved_map_still_reports_where_it_came_from(self) -> None:
        """The branch the inline one is being distinguished from, still intact."""
        api.map_save("chamber", buffer_document(), "*")
        created = api.encounter_create(roster(), seed=11, map_id="chamber")

        source = api.encounter_state(str(created["encounter_id"]))["map_source"]
        assert source is not None
        assert source["map_id"] == "chamber"
        assert source["stale"] is False


class TestTheDispatchIsTheOneDeclaration:
    def test_the_marker_is_a_key_no_battle_map_spec_may_carry(self) -> None:
        """Held against both parsers rather than restated as a literal.

        If ``specs.MAP_KEYS`` ever grew the key the document format declares
        itself with, the dispatch would silently start reading specs as
        documents. This fails first instead.
        """
        assert sessions.DOCUMENT_MARKER not in MAP_KEYS
        assert sessions.DOCUMENT_MARKER in buffer_document()
        assert buffer_document()[sessions.DOCUMENT_MARKER] == FORMAT
