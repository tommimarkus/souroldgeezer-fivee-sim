"""The static pages' contracts: injection slots, and the offline guarantee.

The editor and viewer are localhost tools that must work with no network at
all — served by the localhost editor process or opened straight from disk. So
the one property these tests defend hardest is that **no static asset
references anything external**: no CDN scripts, no font services, no
protocol-relative sneaks. Fonts are the system stack and every icon is drawn
on the canvas, which is why the guarantee can be a regex rather than a hope.

The injection contracts are byte-level: the server replaces the config marker
and ``replay_export(embed=True)`` replaces the embedded-data slot, so each
must appear exactly once, exactly as written.

**What these tests do not do, stated because it is invisible from a green run.**
Every assertion here reads the assets as *text*. Nothing in this file executes
``renderer.js``, opens a page, or draws a canvas, so nothing here would notice a
renderer that drew nothing, a token injected into the wrong element, or a viewer
unable to parse its own embedded bundle.

This file used to end by saying that if the editor grew past a convenience, this
was where the argument for a real harness started. It grew, the argument was
made, and it was answered: ``scripts/check-editor-behaviour.mjs``, at the
repository root, drives the same three shipped files under ``node`` — each
page's own inline script against a stub DOM — and asserts what a page *decided*.
It exists because three real ``editor.html`` defects shipped green past this
file, one of them a resize that wrote a half-resized document to disk. It stays
outside pytest so that no browser toolchain enters a Python repository.

So the division is **text contracts here, behaviour there**, and a new assertion
belongs on whichever side can actually see it: a substring search cannot tell
you whether the tile loop consulted an override, and a stub DOM cannot tell you
whether an injection slot appears exactly once. Neither half is a browser —
there is no layout, no CSS, no pixels — but "nothing executes these files" is no
longer true of the repository, only of this file.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from fivee_sim.web.http_server import CONFIG_MARKER
from fivee_sim.web.routes import api_routes

STATIC = Path(str(resources.files("fivee_sim.web"))) / "static"
#: Every served HTML page. Claims parametrized over this one are claims about
#: being a page of this service at all: a doctype, balanced tags, one config
#: marker, no off-origin reference.
PAGES = ("editor.html", "viewer.html", "home.html")
#: The two that draw. The landing page has no canvas and must not load the
#: renderer, so the drawing claims cannot live on ``PAGES`` — a tuple that
#: quietly grew a third member would otherwise start asserting the landing page
#: pulls in a script it has no use for.
CANVAS_PAGES = ("editor.html", "viewer.html")
ASSETS = (*PAGES, "renderer.js")

EMBED_SLOT = '<script type="application/json" id="embedded-data">null</script>'
RENDERER_TAG = '<script src="/assets/renderer.js"></script>'

#: An absolute URL anywhere, a protocol-relative src/href, or a CSS url()
#: with any scheme but data: — each one is a network dependency in disguise.
_EXTERNAL = re.compile(
    r"https?://"
    r"|(?:src|href)\s*=\s*\\?[\"']//"
    r"|url\(\s*[\"']?(?!data:)[a-z][a-z0-9+.-]*:",
    re.IGNORECASE,
)
#: Every src/href value, including one inside a JS string with escaped quotes.
_REFERENCE = re.compile(r"""(?:src|href)=\\?["']([^"'\\]+)""")
#: Same-origin routes the one server actually answers on. Root-relative paths
#: are local by construction, but the set stays explicit rather than becoming
#: ``startswith("/")``: a typo'd route should fail here, not 404 in a browser.
_ALLOWED_REFERENCES = frozenset(
    {"/assets/renderer.js", "renderer.js", "/", "/editor", "/viewer"}
)


#: The animated-family declaration, loaded the way the invalid-replay corpus is
#: in ``test_replay_validation.py`` — beside the module that asserts on it.
ANIMATED_FAMILIES: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "animated-event-families.json").read_text(
        encoding="utf-8"
    )
)


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


class TestInjectionContracts:
    @pytest.mark.parametrize("page", PAGES)
    def test_each_page_carries_the_config_marker_exactly_once(self, page: str) -> None:
        assert read(page).count(CONFIG_MARKER) == 1

    def test_the_viewer_carries_the_embedded_data_slot_exactly_once(self) -> None:
        assert read("viewer.html").count(EMBED_SLOT) == 1
        assert read("viewer.html").count('id="embedded-data"') == 1

    def test_the_editor_has_no_embedded_data_slot(self) -> None:
        # The slot is the viewer's contract with replay_export; a second copy
        # anywhere would make "replace it exactly once" ambiguous.
        assert 'id="embedded-data"' not in read("editor.html")

    @pytest.mark.parametrize("page", CANVAS_PAGES)
    def test_each_page_loads_the_shared_renderer_by_its_served_route(self, page: str) -> None:
        # replay_export(embed=True) inlines the renderer by replacing this
        # exact tag, so it must exist verbatim and exactly once.
        assert read(page).count(RENDERER_TAG) == 1

    def test_the_renderer_defines_its_single_namespace(self) -> None:
        assert "var FiveeRenderer" in read("renderer.js")


class TestOneServiceTwoPages:
    """Both pages belong to one launch, and each says so in its own way.

    The asymmetry is the contract and is why these are separate assertions:
    the editor is only ever served, so its link out is unconditional; the
    viewer also ships as a standalone export, so everything that depends on a
    server has to start hidden. Whether the gate actually *works* is a
    behaviour claim — ``scripts/check-editor-behaviour.mjs`` owns it.
    """

    def test_the_editor_links_to_the_viewer_on_the_same_server(self) -> None:
        assert read("editor.html").count('href="/viewer"') == 1

    def test_the_viewer_ships_its_served_controls_hidden(self) -> None:
        source = read("viewer.html")
        # `hidden` on the element itself, so the export is correct before a
        # single line of script runs — not un-hidden and then re-hidden.
        assert '<label id="served-replays" hidden>' in source
        assert '<a id="link-home" href="/" hidden>' in source
        assert '<a id="link-editor" href="/editor" hidden>' in source

    def test_the_viewer_reaches_the_network_only_behind_the_config_gate(self) -> None:
        # The offline guarantee, as source: the page's single fetch call sits
        # inside apiGet, and apiGet is only reachable from connectToServer,
        # which the boot block calls only when the injected config exists.
        source = read("viewer.html")
        assert source.count("window.fetch(") == 1
        assert "else if (window.__FIVEE_EDITOR__) { connectToServer(); }" in source

    def test_the_served_viewer_reuses_the_one_bundle_load_path(self) -> None:
        # A second load path is how the validation, the level wiring and the
        # empty-state hiding drift apart; the served source must land in the
        # same loadBundle the file picker uses.
        assert "loadBundle(answer.json, id);" in read("viewer.html")


