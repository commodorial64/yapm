# installed db
import fcntl
import json
from pathlib import Path
from typing import Dict

from . import paths as paths

# fcntl-based lock
class _FileLock:
    def __init__(self, path):
        self._path = path
        self._fd = None

    def __enter__(self):
        self._fd = open(self._path, 'a')
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except OSError:
            self._fd.close()
            self._fd = None
        return self

    def __exit__(self, *args):
        if self._fd:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            self._fd.close()

def _write_db(db: Dict):
    with open(paths.DB_FILE, "w") as f:
        json.dump(db, f, indent=4)

def load_db() -> Dict:
    try:
        paths.DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not paths.DB_FILE.exists():
            paths.DB_FILE.write_text("{}")
        with _FileLock(paths.DB_FILE):
            with open(paths.DB_FILE) as f:
                db = json.load(f)
            migrated = False
            new_db = {}
            for k, v in db.items():
                if "/" in k:
                    author, name = k.split("/", 1)
                    v.setdefault("metadata", {})["author"] = author
                    new_db[name] = v
                    migrated = True
                else:
                    new_db[k] = v
            if migrated:
                _write_db(new_db)
        return new_db
    except (OSError, PermissionError):
        # read-only fallback (e.g. neofetch counting)
        try:
            with open(paths.DB_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

def save_db(db: Dict):
    with _FileLock(paths.DB_FILE):
        _write_db(db)
