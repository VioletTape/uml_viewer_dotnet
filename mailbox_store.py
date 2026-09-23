"""Locked, atomic JSON mailbox updates shared by the HTTP server and worker."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile


@contextmanager
def mailbox(project_path, name):
    directory = Path(project_path) / ".uml-viewer"
    directory.mkdir(exist_ok=True)
    path = directory / name
    # Lock a separate file: atomic replacement changes the JSON file's inode.
    with (directory / (name + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            items = json.loads(path.read_text()) if path.exists() else []
            if not isinstance(items, list):
                raise ValueError(f"Invalid mailbox: {name} must contain a JSON list")
            original = json.dumps(items)
            yield items
            if json.dumps(items) != original:
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as f:
                        temporary = f.name
                        json.dump(items, f, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temporary, path)
                finally:
                    if temporary and os.path.exists(temporary):
                        os.unlink(temporary)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
