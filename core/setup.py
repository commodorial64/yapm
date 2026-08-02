# setup + init
import json
import os
import shutil
import sys
from pathlib import Path

from .completions import _detect_shell, _detect_user, _install_completions_bash, _install_completions_fish, _install_completions_zsh, _install_fetch_count, _user_home
from .config import save_config
from .db import save_db
from .paths import BIN_DIR, CACHE_DIR, CONFIG_DIR, CONFIG_FILE, CURRENT_VERSION, DATA_DIR, DB_FILE, DEFAULT_CONFIG, INSTALL_DIR, LOCK_FILE, _yapm_entry

SETUP_MARKER = DATA_DIR / ".setup_done"

def check_deps():
    missing = []
    for cmd in ("zstd", "tar"):
        if not shutil.which(cmd):
            missing.append(cmd)
    if missing:
        print(f"Error: required tools not found: {', '.join(missing)}")
        print(f"  Install them with: sudo pacman -S {' '.join(missing)}")
        sys.exit(1)

def ensure_dirs():
    check_deps()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    # check for another running yapm instance
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
            os.kill(old_pid, 0)
            print(f"Error: another yapm instance is running (pid {old_pid}).")
            print("  If this is a mistake, remove /var/lib/yapm/yapm.lock.")
            sys.exit(1)
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # stale lock or can't check — safe to proceed
    LOCK_FILE.write_text(str(os.getpid()))

    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
    else:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
        if config.get("version", 0) < CURRENT_VERSION:
            config["version"] = CURRENT_VERSION
            if "mirrors" not in config:
                config["mirrors"] = DEFAULT_CONFIG["mirrors"]
            save_config(config)

    if not DB_FILE.exists():
        save_db({})

def setup():
    marker_user = _user_home() / ".yapm" / ".setup_done"
    if SETUP_MARKER.exists() or marker_user.exists():
        print("yapm is already set up. To re-run: rm ~/.yapm/.setup_done && yapm setup")
        return

    shell = _detect_shell()
    yapm_path = _yapm_entry()

    print(f"Setting up yapm for {shell}...")

    if shell == "bash":
        _install_completions_bash(yapm_path)
    elif shell == "zsh":
        _install_completions_zsh(yapm_path)
    elif shell == "fish":
        _install_completions_fish(yapm_path)

    _install_fetch_count(shell)

    # rebuild zsh completion cache
    if shell == "zsh":
        user_home = _user_home()
        for f in user_home.glob(".zcompdump*"):
            try:
                f.unlink()
            except OSError:
                pass

    # make installed.json world-readable so neofetch/fastfetch can count packages
    try:
        DB_FILE.chmod(0o644)
        DB_FILE.parent.chmod(0o755)
    except (OSError, PermissionError):
        pass

    for marker in (SETUP_MARKER, marker_user):
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"shell": shell, "user": _detect_user()}))
        except (OSError, PermissionError):
            pass

    print(f"\nSetup complete. Open a new shell or run 'source ~/.{shell}rc' to activate.")
