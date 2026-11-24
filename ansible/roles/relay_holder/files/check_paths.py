"""Reject redirected or shared-writable destination ancestry before IaC writes."""
from pathlib import Path
import stat
import sys


def check(uid, paths):
    for value in paths:
        path = Path(value)
        if not path.is_absolute() or str(path) != value:
            raise ValueError('Canonical path required')
        for parent in [path, *path.parents]:
            try:
                meta = parent.lstat()
            except FileNotFoundError:
                continue
            if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid not in (0, uid)
                    or meta.st_mode & 0o022 or parent.resolve() != parent):
                raise ValueError('Unsafe directory ancestry')


if __name__ == '__main__':
    try:
        check(int(sys.argv[1]), sys.argv[2:])
    except Exception:
        sys.exit('Holder destination ancestry is not trusted')
