"""Stage a pinned CI archive; never execute it or replace published directories."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tarfile
import tempfile


def stage(archive, digest, destination):
    archive, destination = Path(archive), Path(destination)
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid digest")
    meta = archive.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
        raise ValueError("Archive must be a regular unlinked file")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError("Archive digest mismatch")
    parent = destination.parent
    if parent.resolve() != parent or not parent.is_dir():
        raise ValueError("Canonical staging parent required")
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError("Invalid existing staging directory")
        # The upstream installer re-verifies every source byte against its pin.
        return False
    temporary = Path(tempfile.mkdtemp(prefix=".extract-", dir=parent))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            if len(members) > 250000 or sum(m.size for m in members) > 4 * 1024**3:
                raise ValueError("Archive exceeds bounds")
            for member in members:
                if member.name.startswith('/'):
                    raise ValueError("Absolute entry refused")
                if member.islnk() or not (member.isdir() or member.isfile() or member.issym()):
                    raise ValueError("Unsupported archive entry")
                if member.issym() and os.path.isabs(member.linkname):
                    raise ValueError("Absolute link refused")
            tar.extractall(temporary, members=members, filter="data")
        temporary.chmod(0o755)
        temporary.rename(destination)
        return True
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


if __name__ == "__main__":
    try:
        print(json.dumps({"changed": stage(*sys.argv[1:])}))
    except Exception:
        sys.exit("Pinned archive staging failed; retained published files were not replaced")
