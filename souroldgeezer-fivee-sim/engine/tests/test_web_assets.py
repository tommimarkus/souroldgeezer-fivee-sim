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
        assert 'id="height-feet"' in read("editor.html")

    def test_the_editor_carries_the_heights_toggle_and_datum_field(self) -> None:
        assert 'id="btn-heights"' in read("editor.html")
        assert 'id="elevation-default"' in read("editor.html")

    def test_the_renderer_knows_the_labels_overlay_channel(self) -> None:
        assert "labels" in read("renderer.js")

    def test_the_renderer_defines_the_shared_culling_helper(self) -> None:
        # The editor's overlay builder culls to the viewport with the same
        # helper the renderer draws by, so the name is shared surface.
        assert "visibleBounds" in read("renderer.js")


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