class TestViewerFeatureVisibility:
    # The one place the two pages deliberately disagree about what to draw.
    # Presence only, like every assertion in this file — see the module
    # docstring for why nothing here executes.

    def test_the_viewer_hides_spawn_hints_from_a_replay_audience(self) -> None:
        # A spawn hint is authoring furniture: it claims no square, no fight
        # ever operates it, and by the time a replay plays the tokens it was
        # placed for are already on the map. The shared renderer draws every
        # feature it is handed, so the filter has to sit on the viewer's side
        # of the call rather than in renderer.js.
        source = read("viewer.html")
        assert 'HIDDEN_FEATURE_KINDS = ["spawn"]' in source
        assert "mapDoc = displayDoc(" in source

    def test_a_loaded_replay_hides_the_empty_state_over_the_canvas(self) -> None:
        # The id rule sets display:flex, which outranks the browser's default
        # [hidden] rule unless the page states the contract explicitly.
        assert "#empty-note[hidden] { display: none; }" in read("viewer.html")

    def test_the_event_ticker_reflows_below_the_map_on_a_narrow_screen(self) -> None:
        source = read("viewer.html")
        assert "@media (max-width: 40rem)" in source
        assert "#layout { flex-direction: column; }" in source
        assert "inline-size: 100%; block-size: 9rem; flex: 0 0 9rem;" in source

    def test_the_door_replay_reads_the_unfiltered_bundle(self) -> None:
        # The filter shapes what is *drawn*; it must not shape what is
        # *replayed*. Door open/closed state is seeded from the bundle's own
        # feature list, so that lookup stays on bundle.map — pointing it at
        # the filtered document would couple every future hidden kind to the
        # correctness of the door timeline.
        assert "(bundle.map.features || [])" in read("viewer.html")

    def test_the_shared_renderer_still_draws_spawn_hints(self) -> None:
        # The editor is where you place them, so the capability stays put.
        # This is what makes the filter above the viewer's *policy* rather
        # than a feature deleted from both pages at once.
        assert 'feature.kind === "spawn"' in read("renderer.js")
        assert '<option value="spawn">' in read("editor.html")


