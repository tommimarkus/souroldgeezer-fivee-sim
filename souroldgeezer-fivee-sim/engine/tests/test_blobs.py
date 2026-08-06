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
import re
from pathlib import Path
from typing import Any

import pytest

from fivee_sim.paths import BLOBS_ENV, blobs_root
from fivee_sim.service import blobs, durable

PAYLOAD: dict[str, Any] = {
    "records": {"spells": {"Signal Flare": {"level": 1}}},
    "conditions": ["prone", "blinded"],
    "generation": 3,
}

# Every blob this module writes lands under the test's own directory, and no
# fixture here arranges that: ``conftest._isolate_server_state`` is autouse and
# already points ``FIVEE_SIM_BLOBS`` at ``tmp_path / "blobs"`` for the whole
# suite. A second fixture setting the same variable to the same value read like
# a local guarantee while being a copy of one, so it would go on agreeing after
# the shared one changed.


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

    The plant is not scenery, and the arithmetic is worth spelling out because
    it is the only thing that makes this a traversal test rather than a second
    grammar test. ``blob_path`` builds ``blobs_root() / f"{reference}.json"``,
    the suite's blobs root is ``tmp_path / "blobs"``, so ``../secret`` resolves
    to exactly ``tmp_path / "secret.json"`` — this file. Delete the grammar
    check and ``get`` reads it.
    """
    planted = tmp_path / "secret.json"
    planted.write_text('{"stolen": true}', encoding="utf-8")
    assert planted == (blobs_root() / "../secret.json").resolve(), (
        "the plant has to sit where '../secret' actually lands, or this test "
        "proves only that the grammar rejects a string"
    )

    with pytest.raises(blobs.BlobError, match=r"not a blob reference: '\.\./secret'"):
        blobs.get("../secret")


@pytest.mark.parametrize(
    "reference",
    [
        pytest.param("0" * 63, id="one-digit-short"),
        pytest.param("0" * 65, id="one-digit-long"),
        pytest.param("0" * 64 + ".json", id="caller-supplied-the-suffix"),
        pytest.param("A" * 64, id="uppercase-hex"),
        pytest.param("g" * 64, id="right-length-outside-hex"),
        pytest.param("", id="empty"),
    ],
)
def test_a_reference_off_the_grammars_boundary_is_refused(reference: str) -> None:
    """The grammar is length *and* alphabet, checked at both edges of each.

    One invalid case proves a check exists; it does not say where the check
    stops. ``[0-9a-f]{64}`` under ``fullmatch`` is four separable decisions —
    the alphabet, its case, the length, and anchoring at both ends — and the
    uppercase row is the one that matters most in practice, because a digest
    pasted from a tool that prints uppercase hex is the plausible mistake and
    naming a *different* file is the outcome to refuse.
    """
    with pytest.raises(blobs.BlobError, match=f"not a blob reference: {reference!r}"):
        blobs.get(reference)


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
    path = blobs_root() / f"{reference}.json"
    path.write_text('{"switched": true}', encoding="utf-8")

    with pytest.raises(blobs.BlobError, match=f"blob {reference!r} does not match its name"):
        blobs.get(reference)


def test_the_mismatch_refusal_names_the_file_and_the_repair() -> None:
    """A poisoned blob is the one refusal here nothing in the engine can heal.

    ``put`` declines to rewrite a name that already exists, blobs are never
    deleted, and no operation reaps one — so a caller holding the correct bytes
    still cannot replace a corrupted file, and every fight naming it stays
    unrecoverable. The digest alone does not locate it: it is a 64-character
    name under a root the caller may never have configured. Naming the path and
    the one action that fixes it is what turns a dead end into a chore.
    """
    reference = blobs.put(PAYLOAD)
    path = blobs_root() / f"{reference}.json"
    path.write_text('{"switched": true}', encoding="utf-8")

    with pytest.raises(blobs.BlobError, match=re.escape(str(path))) as refusal:
        blobs.get(reference)

    assert "remove" in str(refusal.value)
    # And the named repair actually is one, rather than advice that fails.
    path.unlink()
    assert blobs.put(PAYLOAD) == reference
    assert blobs.get(reference) == PAYLOAD


def test_a_blob_that_is_not_json_at_all_is_refused() -> None:
    reference = blobs.put(PAYLOAD)
    path = blobs_root() / f"{reference}.json"
    path.write_text("not json at all", encoding="utf-8")

    with pytest.raises(blobs.BlobError, match=f"blob {reference!r} is not valid JSON"):
        blobs.get(reference)


def test_a_blob_that_parses_but_is_not_an_object_is_refused() -> None:
    """Valid JSON and still not a payload — a separate branch from the one above.

    ``[1, 2, 3]`` parses, so the JSON refusal never fires; what catches it is the
    type check, and the caller's annotation says ``dict``. Without this case that
    check is unreached and ``get`` is typed on a promise nothing holds it to.
    """
    reference = blobs.put(PAYLOAD)
    path = blobs_root() / f"{reference}.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(blobs.BlobError, match=f"blob {reference!r} is not a JSON object"):
        blobs.get(reference)


def test_a_blob_that_is_not_valid_utf8_is_refused() -> None:
    """The read's own failure mode, named rather than raised past.

    ``read_text`` raises ``UnicodeDecodeError``, which is a ``ValueError`` but
    not a ``BlobError``, so a caller catching this module's error would miss it
    and the adapter would answer a bare 500 instead of a named refusal.
    """
    reference = blobs.put(PAYLOAD)
    path = blobs_root() / f"{reference}.json"
    path.write_bytes(b'{"records": "\xff\xfe"}')

    with pytest.raises(blobs.BlobError, match=f"cannot read blob {reference!r}"):
        blobs.get(reference)


def test_a_blob_too_large_to_be_one_is_refused_before_it_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is on the file, checked before the bytes are in memory.

    A blob holds a content snapshot or a map document, and the engine already
    refuses either at 4 MiB on its own read path — ``MAX_PACK_BYTES`` and
    ``MAX_MAP_BYTES``. Arriving by digest is not a reason to drop the bound:
    the module's whole posture is that it is reading bytes it did not write in
    this process, and its integrity check runs *after* the read, so an
    unbounded read is a hole upstream of the thing built to close it.

    Asserted through ``st_size`` rather than by writing 16 MiB, because the
    check is on the stat and a test that spends a real 16 MiB to prove it would
    be paid for on every run.
    """
    reference = blobs.put(PAYLOAD)
    path = blobs_root() / f"{reference}.json"
    monkeypatch.setattr(blobs, "MAX_BLOB_BYTES", path.stat().st_size - 1)

    with pytest.raises(blobs.BlobError, match=f"blob {reference!r} is .* bytes, over the"):
        blobs.get(reference)


