"""Host-wide manager lock.

Serialises the short critical sections (quota check + port allocation,
nginx rewrites) across threads *and* processes (REST service, CLI, watcher).
Long-running work such as ``docker run`` or waiting for MCP Hub happens
outside the lock.
"""
from __future__ import annotations

import fcntl
import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from .config import settings


class LockTimeout(RuntimeError):
    pass


class _HostLock:
    def __init__(self) -> None:
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._fd: Optional[int] = None

    def acquire(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        if not self._thread_lock.acquire(timeout=timeout):
            raise LockTimeout("Timed out waiting for manager lock (thread)")
        try:
            if self._depth == 0:
                path = settings().lock_file
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            os.close(fd)
                            raise LockTimeout("Timed out waiting for manager lock (file)")
                        time.sleep(0.1)
                self._fd = fd
            self._depth += 1
        except BaseException:
            self._thread_lock.release()
            raise

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        self._thread_lock.release()


_lock = _HostLock()


@contextmanager
def host_lock(timeout: float = 120.0) -> Iterator[None]:
    _lock.acquire(timeout)
    try:
        yield
    finally:
        _lock.release()