class TestEditorGroundControls:
    # Presence only, behaviour never — the text-only boundary in the module
    # docstring applies here as everywhere else in this file.

    def test_the_editor_offers_the_height_tool_exactly_once(self) -> None:
        assert read("editor.html").count('data-tool="height"') == 1

    def test_the_editor_carries_the_height_feet_input(self) -> None:
        # Exactly once: byId() silently answers with the first of a duplicated
        # id, so a copy-paste double would break the wiring while a bare
        # presence check stayed green.
        assert read("editor.html").count('id="height-feet"') == 1

    def test_the_editor_carries_the_heights_toggle_and_datum_field(self) -> None:
        assert read("editor.html").count('id="btn-heights"') == 1
        assert read("editor.html").count('id="elevation-default"') == 1

    def test_the_editor_carries_the_level_switcher_exactly_once(self) -> None:
        assert read("editor.html").count('id="level-select"') == 1

    def test_the_editor_carries_environment_authoring_controls_exactly_once(self) -> None:
        source = read("editor.html")
        for element_id in (
            "ambient-light",
            "feature-config",
            "feature-sight-levels",
            "feature-light-bright",
            "feature-light-dim",
            "feature-light-color",
        ):
            assert source.count(f'id="{element_id}"') == 1
        assert source.count('<option value="opening">') == 1
        assert source.count('<option value="light">') == 1

    def test_the_editor_lifecycle_carries_the_storeys(self) -> None:
        # The layer-left-unwired failure, checked the only way this file can:
        # every place that snapshots or replaces the document has to name
        # `levels`, or an undo would delete the floors above the ground.
        source = read("editor.html")
        assert "elevation: payload.elevation, levels: payload.levels" in source
        assert "doc.levels = previous.levels" in source

    def test_the_editor_resize_walks_every_plane(self) -> None:
        # A frame change is document-wide; resizing the ground alone would
        # leave the storeys mislocated over it.
        source = read("editor.html")
        assert "[doc].concat(doc.levels" in source
        assert "planes.forEach(" in source

    def test_the_editor_resize_translates_a_fixtures_overlay_cells(self) -> None:
        # The same failure one layer deeper. A fixture's `affects` cells carry
        # coordinates, but they sit *inside* the feature record, so the
        # Object.assign that moves `at` copies them verbatim — leaving a flood
        # mislocated by exactly the anchor offset, and only on a resized map.
        # Anchored to the offset arithmetic itself, not to the variables it
        # writes into: `moved.affects = groups` stays true however wrong the
        # translation is, and a sign flip is the specific regression this
        # commit exists to fix.
        source = read("editor.html")
        assert "var cx = cell[0] + offX, cy = cell[1] + offY;" in source
        assert "moved.affects = groups" in source
        # The crop's other half: a group emptied by it is dropped rather than
        # left as an empty list the validator would refuse.
        assert "delete moved.affects" in source

    def test_the_editor_resize_refuses_to_orphan_a_prerequisite(self) -> None:
        # Dropping a fixture another one requires leaves a document that no
        # longer parses, so the *save* would fail naming a missing
        # prerequisite rather than the resize that removed it.
        assert "requires it; move or remove it first" in read("editor.html")

    def test_the_editor_resize_refuses_before_it_touches_the_document(self) -> None:
        # Order is the whole guarantee: the refusal has to be decided before
        # snapshot() and the mutation loop, or a refused resize still leaves
        # the document changed. A position compare is as much as the text-only
        # boundary allows, and it does pin the one thing that matters.
        source = read("editor.html")
        handler = source.index('byId("dlg-resize-go")')
        assert source.index("if (blocked) {", handler) < source.index("snapshot();", handler)

    def test_the_editor_resize_guards_every_shape_it_reads_from_a_file(self) -> None:
        # These run after snapshot() and after earlier planes were rewritten,
        # so a throw here leaves a half-resized document — and the Download
        # button writes it out without the server ever seeing it.
        source = read("editor.html")
        assert 'if (!group || typeof group !== "object") { return; }' in source
        assert "!Number.isFinite(cell[0]) || !Number.isFinite(cell[1])" in source
        assert "Array.isArray(f.requires) ? f.requires : []" in source

    def test_the_renderer_is_never_handed_a_storey_it_must_understand(self) -> None:
        # The renderer stays pure: it is given the active plane's tiles and
        # features under the document's grid and legend, which is the shape it
        # already draws, so it never learns that levels exist.
        source = read("editor.html")
        assert "R.render(ctx, renderable(), view," in source
        assert "levels" not in read("renderer.js")

    def test_the_renderer_knows_the_labels_overlay_channel(self) -> None:
        # Anchored to the overlay access, not the bare word, which could
        # survive in a comment after the channel itself was renamed away.
        assert "overlays.labels" in read("renderer.js")

    def test_the_renderer_defines_the_shared_culling_helper(self) -> None:
        # The editor's overlay builder culls to the viewport with the same
        # helper the renderer draws by — both sides of the shared surface.
        assert "visibleBounds" in read("renderer.js")
        assert "R.visibleBounds(" in read("editor.html")

    def test_the_renderer_knows_the_edges_overlay_channel(self) -> None:
        # The cell-boundary stroke channel the relief overlay draws its
        # climb and slope steps through. Anchored to the overlay access for
        # the same reason as the labels channel above.
        assert "overlays.edges" in read("renderer.js")

    def test_the_renderer_knows_the_terrain_override_channel(self) -> None:
        # What a fixture does to the ground reaches the canvas as one generic
        # per-square channel. Anchored on the overlay access, per the labels
        # and edges channels above.
        assert "overlays.terrainOverrides" in read("renderer.js")

    def test_an_override_is_applied_before_the_colour_and_the_texture(self) -> None:
        # Load-bearing placement: `kind` is read again for the hatch and notch
        # branches, so an override taken after the fill would recolour a
        # flooded square while leaving it hatched as difficult terrain.
        source = read("renderer.js")
        override = source.index("overlays.terrainOverrides")
        assert override < source.index("ctx.fillStyle = terrainColor(")
        assert override < source.index('if (kind === "difficult")')

    def test_the_override_derivation_is_exported_once_for_both_pages(self) -> None:
        # The editor and the viewer both need "which squares does this
        # document's fixtures decide, given which stand open". Server-side that
        # derivation is MapFeature.claims(), which names drift as its risk; a
        # copy per page would make three.
        source = read("renderer.js")
        assert "function terrainOverridesFor(doc, states)" in source
        assert "terrainOverridesFor: terrainOverridesFor" in source

    def test_the_override_channel_stays_generic(self) -> None:
        # Same rule the edge channel is held to: the renderer is handed squares
        # and kinds, never fixtures. It must not learn what `affects` is, or
        # the viewer's synthesized mapless plane stops being a document it can
        # draw. The derivation helper may know; the drawing loop may not.
        source = read("renderer.js")
        loop = source.index("for (var cy = y0; cy < y1; cy++)")
        assert "affects" not in source[loop:]

    def test_the_edge_channel_stays_generic(self) -> None:
        # The channel is a stroke on a named side of a cell, nothing more:
        # the renderer must not learn what elevation is, or the viewer's
        # synthesized mapless plane — which has no height layer at all —
        # would stop being a document the renderer can draw.
        assert "elevation" not in read("renderer.js")

    def test_the_editor_carries_the_relief_legend_and_values_toggle(self) -> None:
        # Exactly once apiece: byId() answers with the first of a duplicated
        # id, so a copy-paste double would break the wiring silently.
        assert read("editor.html").count('id="elev-legend"') == 1
        assert read("editor.html").count('id="btn-heights-values"') == 1

    def test_the_editor_shows_the_running_version_persistently(self) -> None:
        # Exactly once for the byId() reason above. Anchored to the slot and
        # to the write into it, because a slot nothing fills is a blank
        # corner of the footer rather than a visible failure — and the
        # version was previously announced only in a status line that the
        # next message overwrote.
        source = read("editor.html")
        assert source.count('id="version-note"') == 1
        assert 'byId("version-note").textContent' in source

    def test_the_version_shown_comes_from_the_serving_engine(self) -> None:
        # Read off the injected launch config, not a literal in the page: the
        # page is a static asset that no release step rewrites, so a hardcoded
        # version would be wrong the moment it shipped.
        source = read("editor.html")
        assert "CONFIG.version" in source
        assert not re.search(r'version-note"\)\.textContent\s*=\s*"[0-9]', source)

    def test_the_relief_overlay_draws_the_movement_thresholds(self) -> None:
        # The step edges are keyed to what the engine charges for the step,
        # not to arbitrary prettiness: over 5 feet is climbed, 2 feet and up
        # is a slope and so Difficult Terrain. A refactor that loses these
        # bounds turns a rules-bearing overlay back into decoration.
        source = read("editor.html")
        assert "CLIMB_FEET = 5" in source
        assert "SLOPE_FEET = 2" in source


