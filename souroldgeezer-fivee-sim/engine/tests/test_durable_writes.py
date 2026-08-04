"""Durable writes under genuine concurrency: separate processes, not just threads.

The suite already had a twelve-thread append case, and it passed throughout the
window in which two *processes* could destroy a journal outright — a
``threading.RLock`` answers every thread and no second process. Every case here
therefore spends a real interpreter, and the one that matters asserts the
property a corrupted chain takes away: the fight can still be read back.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fivee_sim.service import durable, encounter_journal
from fivee_sim.service.errors import RequestError, StaleWriteError

from . import api
from .conftest import mapless_fight

#: Each child re-reads and retries, so a refusal costs progress but never the file.
_APPENDER = """
import os, sys
os.environ["FIVEE_SIM_ENCOUNTERS"] = {root!r}
from fivee_sim.service import durable, encounter_journal
from fivee_sim.service.errors import StaleWriteError
tag, rounds = sys.argv[1], int(sys.argv[2])
wins = refusals = 0
for index in range(rounds):
    records, _ = encounter_journal.read({encounter_id!r})
    head = str(records[-1]["sha256"]) if records else ""
    try:
        encounter_journal.append(
            {encounter_id!r},
            {{"kind": "note", "who": tag, "index": index}},
            expected_head={expected!s},
        )
        wins += 1
    except durable.StaleWriteError:
        refusals += 1
print(f"{{wins}} {{refusals}}")
"""


def _run_appender(
    root: Path, encounter_id: str, tag: str, rounds: int, *, guarded: bool
) -> subprocess.Popen[str]:
    code = _APPENDER.format(
        root=str(root),
        encounter_id=encounter_id,
        expected="head" if guarded else "None",
    )
    return subprocess.Popen(
        [sys.executable, "-c", code, tag, str(rounds)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _tally(process: subprocess.Popen[str]) -> tuple[int, int]:
    stdout, stderr = process.communicate(timeout=120)
    assert process.returncode == 0, f"appender died: {stderr}"
    wins, refusals = stdout.split()
    return int(wins), int(refusals)


def test_two_processes_appending_one_journal_leave_a_readable_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline defect: this left a journal no server could ever recover."""
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=201)
    rounds = 25

    workers = [
        _run_appender(root, encounter_id, tag, rounds, guarded=False)
        for tag in ("agent-a", "agent-b")
    ]
    tallies = [_tally(worker) for worker in workers]

    # No precondition means no refusal: the lock serialises them, it does not judge.
    assert [refusals for _wins, refusals in tallies] == [0, 0]
    records, warning = encounter_journal.read(encounter_id)
    assert warning is None
    assert len(records) == 1 + sum(wins for wins, _refusals in tallies)


def test_a_stale_expected_head_is_refused_rather_than_forking_the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=202)
    records, _warning = encounter_journal.read(encounter_id)
    stale_head = str(records[-1]["sha256"])

    encounter_journal.append(encounter_id, {"kind": "note", "index": 0})

    with pytest.raises(durable.StaleWriteError, match="has advanced"):
        encounter_journal.append(
            encounter_id, {"kind": "note", "index": 1}, expected_head=stale_head
        )
    verified, warning = encounter_journal.read(encounter_id)
    assert warning is None
    assert len(verified) == 2


def test_two_guarded_processes_still_leave_a_verifiable_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integrity under the guarded path; the refusal itself is pinned above.

    This case deliberately does not assert that a refusal happened. Whether the
    two children collide is a timing question, so asserting it would be flaky,
    and hand-mutation confirms the gap is real: disabling the expected_head
    check leaves this test green while the two tests either side of it fail.
    Its job is the invariant that survives whatever the interleaving was.
    """
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=203)
    rounds = 25

    workers = [
        _run_appender(root, encounter_id, tag, rounds, guarded=True)
        for tag in ("agent-a", "agent-b")
    ]
    tallies = [_tally(worker) for worker in workers]

    records, warning = encounter_journal.read(encounter_id)
    assert warning is None
    # Every acknowledged append landed exactly once, and read() re-verifies the
    # whole chain — the property a second writer used to destroy outright.
    assert len(records) == 1 + sum(wins for wins, _refusals in tallies)
    assert sum(wins + refusals for wins, refusals in tallies) == 2 * rounds


def test_a_second_process_acting_on_a_live_encounter_is_refused_not_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daily-note question, answered at the tool boundary."""
    root = tmp_path / "journal"
    monkeypatch.setenv("FIVEE_SIM_ENCOUNTERS", str(root))
    encounter_id = mapless_fight(seed=204)

    # A second server advances the same fight and leaves the journal ahead of ours.
    elsewhere = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import os\nos.environ['FIVEE_SIM_ENCOUNTERS'] = {str(root)!r}\n"
            "from fivee_sim.service import encounters, sessions\n"
            f"encounters.advance(sessions.EngineState(), {encounter_id!r})\n",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert elsewhere.returncode == 0, elsewhere.stderr

    # A ``RequestError`` rather than a ``StaleWriteError``: the encounter
    # session drops its stale copy and re-raises, because the caller's fix here
    # is to read the fight again rather than to re-send the same write.
    with pytest.raises(RequestError, match="has advanced"):
        api.encounter_advance(encounter_id)


