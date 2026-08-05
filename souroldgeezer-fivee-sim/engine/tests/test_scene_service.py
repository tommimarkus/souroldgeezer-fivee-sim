"""The scene service: a saved ``encounter.create`` body, and only the envelope.

Two claims are pinned here and they pull in opposite directions, which is the
whole design.

**The envelope is checked.** Unknown keys, a seed that is not a whole number, a
scene naming both an inline map and a saved one, a ``map_id`` that resolves to
nothing — each is a diagnostic, and none of them reaches disk.

**A combatant spec is not.** Whether a creature specification is legal belongs
to ``encounter.create``, which refuses it at Play time with its own messages. A
second copy of that judgement here would be two owners of one rule, and the
copy would drift the first time a spec field is added. The cost is deliberate: a
scene can be saved that will not start, because an editor buffer is a draft.
``test_a_combatant_spec_is_not_second_guessed_here`` is that trade, written down
as a test — it saves a scene the engine will refuse and then watches it be
refused, by the owner that owns it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fivee_sim.service import scenes as service
from fivee_sim.service.common import sha256_of
from fivee_sim.service.errors import NotFoundError, RequestError, StaleWriteError
from fivee_sim.web import routes

from . import api


def scene() -> dict[str, Any]:
    """A whole scene, rebuilt fresh so a test may mutate freely."""
    return {
        "name": "Ambush at the ford",
        "combatants": [
            {
                "name": "Thora",
                "team": "party",
                "ac": 16,
                "max_hp": 30,
                "position": [1, 1],
            },
            {"monster": "Goblin Warrior", "team": "monsters", "position": [3, 2]},
        ],
        "seed": 20260805,
        "movement_rule": "5-5-5",
    }


def problems(document: Any, **kwargs: Any) -> list[str]:
    """Every error message one validation reported, for a substring assertion."""
    return [entry["problem"] for entry in service.validate(document, **kwargs)["errors"]]


class TestTheEnvelope:
    def test_a_whole_scene_validates_clean(self) -> None:
        report = service.validate(scene())
        assert report == {"ok": True, "errors": [], "warnings": []}

    def test_a_scene_that_is_not_an_object_is_refused(self) -> None:
        assert any("must be an object" in one for one in problems([{"name": "Thora"}]))

    def test_an_unknown_key_is_refused_and_names_the_valid_ones(self) -> None:
        stray = scene()
        stray["combatnats"] = []
        reported = problems(stray)
        assert any("'combatnats'" in one for one in reported)
        # Named rather than merely refused: a misspelling is only actionable if
        # the refusal says what was meant. Derived from the declaration, so a
        # key added to a scene tomorrow is offered by this message today.
        assert any(
            all(key in one for key in service.SCENE_KEYS) for one in reported
        ), reported

    def test_combatants_are_required(self) -> None:
        bare = scene()
        del bare["combatants"]
        assert any("'combatants' is required" in one for one in problems(bare))

    def test_combatants_must_be_a_list_of_objects(self) -> None:
        assert any("list" in one for one in problems({**scene(), "combatants": "Thora"}))
        assert any(
            "object" in one for one in problems({**scene(), "combatants": ["Thora"]})
        )

    def test_a_seed_that_is_not_a_whole_number_is_refused(self) -> None:
        assert any("whole number" in one for one in problems({**scene(), "seed": "7"}))
        # ``True`` is an ``int`` in Python and is not a seed anywhere else in
        # this engine either.
        assert any("whole number" in one for one in problems({**scene(), "seed": True}))

    def test_a_scene_with_no_seed_is_valid_and_says_what_that_costs(self) -> None:
        seedless = scene()
        del seedless["seed"]
        report = service.validate(seedless)
        assert report["ok"] is True
        assert report["errors"] == []
        assert any("seed" in entry["problem"] for entry in report["warnings"])

    def test_a_scene_may_not_name_both_an_inline_map_and_a_saved_one(self) -> None:
        both = {**scene(), "map": {"format": "fivee-sim-map"}, "map_id": "chamber"}
        assert any("not both" in one for one in problems(both))

    def test_a_scene_may_name_neither_because_a_mapless_fight_is_a_fight(self) -> None:
        """``encounter.create`` takes neither, so a scene that omits both starts.

        The envelope refuses *both together*, which is the refusal
        ``sessions.resolve_battle_map`` makes, and nothing more: a theatre-of-
        the-mind encounter is an ordinary encounter and has to stay a savable
        scene.
        """
        assert service.validate(scene())["ok"] is True

    def test_an_unresolvable_map_id_is_refused_only_when_the_index_is_known(
        self,
    ) -> None:
        """The one check that needs a second directory, and it is passed in.

        ``map_ids`` of ``None`` is *the caller has no map index*, not *there are
        no maps*: a scene service that reached for the maps directory itself
        would import the map document, the grid, and the kernel behind them, and
        a scene is a request body rather than a domain object.
        """
        named = {**scene(), "map_id": "chamber"}
        assert service.validate(named)["ok"] is True
        assert service.validate(named, map_ids=["chamber"])["ok"] is True
        reported = problems(named, map_ids=["ford"])
        assert any("no saved map 'chamber'" in one for one in reported), reported
        assert any("ford" in one for one in reported), reported

    def test_content_paths_must_be_a_list_of_text(self) -> None:
        assert any(
            "list" in one for one in problems({**scene(), "content_paths": "packs"})
        )
        assert any(
            "text" in one for one in problems({**scene(), "content_paths": [7]})
        )

    def test_a_movement_rule_is_checked_for_being_text_and_no_further(self) -> None:
        """Which rules exist is ``kernel.grid``'s to say, and it says so at Play.

        The same boundary the combatant specs sit behind: this layer may not
        import the enum, so it checks the shape and leaves the meaning to the
        operation that resolves it.
        """
        assert any(
            "text" in one for one in problems({**scene(), "movement_rule": 555})
        )
        assert service.validate({**scene(), "movement_rule": "not-a-rule"})["ok"] is True

    def test_a_combatant_spec_is_not_second_guessed_here(self, tmp_path: Path) -> None:
        """The trade Decision 1 accepts, both halves of it, in one test.

        A spec nobody can build is *saved* — the envelope is well formed — and
        then refused by ``encounter.create``, which is the one owner of what a
        combatant is. If this ever starts failing on the first assertion,
        someone has copied spec validation into the scene service and there are
        two owners again.

        Saved to disk and read back, and it is :func:`load`'s answer that is
        handed to the fight. Validating in memory and then posting the dict
        above would prove nothing about a *stored* draft: the roster this layer
        returns is the roster Play posts, so that is the roster the refusal has
        to be asserted on.
        """
        broken = {"team": "party", "ac": 16}
        unbuildable = {**scene(), "combatants": [broken, scene()["combatants"][1]]}
        assert service.validate(unbuildable)["ok"] is True

        service.save("draft", unbuildable, tmp_path)
        stored = service.load("draft", tmp_path)["document"]
        assert stored["combatants"] == unbuildable["combatants"], stored

        # The roster is two long, so the refusal that comes back is the spec
        # being judged rather than the arity — shape before arity is
        # ``combatants_from_specs``'s own order.
        with pytest.raises(RequestError, match="combatant spec is missing 'name'"):
            api.encounter_create(list(stored["combatants"]))


class TestScenesAreEncounterBodies:
    def test_the_scene_keys_are_the_encounter_create_body_plus_its_labels(self) -> None:
        """Derived from the contract, never restated: one declaration, two readers.

        ``encounter.create``'s own body schema is the authority on what an
        encounter takes. A key added there and not here would be a key a scene
        silently drops — the saved fight would start, and start *differently*.
        """
        creation = next(
            route for route in routes.api_routes() if route.operation == "encounter.create"
        )
        declared = set((creation.body_schema or {}).get("properties", {}))
        assert declared, "encounter.create declares no body; this test would be vacuous"
        assert set(service.ENCOUNTER_KEYS) == declared
        assert set(service.ENCOUNTER_KEYS) <= set(service.SCENE_KEYS)


class TestFiles:
    def test_save_then_load_round_trips_byte_identically(self, tmp_path: Path) -> None:
        saved = service.save("ford", scene(), tmp_path)
        assert saved["saved"] is True
        assert saved["scene_id"] == "ford"
        assert saved["warnings"] == []

        written = Path(str(saved["path"]))
        assert written.read_text(encoding="utf-8") == service.render(scene())
        assert saved["sha256"] == sha256_of(written.read_text(encoding="utf-8"))
        assert saved["bytes"] == len(written.read_bytes())

        loaded = service.load("ford", tmp_path)
        assert loaded["document"] == scene()
        assert loaded["sha256"] == saved["sha256"]

    def test_an_invalid_envelope_never_reaches_disk(self, tmp_path: Path) -> None:
        with pytest.raises(RequestError, match="'combatants' is required"):
            service.save("ford", {"seed": 1}, tmp_path)
        assert not service.path_for_id("ford", tmp_path).exists()

    def test_saving_over_an_id_with_no_version_is_refused(self, tmp_path: Path) -> None:
        service.save("ford", scene(), tmp_path)
        with pytest.raises(RequestError, match="already exists"):
            service.save("ford", scene(), tmp_path)

    def test_a_stale_expected_sha_is_refused_and_the_file_is_untouched(
        self, tmp_path: Path
    ) -> None:
        first = service.save("ford", scene(), tmp_path)
        moved = {**scene(), "name": "Ambush, second thoughts"}
        service.save("ford", moved, tmp_path, expected_sha256="*")
        after_other_writer = service.path_for_id("ford", tmp_path).read_bytes()

        with pytest.raises(StaleWriteError, match="has advanced"):
            service.save("ford", scene(), tmp_path, expected_sha256=str(first["sha256"]))
        assert service.path_for_id("ford", tmp_path).read_bytes() == after_other_writer

    def test_a_matching_expected_sha_writes(self, tmp_path: Path) -> None:
        saved = service.save("ford", scene(), tmp_path)
        moved = {**scene(), "name": "Ambush, revised"}
        again = service.save(
            "ford", moved, tmp_path, expected_sha256=str(saved["sha256"])
        )
        assert again["sha256"] != saved["sha256"]
        assert service.load("ford", tmp_path)["document"] == moved

    def test_the_listing_names_every_scene_and_skips_everything_else(
        self, tmp_path: Path
    ) -> None:
        service.save("ford", scene(), tmp_path)
        service.save("crypt", {**scene(), "name": "The crypt", "map_id": "crypt"}, tmp_path)
        (tmp_path / "not-a-scene.json").write_text('{"pack": "x"}', encoding="utf-8")
        (tmp_path / "junk.json").write_text("{", encoding="utf-8")

        found = service.index(tmp_path)
        assert sorted(found) == ["crypt", "ford"]
        assert found["crypt"]["name"] == "The crypt"
        assert found["crypt"]["map_id"] == "crypt"
        assert found["ford"]["map_id"] is None
        assert found["ford"]["combatants"] == 2
        assert found["ford"]["seed"] == 20260805
        assert Path(str(found["ford"]["path"])).name == "ford.json"

    def test_an_unknown_id_says_what_is_there(self, tmp_path: Path) -> None:
        service.save("ford", scene(), tmp_path)
        with pytest.raises(NotFoundError, match="no scene 'crypt'; scenes here: ford"):
            service.load("crypt", tmp_path)

    def test_a_traversal_id_is_refused_by_the_grammar(self, tmp_path: Path) -> None:
        """An id outside the slug alphabet cannot name a file here, so it is
        simply unknown — refused before any directory is read, which is why the
        message names no neighbours."""
        service.save("ford", scene(), tmp_path)
        with pytest.raises(NotFoundError, match=r"no scene '\.\./secret'"):
            service.load("../secret", tmp_path)
        with pytest.raises(NotFoundError, match=r"no scene '\.\./secret'"):
            service.path_for_id("../secret", tmp_path)

    def test_a_file_that_is_not_a_scene_is_not_loadable_as_one(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "pack.json").write_text(
            json.dumps({"creatures": []}), encoding="utf-8"
        )
        with pytest.raises(NotFoundError, match="no scene 'pack'"):
            service.load("pack", tmp_path)

    def test_scenes_root_prefers_the_environment_then_the_project(self) -> None:
        assert service.scenes_root({"FIVEE_SIM_SCENES": "/somewhere/scenes"}) == Path(
            "/somewhere/scenes"
        )
        assert service.scenes_root({"FIVEE_SIM_PROJECT_DIR": "/neutral"}) == Path(
            "/neutral/.fivee-sim/scenes"
        )
        assert service.scenes_root({"CLAUDE_PROJECT_DIR": "/repo"}) == Path(
            "/repo/.fivee-sim/scenes"
        )
        assert service.scenes_root({}) == Path.cwd() / ".fivee-sim" / "scenes"

    def test_the_configured_root_is_where_a_call_that_names_none_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default root is resolved per call, never captured at import.

        A module-level default would make every launch on one machine share the
        directory that happened to be configured when the first one imported
        this module — the same trap ``paths`` exists to close for maps.
        """
        root = tmp_path / "elsewhere"
        monkeypatch.setenv("FIVEE_SIM_SCENES", str(root))
        api.scene_save("ford", scene())
        assert (root / "ford.json").is_file()
        assert [entry["id"] for entry in api.scene_list()["scenes"]] == ["ford"]
        assert api.scene_get("ford")["document"] == scene()