class TestEditorStackedStoreys:
    # The onion-skin view: the storeys either side of the one being edited,
    # printed through it. Presence and wiring as text only, as everywhere here.

    def test_the_editor_carries_the_stack_toggle_and_its_key(self) -> None:
        # Exactly once apiece: byId() answers with the first of a duplicated
        # id, so a copy-paste double would break the wiring silently.
        source = read("editor.html")
        assert source.count('id="btn-stack"') == 1
        assert source.count('id="stack-legend"') == 1

    def test_the_stack_overlay_is_built_and_handed_to_the_generic_channels(self) -> None:
        # Built in the page and fed through marks/labels, like the relief
        # overlay: the renderer is never taught what a storey is.
        source = read("editor.html")
        assert "function buildStackOverlay(marks, labels)" in source
        assert "buildStackOverlay(marks, labels);" in source

    def test_the_ghosts_resolve_terrain_color_through_the_renderer(self) -> None:
        # Reused, not re-tabulated. A second fallback color table in the page
        # would drift from the renderer's the first time either moved, and a
        # document palette resolved by only one of them would draw a kind two
        # different colors on the same canvas. Anchored to the ghost's own
        # call, not the bare name: the legend swatches already call it, so
        # `R.terrainColor(` alone stays green against a ghost that computes
        # its ink some other way.
        assert (
            "R.terrainColor(kind, context.dark, context.styles, doc.palette)"
            in read("editor.html")
        )

    def test_the_ghosts_are_built_after_the_relief_they_must_sit_over(self) -> None:
        # The relief overlay washes every non-flat square at alpha 1, so a
        # ghost pushed before it is a ghost painted out. Order in the marks
        # list is draw order on the canvas.
        source = read("editor.html")
        assert source.index("buildReliefOverlay(marks, edges, labels);") < source.index(
            "buildStackOverlay(marks, labels);"
        )

    def test_the_stack_states_how_far_it_reaches_and_names_what_it_drops(self) -> None:
        # Beyond a couple of storeys the ghosts are mud, so the view is capped
        # — and a capped view that stayed silent would read as "this is every
        # floor there is". The key says which storeys it left out.
        source = read("editor.html")
        assert "STACK_REACH = 2" in source
        assert "not drawn" in source

    def test_a_ghosted_storey_is_never_the_edited_one(self) -> None:
        # The whole feature is overlay: the plane handed to the renderer, and
        # so the plane every tool paints and every hit test picks, is still the
        # active one alone.
        source = read("editor.html")
        assert "tiles: plane().tiles, features: plane().features" in source
        assert "R.render(ctx, renderable(), view," in source


class TestViewerFixtureOverlay:
    # What a fixture did to the ground, drawn under the replay. Text only, per
    # the module docstring — and that boundary is why these three were also
    # driven under node, where the seeding bug below was reproduced first.

    def test_the_viewer_seeds_every_fixture_not_only_the_doors(self) -> None:
        # The bug this class was written for: the seed loop gated on
        # `kind === "door"`, so a sluice never entered featureStates at all
        # and a bundle whose live open-list disagreed with the document
        # silently drew the document. `state` is what makes a feature one the
        # fight owns — map_document.py picks fixtures by exactly that test —
        # so the page must pick them by it too. Anchored on the guard itself:
        # the door gate must be gone *and* the state gate present, because
        # dropping the gate entirely would seed spawns and stairways as
        # fixtures and pass a check for either half alone.
        source = read("viewer.html")
        seeding = source[source.index("function initialState") : source.index("function fold")]
        assert 'feature.kind === "door"' not in seeding
        assert "feature.state === undefined || feature.state === null" in seeding

    def test_the_viewer_hands_the_frame_its_terrain_overrides(self) -> None:
        # Anchored on the overlay key *and* what fills it: `terrainOverrides`
        # alone would stay green against a page that passed an empty object,
        # which is exactly what a fixture-blind viewer looks like.
        assert "terrainOverrides: state.terrainOverrides" in read("viewer.html")

    def test_the_viewer_derives_overrides_through_the_shared_helper(self) -> None:
        # The renderer exports the derivation so that the question "which
        # squares does this document's fixtures decide" has one answer, not
        # one per page. A private copy here would make three with
        # MapFeature.claims(), which is the drift its docstring names.
        source = read("viewer.html")
        assert "R.terrainOverridesFor(mapDoc, " in source
        assert "function terrainOverridesFor" not in source

    def test_the_overlay_is_derived_per_state_change_not_per_frame(self) -> None:
        # stateAt() rebuilds from zero on any non-incremental scrub, so a
        # scrub drag replays the whole log — and redraw() also runs on a
        # bare window resize. Deriving inside the frame would pay for the
        # walk on both. It hangs off the state instead, so the only place it
        # is computed is the place the state changes.
        source = read("viewer.html")
        frame = source[source.index("function redraw") : source.index("function drawTicks")]
        assert "R.terrainOverridesFor(" not in frame


class TestTerrainColors:
    # Presence and precedence as text, never a drawn pixel — the boundary in
    # the module docstring holds here too.

    def test_the_renderer_resolves_a_kind_against_the_documents_palette(self) -> None:
        # Anchored to the lookup rather than the word, which could survive in a
        # comment after the argument itself was dropped.
        assert "palette[kind]" in read("renderer.js")

    def test_an_authored_color_outranks_the_pages_theme(self) -> None:
        # The pages define --terrain-* for all thirteen bundled kinds, so a
        # palette consulted after the custom property would never color one.
        renderer = read("renderer.js")
        assert renderer.index("palette[kind]") < renderer.index("getPropertyValue(")

    def test_the_renderer_draws_tiles_with_the_documents_palette(self) -> None:
        assert "doc.palette" in read("renderer.js")

    def test_the_editor_carries_colors_through_the_document_plumbing(self) -> None:
        # contentOf feeds undo, the dirty check and the save digest; a layer
        # missing from it is one every unrelated edit silently discards.
        editor = read("editor.html")
        assert "palette: payload.palette" in editor
        assert "doc.palette = previous.palette" in editor

    def test_the_editor_offers_a_color_control_per_legend_row(self) -> None:
        editor = read("editor.html")
        assert '.type = "color"' in editor
        assert "legend-clear" in editor

    def test_the_renderer_normalises_a_color_for_the_picker(self) -> None:
        # <input type="color"> takes #rrggbb only, and the computed color may
        # be an hsl() from the hash fallback or a CSS variable.
        assert "asHex" in read("renderer.js")
        assert "R.asHex(" in read("editor.html")


