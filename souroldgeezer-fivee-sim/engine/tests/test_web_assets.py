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
Every assertion here reads the assets as *text*. Nothing executes
``renderer.js``, opens a page, or draws a canvas, so no test in this repo covers
the behaviour of the three files a user actually looks at: a renderer that drew
nothing, a token injected into the wrong element, or a viewer unable to parse
its own embedded bundle would all ship green. That is a deliberate boundary —
these are localhost, single-user tools, and driving them would put a browser
toolchain into a Python repository — but it is a boundary, not coverage. If the
editor grows past a convenience, this file is where the argument for a real
harness starts.
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

import pytest

from fivee_sim.editor.http_server import CONFIG_MARKER

STATIC = Path(str(resources.files("fivee_sim.editor"))) / "static"
PAGES = ("editor.html", "viewer.html")
ASSETS = ("editor.html", "viewer.html", "renderer.js")

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
_ALLOWED_REFERENCES = frozenset({"/assets/renderer.js", "renderer.js"})


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

    @pytest.mark.parametrize("page", PAGES)
    def test_each_page_loads_the_shared_renderer_by_its_served_route(self, page: str) -> None:
        # replay_export(embed=True) inlines the renderer by replacing this
        # exact tag, so it must exist verbatim and exactly once.
        assert read(page).count(RENDERER_TAG) == 1

    def test_the_renderer_defines_its_single_namespace(self) -> None:
        assert "var FiveeRenderer" in read("renderer.js")


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
