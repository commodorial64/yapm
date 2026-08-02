# fetch packages + resolve deps
import io
import re
import sys
import zipfile
from typing import Dict, List, Optional

from .config import resolve_hall, sorted_mirrors
from .download import download, is_valid_zip
from .index import get_pkg_info, load_index
from .paths import VIRTUAL_PROVIDERS
from .utils import normalize, pkg_basename

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

def fetch_package(pkg: str, mirror_url: Optional[str] = None, version: Optional[str] = None, arch_mode: bool = False, hall: Optional[str] = None) -> Optional[bytes]:
    idx = load_index()
    packages = idx.get("packages", {})
    pkg_entry = packages.get(pkg, {})

    # pinned mirror — find the format that belongs to it
    if mirror_url and pkg_entry:
        matched_fmt = None
        for fmt in ("yapm", "arch", "deb", "nix"):
            sub = pkg_entry.get(fmt)
            if sub and sub.get("mirror", "") == mirror_url:
                matched_fmt = fmt
                break
        if matched_fmt:
            pkg_info = dict(pkg_entry[matched_fmt])
            if "versions" in pkg_info:
                ver = version or pkg_info.get("latest", "0.0.0")
                ver_info = pkg_info["versions"].get(ver, {})
                pkg_info = dict(ver_info)
                pkg_info["version"] = ver
                pkg_info["format"] = matched_fmt
                pkg_info["mirror"] = mirror_url
            else:
                pkg_info["format"] = matched_fmt
        else:
            pkg_info = get_pkg_info(idx, pkg, version, arch_mode=arch_mode)
    else:
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

    for mirror in (resolve_hall(hall) if hall else sorted_mirrors()):
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