def test_atomic_write_never_exposes_a_half_written_file(tmp_path: Path) -> None:
    target = tmp_path / "map.json"
    durable.atomic_write(target, json.dumps({"generation": 0}))

    writer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import json\nfrom fivee_sim.service import durable\n"
            "big = json.dumps({'generation': 1, 'filler': 'x' * 2_000_000})\n"
            f"durable.atomic_write({str(target)!r}, big)\n",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    seen = set()
    while writer.poll() is None:
        try:
            seen.add(json.loads(target.read_text(encoding="utf-8"))["generation"])
        except (json.JSONDecodeError, KeyError, OSError):  # pragma: no cover
            pytest.fail("a reader observed a partially written file")
    _stdout, stderr = writer.communicate(timeout=120)
    assert writer.returncode == 0, stderr
    # Without this the case is vacuous: an empty ``seen`` satisfies the subset
    # check, so a write that outran the reader would pass having proved nothing.
    assert seen, "the reader never observed the file; the race went unexercised"
    assert seen <= {0, 1}


def test_map_save_refuses_a_version_someone_else_replaced(tmp_path: Path) -> None:
    """The fourth writer: two agents, or an agent and the open editor."""
    generated = api.map_generate(
        "dungeon", {"width": 20, "height": 16}, seed=11, save_as="shared"
    )
    read_at = str(generated["saved"]["sha256"])

    # A cell that is actually floor, so the edit genuinely moves the file on:
    # painting an already-wall square writes identical bytes and nothing is stale.
    tiles = generated["document"]["tiles"]
    floor = next((row.index("."), y) for y, row in enumerate(tiles) if "." in row)
    edited = api.map_edit(
        "shared", [{"op": "paint", "cells": [list(floor)], "terrain": "wall"}]
    )
    assert edited["sha256"] != read_at

    with pytest.raises(StaleWriteError, match="has advanced"):
        api.map_save("shared", generated["document"], expected_sha256=read_at)


def test_a_symlink_planted_at_the_scratch_path_cannot_divert_a_write(
    tmp_path: Path,
) -> None:
    """A constructed scratch name was guessable and ``open('w')`` follows links.

    With ``.{name}.{pid}.tmp`` this diverted the write into the link's target
    and then renamed the symlink over the map, so every later write landed on
    the victim too — a directory-write primitive escalating to arbitrary-file
    write. ``mkstemp`` closes it: unpredictable name, ``O_CREAT|O_EXCL``.
    """
    target = tmp_path / "map.json"
    durable.atomic_write(target, '{"generation": 0}')
    victim = tmp_path / "victim.txt"
    victim.write_text("do not clobber", encoding="utf-8")
    import os as _os

    (tmp_path / f".{target.name}.{_os.getpid()}.tmp").symlink_to(victim)

    durable.atomic_write(target, '{"generation": 1}')

    assert victim.read_text(encoding="utf-8") == "do not clobber"
    assert not target.is_symlink()
    assert json.loads(target.read_text(encoding="utf-8"))["generation"] == 1


def test_a_replaced_file_keeps_the_permissions_it_had(tmp_path: Path) -> None:
    """``mkstemp`` creates 0600; a replace must not silently tighten a file."""
    import stat as _stat

    target = tmp_path / "map.json"
    durable.atomic_write(target, '{"generation": 0}')
    target.chmod(0o644)

    durable.atomic_write(target, '{"generation": 1}')

    assert _stat.S_IMODE(target.stat().st_mode) == 0o644


def test_a_map_edit_guards_itself_without_the_caller_asking(tmp_path: Path) -> None:
    """Opt-in protection protects nobody, so the read-modify-write guards itself.

    An edit reads the file, changes the document, and writes it back. Without a
    precondition, two agents editing one map each get told they succeeded and
    the slower one's edit is gone. The version this call read *is* the
    precondition it writes under, so the loser is refused instead — here the
    caller supplies a version deliberately, which is the same check reached from
    the outside.
    """
    generated = api.map_generate(
        "dungeon", {"width": 20, "height": 16}, seed=13, save_as="shared"
    )
    tiles = generated["document"]["tiles"]
    floor = next((row.index("."), y) for y, row in enumerate(tiles) if "." in row)
    read_at = str(generated["saved"]["sha256"])

    # Someone else edits the same file first.
    api.map_edit("shared", [{"op": "paint", "cells": [list(floor)], "terrain": "wall"}])

    with pytest.raises(StaleWriteError, match="has advanced"):
        api.map_edit(
            "shared",
            [{"op": "set_name", "name": "mine"}],
            expected_sha256=read_at,
        )


class TestLockLifecycle:
    """The platform call is one line; the lifecycle around it is the contract.

    ``fcntl.flock`` runs here and ``msvcrt.locking`` never does, so the Windows
    branch has no direct coverage and this repo has no Windows CI to give it
    any. What *is* platform-independent is the order — acquire, body, release,
    close — and that a body which raises still releases. Substituting the two
    primitives pins that much on every platform.
    """

    def test_the_lock_is_released_and_closed_after_a_normal_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(durable, "_acquire", lambda fd: calls.append("acquire"))
        monkeypatch.setattr(durable, "_release", lambda fd: calls.append("release"))

        with durable.file_lock(tmp_path / "map.json"):
            calls.append("body")

        assert calls == ["acquire", "body", "release"]

    def test_a_raising_body_still_releases_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(durable, "_acquire", lambda fd: calls.append("acquire"))
        monkeypatch.setattr(durable, "_release", lambda fd: calls.append("release"))

        with pytest.raises(RuntimeError, match="boom"):
            with durable.file_lock(tmp_path / "map.json"):
                raise RuntimeError("boom")

        assert calls == ["acquire", "release"], "a failed body must not strand the lock"

    def test_a_failing_release_still_closes_the_descriptor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        leaked: list[int] = []

        def remember(descriptor: int) -> None:
            leaked.append(descriptor)
            raise OSError("release failed")

        monkeypatch.setattr(durable, "_acquire", lambda fd: None)
        monkeypatch.setattr(durable, "_release", remember)

        with pytest.raises(OSError, match="release failed"):
            with durable.file_lock(tmp_path / "map.json"):
                pass

        with pytest.raises(OSError):
            os.fstat(leaked[0])
