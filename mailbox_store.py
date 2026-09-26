"""Locked, atomic JSON mailbox updates shared by the HTTP server and worker."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False


@contextmanager
def mailbox(project_path, name, read_only=False):
    directory = Path(project_path) / ".uml-viewer"
    directory.mkdir(exist_ok=True)
    path = directory / name
    lock_file = directory / (name + ".lock")
    # Lock a separate file: atomic replacement changes the JSON file's inode.
    with lock_file.open("a") as lock:
        if HAS_FCNTL:
            flags = fcntl.LOCK_SH if read_only else fcntl.LOCK_EX
            fcntl.flock(lock, flags)
        try:
            if path.exists():
                raw = path.read_text(encoding="utf-8")
                items = json.loads(raw)
            else:
                raw = ""
                items = []

            if not isinstance(items, list):
                raise ValueError(f"Invalid mailbox: {name} must contain a JSON list")

            if read_only:
                yield items
                return

            yield items

            new_raw = json.dumps(items, indent=2)
            if new_raw != raw:
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as f:
                        temporary = f.name
                        f.write(new_raw)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temporary, path)
                finally:
                    if temporary and os.path.exists(temporary):
                        try:
                            os.unlink(temporary)
                        except OSError:
                            pass
        finally:
            if HAS_FCNTL:
                fcntl.flock(lock, fcntl.LOCK_UN)