class TestSessionRestore:
    # A refresh must not lose the open map. Presence and ordering as text —
    # nothing here reloads a page, per the module docstring's boundary.

    def test_the_editor_names_a_versioned_storage_key_exactly_once(self) -> None:
        # Versioned so a payload written by an older editor is dropped rather
        # than half-understood; once, because two keys would mean two answers
        # to "what is open".
        assert read("editor.html").count('"fivee-editor-open-map-v1"') == 1

    def test_every_storage_access_sits_behind_a_guard(self) -> None:
        # The page also runs from file://, where a browser may refuse storage
        # outright, and the serverless mode has to keep working. So storage is
        # reached only through the two helpers, and in each one the `try` opens
        # *before* the access — counting two try blocks in the region would not
        # say that, and a bare access ahead of the guard throws on boot and
        # takes the whole editor with it.
        # Counted with the dot, which is what an access looks like — the bare
        # word also appears in the prose explaining why the guard is there.
        source = read("editor.html")
        accessors = source[
            source.index("function readSession") : source.index("function rememberSession")
        ]
        assert source.count("sessionStorage.") == accessors.count("sessionStorage.")
        for helper in ("function readSession", "function writeSession"):
            body = source[source.index(helper) :]
            body = body[: body.index("\n  }\n")]
            assert body.index("try {") < body.index("sessionStorage."), helper

    def test_the_editor_restores_before_the_ping_reports_status(self) -> None:
        # /ping answers asynchronously and calls setStatus, so the restore has
        # to run first *and* the handler has to stand down when it did —
        # either half alone leaves the "reopened" line silently overwritten.
        # Anchored on the call site, not on `restoreSession()` alone: that
        # substring also matches the function's own declaration, which always
        # precedes /ping, so the comparison could never have failed.
        source = read("editor.html")
        call = "var restored = restoreSession();"
        assert call in source
        assert source.index(call) < source.index('request("GET", "/ping")')
        assert "response.json.ok && !restored" in source

    def test_the_editor_persists_the_open_map_when_the_page_goes_away(self) -> None:
        # pagehide fires on reload, which is the case this feature exists for.
        assert 'addEventListener("pagehide"' in read("editor.html")

    def test_the_stored_state_carries_what_a_later_save_needs(self) -> None:
        # Without the id the save has no target; without the etag it either
        # overwrites another tab's work or 409s on a map nobody touched.
        assert "id: mapId, etag: etag" in read("editor.html")

    def test_a_restored_document_keeps_its_own_baseline(self) -> None:
        # baseline drives the dirty check that stamps provenance.edited, so a
        # restore that recomputed it would call the restored edits pristine.
        assert "source.baseline" in read("editor.html")


class TestLandingPage:
    """The root page: a signpost to the other two and to the contract.

    Its whole job is to be derived rather than written. The operation list is
    fetched from ``GET /api/v1/operations`` — the same table ``routes.py``
    dispatches from — so the assertion that matters here is the *absence* of a
    hand-written one: a page that spells the operations into its own markup
    would be a second source of truth, green on the day it shipped and wrong by
    the next route added.
    """

    def test_the_landing_page_links_to_each_other_page_exactly_once(self) -> None:
        source = read("home.html")
        assert source.count('href="/editor"') == 1
        assert source.count('href="/viewer"') == 1

    def test_the_landing_page_does_not_spell_out_the_operation_list(self) -> None:
        # Named operations in the markup are the failure this page is designed
        # to avoid; the index is rendered from what the server answered.
        #
        # Checked against the route table rather than a handful of names typed
        # here: a sample of three would keep passing while the page hardcoded
        # the other thirty-six, and would stop meaning anything the moment one
        # of the three was renamed.
        source = read("home.html")
        named = sorted(route.operation for route in api_routes() if route.operation in source)
        assert not named, f"home.html names operations in its own markup: {named}"
        assert 'id="operations"' in source

    def test_the_landing_page_reaches_the_network_only_behind_the_config_gate(self) -> None:
        # The same shape the viewer is held to: one fetch call, reachable only
        # when the server injected a config. Opened from disk it renders its
        # links and says so, rather than throwing at an undefined token.
        source = read("home.html")
        assert source.count("window.fetch(") == 1
        assert "if (CONFIG) { loadOperations(); }" in source

    def test_the_landing_page_sends_the_launch_token(self) -> None:
        # Every /api request needs it; a page that fetched without it would
        # render an empty index and look like an engine with no operations.
        assert '"X-Fivee-Editor-Token": CONFIG.token' in read("home.html")

    def test_the_landing_page_has_no_embedded_data_slot(self) -> None:
        # The slot belongs to the viewer's contract with the replay export;
        # a second copy would make "replace it exactly once" ambiguous.
        assert 'id="embedded-data"' not in read("home.html")


class TestOfflineGuarantee:
    @pytest.mark.parametrize("asset", ASSETS)
    def test_no_asset_references_an_external_url(self, asset: str) -> None:
        found = _EXTERNAL.search(read(asset))
        assert found is None, f"{asset} reaches off-origin: {found.group(0)!r}"

    @pytest.mark.parametrize("page", PAGES)
    def test_every_src_and_href_is_local(self, page: str) -> None:
        for value in _REFERENCE.findall(read(page)):
            assert (
                value in _ALLOWED_REFERENCES
                or value.startswith("#")
                or value.startswith("data:")
            ), f"{page} references {value!r}"

    @pytest.mark.parametrize("asset", ASSETS)
    def test_no_asset_imports_a_font(self, asset: str) -> None:
        text = read(asset).lower()
        assert "@font-face" not in text
        assert "@import" not in text


