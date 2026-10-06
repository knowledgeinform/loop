"""Create SQLite privately and restrict its existing sidecars without reading data."""
import os
from pathlib import Path
import stat
import sys


def _restrict(path, *, create=False):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileNotFoundError:
        if create:
            raise
        return
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('SQLite storage must be a regular file')
        if info.st_nlink != 1:
            raise ValueError('SQLite storage must not have additional hard links')
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def secure_sqlite(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _restrict(path, create=True)
    for suffix in ('-wal', '-shm', '-journal'):
        _restrict(Path(str(path) + suffix))


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: secure_sqlite.py DATABASE_PATH')
    secure_sqlite(sys.argv[1])
