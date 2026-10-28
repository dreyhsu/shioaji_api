"""Nonblocking process leases using only the host platform's standard library.

Windows locks byte zero with msvcrt; Unix uses flock. Never unlink lock files:
all contenders must keep locking the same file. Closing the handle (including
process death) releases ownership. No OS-specific module is imported on import.
"""
from __future__ import annotations

import errno
from pathlib import Path
import sys


class LeaseUnavailable(RuntimeError):
    """Another process already owns this lease."""


class FileLease:
    def __init__(self, path, *, _platform=None):
        self.path = Path(path)
        self._platform = sys.platform if _platform is None else _platform
        self._stream = None

    def acquire(self):
        if self._stream is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if self._platform == "win32":
                import msvcrt
                # msvcrt locks from the current position, including beyond EOF.
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(error, "winerror", None) == 33:
                raise LeaseUnavailable(f"another process owns {self.path}") from error
            raise
        except BaseException:
            stream.close()
            raise
        self._stream = stream
        return self

    def close(self):
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            if self._platform == "win32":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.close()
