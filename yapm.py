#!/usr/bin/env python3

import argparse
import ast
import fcntl
import gzip
import io
import itertools
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import urllib.error
import zipfile
from pathlib import Path
from typing import List, Dict, Optional

VIRTUAL_PROVIDERS = frozenset({"sh", "awk", "perl", "python", "ruby"})

# ============================================================
# COLOR OUTPUT
# ============================================================

_COLOR_ENABLED = sys.stdout.isatty()

class Color:
    RESET   = "\033[0m"  if _COLOR_ENABLED else ""
    BOLD    = "\033[1m"   if _COLOR_ENABLED else ""
    RED     = "\033[31m"  if _COLOR_ENABLED else ""
    GREEN   = "\033[32m"  if _COLOR_ENABLED else ""
    YELLOW  = "\033[33m"  if _COLOR_ENABLED else ""
    BLUE    = "\033[34m"  if _COLOR_ENABLED else ""
    CYAN    = "\033[36m"  if _COLOR_ENABLED else ""
    DIM     = "\033[2m"   if _COLOR_ENABLED else ""


def _parse_ver(v: str):
    """Parse a version string into a tuple of (major, minor, patch, prerelease) for comparison."""
    v = v.strip()
    parts = []
    for p in v.split("."):
        parts.append(int(''.join(c for c in p if c.isdigit()) or '0'))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])

# ============================================================
# CONFIGURATION PATHS
# ============================================================

APP_VERSION = "0.3.1-alpha"
CURRENT_VERSION = 2  # Config version

# yapm always runs as root — all paths are system-wide
CONFIG_DIR  = Path("/etc/yapm")
CONFIG_FILE = CONFIG_DIR / "config.json"

DATA_DIR    = Path("/var/lib/yapm")
INSTALL_DIR = DATA_DIR / "packages"
DB_FILE     = DATA_DIR / "installed.json"

CACHE_DIR   = DATA_DIR / "cache"
INDEX_FILE  = CACHE_DIR / "index.json"
BIN_DIR     = Path("/usr/local/bin")
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
    "yapm.yapm": False,
}

DEFAULT_CONFIG = {
    "version": CURRENT_VERSION,
    "mirrors": [
        {"url": "https://archive.ubuntu.com/ubuntu/", "priority": 10},
        {"url": "https://deb.debian.org/debian/", "priority": 20},
        {"url": "https://mirror.rackspace.com/archlinux/", "priority": 30},
        {"url": "https://yapm.pages.dev/", "priority": 50}
    ]
}

def require_root():
    """Abort immediately if not running as root."""
    if os.getuid() != 0:
        print("Error: yapm must be run with sudo.")
        print("  Try: sudo yapm <command>")
        sys.exit(1)


def check_deps():
    """Check that required external tools are available."""
    missing = []
    for cmd in ("zstd", "tar"):
        if not shutil.which(cmd):
            missing.append(cmd)
    if missing:
        print(f"Error: required tools not found: {', '.join(missing)}")
        print(f"  Install them with: sudo pacman -S {' '.join(missing)}")
        sys.exit(1)


class _FileLock:
    """Simple file-based lock using fcntl."""
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

# ============================================================
# INITIALIZATION
# ============================================================

def ensure_dirs():
    check_deps()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Check for another running yapm instance
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

def load_config() -> Dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: Corrupted config file, using defaults: {e}")
        return DEFAULT_CONFIG

def save_config(config: Dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

def load_yapm_conf() -> Dict[str, str]:
    result = {}
    for path in [YAPM_CONF_SYSTEM, YAPM_CONF_USER]:
        if path.exists():
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        result[k.strip()] = v.strip()
    return result

def save_yapm_conf(overrides: Dict[str, str]):
    YAPM_CONF_USER.parent.mkdir(parents=True, exist_ok=True)
    with open(YAPM_CONF_USER, "w") as f:
        f.write("# YAPM Configuration\n")
        for k in KNOWN_FLAGS:
            v = overrides.get(k, str(KNOWN_FLAGS[k]).lower())
            f.write(f"{k} = {v}\n")

def config_flag(name: str) -> bool:
    conf = load_yapm_conf()
    riot = conf.get("yapm.riot", "false").lower() == "true"
    if riot and name in ("yapm.insroot", "yapm.hooks", "yapm.noconfirm"):
        return True
    val = conf.get(name, str(KNOWN_FLAGS.get(name, "false")))
    return val.lower() == "true"

def load_db() -> Dict:
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

def _write_db(db: Dict):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=4)

def save_db(db: Dict):
    with _FileLock(DB_FILE):
        _write_db(db)

# ============================================================
# UTILITIES
# ============================================================

def normalize(url: str) -> str:
    return url if url.endswith("/") else url + "/"

def pkg_basename(key: str) -> str:
    """Strip author prefix from a key for filename guessing (author/name -> name)."""
    return key.split("/", 1)[-1]

def format_key(key: str) -> str:
    """Convert internal key to display form (author/name -> author@name)."""
    if "/" in key:
        a, n = key.split("/", 1)
        return f"{a}@{n}"
    return key

def sorted_mirrors() -> List[Dict]:
    config = load_config()
    return sorted(config["mirrors"], key=lambda x: x["priority"])

def validate_mirror(url: str) -> bool:
    try:
        if url.startswith("file://"):
            return Path(url[7:]).exists()
        req = urllib.request.Request(normalize(url), method="HEAD", headers={'User-Agent': 'yapm/1.0'})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status < 400
    except Exception:
        return False

def download(url: str, desc: str = "Downloading", silent_errors: bool = False) -> Optional[bytes]:
    max_retries = 5
    chunks = []
    downloaded = 0
    size = 0
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'yapm/1.0'})
            if downloaded > 0:
                req.add_header("Range", f"bytes={downloaded}-")
            with urllib.request.urlopen(req, timeout=120) as response:
                if attempt == 0 or response.status != 206:
                    chunks = []
                    downloaded = 0
                    size = int(response.headers.get('content-length', 0))
                
                chunk_size = 8192
                interrupted = False
                while True:
                    try:
                        chunk = response.read(chunk_size)
                    except Exception:
                        interrupted = True
                        break
                    if not chunk: break
                    chunks.append(chunk)
                    downloaded += len(chunk)
                    if size:
                        percent = int(downloaded * 100 / size)
                        cols, _ = shutil.get_terminal_size((80, 20))
                        bar_len = min(40, cols - len(desc) - 30)
                        if bar_len < 10: bar_len = 10
                        filled = int(bar_len * downloaded / size)
                        
                        if filled >= bar_len:
                            bar = "=" * bar_len
                        else:
                            bar = "=" * filled + ">" + " " * (bar_len - filled - 1)
                            
                        brown = "\033[38;2;160;120;90m"
                        reset = "\033[0m"
                        
                        sz_str = f"{downloaded/1048576:.1f}/{size/1048576:.1f}MB" if size > 1048576 else f"{downloaded/1024:.0f}/{size/1024:.0f}KB"
                        
                        print(f"\r\033[K{brown}/yapm > {desc} [{bar}] {percent:3d}%{reset} \033[38;5;242m({sz_str})\033[0m", end="", flush=True)
                
                if interrupted or (size > 0 and downloaded < size):
                    if attempt < max_retries - 1:
                        time.sleep(1)
                        continue
                    else:
                        print()
                        if not silent_errors:
                            print(f"\nDownload incomplete (got {downloaded} of {size} bytes)")
                        return None
                print()
                return b"".join(chunks)
        except urllib.error.HTTPError as e:
            if e.code == 416 and downloaded > 0 and downloaded >= size:
                print()
                return b"".join(chunks)
            if not silent_errors or e.code != 404:
                print(f"\nError downloading {url}: {e}")
            if e.code in (404, 403, 401):
                return None
        except Exception as e:
            if attempt == max_retries - 1:
                if not silent_errors:
                    print(f"\nError downloading {url}: {e}")
                return None
            time.sleep(1)
            continue
    return None

def is_valid_zip(data: bytes) -> bool:
    """Check ZIP magic bytes (PK\x03\x04) or ZSTD magic bytes (28 B5 2F FD) to avoid treating HTML 404 pages as packages."""
    if len(data) > 3 and data[:2] == b'PK':
        return True
    if len(data) > 3 and data[:4] == b'\x28\xb5\x2f\xfd':
        return True
    return False

