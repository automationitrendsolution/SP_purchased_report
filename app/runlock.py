"""Single-instance execution lock.

An OS-level lock on a dedicated file (``flock`` on Linux, ``msvcrt.locking``
on Windows for local development): the kernel releases it if the process is
killed, so a crashed run cannot wedge the schedule the way a stale PID file
would.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from .exceptions import LockError
from .logging_config import get_logger

logger = get_logger("lock")

if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _try_lock(handle: IO[str]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: IO[str]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def run_lock(lock_file: Path, *, enabled: bool = True) -> Iterator[None]:
    """Hold an exclusive lock for the duration of the block.

    Raises:
        LockError: If another process already holds the lock.
    """
    if not enabled:
        yield
        return

    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_file.open("a+")
    try:
        try:
            _try_lock(handle)
        except OSError as exc:
            raise LockError(
                f"Another run is in progress (lock {lock_file}). Refusing to start a second run."
            ) from exc

        # The PID is informational only; on Windows the locked byte cannot be
        # rewritten, so it is written past it.
        try:
            handle.seek(0, os.SEEK_END)
            handle.write(f"{os.getpid()}\n")
            handle.flush()
        except OSError:  # pragma: no cover - cosmetic
            pass
        logger.debug("Acquired run lock %s", lock_file)
        try:
            yield
        finally:
            _unlock(handle)
            logger.debug("Released run lock %s", lock_file)
    finally:
        handle.close()