class TestEditorFixturePreview:
    # The editor's half of "see the fixtures": a lens over what is drawn, and
    # an inspector that stops hiding what a fixture does. Text and ordering
    # only, per the module docstring — the behaviour these assertions cannot
    # reach was driven under node instead, which is where "the toggle changes
    # the picture and not the document" is actually proven.

    def test_the_editor_carries_the_preview_toggle_and_its_list_once_each(self) -> None:
        # Exactly once apiece: byId() answers with the first of a duplicated
        # id, so a copy-paste double would wire the toggle to the wrong node
        # while a bare presence check stayed green.
        source = read("editor.html")
        assert source.count('id="btn-preview"') == 1
        assert source.count('id="fixture-list"') == 1

    def test_the_preview_derives_its_squares_through_the_shared_helper(self) -> None:
        # renderer.js exports terrainOverridesFor precisely so neither page
        # re-derives "which square shows what given which fixtures stand
        # open". Server-side that derivation is MapFeature.claims(), whose
        # docstring names drift as the risk; a copy here would make three.
        assert "R.terrainOverridesFor(" in read("editor.html")

    def test_the_preview_is_derived_from_the_storey_being_drawn(self) -> None:
        # renderable() is the active plane; `doc` is the ground. Handing the
        # helper the document would recolour the storey being edited with the
        # ground floor's fixtures — and an override is keyed "x,y" with no
        # level in it, so nothing downstream could catch the mix-up.
        assert "R.terrainOverridesFor(renderable(), previewOpen)" in read("editor.html")

    def test_the_preview_reaches_the_canvas_through_the_live_state_channels(self) -> None:
        # Both halves of a fixture, through the two channels the renderer
        # already carries a fight's live state on: the ground through
        # terrainOverrides, the door glyph through featureStates. The page
        # teaches the renderer nothing new to preview a fixture.
        source = read("editor.html")
        assert "overlays.terrainOverrides = " in source
        assert "overlays.featureStates = previewOpen" in source

    def test_the_preview_channels_are_handed_down_only_while_it_is_on(self) -> None:
        # An override map passed unconditionally costs the tile loop a lookup
        # per cell forever — the fast path renderer.js hoists exists for the
        # documents that hand down none — and a featureStates map left in
        # place would keep flipping door glyphs after the lens was switched
        # off. Anchored on the order, which is what "only while on" means.
        source = read("editor.html")
        assert source.index("if (previewOn) {") < source.index("overlays.terrainOverrides = ")

    def test_the_preview_never_reaches_the_document(self) -> None:
        # The whole promise of the control. contentOf feeds undo, the dirty
        # check and the save, so a preview that leaked into it would stamp
        # provenance.edited on a map nobody edited.
        source = read("editor.html")
        body = source[source.index("function contentOf(") : source.index("function snapshot(")]
        assert "previewOpen" not in body
        assert "previewOn" not in body

    def test_the_preview_toggle_takes_no_undo_snapshot(self) -> None:
        # Every control that changes the document calls snapshot() first; this
        # one must not, because there is nothing to undo — and a snapshot here
        # would push a no-op entry that swallows a real undo.
        source = read("editor.html")
        handler = source.index('byId("btn-preview").addEventListener')
        assert "snapshot()" not in source[handler : source.index("});", handler)]

    def test_the_preview_is_reset_when_a_document_is_opened(self) -> None:
        # The layer checklist's third entry, and the first page state to need
        # it: a preview left set across an open would draw the new map through
        # the old map's fixtures, whose ids may even collide with this one's.
        # Both halves — the toggle and the per-fixture choices.
        source = read("editor.html")
        body = source[
            source.index("function loadDocument(") : source.index("function renderProvenance(")
        ]
        assert "previewOn = false;" in body
        assert "previewOpen = Object.create(null);" in body

    def test_a_restored_session_resets_the_preview_through_the_same_door(self) -> None:
        # Session restore does not get its own reset because it does not get
        # its own load: it hands the stored payload to loadDocument like every
        # other opener, so the reset above covers it. A restore that built the
        # document itself would need the checklist walked a second time.
        source = read("editor.html")
        body = source[
            source.index("function restoreSession(") : source.index('addEventListener("pagehide"')
        ]
        assert "loadDocument(state.doc, {" in body

    def test_the_preview_stands_down_on_a_map_with_no_fixtures(self) -> None:
        # Exactly as the Level control is disabled on a map with no storeys.
        # Both halves, because either alone is a live-looking control over
        # nothing: the toggle is disabled, *and* a lens left on across a load
        # or a level switch that brought no fixtures is switched off rather
        # than left claiming to show something.
        source = read("editor.html")
        body = source[source.index("function renderFixtureList(") :]
        body = body[: body.index("\n  }\n")]
        assert 'var button = byId("btn-preview");' in body
        assert "button.disabled = !fixtures.length;" in body
        assert "if (!fixtures.length) { previewOn = false; }" in body

    def test_a_fixture_is_any_feature_carrying_a_state(self) -> None:
        # The document's own definition of what a fight can operate, and not a
        # list of kinds: a sluice, a lever and a spike are fixtures, a spawn
        # hint and a drawn stairway are not. A kind check here would preview
        # doors and nothing else.
        source = read("editor.html")
        body = source[source.index("function fixturesOnPlane(") :]
        body = body[: body.index("\n  }\n")]
        assert "f.state !== undefined" in body
        assert "kind" not in body

    def test_the_fixture_list_follows_the_storey_being_edited(self) -> None:
        # The preview is derived from the active plane, so its list has to be
        # too: a level switch that left the ground's fixtures listed would
        # offer checkboxes for squares this storey has never heard of.
        source = read("editor.html")
        handler = source.index('byId("level-select").addEventListener')
        assert "renderFixtureList();" in source[handler : source.index("});", handler)]

    def test_the_fixture_list_is_rebuilt_wherever_a_fixture_appears(self) -> None:
        # The door tool creates fixtures and the delete button removes them,
        # so a list built only at load would go stale mid-session — offering a
        # checkbox for a fixture that is gone, and none for the one just
        # placed. Neither handler can lean on redraw() to rebuild it: redraw
        # runs on every pointer move, and rebuilding the panel there would
        # tear the checkboxes out from under the pointer thirty times a second.
        source = read("editor.html")
        for handler in ("function cycleDoor(", 'byId("btn-delete-feature").addEventListener'):
            start = source.index(handler)
            assert "renderFixtureList();" in source[start : source.index("\n  }", start)], handler

    def test_the_inspector_shows_the_seven_keys_that_make_a_fixture(self) -> None:
        # A state alone left a loaded sluice indistinguishable from a bare door:
        # the block stopped there, so nothing on the page said
        # what operating it would do. Anchored on the rendered label of each
        # line, not the bare word — every one of these names also appears in
        # the resize code and in the prose around it.
        source = read("editor.html")
        info = source[
            source.index("function renderFeatureInfo(") : source.index(
                'byId("btn-delete-feature").addEventListener'
            )
        ]
        for label in (
            '"terrain: "',
            '"elevation: "',
            '"affects: "',
            '"requires: "',
            '"trigger: "',
            '"costs_action: "',
            '"check: "',
        ):
            assert label in info, label

    def test_the_inspector_counts_an_overlay_rather_than_listing_it(self) -> None:
        # A fixture may govern a whole room. The panel is 230px wide and this
        # block is meant to be read at a glance, so `affects` reports how many
        # groups and how many squares — never the squares themselves.
        source = read("editor.html")
        info = source[
            source.index("function renderFeatureInfo(") : source.index(
                'byId("btn-delete-feature").addEventListener'
            )
        ]
        assert '" group(s), "' in info
        assert '" square(s)"' in info

    def test_the_inspector_guards_the_shapes_a_hand_written_file_may_carry(self) -> None:
        # It runs on every selection click, and a throw here leaves the panel
        # showing the previously selected feature while the click looks like
        # it simply missed. A hand-opened file may carry `affects: "nope"`.
        source = read("editor.html")
        info = source[
            source.index("function renderFeatureInfo(") : source.index(
                'byId("btn-delete-feature").addEventListener'
            )
        ]
        assert "Array.isArray(feature.affects)" in info
        assert "Array.isArray(feature.requires)" in info

    def test_selected_doors_offer_orientation_hinge_swing_and_link_controls(self) -> None:
        source = read("editor.html")
        for control in (
            "door-config",
            "door-orientation",
            "door-hinge",
            "door-swing",
            "door-linked",
        ):
            assert source.count(f'id="{control}"') == 1, control

        info = source[
            source.index("function renderFeatureInfo(") : source.index(
                'byId("btn-delete-feature").addEventListener'
            )
        ]
        assert 'feature.kind === "door"' in info
        assert "renderDoorControls" in info


