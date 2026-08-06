"""The fourth storage kind: content-addressed, immutable, shared.

A journal is a working record, a document is edited under a precondition, an
export leaves the machine. A blob is none of those — it is a payload some
record wants to *name* rather than carry, and the name is a digest of the
payload itself. Everything below is a consequence of that one sentence, so the
tests are grouped by which consequence they are checking.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from fivee_sim.paths import BLOBS_ENV, blobs_root
from fivee_sim.service import blobs

PAYLOAD: dict[str, Any] = {
    "records": {"spells": {"Signal Flare": {"level": 1}}},
    "conditions": ["prone", "blinded"],
    "generation": 3,
}


@pytest.fixture(autouse=True)
def _blobs_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every blob this module writes lands under the test's own directory."""
    monkeypatch.setenv(BLOBS_ENV, str(tmp_path / "blobs"))


# --- the name is the content ------------------------------------------------
def test_a_payload_put_comes_back_out_under_the_name_it_was_given() -> None:
    reference = blobs.put(PAYLOAD)

    assert blobs.get(reference) == PAYLOAD


def test_the_reference_is_the_digest_of_the_bytes_on_disk() -> None:
    """Content addressing, checked rather than assumed.

    The whole design rests on the name being derivable from the payload: it is
    why publishing needs no lock, why freshness needs no stamp, and why two
    fights capturing identical content share one file. A reference that were
    merely *unique* would satisfy every other test here and none of that.
    """
    reference = blobs.put(PAYLOAD)

    written = (blobs_root() / f"{reference}.json").read_bytes()

    assert reference == hashlib.sha256(written).hexdigest()


def test_the_same_payload_from_two_callers_is_one_file() -> None:
    """One content snapshot serves every fight that captured it.

    Key order is deliberately different between the two calls: the digest is
    over the canonical rendering, not over whatever dict the caller happened to
    build, or a shared blob would depend on insertion order.
    """
    first = blobs.put(PAYLOAD)
    second = blobs.put(dict(reversed(list(PAYLOAD.items()))))

    assert first == second
    assert sorted(path.name for path in blobs_root().iterdir()) == [f"{first}.json"]


def test_two_different_payloads_are_two_files() -> None:
    first = blobs.put(PAYLOAD)
    second = blobs.put({**PAYLOAD, "generation": 4})

    assert first != second
    assert len(list(blobs_root().iterdir())) == 2


# --- what a caller may name -------------------------------------------------
def test_a_reference_outside_the_digest_grammar_is_refused_before_any_read(
    tmp_path: Path,
) -> None:
    """A blob id is a sha256 and nothing else, so traversal never gets a path.

    The same reasoning as ``common.ID_PATTERN``: an id outside the grammar
    cannot name a file we wrote, so it is refused rather than half-resolved.
    """
    planted = tmp_path / "secret.json"
    planted.write_text('{"stolen": true}', encoding="utf-8")

    with pytest.raises(blobs.BlobError, match=r"not a blob reference: '\.\./secret'"):
        blobs.get("../secret")


def test_a_reference_of_the_right_shape_that_names_nothing_says_so() -> None:
    absent = "0" * 64

    with pytest.raises(blobs.BlobError, match=f"no blob {absent!r}"):
        blobs.get(absent)


def test_a_blob_whose_bytes_no_longer_hash_to_its_name_is_refused() -> None:
    """The name is the content, so disagreement is detectable for free.

    A blob is the one artifact here with no hash chain and no version
    precondition over it, because it needs neither: it cannot legally change.
    Reading one back is therefore also the whole of checking it, and a swapped
    content snapshot is a named refusal rather than a fight quietly recovered
    under somebody else's rules.
    """
    reference = blobs.put(PAYLOAD)
    (blobs_root() / f"{reference}.json").write_text('{"switched": true}', encoding="utf-8")

    with pytest.raises(blobs.BlobError, match=f"blob {reference!r} does not match its name"):
        blobs.get(reference)


def test_a_blob_that_is_not_a_json_object_is_refused() -> None:
    reference = blobs.put(PAYLOAD)
    path = blobs_root() / f"{reference}.json"
    path.write_text("not json at all", encoding="utf-8")

    with pytest.raises(blobs.BlobError, match=f"blob {reference!r} is not valid JSON"):
        blobs.get(reference)


# --- immutability, and what follows from it ---------------------------------
def test_putting_a_payload_twice_leaves_the_first_file_untouched() -> None:
    """Freshness needs no stamp: the file's existence *is* the check.

    The launcher's ``src/<source-id>`` copy makes the same trade. Rewriting
    would be harmless and pointless — the bytes cannot differ — so the second
    caller pays a ``stat`` rather than an 11 KB write.
    """
    reference = blobs.put(PAYLOAD)
    path = blobs_root() / f"{reference}.json"
    before = path.stat().st_mtime_ns

    assert blobs.put(dict(PAYLOAD)) == reference
    assert path.stat().st_mtime_ns == before


def test_a_blob_is_published_whole_or_not_at_all() -> None:
    """``atomic_write`` publishes by rename, so no reader sees a prefix.

    Asserted through the temporary file it leaves nowhere: the directory holds
    the blob and nothing else once the call returns.
    """
    reference = blobs.put(PAYLOAD)

    assert [path.name for path in blobs_root().iterdir()] == [f"{reference}.json"]


def test_a_blob_is_valid_json_a_second_reader_can_parse() -> None:
    """It is a file on disk, not an internal encoding.

    ``encounter.list`` reports a journal path for a caller to read; the same
    kindness has to hold here, or a fight's captured content becomes opaque the
    moment it stops being carried inline.
    """
    reference = blobs.put(PAYLOAD)

    text = (blobs_root() / f"{reference}.json").read_text(encoding="utf-8")

    assert json.loads(text) == PAYLOAD