def safe_extract(archive_path: Path, target: Path):
    if config_flag("yapm.dangerzone"):
        print("DANGERZONE: safety checks disabled. You asked for this.")
        if archive_path.name.endswith(".zst") or archive_path.name.endswith(".tar.zst"):
            subprocess.run(["tar", "--use-compress-program=zstd", "-xf", str(archive_path), "-C", str(target)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            with zipfile.ZipFile(archive_path) as z:
                z.extractall(target)
        return

    resolved_target = target.resolve()

    with open(archive_path, "rb") as f:
        magic = f.read(4)
        
    if magic[:2] == b'PK':
        with zipfile.ZipFile(archive_path) as z:
            for member in z.infolist():
                member_path = (target / member.filename).resolve()
                try:
                    member_path.relative_to(resolved_target)
                except ValueError:
                    raise Exception("Unsafe zip detected: path traversal attempt")
                z.extract(member, target)
                attr = member.external_attr >> 16
                if attr != 0:
                    os.chmod(member_path, attr)
    elif magic == b'\x28\xb5\x2f\xfd':
        with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
            subprocess.run(["zstd", "-d", "-f", str(archive_path), "-o", tmp.name], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with tarfile.open(tmp.name) as tar:
                for member in tar.getmembers():
                    member_path = (target / member.name).resolve()
                    try:
                        member_path.relative_to(resolved_target)
                    except ValueError:
                        raise Exception("Unsafe tar detected: path traversal attempt")
                    tar.extract(member, target)
    else:
        raise Exception("Unknown archive format")

def parse_pkginfo(data: bytes) -> dict:
    result: dict = {"depends": []}
    with tempfile.TemporaryDirectory() as td:
        archive_path = Path(td) / "pkg.tar.zst"
        archive_path.write_bytes(data)
        
        tar_path = Path(td) / "pkg.tar"
        try:
            subprocess.run(["zstd", "-d", "-f", str(archive_path), "-o", str(tar_path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with tarfile.open(tar_path) as tar:
                try:
                    f = tar.extractfile(".PKGINFO")
                    if f:
                        content = f.read().decode('utf-8')
                        for line in content.splitlines():
                            line = line.strip()
                            if not line or line.startswith('#'):
                                continue
                            if '=' in line:
                                k, v = line.split('=', 1)
                                k = k.strip()
                                v = v.strip()
                                if k == "depend":
                                    result["depends"].append(v)
                                else:
                                    result[k] = v
                except KeyError:
                    pass
        except Exception as e:
            print(f"Warning: Failed to parse .PKGINFO: {e}")
    return result

def parse_yapm_data(content: str) -> dict:
    data = {"METADATA": {}, "CONTENT": {}, "FILES": {}}
    current_section = None
    
    # Strip multi-line comments /* ... */
    content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
    
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('//'):
            continue
            
        # Strip inline comments
        if '//' in line:
            line = line.split('//')[0].strip()
            
        if line.startswith('[') and line.endswith(']'):
            current_section = line[1:-1]
            continue
            
        if current_section and '=' in line:
            parts = line.split('=', 1)
            key = parts[0].strip().strip('"').strip("'")
            val = parts[1].strip()
            
            if val.startswith('[') and val.endswith(']'):
                try:
                    val = ast.literal_eval(val)
                except Exception:
                    val = []
            else:
                val = val.strip('"').strip("'")
                
            data[current_section][key] = val
            
    return data

# ============================================================
# MIRROR COMMANDS
# ============================================================

def mirror_list():
    for i, m in enumerate(sorted_mirrors(), 1):
        print(f"[{i}] {m['url']} (priority {m['priority']})")

def mirror_add(url: str, priority: int):
    config = load_config()
    url = normalize(url)
    for m in config["mirrors"]:
        if m["url"] == url:
            print("Mirror already exists.")
            return
    config["mirrors"].append({"url": url, "priority": priority})
    save_config(config)
    print(f"Added mirror {url} with priority {priority}")

def mirror_remove(url: str):
    config = load_config()
    url = normalize(url)
    before = len(config["mirrors"])
    config["mirrors"] = [m for m in config["mirrors"] if m["url"] != url]
    if len(config["mirrors"]) == before:
        print("Mirror not found.")
    else:
        save_config(config)
        print("Mirror removed.")

def mirror_refresh():
    config = load_config()
    valid = []
    print("Refreshing mirrors...")
    for m in config["mirrors"]:
        ok = validate_mirror(m["url"])
        print(f"  {m['url']} -> {'OK' if ok else 'FAILED'}")
        if ok: valid.append(m)
    config["mirrors"] = valid
    save_config(config)
    print("Refresh complete.")

def mirror_preset():
    save_config(DEFAULT_CONFIG)
    print("Restored default mirrors.")

# ============================================================
# PACKAGE EXTRACTION ENGINES
# ============================================================

def extract_deb(data: bytes, target: Path):
    with tempfile.TemporaryDirectory() as td:
        deb_path = Path(td) / "pkg.deb"
        with open(deb_path, "wb") as f:
            f.write(data)
        try:
            print("  Extracting DEB container...")
            subprocess.run(["ar", "x", "pkg.deb"], cwd=td, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            for f in Path(td).iterdir():
                if f.name.startswith("data.tar"):
                    print("  Extracting DEB data payload...")
                    subprocess.run(["tar", "-xf", f.name, "-C", str(target)], cwd=td, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                    break
        except subprocess.CalledProcessError as e:
            print(f"Error extracting DEB package: {e}\nStderr: {e.stderr}")
            raise
        except Exception as e:
            print(f"Error extracting DEB package: {e}")
            raise

def extract_arch(data: bytes, target: Path):
    with tempfile.TemporaryDirectory() as td:
        arch_path = Path(td) / "pkg.tar.zst"
        with open(arch_path, "wb") as f:
            f.write(data)
        try:
            print("  Extracting Arch ZSTD container...")
            subprocess.run(["tar", "--use-compress-program=zstd", "-xf", "pkg.tar.zst", "-C", str(target)], cwd=td, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except subprocess.CalledProcessError as e:
            print(f"Error extracting Arch package: {e}\nStderr: {e.stderr}")
            raise
        except Exception as e:
            print(f"Error extracting Arch package: {e}")
            raise

def get_arch_file_list(data: bytes) -> List[str]:
    """Extract the list of file paths from an Arch .pkg.tar.zst without fully extracting."""
    with tempfile.TemporaryDirectory() as td:
        pkg_path = Path(td) / "pkg.tar.zst"
        pkg_path.write_bytes(data)
        tar_path = Path(td) / "pkg.tar"
        try:
            subprocess.run(["zstd", "-d", "-f", str(pkg_path), "-o", str(tar_path)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with tarfile.open(tar_path) as tar:
                return [m.name for m in tar.getmembers() if m.isfile() or m.issym() or m.islnk()]
        except Exception:
            return []

def get_deb_file_list(data: bytes) -> List[str]:
    """Extract the list of file paths from a .deb without fully extracting."""
    with tempfile.TemporaryDirectory() as td:
        deb_path = Path(td) / "pkg.deb"
        deb_path.write_bytes(data)
        try:
            subprocess.run(["ar", "x", "pkg.deb"], cwd=td, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for f in Path(td).iterdir():
                if f.name.startswith("data.tar"):
                    with tarfile.open(f) as tar:
                        return [m.name for m in tar.getmembers() if m.isfile() or m.issym() or m.islnk()]
        except Exception:
            pass
    return []

def run_pkg_install_hook(pkg_data: bytes, root: Path, phase: str):
    with tempfile.TemporaryDirectory() as td:
        pkg_path = Path(td) / "pkg.tar.zst"
        pkg_path.write_bytes(pkg_data)
        try:
            subprocess.run(["tar", "--use-compress-program=zstd", "-xf", "pkg.tar.zst", ".INSTALL"],
                           cwd=td, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            return
        install_file = Path(td) / ".INSTALL"
        if not install_file.exists():
            return
        tmp_hook = root / "tmp" / "yapm_install_hook.sh"
        tmp_hook.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(install_file, tmp_hook)
        os.chmod(tmp_hook, 0o755)
        script = f"source /tmp/yapm_install_hook.sh && if type {phase} >/dev/null 2>&1; then {phase}; fi"
        print(f"  Running {phase} hook...")
        if str(root) != "/":
            if not shutil.which("arch-chroot"):
                print(f"  Warning: arch-chroot not found, skipping {phase} hook.")
            else:
                subprocess.run(["arch-chroot", str(root), "bash", "-c", script], check=False)
        else:
            subprocess.run(["bash", "-c", script], check=False)
        tmp_hook.unlink(missing_ok=True)

# ============================================================
# PACKAGE LOGIC
# ============================================================

_UBUNTU_DISTROS = ["noble", "jammy", "focal", "bionic"]
_DEBIAN_DISTROS = ["trixie", "bookworm", "bullseye", "buster"]

def _detect_deb_distro(mirror_url: str) -> str:
    """Detect the appropriate Debian/Ubuntu distro codename from a mirror URL."""
    if "ubuntu" in mirror_url:
        for d in _UBUNTU_DISTROS:
            return d  # Use first (newest) for now; could probe mirror later
    for d in _DEBIAN_DISTROS:
        return d
    return "bookworm"

def parse_debian_index(mirror_url: str, merged_index: dict):
    dist = _detect_deb_distro(mirror_url)
    url = normalize(mirror_url) + f"dists/{dist}/main/binary-amd64/Packages.gz"
    data = download(url, desc=f"Fetching Debian index from {mirror_url}")
    if not data: return
    
    try:
        print("  Parsing Debian Packages.gz...")
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            content = gz.read().decode('utf-8', errors='ignore')
            
        current_pkg = {}
        for line in content.splitlines():
            if not line.strip():
                if current_pkg and "name" in current_pkg:
                    name = current_pkg["name"]
                    merged_index["packages"].setdefault(name, {})["deb"] = {
                        "version": current_pkg.get("version", "0.0.0"),
                        "mirror": mirror_url,
                        "format": "deb",
                        "download_path": current_pkg.get("filename", "")
                    }
                current_pkg = {}
                continue
                
            if line.startswith("Package: "): current_pkg["name"] = line.split(":", 1)[1].strip()
            elif line.startswith("Version: "): current_pkg["version"] = line.split(":", 1)[1].strip()
            elif line.startswith("Filename: "): current_pkg["filename"] = line.split(":", 1)[1].strip()
    except Exception as e:
        print(f"Error parsing Debian index: {e}")

def parse_arch_index(mirror_url: str, merged_index: dict):
    for repo in ("core", "extra", "community"):
        url = normalize(mirror_url) + f"{repo}/os/x86_64/{repo}.db"
        data = download(url, desc=f"Fetching Arch {repo} index from {mirror_url}")
        if not data:
            continue

        try:
            print(f"  Parsing Arch {repo}.db...")
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                for member in tar.getmembers():
                    if member.name.endswith("desc"):
                        f = tar.extractfile(member)
                        if f:
                            content = f.read().decode('utf-8', errors='ignore')
                            lines = content.splitlines()
                            name, version, arch = "", "", "x86_64"
                            dependencies = []
                            for i, line in enumerate(lines):
                                if line == "%NAME%": name = lines[i+1]
                                elif line == "%VERSION%": version = lines[i+1]
                                elif line == "%ARCH%": arch = lines[i+1]
                                elif line == "%DEPENDS%":
                                    j = i + 1
                                    while j < len(lines) and lines[j] and not lines[j].startswith("%"):
                                        dep = lines[j]
                                        for char in ('<', '>', '='):
                                            dep = dep.split(char)[0]
                                        dependencies.append(dep)
                                        j += 1

                            if name:
                                # Don't overwrite an entry already found in a higher-priority repo
                                merged_index["packages"].setdefault(name, {}).setdefault("arch", {
                                    "version": version,
                                    "mirror": mirror_url,
                                    "format": "arch",
                                    "dependencies": dependencies,
                                    "download_path": f"{repo}/os/x86_64/{name}-{version}-{arch}.pkg.tar.zst"
                                })
        except Exception as e:
            print(f"Error parsing Arch {repo} index: {e}")

def get_pkg_info(idx: dict, pkg: str, version: Optional[str] = None, arch_mode: bool = False) -> Optional[dict]:
    """Look up a specific version of a package.

    The index stores per-format entries as ``{pkg_name: {format: entry_dict}}``.
    When *arch_mode* is ``True`` only the ``"arch"`` sub-entry is considered;
    otherwise the priority order is ``yapm > arch > deb``.
    """
    packages = idx.get("packages", {})
    entry = packages.get(pkg)
    if not entry:
        return None

    # entry is now a dict keyed by format — select the right sub-entry
    if arch_mode:
        sub = entry.get("arch")
        if not sub:
            return None
        entry = sub
    else:
        for fmt in ("yapm", "arch", "deb"):
            if fmt in entry:
                entry = entry[fmt]
                break
        else:
            return None

    if "versions" in entry:
        ver = version or entry.get("latest", "0.0.0")
        ver_info = entry["versions"].get(ver)
        if not ver_info:
            return None
        result = dict(ver_info)
        result["version"] = ver
        result["mirror"] = entry.get("mirror", "")
        result["format"] = entry.get("format", "yapm")
        result["latest"] = entry.get("latest", ver)
        result["_key"] = pkg
        return result
    return dict(entry)

def update_index():
    if config_flag("yapm.yapm"):
        print("found 0 updates")
        time.sleep(1)
        print("just kidding")
    print("Updating package index...")
    merged_index = {"packages": {}}
    for mirror in sorted_mirrors():
        url = mirror["url"]
        if "ubuntu.com" in url or "debian.org" in url:
            parse_debian_index(url, merged_index)
        elif "archlinux" in url:
            parse_arch_index(url, merged_index)
        else:
            index_url = normalize(url) + "index.json"
            data = download(index_url, desc=f"Fetching YAPM index from {url}")
            if data:
                try:
                    index = json.loads(data)
                    pkgs = index.get("packages", {})
                    if isinstance(pkgs, list):
                        new_pkgs = {p: {"version": "0.0.0", "dependencies": []} for p in pkgs}
                        pkgs = new_pkgs

                    for pkg_name, pkg_info in pkgs.items():
                        if "/" in pkg_name:
                            pkg_name = pkg_name.split("/", 1)[-1]
                            
                        if "versions" in pkg_info:
                            merged_index["packages"].setdefault(pkg_name, {})["yapm"] = {
                                "latest": pkg_info.get("latest", ""),
                                "mirror": url,
                                "format": "yapm",
                                "versions": pkg_info.get("versions", {})
                            }
                        else:
                            merged_index["packages"].setdefault(pkg_name, {})["yapm"] = {
                                **pkg_info,
                                "mirror": url,
                                "format": "yapm"
                            }
                except Exception as e:
                    print(f"Error parsing index from {url}: {e}")

    with open(INDEX_FILE, "w") as f:
        json.dump(merged_index, f, indent=4)
    print("Index updated.")

def load_index() -> Dict:
    if not INDEX_FILE.exists():
        print("Warning: Local index not found. Run 'yapm update' first.")
        return {"packages": {}}
    with open(INDEX_FILE) as f:
        idx = json.load(f)
    new_pkgs = {}
    for k, v in idx.get("packages", {}).items():
        name = k.split("/", 1)[-1] if "/" in k else k
        # Normalize to nested-by-format structure
        if any(fmt in v for fmt in ("yapm", "arch", "deb")):
            normalized = v
        else:
            fmt = v.get("format", "yapm")
            normalized = {fmt: v}
        
        if name in new_pkgs:
            # Merge: add any format sub-entries not already present
            for fmt, entry in normalized.items():
                if fmt not in new_pkgs[name]:
                    new_pkgs[name][fmt] = entry
        else:
            new_pkgs[name] = dict(normalized)
    idx["packages"] = new_pkgs
    return idx

def fetch_from_github(pkg_name: str, repo: str, version: Optional[str]) -> Optional[bytes]:
    branches = ["main", "master"]
    dirs = ["", "packages/"]
    
    candidates = []
    if version and version != "0.0.0":
        candidates.append(f"{pkg_name}-{version}.yapm")
    candidates.append(f"{pkg_name}.yapm")
    
    for branch in branches:
        for d in dirs:
            for cand in candidates:
                url = f"https://raw.githubusercontent.com/{repo}/{branch}/{d}{cand}"
                data = download(url, desc=f"Downloading {pkg_name} from GitHub", silent_errors=True)
                if data and is_valid_zip(data):
                    return data
    return None

def fetch_package(pkg: str, mirror_url: Optional[str] = None, version: Optional[str] = None, arch_mode: bool = False) -> Optional[bytes]:
    idx = load_index()
    pkg_info = get_pkg_info(idx, pkg, version, arch_mode=arch_mode)
    fmt = "arch" if arch_mode else (pkg_info or {}).get("format", "yapm")
    base = pkg_basename(pkg)

    def _try_at(m_url: str) -> Optional[bytes]:
        if fmt in ("deb", "arch"):
            download_path = (pkg_info or {}).get("download_path", "")
            if download_path:
                return download(normalize(m_url) + download_path, desc=f"Downloading {pkg}")
            if arch_mode:
                print(f"Warning: Package '{pkg}' not found in Arch index (mirror is pinned to Arch). Skipping.")
            return None
        candidates = []
        if pkg_info and pkg_info.get("filename"):
            candidates.append(pkg_info["filename"])
        else:
            candidates.append(f"{base}.yapm")
            v = version or (pkg_info.get("version") if pkg_info else "")
            if v and v != "0.0.0":
                candidates.append(f"{base}-{v}.yapm")
        for cand in candidates:
            url = normalize(m_url) + cand
            data = download(url, desc=f"Downloading {pkg}")
            if data and is_valid_zip(data):
                return data
        return None

    if mirror_url:
        return _try_at(mirror_url)

    if pkg_info and pkg_info.get("mirror"):
        data = _try_at(pkg_info["mirror"])
        if data:
            return data

    for mirror in sorted_mirrors():
        data = _try_at(mirror["url"])
        if data:
            return data
    return None

def resolve_dependencies(pkg: str, idx: Dict, db: Dict, to_install: List[str], path: set, visited: set, version: Optional[str] = None, arch_mode: bool = False):
    if pkg in to_install or pkg in db or pkg in VIRTUAL_PROVIDERS or pkg in visited:
        return
    if pkg in path:
        print(f"Error: Circular dependency detected: {' -> '.join(path)} -> {pkg}")
        sys.exit(1)

    path.add(pkg)
    visited.add(pkg)
    pkg_info = get_pkg_info(idx, pkg, version, arch_mode=arch_mode)
    if pkg_info:
        for dep in pkg_info.get("dependencies", []):
            if dep in VIRTUAL_PROVIDERS:
                continue
            if re.match(r'^lib.*\.so', dep) or re.search(r'\.so(\.[0-9]+)*$', dep):
                continue
            resolve_dependencies(dep, idx, db, to_install, path, visited, arch_mode=arch_mode)
        to_install.append(pkg)
    else:
        if arch_mode:
            print(f"Warning: Package '{pkg}' not found in Arch index (mirror is pinned to Arch). Skipping.")
        else:
            print(f"Warning: Package '{pkg}' not found in index. Cannot resolve its dependencies.")
    path.remove(pkg)

def _install_single(pkg_name: str, db: Dict, data: bytes, fmt: str):
    if config_flag("yapm.nativenationality") and fmt != "yapm":
        print("yapm.nativenationality is enabled — only native .yapm packages allowed")
        sys.exit(1)

    # Determine extraction target:
    # - .yapm packages always go to sandbox (they have manifests)
    # - arch/deb extract to ROOT_DIR when --root is set (respect native prefix)
    # - arch/deb stay in sandbox when running on host (safety)
    use_root = fmt in ("arch", "deb") and str(ROOT_DIR) != "/"
    file_list: List[str] = []

    if use_root:
        extract_target = ROOT_DIR
    else:
        extract_target = INSTALL_DIR / pkg_name
        if extract_target.exists():
            shutil.rmtree(extract_target)
        extract_target.mkdir(parents=True, exist_ok=True)

    try:
        if fmt == "yapm":
            tmp = tempfile.NamedTemporaryFile(delete=False)
            tmp.write(data)
            tmp.close()
            safe_extract(Path(tmp.name), extract_target)
            os.unlink(tmp.name)
        elif fmt == "deb":
            extract_deb(data, extract_target)
        elif fmt == "arch":
            extract_arch(data, extract_target)
    except Exception as e:
        print(f"Installation failed: {e}")
        sys.exit(1)

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    pkg_meta = {"version": "0.0.0", "dependencies": [], "format": fmt}

    if fmt == "yapm":
        # .yapm packages use manifest-driven installation
        yapm_data_path = extract_target / "yapm.data"
        if yapm_data_path.exists():
            with open(yapm_data_path) as f:
                y_data = parse_yapm_data(f.read())
                
            meta = y_data.get("METADATA", {})
            pkg_meta["version"] = meta.get("version", "0.0.0")
            if "description" in meta: pkg_meta["description"] = meta["description"]
            if "dependencies" in meta: pkg_meta["dependencies"] = meta["dependencies"]
            
            content_info = y_data.get("CONTENT", {})
            
            build_file = content_info.get("BuildFile")
            if build_file and (extract_target / build_file).exists():
                print(f"  Running build script: {build_file}...")
                os.chmod(extract_target / build_file, 0o755)
                subprocess.run([str(extract_target / build_file)], cwd=extract_target, check=True)
                
            pre_install = content_info.get("PreInstall")
            if pre_install and (extract_target / pre_install).exists():
                print("  Running pre-install script...")
                os.chmod(extract_target / pre_install, 0o755)
                subprocess.run([str(extract_target / pre_install)], cwd=extract_target, check=True)
                
            # File Mappings — root absolute destinations at ROOT_DIR
            files_info = y_data.get("FILES", {})
            for src, dest in files_info.items():
                src_path = extract_target / src
                if dest.startswith("/"):
                    dest_path = ROOT_DIR / dest.lstrip("/")
                else:
                    dest_path = extract_target / dest
                if src_path.exists():
                    print(f"  Mapping file: {src} -> {dest}")
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    if dest_path.exists() or dest_path.is_symlink():
                        os.unlink(dest_path)
                    shutil.copy2(src_path, dest_path)
                    chaos_yap_on_extract(src)
                    
            run_file = content_info.get("RunFile")
            if run_file and (extract_target / run_file).exists():
                dest = BIN_DIR / Path(run_file).name
                if dest.exists() or dest.is_symlink():
                    os.unlink(dest)
                os.chmod(extract_target / run_file, 0o755)
                symlink_src = ROOT_DIR / (extract_target / run_file).relative_to(ROOT_DIR)
                os.symlink(symlink_src, dest)
                print(f"  Linked executable {Path(run_file).name} -> {dest}")
                chaos_yap_on_extract(run_file)
                
            post_install = content_info.get("PostInstall")
            if post_install and (extract_target / post_install).exists():
                print("  Running post-install script...")
                os.chmod(extract_target / post_install, 0o755)
                subprocess.run([str(extract_target / post_install)], cwd=extract_target, check=True)
        else:
            # Fallback for .yapm without manifest
            bin_source_dirs = [extract_target / "src", extract_target / "usr" / "bin", extract_target / "bin"]
            for src_dir in bin_source_dirs:
                if src_dir.exists() and src_dir.is_dir():
                    for item in src_dir.iterdir():
                        if item.is_file() and os.access(item, os.X_OK):
                            dest = BIN_DIR / item.name
                            if dest.exists() or dest.is_symlink():
                                os.unlink(dest)
                            symlink_src = ROOT_DIR / item.relative_to(ROOT_DIR)
                            os.symlink(symlink_src, dest)
                            print(f"  Linked {item.name} -> {dest}")

            metadata_path = extract_target / "metadata.json"
            if metadata_path.exists():
                try:
                    with open(metadata_path) as f:
                        pkg_meta.update(json.load(f))
                except Exception:
                    pass
    elif use_root:
        # arch/deb extracted to ROOT_DIR — track installed files
        if fmt == "arch":
            file_list = get_arch_file_list(data)
        else:
            file_list = get_deb_file_list(data)

        # Extract metadata from the package
        if fmt == "arch":
            pkginfo = parse_pkginfo(data)
            if pkginfo:
                pkg_meta["version"] = pkginfo.get("pkgver", "0.0.0")
                pkg_meta["dependencies"] = pkginfo.get("depends", [])
                pkg_meta["description"] = pkginfo.get("pkgdesc", "")
        elif fmt == "deb":
            # Try to extract version from control
            with tempfile.TemporaryDirectory() as td:
                deb_path = Path(td) / "pkg.deb"
                deb_path.write_bytes(data)
                try:
                    subprocess.run(["ar", "x", "pkg.deb"], cwd=td, check=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    for f in Path(td).iterdir():
                        if f.name.startswith("control"):
                            with open(f) as fh:
                                for line in fh:
                                    if line.startswith("Version: "):
                                        pkg_meta["version"] = line.split(":", 1)[1].strip()
                                    elif line.startswith("Depends: "):
                                        dep_str = line.split(":", 1)[1].strip()
                                        pkg_meta["dependencies"] = [d.strip().split()[0] for d in dep_str.split(",")]
                                    elif line.startswith("Description: "):
                                        pkg_meta["description"] = line.split(":", 1)[1].strip()
                except Exception:
                    pass
    else:
        # arch/deb in sandbox mode (running on host) — fallback linking
        bin_source_dirs = [extract_target / "src", extract_target / "usr" / "bin", extract_target / "bin"]
        for src_dir in bin_source_dirs:
            if src_dir.exists() and src_dir.is_dir():
                for item in src_dir.iterdir():
                    if item.is_file() and os.access(item, os.X_OK):
                        dest = BIN_DIR / item.name
                        if dest.exists() or dest.is_symlink():
                            os.unlink(dest)
                        symlink_src = ROOT_DIR / item.relative_to(ROOT_DIR)
                        os.symlink(symlink_src, dest)
                        print(f"  Linked {item.name} -> {dest}")

        metadata_path = extract_target / "metadata.json"
        if metadata_path.exists():
            try:
                with open(metadata_path) as f:
                    pkg_meta.update(json.load(f))
            except Exception:
                pass

    db_entry = {
        "version": pkg_meta.get("version", "0.0.0"),
        "path": str(extract_target),
        "dependencies": pkg_meta.get("dependencies", []),
        "format": fmt,
        "metadata": pkg_meta
    }
    if use_root and file_list:
        db_entry["files"] = file_list

    db[pkg_name] = db_entry

    save_db(db)

def install_package(packages: List[str], fmt: str, mirror_index: Optional[int] = None, root: Optional[str] = None, noconfirm: bool = False, dry_run: bool = False):
    if config_flag("yapm.yapm"):
        chaos_spinner(3)
    if config_flag("yapm.autoupdate"):
        update_index()

    if root and root != "/":
        if not config_flag("yapm.insroot"):
            print("enable yapm.insroot to use this feature")
            sys.exit(1)
        set_root_dir(root)

    db = load_db()
    idx = load_index()

    global_pinned_mirror = None
    if mirror_index is not None:
        all_mirrors = sorted_mirrors()
        if mirror_index < 1 or mirror_index > len(all_mirrors):
            print(f"Error: mirror index {mirror_index} is out of range.")
            print("Available mirrors (use 'yapm mirror list' to see them):")
            for i, m in enumerate(all_mirrors, 1):
                print(f"  [{i}] {m['url']} (priority {m['priority']})")
            sys.exit(1)
        global_pinned_mirror = all_mirrors[mirror_index - 1]["url"]
        print(f"Pinned to mirror [{mirror_index}]: {global_pinned_mirror}")

    arch_mode = global_pinned_mirror is not None and "archlinux" in global_pinned_mirror
    if arch_mode:
        print("  → Arch mirror detected: forcing arch package format")

    pre_fetched_data = {}
    to_install_merged = []
    seen = set()
    visited = set()
    pin_version = {}
    pin_mirror = {}

    local_installs = []

    for pkg in packages:
        pkg_spec = pkg
        pkg_version = None
        if "=" in pkg_spec:
            pkg_spec, pkg_version = pkg_spec.split("=", 1)

        pkg_source = None
        if "@" in pkg_spec:
            pkg_name, pkg_source = pkg_spec.rsplit("@", 1)
        else:
            pkg_name = pkg_spec

        pkg_pinned_mirror = global_pinned_mirror
        is_github = False
        github_repo = None

        pkg_path = Path(pkg_name)
        if pkg_path.is_file():
            local_installs.append(pkg_path)
            continue

        if pkg_source:
            if pkg_source.startswith("github:"):
                is_github = True
                github_repo = pkg_source[7:]
            else:
                mirrors = sorted_mirrors()
                matched_mirror = None
                for m in mirrors:
                    if pkg_source == m["url"]:
                        matched_mirror = m["url"]
                        break
                if not matched_mirror:
                    for m in mirrors:
                        if pkg_source in m["url"]:
                            matched_mirror = m["url"]
                            break
                if matched_mirror:
                    pkg_pinned_mirror = matched_mirror
                else:
                    print(f"Error: Unknown source '{pkg_source}' — not a configured mirror and not a github:User/Repo reference")
                    sys.exit(1)

        if is_github and github_repo:
            print(f"Fetching {pkg_name} from GitHub ({github_repo})...")
            data = fetch_from_github(pkg_name, github_repo, pkg_version)
            if not data:
                print(f"Failed to fetch {pkg_name} from GitHub. Aborting.")
                sys.exit(1)
            pre_fetched_data[pkg_name] = data
            meta = {}
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for member in z.infolist():
                        if member.filename.endswith("yapm.data"):
                            content = z.read(member.filename).decode('utf-8')
                            y_data = parse_yapm_data(content)
                            meta = y_data.get("METADATA", {})
                            break
            except Exception:
                pass
            idx.setdefault("packages", {}).setdefault(pkg_name, {})["yapm"] = {
                "version": meta.get("version", "0.0.0"),
                "dependencies": meta.get("dependencies", []),
                "format": "yapm"
            }

        pin_version[pkg_name] = pkg_version
        pin_mirror[pkg_name] = pkg_pinned_mirror
        resolve_dependencies(pkg_name, idx, db, to_install_merged, seen, visited, version=pkg_version, arch_mode=arch_mode)

    for pkg_path in local_installs:
        local_fmt = fmt
        if pkg_path.suffix == ".deb": local_fmt = "deb"
        elif pkg_path.name.endswith(".pkg.tar.zst"): local_fmt = "arch"
        elif pkg_path.suffix == ".yapm": local_fmt = "yapm"

        if pkg_path.name.endswith(".pkg.tar.zst"):
            pkg_name = pkg_path.name[:-12]
        else:
            pkg_name = pkg_path.stem

        print(f"Installing {local_fmt.upper()} from local file: {pkg_path}")
        with open(pkg_path, "rb") as f:
            data = f.read()
        _install_single(pkg_name, db, data, local_fmt)
        if local_fmt == "arch":
            if config_flag("yapm.hooks"):
                run_pkg_install_hook(data, ROOT_DIR, "post_install")
            pkginfo = parse_pkginfo(data)
            if pkginfo:
                db[pkg_name]["version"] = pkginfo.get("pkgver", "0.0.0")
                db[pkg_name]["dependencies"] = pkginfo.get("depends", [])
                db[pkg_name].setdefault("metadata", {})["description"] = pkginfo.get("pkgdesc", "")
                save_db(db)
        print(f"Installed {pkg_name} successfully.")

    if not to_install_merged:
        if not local_installs:
            print("Nothing to install.")
        return

    print(f"The following packages will be installed: {', '.join(to_install_merged)}")

    if config_flag("yapm.yapm"):
        chaos_confirm(3)
        for p in to_install_merged:
            chaos_delay(0.5)
            print(f"  → {chaos_wrong_name(p)}")
        print()
        chaos_opinion(to_install_merged[0])

    if not noconfirm and not config_flag("yapm.noconfirm"):
        try:
            choice = input("Proceed with installation? [Y/n] ").strip().lower()
            if choice not in ('', 'y', 'yes'):
                print("Aborted.")
                sys.exit(0)
        except (ValueError, EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)

    if dry_run:
        print("(dry run — no changes made)")
        return

    for p in to_install_merged:
        p_ver = pin_version.get(p)
        p_mirror = pin_mirror.get(p)
        chaos_interrupt()
        display_p = chaos_wrong_name(p)
        print(f"Installing {display_p}...")

        if p in pre_fetched_data:
            data = pre_fetched_data[p]
        else:
            data = fetch_package(p, mirror_url=p_mirror, version=p_ver, arch_mode=arch_mode)

        if not data:
            print(f"Failed to fetch {p}. Aborting.")
            sys.exit(1)

        fetched_fmt = "arch" if arch_mode else (get_pkg_info(idx, p, p_ver) or {}).get("format", "yapm")

        if config_flag("yapm.paranoid"):
            expected_fmt = fetched_fmt
            if data[:2] == b'PK':
                actual_fmt = "yapm" if expected_fmt in ("yapm", "deb") else None
            elif data[:4] == b'\x28\xb5\x2f\xfd':
                actual_fmt = "arch"
            else:
                actual_fmt = None
            if expected_fmt == "deb":
                actual_fmt = "yapm" if data[:2] == b'PK' else None
            if expected_fmt == "arch":
                actual_fmt = "arch" if data[:4] == b'\x28\xb5\x2f\xfd' else "deb" if data[:2] == b'PK' else None
            if not actual_fmt or actual_fmt != expected_fmt:
                print(f"Warning: Package '{p}' has mismatched format (expected {expected_fmt}, got {actual_fmt or 'unknown'}). Refusing to install.")
                sys.exit(1)

        _install_single(p, db, data, fetched_fmt)
        if fetched_fmt == "arch" and config_flag("yapm.hooks"):
            run_pkg_install_hook(data, ROOT_DIR, "post_install")
        print(f"Installed {chaos_wrong_name(p)}.")

    if "linux" in to_install_merged and str(ROOT_DIR) != "/":
        print("Running mkinitcpio for bootstrapped system...")
        subprocess.run(["arch-chroot", str(ROOT_DIR), "mkinitcpio", "-P"], check=False)

    if config_flag("yapm.yapm"):
        print("something may or may not have gone wrong. who can say really")

    chaos_post_operation()

def remove_package(pkg: str, noconfirm: bool = False):
    db = load_db()

    pkg_key = pkg

    if pkg_key not in db:
        print(f"Package '{pkg_key}' not installed.")
        return

    if not noconfirm and not config_flag("yapm.noconfirm"):
        try:
            choice = input(f"Remove {format_key(pkg_key)}? [y/N] ").strip().lower()
            if choice not in ('y', 'yes'):
                print("Aborted.")
                return
        except (ValueError, EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return

    pkg_info = db[pkg_key]
    file_list = pkg_info.get("files", [])

    if file_list:
        # File-list-based removal (packages extracted to ROOT_DIR)
        root_ref = Path(pkg_info.get("path", "/"))
        removed = 0
        for f in file_list:
            full_path = root_ref / f
            if full_path.is_symlink() or full_path.is_file():
                os.unlink(full_path)
                removed += 1
            elif full_path.is_dir():
                try:
                    full_path.rmdir()  # only removes empty dirs
                except OSError:
                    pass  # non-empty, leave it
        # Clean up empty parent dirs left behind
        dirs_to_check = set()
        for f in file_list:
            p = (root_ref / f).parent
            while p != root_ref and p != root_ref.parent:
                dirs_to_check.add(p)
                p = p.parent
        for d in sorted(dirs_to_check, reverse=True):
            try:
                if d.exists() and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass
        print(f"Removed {format_key(pkg_key)} ({removed} files).")
    else:
        # Directory-based removal (sandbox packages)
        target = Path(pkg_info["path"])
        bin_source_dirs = [target / "src", target / "usr" / "bin", target / "bin"]
        
        for src_dir in bin_source_dirs:
            if src_dir.exists() and src_dir.is_dir():
                for item in src_dir.iterdir():
                    dest = BIN_DIR / item.name
                    if dest.is_symlink() and str(dest.resolve()) == str(item.resolve()):
                        os.unlink(dest)
                        print(f"Removed link {dest}")

        shutil.rmtree(target, ignore_errors=True)
        print(f"Removed {format_key(pkg_key)}.")

    del db[pkg_key]
    save_db(db)

def upgrade_packages(refresh: bool = False, dry_run: bool = False):
    if refresh or config_flag("yapm.autoupdate"):
        update_index()
    db = load_db()
    idx = load_index()

    to_upgrade = []
    for pkg, info in db.items():
        local_ver = info.get("version", "0.0.0")
        formats_entry = idx.get("packages", {}).get(pkg)
        if not formats_entry:
            continue
        installed_fmt = info.get("format", "yapm")
        remote_info = formats_entry.get(installed_fmt)
        if not remote_info:
            continue
        if "versions" in remote_info:
            remote_ver = remote_info.get("latest", "0.0.0")
        else:
            remote_ver = remote_info.get("version", "0.0.0")
        if _parse_ver(remote_ver) > _parse_ver(local_ver):
            to_upgrade.append((pkg, remote_ver))

    if not to_upgrade:
        print("Everything is up to date.")
        return

    print("The following packages will be upgraded:")
    for pkg, ver in to_upgrade:
        print(f"  {pkg} ({db[pkg].get('version', '0.0.0')} -> {ver})")

    if dry_run:
        print("(dry run — no changes made)")
        return

    for pkg, ver in to_upgrade:
        chaos_interrupt()
        print(f"Upgrading {pkg}...")
        data = fetch_package(pkg, version=ver)
        if not data:
            print(f"Failed to fetch {pkg}. Skipping.")
            continue
        _install_single(pkg, db, data, "yapm")
        print(f"Upgraded {pkg}.")

    chaos_post_operation()

def init_package(noconfirm: bool = False, root: Optional[str] = None):
    """Bootstrap a Riot system by ensuring bash is installed."""
    if not config_flag("yapm.riot"):
        print("Error: yapm init requires yapm.riot to be enabled.")
        print("  Run: yapm config enable yapm.riot")
        sys.exit(1)

    db = load_db()
    if "bash" in db:
        print("bash is already installed.")
        return

    print("Bootstrapping system: installing bash...")
    install_package(["bash"], fmt="yapm", noconfirm=True, root=root)
    print("bash installed. Shell is ready.")

def list_installed(outdated: bool = False, json_output: bool = False):
    db = load_db()
    if not db:
        if json_output:
            print("[]")
        else:
            print("No packages installed.")
        return

    if json_output:
        print(json.dumps(db, indent=2))
        return

    if outdated:
        idx = load_index()
        found = False
        for pkg, info in db.items():
            local_ver = info.get("version", "0.0.0")
            installed_fmt = info.get("format", "yapm")
            formats_entry = idx.get("packages", {}).get(pkg)
            if not formats_entry:
                continue
            remote_info = formats_entry.get(installed_fmt)
            if not remote_info:
                continue
            if "versions" in remote_info:
                remote_ver = remote_info.get("latest", "0.0.0")
            else:
                remote_ver = remote_info.get("version", "0.0.0")
            if _parse_ver(remote_ver) > _parse_ver(local_ver):
                print(f"  {pkg} {Color.YELLOW}{local_ver}{Color.RESET} -> {Color.GREEN}{remote_ver}{Color.RESET}")
                found = True
        if not found:
            print("Everything is up to date.")
        return

    for pkg, info in db.items():
        ver = info.get("version", "0.0.0")
        fmt = info.get("format", "yapm")
        print(f"{pkg} (v{ver}) [{fmt.upper()}]")

def uninstall_yapm():
    # require_root() has already run before this point
    print("Uninstalling system-wide yapm...")
    script_path = Path(__file__).resolve()
    if "bin/yapm" in str(script_path):
        os.unlink(script_path)
    else:
        std_bin = Path("/usr/local/bin/yapm")
        if std_bin.exists():
            os.unlink(std_bin)

    shutil.rmtree("/etc/yapm", ignore_errors=True)
    shutil.rmtree("/var/lib/yapm", ignore_errors=True)
    print("Successfully uninstalled yapm.")

YAPM_SOURCE_URL = "https://raw.githubusercontent.com/commodorial64/yapm/main/yapm.py"

def update_yapm(force: bool = False):
    print(f"Fetching latest yapm from {YAPM_SOURCE_URL} ...")
    data = download(YAPM_SOURCE_URL, desc="Downloading yapm")
    if not data:
        print("Error: failed to download the latest yapm.")
        sys.exit(1)

    new_src = data.decode("utf-8", errors="replace")

    # Parse APP_VERSION from the downloaded script
    m = re.search(r'^APP_VERSION\s*=\s*["\'](.+?)["\']', new_src, re.MULTILINE)
    if not m:
        print("Error: could not determine version of the downloaded script.")
        sys.exit(1)
    new_ver = m.group(1)

    print(f"  Installed : {APP_VERSION}")
    print(f"  Available : {new_ver}")

    if not force and _parse_ver(new_ver) == _parse_ver(APP_VERSION):
        print("yapm is already up to date.")
        return

    if not force and _parse_ver(new_ver) < _parse_ver(APP_VERSION):
        print("Downloaded version is older than installed. Use --force to override.")
        return

    # Atomic replace: write to a temp file beside the target, then rename
    target = Path("/usr/local/bin/yapm")
    tmp = target.with_suffix(".tmp")
    try:
        tmp.write_bytes(data)
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)   # atomic on Linux
    except Exception as e:
        print(f"Error writing new yapm: {e}")
        tmp.unlink(missing_ok=True)
        sys.exit(1)

    print(f"yapm upgraded: {APP_VERSION} -> {new_ver}")
    print("Restart yapm to use the new version.")

def info_package(pkg: str):
    idx = load_index()
    db = load_db()

    pkg_key = pkg

    print(f"Package: {pkg_key}")

    if pkg_key in db:
        print(f"Status: Installed (v{db[pkg_key].get('version', '0.0.0')}) [Format: {db[pkg_key].get('format', 'yapm').upper()}]")
        meta = db[pkg_key].get("metadata", {})
        if "description" in meta:
            print(f"Description: {meta['description']}")
        if "dependencies" in meta and meta["dependencies"]:
            print(f"Dependencies: {', '.join(meta['dependencies'])}")
    else:
        print("Status: Not installed")

    if pkg_key in idx.get("packages", {}):
        formats_entry = idx["packages"][pkg_key]
        for fmt_name in ("yapm", "arch", "deb"):
            entry = formats_entry.get(fmt_name)
            if not entry:
                continue
            print(f"[{fmt_name.upper()} format]")
            if "versions" in entry:
                print(f"  Available versions: {', '.join(sorted(entry['versions'].keys()))}")
                print(f"  Latest: {entry.get('latest', 'unknown')}")
                ver_info = entry["versions"].get(entry.get("latest", ""), {})
                if "dependencies" in ver_info and ver_info["dependencies"]:
                    print(f"  Dependencies: {', '.join(ver_info['dependencies'])}")
            else:
                print(f"  Remote Version: {entry.get('version', '0.0.0')}")
                if "dependencies" in entry and entry["dependencies"]:
                    print(f"  Remote Dependencies: {', '.join(entry['dependencies'])}")
    else:
        print("Not found in remote index.")

def search_package(term: str):
    idx = load_index()
    db = load_db()
    found = False
    term_lower = term.lower()

    for pkg_key, formats_entry in idx.get("packages", {}).items():
        display = pkg_key
        display_lower = display.lower()

        for fmt_name in ("yapm", "arch", "deb"):
            entry = formats_entry.get(fmt_name)
            if not entry:
                continue
            if "versions" in entry:
                latest_ver = entry.get("latest", "")
                ver_info = entry["versions"].get(latest_ver, {})
            else:
                latest_ver = entry.get("version", "0.0.0")
                ver_info = entry
            desc = ver_info.get("description", "").lower()

            if term_lower in display_lower or term_lower in desc:
                installed_mark = ""
                if pkg_key in db:
                    local_ver = db[pkg_key].get("version", "?")
                    installed_mark = f" {Color.GREEN}[installed {local_ver}]{Color.RESET}"
                print(f"{display} (v{latest_ver}) - {ver_info.get('description', 'No description')}{installed_mark}")
                found = True
                break

    if not found:
        print("No matches found in local index. Try 'yapm update' first.")

# ============================================================
# QOL COMMANDS
# ============================================================

def outdated_packages():
    """Show installed packages that have newer versions available."""
    db = load_db()
    idx = load_index()
    found = False

    for pkg, info in db.items():
        local_ver = info.get("version", "0.0.0")
        installed_fmt = info.get("format", "yapm")
        formats_entry = idx.get("packages", {}).get(pkg)
        if not formats_entry:
            continue
        remote_info = formats_entry.get(installed_fmt)
        if not remote_info:
            continue
        if "versions" in remote_info:
            remote_ver = remote_info.get("latest", "0.0.0")
        else:
            remote_ver = remote_info.get("version", "0.0.0")
        if _parse_ver(remote_ver) > _parse_ver(local_ver):
            print(f"  {pkg} {Color.YELLOW}{local_ver}{Color.RESET} -> {Color.GREEN}{remote_ver}{Color.RESET}")
            found = True

    if not found:
        print("Everything is up to date.")


def list_files(pkg: str):
    """List files installed by a package."""
    db = load_db()
    if pkg not in db:
        print(f"Package '{pkg}' is not installed.")
        sys.exit(1)

    info = db[pkg]
    file_list = info.get("files", [])
    if file_list:
        for f in sorted(file_list):
            print(f)
    else:
        target = Path(info.get("path", ""))
        if target.exists():
            for root, dirs, files in os.walk(target):
                for f in sorted(files):
                    print(str(Path(root).joinpath(f).relative_to(target)))
        else:
            print("No files found.")


def why_package(pkg: str):
    """Show which installed packages depend on the given package."""
    db = load_db()
    if pkg not in db:
        print(f"Package '{pkg}' is not installed.")
        sys.exit(1)

    dependents = []
    for name, info in db.items():
        if name == pkg:
            continue
        deps = info.get("dependencies", [])
        if pkg in deps:
            dependents.append(name)

    if dependents:
        print(f"Package '{pkg}' is required by:")
        for d in sorted(dependents):
            print(f"  {d}")
    else:
        print(f"No installed packages depend on '{pkg}'.")


def clean_cache():
    """Remove all cached index and download files."""
    if not CACHE_DIR.exists():
        print("Cache is already clean.")
        return
    size = sum(f.stat().st_size for f in CACHE_DIR.rglob("*") if f.is_file())
    shutil.rmtree(CACHE_DIR)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Cache cleaned ({size / 1024:.1f} KB freed).")


def mirror_test():
    """Test all mirrors without removing unreachable ones."""
    config = load_config()
    print("Testing mirrors...")
    for m in config["mirrors"]:
        ok = validate_mirror(m["url"])
        status = f"{Color.GREEN}OK{Color.RESET}" if ok else f"{Color.RED}FAILED{Color.RESET}"
        print(f"  {m['url']} -> {status}")


def repair_package(pkg: str):
    """Re-create missing symlinks for an installed package."""
    db = load_db()
    if pkg not in db:
        print(f"Package '{pkg}' is not installed.")
        sys.exit(1)

    info = db[pkg]
    target = Path(info.get("path", ""))
    if not target.exists():
        print(f"Error: package directory {target} does not exist.")
        sys.exit(1)

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    fixed = 0
    bin_source_dirs = [target / "src", target / "usr" / "bin", target / "bin"]
    for src_dir in bin_source_dirs:
        if src_dir.exists() and src_dir.is_dir():
            for item in src_dir.iterdir():
                if item.is_file() and os.access(item, os.X_OK):
                    dest = BIN_DIR / item.name
                    symlink_src = ROOT_DIR / item.relative_to(ROOT_DIR)
                    if not dest.exists():
                        os.symlink(symlink_src, dest)
                        print(f"  Created symlink {item.name} -> {dest}")
                        fixed += 1

    if fixed:
        print(f"Repaired {fixed} missing symlinks for {pkg}.")
    else:
        print(f"No missing symlinks for {pkg}.")


def build_package(directory: str):
    """Build a .yapm package from a source directory."""
    source_dir = Path(directory)
    if not source_dir.exists() or not source_dir.is_dir():
        print(f"Error: Directory '{directory}' does not exist.")
        sys.exit(1)

    yapm_data_path = source_dir / "yapm.data"
    if not yapm_data_path.exists():
        print(f"Error: No yapm.data found in '{directory}'. Cannot build package.")
        sys.exit(1)

    with open(yapm_data_path) as f:
        y_data = parse_yapm_data(f.read())

    meta = y_data.get("METADATA", {})
    required = ["name", "version", "description", "author", "license"]
    missing = [f for f in required if not meta.get(f)]
    if missing:
        print(f"Error: yapm.data is missing required fields: {', '.join(missing)}")
        sys.exit(1)

    name = meta["name"]
    version = meta["version"]

    out_file = f"{name}-{version}.yapm"
    print(f"Building {out_file} from {directory}...")

    with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
        with tarfile.open(tmp.name, 'w') as tar:
            for root, dirs, files in os.walk(source_dir):
                for file in files:
                    file_path = Path(root) / file
                    arcname = file_path.relative_to(source_dir)
                    tar.add(file_path, arcname=arcname)

        subprocess.run(["zstd", "-f", "-19", tmp.name, "-o", out_file], check=True, stdout=subprocess.DEVNULL)

    sudo_uid = os.environ.get('SUDO_UID')
    sudo_gid = os.environ.get('SUDO_GID')
    if sudo_uid and sudo_gid:
        try:
            os.chown(out_file, int(sudo_uid), int(sudo_gid))
        except Exception:
            pass

    print(f"Success! Package built: {out_file}")

# ============================================================
# CONFIG COMMAND
# ============================================================

HIDDEN_FLAGS = {"yapm.yapm"}

YAPM_CONTRIB_REPO = "commodorial64/yapm-contrib"

def submit_package(package_path: str):
    """Submit a .yapm package to the yapm-contrib repo via a GitHub PR."""
    pkg = Path(package_path).resolve()
    if not pkg.exists():
        print(f"Error: {pkg} does not exist.")
        sys.exit(1)
    if not pkg.name.endswith(".yapm"):
        print(f"Error: {pkg.name} is not a .yapm file.")
        sys.exit(1)

    # validate it's a valid tar.zst
    with open(pkg, "rb") as f:
        magic = f.read(4)
    if magic != b'\x28\xb5\x2f\xfd':
        print(f"Error: {pkg.name} is not a valid tar.zst archive.")
        sys.exit(1)

    # check yapm.data exists inside
    try:
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["zstd", "-d", "-f", str(pkg), "-o", f"{td}/pkg.tar"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with tarfile.open(f"{td}/pkg.tar") as tar:
                names = [n.split("/")[-1] for n in tar.getnames()]
                if "yapm.data" not in names:
                    print(f"Error: {pkg.name} is missing yapm.data.")
                    sys.exit(1)
    except Exception as e:
        print(f"Error validating package: {e}")
        sys.exit(1)

    # check gh is available and authenticated
    if not shutil.which("gh"):
        print("Error: 'gh' CLI is required. Install it from https://cli.github.com/")
        sys.exit(1)
    try:
        subprocess.run(["gh", "auth", "status"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        print("Error: not logged into GitHub. Run 'gh auth login' first.")
        sys.exit(1)

    branch = f"submit-{pkg.stem}"
    tmpdir = tempfile.mkdtemp()

    try:
        print("Forking yapm-contrib...")
        subprocess.run(["gh", "repo", "fork", YAPM_CONTRIB_REPO, "--clone=false"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        # get fork owner
        result = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                                capture_output=True, text=True, check=True)
        fork_owner = result.stdout.strip()
        fork_url = f"https://github.com/{fork_owner}/yapm-contrib.git"

        print(f"Cloning fork ({fork_owner}/yapm-contrib)...")
        subprocess.run(["git", "clone", fork_url, tmpdir], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        subprocess.run(["git", "checkout", "-b", branch], cwd=tmpdir, check=True,
                       stdout=subprocess.DEVNULL)

        shutil.copy2(pkg, tmpdir)
        subprocess.run(["git", "add", pkg.name], cwd=tmpdir, check=True)
        subprocess.run(["git", "commit", "-m", f"add {pkg.stem}"], cwd=tmpdir, check=True)

        print(f"Pushing branch '{branch}'...")
        subprocess.run(["git", "push", "-u", "origin", branch], cwd=tmpdir, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        print("Opening PR...")
        result = subprocess.run(
            ["gh", "pr", "create",
             "--repo", YAPM_CONTRIB_REPO,
             "--title", f"add {pkg.stem}",
             "--body", f"Submit `{pkg.name}` to yapm-contrib."],
            cwd=tmpdir, capture_output=True, text=True, check=True
        )
        print(result.stdout.strip())

    except subprocess.CalledProcessError as e:
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def yapm_config_list():
    conf = load_yapm_conf()
    for flag in KNOWN_FLAGS:
        if flag in HIDDEN_FLAGS:
            continue
        state = "on" if conf.get(flag, str(KNOWN_FLAGS[flag]).lower()) == "true" else "off"
        print(f"  {flag} = {state}  (beta)")

def yapm_config_enable(flag: str):
    if flag in HIDDEN_FLAGS:
        print(f"unknown flag: {flag}")
        sys.exit(1)
    if flag not in KNOWN_FLAGS:
        print(f"unknown flag: {flag}")
        sys.exit(1)
    conf = load_yapm_conf()
    conf[flag] = "true"
    save_yapm_conf(conf)
    print(f"  {flag} = on")

def yapm_config_disable(flag: str):
    if flag in HIDDEN_FLAGS:
        print(f"unknown flag: {flag}")
        sys.exit(1)
    if flag not in KNOWN_FLAGS:
        print(f"unknown flag: {flag}")
        sys.exit(1)
    conf = load_yapm_conf()
    conf[flag] = "false"
    save_yapm_conf(conf)
    print(f"  {flag} = off")

# ============================================================
# CHAOS MODE
# ============================================================

CHAOS_THROWBACKS = [
    "yapm? more like yap",
    "WARNING: yapm may conflict with your will to live",
    "have you considered just using pacman",
    "have you considered just using apt",
    "have you considered just using dnf",
    "don't install it, you don't need it!",
    "still here!",
    "extracting... (this is the part where we wait)",
    "you're doing great by the way",
    "what even IS a package really",
]

CHAOS_WRONG_NAMES = {
    "linux": "linus",
    "grub": "grub2",
    "bash": "baxh",
    "systemd": "systemd... (ugh)",
    "python": "pythong",
    "python3": "pythong3",
}

def chaos_interrupt():
    if not config_flag("yapm.yapm"):
        return
    if random.random() < 0.3:
        print(random.choice(CHAOS_THROWBACKS), file=sys.stderr)

def chaos_delay(seconds=0.5):
    time.sleep(seconds)

def chaos_spinner(seconds=3):
    spinner = itertools.cycle(["|", "/", "-", "\\"])
    for _ in range(int(seconds * 10)):
        sys.stdout.write(f"\rthinking... {next(spinner)}")
        sys.stdout.flush()
        time.sleep(0.1)
    sys.stdout.write("\r" + " " * 20 + "\r")
    sys.stdout.flush()

def chaos_confirm(times=3):
    for i in range(times):
        try:
            choice = input(f"are you sure? (type 'yes' to confirm) [{i+1}/{times}] ").strip().lower()
            if choice != "yes":
                print("Aborted.")
                sys.exit(0)
        except (ValueError, EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)

def chaos_wrong_name(name: str) -> str:
    if config_flag("yapm.yapm"):
        base = name.split("-")[0].split(".")[0].lower()
        if base in CHAOS_WRONG_NAMES:
            return CHAOS_WRONG_NAMES[base]
    return name

def chaos_yap_on_extract(filename: str):
    if not config_flag("yapm.yapm"):
        return
    comments = [
        "ooh this one's a big one",
        "never heard of THIS library before",
        f"extracting {filename}... classic",
        "wow another .so file who would have thought",
    ]
    if random.random() < 0.15:
        print(f"  > {random.choice(comments)}")

def chaos_opinion(pkg: str):
    if not config_flag("yapm.yapm"):
        return
    base = pkg.split("-")[0].split(".")[0].lower()
    opinions = {
        "networkmanager": "networkmanager? bold choice",
        "network-manager": "networkmanager? bold choice",
        "vim": "you're installing vim? interesting life decision",
        "linux": "oh linux, a personal favorite",
    }
    if base in opinions:
        print(f"  > {opinions[base]}")

def chaos_post_operation():
    if not config_flag("yapm.yapm"):
        return
    print("done! ...or did i?")
    time.sleep(1)
    print("yes i did :)")

# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        prog="yapm",
        description="yapm — Yet Another Package Manager\n"
                    "Supports native .yapm packages as well as .deb (Debian/Ubuntu) and\n"
                    "Arch Linux packages (.pkg.tar.zst) via upstream mirrors.\n\n"
                    "Run 'yapm update' first to build the local package index.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-f", "--format",
        choices=["yapm", "deb", "arch"],
        default="yapm",
        metavar="FORMAT",
        help="Override the package format for local installs (yapm | deb | arch). "
             "Auto-detected from file extension when installing a local file.",
    )

    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    # install
    p_install = sub.add_parser(
        "install",
        help="Install a package from a mirror or a local file",
        description="Download and install a package by name from the configured mirrors, "
                    "or install directly from a local .yapm / .deb / .pkg.tar.zst file.\n\n"
                    "Dependencies listed in yapm.data are resolved and installed first.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_install.add_argument("package", metavar="PACKAGE", nargs="+",
                           help="Package name(s) (looked up in index) or path(s) to local package file(s)")
    p_install.add_argument("-m", "--mirror", type=int, default=None, metavar="N",
                           help="Pin install to a specific mirror by its index number from "
                                "'yapm mirror list' (e.g. -m 5 for mirror #5)")
    p_install.add_argument("-r", "--root", type=str, default=None, metavar="PATH",
                           help="Install to a different root directory (requires yapm.insroot)")
    p_install.add_argument("-y", "--noconfirm", action="store_true",
                           help="Skip confirmation prompt")
    p_install.add_argument("-n", "--dry-run", action="store_true",
                           help="Show what would be installed without making changes")

    # remove
    p_remove = sub.add_parser(
        "remove",
        help="Remove an installed package",
        description="Uninstall a package, removing its files and any bin symlinks. "
                    "Does NOT automatically remove dependencies.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_remove.add_argument("package", metavar="PACKAGE",
                          help="Name of the installed package to remove")
    p_remove.add_argument("-y", "--noconfirm", action="store_true",
                          help="Skip confirmation prompt")

    # list
    p_list = sub.add_parser(
        "list",
        help="List all installed packages",
        description="Print every installed package along with its version and format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_list.add_argument("--outdated", action="store_true",
                        help="Only show packages with newer versions available")
    p_list.add_argument("--json", action="store_true",
                        help="Output as JSON")

    # info
    p_info = sub.add_parser(
        "info",
        help="Show details about a package",
        description="Display local install status and remote index information for a package, "
                    "including version, description, and dependencies.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_info.add_argument("package", metavar="PACKAGE",
                        help="Package name to inspect")

    # search
    p_search = sub.add_parser(
        "search",
        help="Search the local package index",
        description="Search package names and descriptions in the cached index.\n"
                    "Run 'yapm update' first to ensure the index is up to date.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_search.add_argument("term", metavar="TERM",
                          help="Search term (matched against name and description)")

    # update
    sub.add_parser(
        "update",
        help="Refresh the package index from all mirrors",
        description="Fetch and merge package lists from all configured mirrors into a local\n"
                    "index cache. Supports Debian/Ubuntu (Packages.gz), Arch (core.db),\n"
                    "and native YAPM (index.json) mirror formats.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # upgrade
    p_upgrade = sub.add_parser(
        "upgrade",
        help="Upgrade all installed packages to their latest versions",
        description="Compare installed package versions against the cached index and\n"
                    "re-download any packages where a newer version is available.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_upgrade.add_argument("-y", "--refresh", action="store_true",
                           help="Refresh the package index before upgrading")
    p_upgrade.add_argument("-n", "--dry-run", action="store_true",
                           help="Show what would be upgraded without making changes")
    # fetch
    p_fetch = sub.add_parser(
        "fetch",
        help="Update yapm itself.",
        description="Download and install the latest version of yapm from the github repo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_fetch.add_argument(
        "--force", action="store_true",
        help="Replace the binary even if the downloaded version is the same or older",
    )
        

    # version
    sub.add_parser(
        "version",
        help="Print yapm version information",
        description="Print the yapm application version and the config schema version.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # uninstall
    sub.add_parser(
        "uninstall",
        help="Uninstall yapm itself from the system",
        description="Remove the yapm binary and all of its data directories\n"
                    "(/etc/yapm and /var/lib/yapm). This does NOT remove packages\n"
                    "that were installed by yapm.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # init (riot only)
    p_init = sub.add_parser(
        "init",
        help="Bootstrap the system by installing bash (riot mode)",
        description="Ensure bash is installed on the system. Intended for first-run\n"
                    "bootstrapping on Riot live ISOs. Requires yapm.riot to be enabled.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_init.add_argument("-r", "--root", type=str, default=None, metavar="PATH",
                        help="Install to a different root directory (requires yapm.insroot)")

    # build
    p_build = sub.add_parser(
        "build",
        help="Build a .yapm package from a source directory",
        description="Package a directory into a distributable .yapm file (tar.zst format).\n"
                    "The directory must contain a yapm.data manifest with at least:\n"
                    "  [METADATA]  name = \"pkg\"  version = \"1.0.0\"\n"
                    "The output file is written to the current working directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_build.add_argument("directory", metavar="DIR",
                         help="Path to the directory containing package files and yapm.data")

    # submit
    p_submit = sub.add_parser(
        "submit",
        help="Submit a .yapm package to yapm-contrib",
        description="Fork yapm-contrib, push your .yapm file, and open a pull request.\n"
                    "Requires 'gh' CLI authenticated with GitHub.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_submit.add_argument("package", metavar="PACKAGE",
                          help="Path to the .yapm file to submit")

    # outdated
    sub.add_parser(
        "outdated",
        help="Show installed packages with newer versions available",
        description="Compare installed package versions against the index and\n"
                    "print any that have a newer version in the configured mirrors.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # files
    p_files = sub.add_parser(
        "files",
        help="List files installed by a package",
        description="Print all files that belong to the given installed package.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_files.add_argument("package", metavar="PACKAGE",
                         help="Name of the installed package")

    # why
    p_why = sub.add_parser(
        "why",
        help="Show which packages depend on a given package",
        description="List all installed packages that list the given package\n"
                    "as a dependency.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_why.add_argument("package", metavar="PACKAGE",
                       help="Package name to check dependencies for")

    # clean
    sub.add_parser(
        "clean",
        help="Remove all cached index and download files",
        description="Delete everything under /var/lib/yapm/cache/ to free space.\n"
                    "The cache will be rebuilt automatically on the next 'yapm update'.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # repair
    p_repair = sub.add_parser(
        "repair",
        help="Re-create missing symlinks for an installed package",
        description="Scan the package's bin directories and re-create any\n"
                    "missing symlinks in /usr/local/bin.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_repair.add_argument("package", metavar="PACKAGE",
                          help="Name of the installed package to repair")

    # config (hidden)
    p_config = sub.add_parser("config", help=argparse.SUPPRESS)
    config_sub = p_config.add_subparsers(dest="config_cmd", required=True, metavar="<action>")

    p_config_list = config_sub.add_parser("list", help=argparse.SUPPRESS)
    p_config_enable = config_sub.add_parser("enable", help=argparse.SUPPRESS)
    p_config_enable.add_argument("flag", metavar="FLAG", help=argparse.SUPPRESS)
    p_config_disable = config_sub.add_parser("disable", help=argparse.SUPPRESS)
    p_config_disable.add_argument("flag", metavar="FLAG", help=argparse.SUPPRESS)

    # mirror
    p_mirror = sub.add_parser(
        "mirror",
        help="Manage package mirrors",
        description="Add, remove, list, or validate the package mirrors that yapm uses\n"
                    "when running 'yapm update' and 'yapm install'.\n\n"
                    "Mirrors are sorted by priority; lower numbers are tried first.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mirror_sub = p_mirror.add_subparsers(dest="mirror_cmd", required=True, metavar="<subcommand>")

    m_add = mirror_sub.add_parser(
        "add",
        help="Add a new mirror",
        description="Register a new mirror URL. Use -p to set its priority "
                    "(lower = higher precedence).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    m_add.add_argument("url", metavar="URL", help="Full URL of the mirror (e.g. https://example.com/yapm/)")
    m_add.add_argument("-p", "--priority", type=int, default=10, metavar="N",
                       help="Mirror priority — lower numbers are tried first (default: 10)")

    mirror_sub.add_parser(
        "list",
        help="List all configured mirrors",
        description="Print all registered mirrors in priority order.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    m_remove = mirror_sub.add_parser(
        "remove",
        help="Remove a mirror by URL",
        description="Unregister a mirror. Use 'yapm mirror list' to find the exact URL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    m_remove.add_argument("url", metavar="URL", help="URL of the mirror to remove")

    mirror_sub.add_parser(
        "sync",
        help="Test all mirrors and remove unreachable ones",
        description="Send a HEAD request to each mirror and remove any that fail to respond. "
                    "Useful after adding new mirrors or if 'yapm update' is slow.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    mirror_sub.add_parser(
        "test",
        help="Test all mirrors without removing unreachable ones",
        description="Send a HEAD request to each mirror and report success/failure.\n"
                    "Unlike 'yapm mirror sync', this does NOT remove any mirrors.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    args = parser.parse_args()

    if args.command != "submit":
        require_root()
        ensure_dirs()

    try:
        if config_flag("yapm.yapm"):
            try:
                _dispatch(args)
            except SystemExit:
                print("something may or may not have gone wrong. who can say really")
                sys.exit(0)
        else:
            _dispatch(args)
    finally:
        if LOCK_FILE.exists():
            try:
                LOCK_FILE.unlink()
            except OSError:
                pass

def _dispatch(args):
    if args.command == "install":
        install_package(args.package, args.format, mirror_index=args.mirror, root=args.root, noconfirm=args.noconfirm, dry_run=args.dry_run)
    elif args.command == "remove":
        remove_package(args.package, noconfirm=args.noconfirm)
    elif args.command == "list":
        list_installed(outdated=args.outdated, json_output=args.json)
    elif args.command == "info":
        info_package(args.package)
    elif args.command == "search":
        search_package(args.term)
    elif args.command == "update":
        update_index()
    elif args.command == "upgrade":
        upgrade_packages(refresh=args.refresh, dry_run=args.dry_run)
    elif args.command == "build":
        build_package(args.directory)
    elif args.command == "submit":
        submit_package(args.package)
    elif args.command == "outdated":
        outdated_packages()
    elif args.command == "files":
        list_files(args.package)
    elif args.command == "why":
        why_package(args.package)
    elif args.command == "clean":
        clean_cache()
    elif args.command == "repair":
        repair_package(args.package)
    elif args.command == "version":
        ver = APP_VERSION
        if config_flag("yapm.riot"):
            ver = f"{APP_VERSION}-riot"
        print(f"yapm version {ver}")
        if not config_flag("yapm.riot"):
            print("riot features available via yapm.conf")
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE) as f:
                cv = json.load(f).get("version", "unknown")
            print(f"config version {cv}")
    elif args.command == "uninstall":
        uninstall_yapm()
    elif args.command == "init":
        init_package(root=args.root)
    elif args.command == "fetch":
        update_yapm(force=args.force)
    elif args.command == "mirror":
        if args.mirror_cmd == "add":
            mirror_add(args.url, args.priority)
        elif args.mirror_cmd == "remove":
            mirror_remove(args.url)
        elif args.mirror_cmd == "sync":
            mirror_refresh()
        elif args.mirror_cmd == "test":
            mirror_test()
        elif args.mirror_cmd == "list":
            mirror_list()

    elif args.command == "config":
        if args.config_cmd == "list":
            yapm_config_list()
        elif args.config_cmd == "enable":
            yapm_config_enable(args.flag)
        elif args.config_cmd == "disable":
            yapm_config_disable(args.flag)

if __name__ == "__main__":
    main()