class TestFacingAndCompass:
    """One direction vocabulary, three carriers, and one glyph for all of them.

    Text and source ordering only, per the module docstring. Whether a chevron
    actually points north is a behaviour claim and belongs to
    ``scripts/check-editor-behaviour.mjs`` — but the *first* test in this class
    is what makes that harness's direction cases mean anything at all, so it
    lives here rather than there.
    """

    FACINGS = (
        "north",
        "northeast",
        "east",
        "southeast",
        "south",
        "southwest",
        "west",
        "northwest",
    )

    def test_the_facing_glyph_is_drawn_in_absolute_coordinates(self) -> None:
        # Load-bearing for a check this file cannot make. The behaviour harness
        # records moveTo/lineTo arguments exactly as passed, so a chevron drawn
        # under a translate/rotate pair would record identical coordinates for
        # all eight facings — every "it points the right way" case there would
        # pass against a renderer that ignored the facing entirely. The one
        # transform this file permits is the devicePixelRatio setTransform in
        # resizeCanvas, which is why it is counted rather than forbidden.
        source = read("renderer.js")
        assert "ctx.rotate(" not in source
        assert "ctx.translate(" not in source
        assert source.count("setTransform(") == 1

    def test_the_renderer_spells_grid_north_as_minus_y(self) -> None:
        # The convention the map format has always assumed and never written
        # down: a horizontal door swings north or south, meaning -y and +y. A
        # table that spelled it the other way would silently mirror every
        # chevron and every rose against the doors already on disk.
        source = read("renderer.js")
        assert '"north": [0, -1]' in source
        assert '"southeast": [1, 1]' in source

    def test_one_chevron_serves_all_three_carriers(self) -> None:
        # A creature, a map feature, and the map itself. Three glyphs would be
        # three chances to disagree about what "northeast" looks like. Counted,
        # so a fourth call site has to be an argued-for change rather than a
        # quiet one: the definition plus exactly three uses.
        source = read("renderer.js")
        assert source.count("function drawChevron(") == 1
        assert source.count("drawChevron(") == 4
        assert "token.facing" in source
        assert "feature.facing" in source
        assert "doc.compass" in source

    @pytest.mark.parametrize("page", PAGES)
    def test_only_the_renderer_turns_a_name_into_a_direction(self, page: str) -> None:
        # The pages offer the names; the renderer alone knows which way each
        # one points. An offset table in a page is how a drawn chevron and a
        # drawn rose come to disagree about where northeast is.
        assert "FACING_UNITS" not in read(page)

    def test_the_editor_carries_the_facing_and_compass_controls_once_each(self) -> None:
        # Exactly once apiece: byId() answers with the first of a duplicated
        # id, so a copy-paste double would wire a control to the wrong node
        # while a bare presence check stayed green.
        source = read("editor.html")
        for element_id in ("facing-config", "feature-facing", "map-compass"):
            assert source.count(f'id="{element_id}"') == 1, element_id

    def test_the_editor_names_the_eight_facings_exactly_once(self) -> None:
        # Two controls offer them — a feature's facing and the document's
        # compass — and a second list is how the two come to disagree about
        # what the format accepts.
        source = read("editor.html")
        assert source.count("var FACING_NAMES = [") == 1
        for name in self.FACINGS:
            assert f'"{name}"' in source, name

    def test_the_editor_offers_a_door_no_facing_at_all(self) -> None:
        # The format refuses `facing` on a door, because a door already says
        # where it points three ways over. A control that offered it a fourth
        # answer would author documents the server rejects on save.
        assert (
            'renderFacingControls(feature.kind === "door" ? null : feature)'
            in read("editor.html")
        )

    def test_the_facing_panel_states_its_own_hidden_rule(self) -> None:
        # The id rule sets display:grid, which outranks the browser's default
        # [hidden] rule unless the page states the contract explicitly — so
        # without this line the panel stays on screen for the doors it must
        # never be offered to, and the guard above would be invisible. The same
        # failure #empty-note[hidden] exists for in the viewer.
        assert (
            "#door-config[hidden], #facing-config[hidden] { display: none; }"
            in read("editor.html")
        )

    def test_the_inspector_reports_a_features_facing(self) -> None:
        # Anchored on the rendered label, like the fixture keys above: the bare
        # word appears in the control wiring and in the prose around it.
        source = read("editor.html")
        info = source[
            source.index("function renderFeatureInfo(") : source.index(
                'byId("btn-delete-feature").addEventListener'
            )
        ]
        assert '"facing: "' in info

    def test_the_compass_is_a_property_of_the_document_not_a_storey(self) -> None:
        # A building does not have one true north per floor. Anchored on the
        # read handed to the renderer, because `plane().compass` would draw
        # correctly on the ground and silently stop upstairs.
        source = read("editor.html")
        assert "compass: doc.compass" in source
        assert "plane().compass" not in source

    def test_the_editor_carries_the_compass_through_the_document_plumbing(self) -> None:
        # contentOf feeds undo, the dirty check and the save digest; a layer
        # missing from it is one every unrelated edit silently discards.
        source = read("editor.html")
        assert "compass: payload.compass" in source
        assert "doc.compass = previous.compass" in source

    def test_north_is_written_by_being_left_out(self) -> None:
        # The format's canonical shape, which the server writes: a map whose
        # true north is the grid's carries no compass at all, so a page that
        # wrote the key back would make every such document differ from the one
        # the server would have saved — and stamp provenance.edited for it.
        assert 'if (chosen === "north") { delete doc.compass; }' in read("editor.html")

    def test_the_viewer_carries_facing_through_both_token_build_sites(self) -> None:
        # The viewer builds its token model twice, in two functions, from two
        # different payloads — the bundle's initial state and whatever
        # authoritative state a scrub lands on. A pass-through added to one of
        # them looks entirely correct until a replay is scrubbed.
        source = read("viewer.html")
        initial = source[
            source.index("function initialState") : source.index("function applyAuthoritative")
        ]
        authoritative = source[
            source.index("function applyAuthoritative") : source.index("function checkpointAt")
        ]
        frame = source[source.index("function renderFrame") : source.index("var framePending")]
        assert "facing: creature.facing" in initial
        assert "token.facing = creature.facing" in authoritative
        assert "facing: token.facing" in frame

    def test_the_viewer_derives_no_facing_of_its_own(self) -> None:
        # The engine turns a creature to face its move's final step, and that
        # derived value rides the state the bundle already serialises. A page
        # that recomputed it from a move event's origin and destination would
        # be a second implementation of that rule, free to disagree with the
        # fight it is replaying.
        source = read("viewer.html")
        fold = source[source.index("function fold(") : source.index("function stateAt")]
        assert "facing" not in fold


