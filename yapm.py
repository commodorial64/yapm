#!/usr/bin/env python3

import argparse
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import urllib.error
import zipfile
import subprocess
import gzip
import tarfile
import io
from pathlib import Path
from typing import List, Dict, Optional

VIRTUAL_PROVIDERS = frozenset({"sh", "awk", "perl", "python", "ruby"})

# ============================================================
# CONFIGURATION PATHS
# ============================================================

APP_VERSION = "0.3.1-alpha"
CURRENT_VERSION = 1  # Config version

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

def set_root_dir(root_str: str):
    global ROOT_DIR, INSTALL_DIR, DB_FILE, BIN_DIR
    ROOT_DIR = Path(root_str).resolve()
    if str(ROOT_DIR) == "/":
        return
    INSTALL_DIR = ROOT_DIR / "var/lib/yapm/packages"
    DB_FILE = ROOT_DIR / "var/lib/yapm/installed.json"
    BIN_DIR = ROOT_DIR / "usr/local/bin"

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
        {"url": "https://mirrors.fedoraproject.org/", "priority": 40},
        {"url": "https://yapm.pages.dev/", "priority": 50}
    ]
}

def require_root():
    """Abort immediately if not running as root."""
    if os.getuid() != 0:
        print("Error: yapm must be run with sudo.")
        print("  Try: sudo yapm <command>")
        sys.exit(1)

# ============================================================
# INITIALIZATION
# ============================================================

def ensure_dirs():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)

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
    with open(CONFIG_FILE) as f:
        return json.load(f)

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
        save_db(new_db)
    return new_db

def save_db(db: Dict):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=4)

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
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status < 400
    except Exception:
        return False

def download(url: str, desc: str = "Downloading", silent_errors: bool = False) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'yapm/1.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            size = int(response.headers.get('content-length', 0))
            data = b""
            chunk_size = 8192
            downloaded = 0
            while True:
                chunk = response.read(chunk_size)
                if not chunk: break
                data += chunk
                downloaded += len(chunk)
                if size:
                    percent = int(downloaded * 100 / size)
                    cols, _ = shutil.get_terminal_size((80, 20))
                    bar_len = min(40, cols - len(desc) - 20)
                    filled = int(bar_len * downloaded / size)
                    bar = "█" * filled + "-" * (bar_len - filled)
                    print(f"\r{desc}: [{bar}] {percent}% ({downloaded}/{size} bytes)", end="", flush=True)
            print()
            return data
    except urllib.error.HTTPError as e:
        if not silent_errors or e.code != 404:
            print(f"\nError downloading {url}: {e}")
        return None
    except Exception as e:
        if not silent_errors:
            print(f"\nError downloading {url}: {e}")
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

    with open(archive_path, "rb") as f:
        magic = f.read(4)
        
    if magic[:2] == b'PK':
        with zipfile.ZipFile(archive_path) as z:
            for member in z.infolist():
                member_path = (target / member.filename).resolve()
                if not str(member_path).startswith(str(target.resolve())):
                    raise Exception("Unsafe zip detected")
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
                    if not str(member_path).startswith(str(target.resolve())):
                        raise Exception("Unsafe tar detected")
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
        except Exception:
            pass
    return result

