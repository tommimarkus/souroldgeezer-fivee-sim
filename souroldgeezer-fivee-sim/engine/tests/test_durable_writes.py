"""Durable writes under genuine concurrency: separate processes, not just threads.

The suite already had a twelve-thread append case, and it passed throughout the
window in which two *processes* could destroy a journal outright — a
``threading.RLock`` answers every thread and no second process. Every case here
therefore spends a real interpreter, and the one that matters asserts the
property a corrupted chain takes away: the fight can still be read back.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from fivee_sim.mcp_server import server as api
from fivee_sim.service import durable, encounter_journal

from .conftest import mapless_fight

#: Each child re-reads and retries, so a refusal costs progress but never the file.
_APPENDER = """
import os, sys
os.environ["FIVEE_SIM_ENCOUNTERS"] = {root!r}
from fivee_sim.service import durable, encounter_journal
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
            "from fivee_sim.mcp_server import server as api\n"
            f"api.encounter_advance({encounter_id!r})\n",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert elsewhere.returncode == 0, elsewhere.stderr

    with pytest.raises(api.ToolError, match="has advanced"):
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


def test_map_save_refuses_a_version_someone_else_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fourth writer: two sessions, or a session and the open editor."""
    monkeypatch.setenv("FIVEE_SIM_MAPS", str(tmp_path / "maps"))
    generated = api.map_generate("dungeon", {"width": 20, "height": 16}, seed=11)
    map_id = str(generated["map_id"])
    target = str(tmp_path / "maps" / "shared.json")
    read_at = str(api.map_save(map_id, path=target)["sha256"])

    # A cell that is actually floor, so the edit genuinely moves the file on:
    # painting an already-wall square writes identical bytes and nothing is stale.
    document = api._map_session(map_id).document
    floor = next(
        (row.index("."), y) for y, row in enumerate(document.tiles) if "." in row
    )
    api.map_edit(map_id, [{"op": "paint", "cells": [list(floor)], "terrain": "wall"}])
    moved_on = str(api.map_save(map_id, path=target, overwrite=True)["sha256"])
    assert moved_on != read_at

    with pytest.raises(api.ToolError, match="has advanced"):
        api.map_save(map_id, path=target, overwrite=True, expected_sha256=read_at)


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
