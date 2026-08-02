# paths & constants
import os
import platform
import shutil
import sys
from pathlib import Path

APP_VERSION = "0.6.0"
CURRENT_VERSION = 1  # Config version

VIRTUAL_PROVIDERS = frozenset({"sh", "awk", "perl", "python", "ruby"})

_SYSTEM_ARCH = platform.machine()

_DEB_ARCH_MAP = {
    "x86_64": "amd64",
    "aarch64": "arm64",
    "armv7l": "armhf",
    "i686": "i386",
    "i386": "i386",
}

def _host_arch():
    return _SYSTEM_ARCH

def _deb_arch():
    return _DEB_ARCH_MAP.get(_SYSTEM_ARCH, _SYSTEM_ARCH)

# yapm always runs as root — all paths are system-wide
CONFIG_DIR  = Path("/etc/yapm")
CONFIG_FILE = CONFIG_DIR / "config.json"

DATA_DIR    = Path("/var/lib/yapm")
INSTALL_DIR = DATA_DIR / "packages"
DB_FILE     = DATA_DIR / "installed.json"

CACHE_DIR   = DATA_DIR / "cache"
INDEX_FILE  = CACHE_DIR / "index.json"
BIN_DIR     = Path("/usr/local/bin")
LIB_DIR     = Path("/usr/local/lib")
ROOT_DIR    = Path("/")

LOCK_FILE   = DATA_DIR / "yapm.lock"

def set_root_dir(root_str: str):
    global ROOT_DIR, INSTALL_DIR, DB_FILE, BIN_DIR
    ROOT_DIR = Path(root_str).resolve()
    if str(ROOT_DIR) == "/":
        return
    INSTALL_DIR = ROOT_DIR / "var/lib/yapm/packages"
    DB_FILE = ROOT_DIR / "var/lib/yapm/installed.json"
    BIN_DIR = ROOT_DIR / "usr/bin"

YAPM_CONF_SYSTEM = Path("/etc/yapm/yapm.conf")
YAPM_CONF_USER   = Path.home() / ".config" / "yapm" / "yapm.conf"

KNOWN_FLAGS = {
    "yapm.riot": False,
    "yapm.insroot": False,
    "yapm.hooks": False,
    "yapm.noconfirm": False,
    "yapm.verbose": False,
    "yapm.autoupdate": False,
    "yapm.paranoid": False,
    "yapm.dangerzone": False,
    "yapm.nativenationality": False,
    "yapm.fuckaround": False,
    "yapm.yapm": False,
}

DEFAULT_CONFIG = {
    "version": CURRENT_VERSION,
    "mirrors": [
        {"url": "https://yapm.pages.dev/", "priority": 0},
        {"url": "https://mirror.rackspace.com/archlinux/", "priority": 10},
        {"url": "https://deb.debian.org/debian/", "priority": 20},
        {"url": "https://archive.ubuntu.com/ubuntu/", "priority": 30}
    ]
}

# installed yapm binary, falling back to the repo entry script
def _yapm_entry() -> str:
    return shutil.which("yapm") or str(Path(__file__).resolve().parent.parent / "yapm.py")
