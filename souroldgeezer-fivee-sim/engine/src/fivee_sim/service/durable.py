"""Durable writes that survive a second writer.

Two adapters reach this layer and several processes reach those adapters: every
MCP server on a host resolves the same encounter and map roots, and the editor
is a threading HTTP server whose handlers race each other. A ``threading.RLock``
answers none of that, so the primitives here are the ones that do.

Two guarantees, deliberately separate:

* **Integrity** is unconditional. :func:`file_lock` makes a read-modify-write
  atomic across processes, and :func:`atomic_write` publishes a whole file by
  rename so a concurrent reader sees the old bytes or the new ones, never a
  prefix.
* **Ownership** is opt-in. A caller that passes the version it read gets
  :class:`StaleWriteError` when someone else got there first. Callers that pass
  nothing still cannot corrupt the file — they simply take turns.

The split matters because serialising writes is not the same as agreeing about
them: two servers holding divergent copies of one encounter would produce an
interleaved journal that replays as neither fight. The lock protects the bytes;
only the precondition protects the meaning.

``flock`` is the lock of choice because the kernel releases it when the holder
dies, which removes the dead-owner reclamation a lease would have to hand-roll.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import StaleWriteError

if sys.platform == "win32":  # pragma: no cover - POSIX is the developed platform
    import msvcrt
else:
    import fcntl

__all__ = ["StaleWriteError", "atomic_write", "file_lock", "guarded_write", "lock_path"]


def lock_path(path: Path) -> Path:
    """The sibling lock guarding ``path``.

    A sibling rather than the file itself: the guarded file may not exist yet,
    and a journal's own directory listing must not grow a phantom entry — the
    ``.lock`` suffix keeps it outside the ``enc-*.jsonl`` glob.
    """
    return path.parent / f"{path.name}.lock"


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive cross-process lock covering ``path``.

    Blocking on purpose. The critical sections here are a read and a write of
    one small file, so a waiter is better than a caller that has to invent a
    retry policy.
    """
    guard = lock_path(path)
    guard.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(guard, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform == "win32":  # pragma: no cover - POSIX is the developed platform
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if sys.platform == "win32":  # pragma: no cover
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def fsync_directory(path: Path) -> None:
    """Persist a directory entry, so a rename or creation survives a crash."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: str | Path, text: str) -> None:
    """Publish ``text`` at ``path`` by rename, never by truncate-and-write."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Same directory, so the rename stays within one filesystem and is atomic.
    scratch = target.parent / f".{target.name}.{os.getpid()}.tmp"
    try:
        with scratch.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch, target)
        fsync_directory(target.parent)
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise


def guarded_write(
    path: str | Path,
    render: Callable[[], str],
    *,
    expected: str | None,
    current: Callable[[], str | None],
    subject: str,
) -> None:
    """Write unless someone else has written since ``expected`` was read.

    ``current`` is a callable, not a value, and that is the whole point: read it
    before the lock and the comparison races the write it is meant to guard.
    The caller supplies it because only it knows how its own format is versioned
    — a map hashes its canonical text, a journal names its chain head.

    ``render`` is likewise deferred, so a format that derives the new bytes from
    the old ones (a chain head, a generation counter) reads them under the lock.
    ``expected`` of ``None`` skips the check and keeps integrity alone.
    """
    target = Path(path)
    with file_lock(target):
        if expected is not None:
            on_disk = current()
            if expected != on_disk:
                raise StaleWriteError(subject, expected=expected, current=on_disk)
        atomic_write(target, render())