def parse_yapm_data(content: str) -> dict:
    data = {"METADATA": {}, "CONTENT": {}, "FILES": {}}
    current_section = None
    
    import re
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
                import ast
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
            subprocess.run(["ar", "x", "pkg.deb"], cwd=td, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for f in Path(td).iterdir():
                if f.name.startswith("data.tar"):
                    print("  Extracting DEB data payload...")
                    subprocess.run(["tar", "-xf", f.name, "-C", str(target)], cwd=td, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    break
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
            subprocess.run(["tar", "--use-compress-program=zstd", "-xf", "pkg.tar.zst", "-C", str(target)], cwd=td, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"Error extracting Arch package: {e}")
            raise

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
            subprocess.run(["arch-chroot", str(root), "bash", "-c", script], check=False)
        else:
            subprocess.run(["bash", "-c", script], check=False)
        tmp_hook.unlink(missing_ok=True)

# ============================================================
# PACKAGE LOGIC
# ============================================================

def parse_debian_index(mirror_url: str, merged_index: dict):
    dist = "jammy" if "ubuntu" in mirror_url else "bookworm"
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
    for repo in ("core", "extra"):
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
        if name in new_pkgs:
            continue
        # Convert old flat entries (e.g. {"version": …, "format": "deb"})
        # to the new nested-by-format format {fmt: entry_dict}.
        if any(fmt in v for fmt in ("yapm", "arch", "deb")):
            new_pkgs[name] = v          # already new format
        else:
            fmt = v.get("format", "yapm")
            new_pkgs[name] = {fmt: v}   # wrap old flat entry
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

def resolve_dependencies(pkg: str, idx: Dict, db: Dict, to_install: List[str], path: set, version: Optional[str] = None, arch_mode: bool = False):
    if pkg in to_install or pkg in db or pkg in VIRTUAL_PROVIDERS:
        return
    if pkg in path:
        print(f"Error: Circular dependency detected: {' -> '.join(path)} -> {pkg}")
        sys.exit(1)

    path.add(pkg)
    pkg_info = get_pkg_info(idx, pkg, version, arch_mode=arch_mode)
    if pkg_info:
        for dep in pkg_info.get("dependencies", []):
            import re
            if dep in VIRTUAL_PROVIDERS:
                continue
            if re.match(r'^lib.*\.so', dep) or re.search(r'\.so(\.[0-9]+)*$', dep):
                continue
            resolve_dependencies(dep, idx, db, to_install, path, arch_mode=arch_mode)
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

    target = INSTALL_DIR / pkg_name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    try:
        if fmt == "yapm":
            tmp = tempfile.NamedTemporaryFile(delete=False)
            tmp.write(data)
            tmp.close()
            safe_extract(Path(tmp.name), target)
            os.unlink(tmp.name)
        elif fmt == "deb":
            extract_deb(data, target)
        elif fmt == "arch":
            extract_arch(data, target)
    except Exception as e:
        print(f"Installation failed: {e}")
        sys.exit(1)

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    pkg_meta = {"version": "0.0.0", "dependencies": [], "format": fmt}

    yapm_data_path = target / "yapm.data"
    if yapm_data_path.exists():
        with open(yapm_data_path) as f:
            y_data = parse_yapm_data(f.read())
            
        # Metadata
        meta = y_data.get("METADATA", {})
        pkg_meta["version"] = meta.get("version", "0.0.0")
        if "description" in meta: pkg_meta["description"] = meta["description"]
        if "dependencies" in meta: pkg_meta["dependencies"] = meta["dependencies"]
        
        content_info = y_data.get("CONTENT", {})
        
        # BuildFile
        build_file = content_info.get("BuildFile")
        if build_file and (target / build_file).exists():
            print(f"  Running build script: {build_file}...")
            os.chmod(target / build_file, 0o755)
            subprocess.run([str(target / build_file)], cwd=target, check=True)
            
        # PreInstall
        pre_install = content_info.get("PreInstall")
        if pre_install and (target / pre_install).exists():
            print("  Running pre-install script...")
            os.chmod(target / pre_install, 0o755)
            subprocess.run([str(target / pre_install)], cwd=target, check=True)
            
        # File Mappings
        files_info = y_data.get("FILES", {})
        for src, dest in files_info.items():
            src_path = target / src
            dest_path = Path(dest)
            if src_path.exists():
                print(f"  Mapping file: {src} -> {dest}")
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                if dest_path.exists() or dest_path.is_symlink():
                    os.unlink(dest_path)
                shutil.copy2(src_path, dest_path)
                chaos_yap_on_extract(src)
                
        # RunFile
        run_file = content_info.get("RunFile")
        if run_file and (target / run_file).exists():
            dest = BIN_DIR / Path(run_file).name
            if dest.exists() or dest.is_symlink():
                os.unlink(dest)
            os.chmod(target / run_file, 0o755)
            os.symlink(Path("/") / (target / run_file).relative_to(ROOT_DIR), dest)
            print(f"  Linked executable {Path(run_file).name} -> {dest}")
            chaos_yap_on_extract(run_file)
            
        # PostInstall
        post_install = content_info.get("PostInstall")
        if post_install and (target / post_install).exists():
            print("  Running post-install script...")
            os.chmod(target / post_install, 0o755)
            subprocess.run([str(target / post_install)], cwd=target, check=True)
    else:
        # Fallback to simple extraction linking
        bin_source_dirs = [target / "src", target / "usr" / "bin", target / "bin"]
        for src_dir in bin_source_dirs:
            if src_dir.exists() and src_dir.is_dir():
                for item in src_dir.iterdir():
                    if item.is_file() and os.access(item, os.X_OK):
                        dest = BIN_DIR / item.name
                        if dest.exists() or dest.is_symlink():
                            os.unlink(dest)
                        os.symlink(Path("/") / item.relative_to(ROOT_DIR), dest)
                        print(f"  Linked {item.name} -> {dest}")

        metadata_path = target / "metadata.json"
        if metadata_path.exists():
            try:
                with open(metadata_path) as f:
                    pkg_meta.update(json.load(f))
            except Exception:
                pass

    db[pkg_name] = {
        "version": pkg_meta.get("version", "0.0.0"),
        "path": str(target),
        "dependencies": pkg_meta.get("dependencies", []),
        "format": fmt,
        "metadata": pkg_meta
    }

    save_db(db)

def install_package(packages: List[str], fmt: str, mirror_index: Optional[int] = None, root: Optional[str] = None, noconfirm: bool = False):
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
        resolve_dependencies(pkg_name, idx, db, to_install_merged, seen, version=pkg_version, arch_mode=arch_mode)

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

def remove_package(pkg: str):
    db = load_db()

    pkg_key = pkg

    if pkg_key not in db:
        print(f"Package '{pkg_key}' not installed.")
        return

    target = Path(db[pkg_key]["path"])
    bin_source_dirs = [target / "src", target / "usr" / "bin", target / "bin"]
    
    for src_dir in bin_source_dirs:
        if src_dir.exists() and src_dir.is_dir():
            for item in src_dir.iterdir():
                dest = BIN_DIR / item.name
                if dest.is_symlink() and str(dest.resolve()) == str(item.resolve()):
                    os.unlink(dest)
                    print(f"Removed link {dest}")

    shutil.rmtree(db[pkg_key]["path"], ignore_errors=True)
    del db[pkg_key]
    save_db(db)
    print(f"Removed {format_key(pkg_key)}.")

def upgrade_packages(refresh: bool = False):
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
        if remote_ver > local_ver:
            to_upgrade.append((pkg, remote_ver))

    if not to_upgrade:
        print("Everything is up to date.")
        return

    print("The following packages will be upgraded:")
    for pkg, ver in to_upgrade:
        print(f"  {pkg} ({db[pkg].get('version', '0.0.0')} -> {ver})")

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

def list_installed():
    db = load_db()
    if not db:
        print("No packages installed.")
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

YAPM_SOURCE_URL = "https://raw.githubusercontent.com/galaxyg144/yapm/main/yapm.py"

def update_yapm(force: bool = False):
    import re
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

    if not force and new_ver == APP_VERSION:
        print("yapm is already up to date.")
        return

    if not force and new_ver < APP_VERSION:
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
                print(f"{display} (v{latest_ver}) - {ver_info.get('description', 'No description')}")
                found = True
                break

    if not found:
        print("No matches found in local index. Try 'yapm update' first.")

def build_package(directory: str):
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
        
    name = y_data.get("METADATA", {}).get("name", source_dir.name)
    version = y_data.get("METADATA", {}).get("version", "0.0.0")
    
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

import random
import time

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
    import itertools
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

    # list
    sub.add_parser(
        "list",
        help="List all installed packages",
        description="Print every installed package along with its version and format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

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

    # build
    p_build = sub.add_parser(
        "build",
        help="Build a .yapm package from a source directory",
        description="Package a directory into a distributable .yapm file (ZIP format).\n"
                    "The directory must contain a yapm.data manifest with at least:\n"
                    "  [METADATA]  name = \"pkg\"  version = \"1.0.0\"\n"
                    "The output file is written to the current working directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_build.add_argument("directory", metavar="DIR",
                         help="Path to the directory containing package files and yapm.data")

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

    args = parser.parse_args()

    require_root()
    ensure_dirs()

    if config_flag("yapm.yapm"):
        try:
            _dispatch(args)
        except SystemExit:
            print("something may or may not have gone wrong. who can say really")
            sys.exit(0)
    else:
        _dispatch(args)

def _dispatch(args):
    if args.command == "install":
        install_package(args.package, args.format, mirror_index=args.mirror, root=args.root, noconfirm=args.noconfirm)
    elif args.command == "remove":
        remove_package(args.package)
    elif args.command == "list":
        list_installed()
    elif args.command == "info":
        info_package(args.package)
    elif args.command == "search":
        search_package(args.term)
    elif args.command == "update":
        update_index()
    elif args.command == "upgrade":
        upgrade_packages(refresh=args.refresh)
    elif args.command == "build":
        build_package(args.directory)
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
    elif args.command == "fetch":
        update_yapm(force=args.force)
    elif args.command == "mirror":
        if args.mirror_cmd == "add":
            mirror_add(args.url, args.priority)
        elif args.mirror_cmd == "remove":
            mirror_remove(args.url)
        elif args.mirror_cmd == "sync":
            mirror_refresh()
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