class TestAnimatedEventFamilies:
    """The declaration of what the viewer animates, held against the viewer.

    ``tests/fixtures/animated-event-families.json`` is read by three checks, and
    each closes one direction. ``check-editor-behaviour.mjs`` proves the page
    really animates every family listed, so the file cannot over-claim.
    ``test_replay_sample.py`` requires the showcase to put every listed family on
    screen, so the demo cannot drift. Neither notices a family the *viewer*
    gains, which is what this class is for: a kind added to the dispatch and not
    to the declaration fails here, and goes on failing until the sample shows it.

    This is a source property — which of two functions names which strings — so
    it belongs on the text side of the division this module's docstring draws.
    """

    def viewer_dispatch(self) -> set[str]:
        """Every event kind the viewer's two per-kind dispatches name.

        ``fold`` applies an event to token state and ``eventMarks`` paints the
        pulse; a kind either page-visible way counts. Sliced by function rather
        than searched whole-file, because ``event.kind`` is also read for the
        ticker and the camera, where naming a kind implies no animation at all.
        """
        source = read("viewer.html")
        fold = source[source.index("function fold(") : source.index("function stateAt")]
        marks = source[
            source.index("function eventMarks()") : source.index("function renderFrame")
        ]
        flash = source[source.index("var FLASH_KINDS") :].split(";", 1)[0]
        return (
            set(re.findall(r'kind === "([a-z_]+)"', fold + marks))
            | set(re.findall(r"([a-z_]+):\s*1", flash))
        )

    def test_the_declaration_names_every_kind_the_viewer_dispatches_on(self) -> None:
        declared = {family["kind"] for family in ANIMATED_FAMILIES}

        undeclared = sorted(self.viewer_dispatch() - declared)

        assert undeclared == [], (
            f"viewer.html animates {undeclared}, which the shared declaration does "
            "not list — add it there, then to the showcase that has to demonstrate it"
        )

    def test_the_declaration_invents_no_kind_the_viewer_ignores(self) -> None:
        declared = {family["kind"] for family in ANIMATED_FAMILIES}

        phantom = sorted(declared - self.viewer_dispatch())

        assert phantom == [], (
            f"the shared declaration lists {phantom}, which viewer.html dispatches "
            "on nowhere; the showcase would be demonstrating nothing for it"
        )

    def test_every_declared_family_spells_its_keys_exactly(self) -> None:
        # The node loop reads each observable behind its own `if`, so a typo'd
        # key is not an error there — it is an assertion that silently does not
        # run, while `pulse` alone keeps the observability test below happy. A
        # closed key set is what makes a misspelling fail instead of shrink the
        # coverage. Widening it is a deliberate act: add the reader in
        # check-editor-behaviour.mjs in the same change.
        allowed = {"name", "kind", "pulse", "changes", "becomes", "panel", "event", "initial"}
        required = {"name", "kind", "event"}

        unknown = sorted(
            f"{family.get('kind', '?')}.{key}"
            for family in ANIMATED_FAMILIES
            for key in family.keys() - allowed
        )
        incomplete = sorted(
            f"{family.get('kind', '?')} is missing {sorted(required - family.keys())}"
            for family in ANIMATED_FAMILIES
            if required - family.keys()
        )

        assert unknown == [], (
            f"{unknown} is not a key any consumer reads — a misspelt observable "
            "reads as absent and quietly asserts nothing"
        )
        assert incomplete == [], incomplete

    def test_every_declared_family_states_how_it_is_observable(self) -> None:
        # A family with no observable is one the node harness would loop over
        # and assert nothing about — the shape of hole this declaration exists
        # to close, so it is refused here rather than passing quietly there.
        silent = sorted(
            family["kind"]
            for family in ANIMATED_FAMILIES
            if not family.get("pulse")
            and not {"changes", "becomes", "panel"} & family.keys()
        )

        assert silent == [], (
            f"{silent} declare no pulse and no observable change, so nothing can "
            "check the viewer does anything at all for them"
        )


class TestPagesParse:
    @pytest.mark.parametrize("page", PAGES)
    def test_the_page_opens_with_a_doctype(self, page: str) -> None:
        assert read(page).lstrip().lower().startswith("<!doctype html")

    @pytest.mark.parametrize("page", PAGES)
    def test_script_tags_are_balanced(self, page: str) -> None:
        text = read(page).lower()
        assert text.count("<script") == text.count("</script")

    @pytest.mark.parametrize("page", PAGES)
    def test_basic_structural_tags_are_balanced(self, page: str) -> None:
        text = read(page).lower()
        for tag in ("html", "head", "body", "style", "header", "footer", "canvas"):
            assert len(re.findall(rf"<{tag}[\s>]", text)) == text.count(f"</{tag}>"), tag