def test_a_blob_nested_past_what_the_integrity_check_can_walk_is_refused() -> None:
    """Small file, unbounded recursion — the case a size cap does not cover.

    Five hundred levels is three kilobytes, and the site that gives way is not
    the parse: ``json.loads`` is C and survives twenty thousand. It is
    ``canonical_json``, pure Python, called by the integrity check itself — so
    the module's one defence against bytes it did not write is also the thing
    that blows the stack on them. ``RecursionError`` is not a ``ValueError``,
    so it left ``service/`` entirely and the adapter answered a bare 500.

    The depth is the *file's*, not this test's: the payload is built as text and
    parsed by C, so nothing here recurses in Python until ``get`` does.
    """
    reference = blobs.put(PAYLOAD)
    path = blobs_root() / f"{reference}.json"
    depth = 500
    path.write_text('{"a":' * depth + "1" + "}" * depth, encoding="utf-8")

    with pytest.raises(blobs.BlobError, match=f"blob {reference!r} is nested too deeply"):
        blobs.get(reference)


def test_a_payload_that_cannot_be_rendered_is_refused_as_a_blob_error() -> None:
    """``service/`` raises ``ValueError``-family errors, and that includes here.

    ``canonical_json`` raises ``TypeError`` on a value ``json`` cannot spell,
    which is not one — so an engine change that ever put an unserialisable
    value in a content snapshot would surface as a ``TypeError`` out of a
    module whose documented refusal is ``BlobError``. Not reachable from
    ``/api/v1`` today; ``put`` is public regardless.
    """
    with pytest.raises(blobs.BlobError, match="cannot render a blob"):
        blobs.put({"when": object()})


def test_a_blob_that_cannot_be_written_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write's own failure mode, named rather than raised past.

    A blobs root under a *regular file* cannot be created, so ``atomic_write``'s
    ``mkdir`` fails with an ``OSError``. Nothing else in the suite exercises
    that branch, and a refusal nobody has ever seen is a refusal nobody has
    checked the wording of.
    """
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")
    monkeypatch.setenv(BLOBS_ENV, str(blocked / "blobs"))

    with pytest.raises(blobs.BlobError, match="cannot write blob '[0-9a-f]{64}'"):
        blobs.put(PAYLOAD)


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


def test_publishing_a_blob_leaves_no_scratch_file_behind() -> None:
    """The directory holds the blob and nothing else once the call returns.

    This used to be named for atomicity and could not fail on the question: a
    ``put`` rewritten to a plain ``write_text`` — no rename, no scratch file,
    a reader free to see a prefix — leaves exactly this directory too. So it
    says what it checks. **Atomicity itself is proved by
    ``test_durable_writes.py``'s ``test_atomic_write_never_exposes_a_half_written_file``**,
    over ``atomic_write`` where it belongs, and the test below is what holds
    this module to using it.
    """
    reference = blobs.put(PAYLOAD)

    assert [path.name for path in blobs_root().iterdir()] == [f"{reference}.json"]


def test_a_blob_is_published_through_the_writer_that_owns_atomicity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seam assertion, deliberately, and the one the rename above cannot make.

    ``durable.py`` owns publishing-by-rename for every durable file the engine
    writes, and proves it once. What a caller of it owes is to *be* one — so
    what is checked here is the call, with the real writer still doing the work
    behind the spy. Swap ``atomic_write`` for ``path.write_text`` and every
    other test in this file still passes; this one does not.
    """
    calls: list[tuple[Path, str]] = []

    def spy(path: str | Path, text: str) -> None:
        calls.append((Path(path), text))
        durable.atomic_write(path, text)

    monkeypatch.setattr(blobs, "atomic_write", spy)

    reference = blobs.put(PAYLOAD)

    assert [path for path, _ in calls] == [blobs_root() / f"{reference}.json"]
    assert json.loads(calls[0][1]) == PAYLOAD


def test_a_blob_is_valid_json_a_second_reader_can_parse() -> None:
    """It is a file on disk, not an internal encoding.

    ``encounter.list`` reports a journal path for a caller to read; the same
    kindness has to hold here, or a fight's captured content becomes opaque the
    moment it stops being carried inline.
    """
    reference = blobs.put(PAYLOAD)

    text = (blobs_root() / f"{reference}.json").read_text(encoding="utf-8")

    assert json.loads(text) == PAYLOAD
