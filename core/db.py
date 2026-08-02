# installed db
import fcntl
import json
from pathlib import Path
from typing import Dict

from .paths import DB_FILE

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
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=4)

def load_db() -> Dict:
    try:
        DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not DB_FILE.exists():
            DB_FILE.write_text("{}")
        with _FileLock(DB_FILE):
            with open(DB_FILE) as f:
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
            with open(DB_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

def save_db(db: Dict):
    with _FileLock(DB_FILE):
        _write_db(db)
