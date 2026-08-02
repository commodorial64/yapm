# package index
import base64
import gzip
import io
import json
import shutil
import tarfile
import time
import urllib.request
from typing import Dict, Optional

from .color import Color, _action, _ok
from .config import config_flag, resolve_hall, sorted_mirrors
from .download import download
from .paths import INDEX_FILE, _deb_arch, _host_arch
from .utils import normalize

_UBUNTU_DISTROS = ["noble", "jammy", "focal", "bionic"]
_DEBIAN_DISTROS = ["trixie", "bookworm", "bullseye", "buster"]

def _detect_deb_distro(mirror_url: str) -> str:
    if "ubuntu" in mirror_url:
        for d in _UBUNTU_DISTROS:
            return d  # newest first
    for d in _DEBIAN_DISTROS:
        return d
    return "bookworm"

_NIX_SEARCH_URL = "https://search.nixos.org/backend/latest-44-nixos-unstable/_search"
_NIX_AUTH = base64.b64encode(b"aWVSALXpZv:X8gPHnzL52wFEekuxsfQ9cSh").decode()

def _nix_available():
    return shutil.which("nix-env") is not None

def parse_debian_index(mirror_url: str, merged_index: dict):
    dist = _detect_deb_distro(mirror_url)
    url = normalize(mirror_url) + f"dists/{dist}/main/binary-{_deb_arch()}/Packages.gz"
    data = download(url, desc=f"Fetching Debian index from {mirror_url}")
    if not data: return

    try:
        print("  Parsing Debian Packages.gz...")
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            content = gz.read().decode('utf-8', errors='ignore')

        current_pkg = {}
        depends_continuation = False
        for line in content.splitlines():
            if line.startswith(" ") or line.startswith("\t"):
                # continuation line (multi-line Depends)
                if depends_continuation and current_pkg is not None:
                    current_pkg.setdefault("depends_raw", []).append(line.strip())
                continue

            depends_continuation = False

            if not line.strip():
                if current_pkg and "name" in current_pkg:
                    name = current_pkg["name"]
                    deps = []
                    for dep_str in current_pkg.get("depends_raw", []):
                        for part in dep_str.split(","):
                            # first alternative, drop version constraints
                            pkg_name = part.split("|")[0].strip().split("(")[0].strip()
                            if pkg_name and not pkg_name.startswith("<") and pkg_name not in ("preinst", "postinst", "prerm", "postrm", "dpkg"):
                                deps.append(pkg_name)
                    merged_index["packages"].setdefault(name, {})["deb"] = {
                        "version": current_pkg.get("version", "0.0.0"),
                        "mirror": mirror_url,
                        "format": "deb",
                        "download_path": current_pkg.get("filename", ""),
                        "dependencies": deps,
                    }
                current_pkg = {}
                continue

            if line.startswith("Package: "):
                current_pkg = {"depends_raw": []}
                current_pkg["name"] = line.split(":", 1)[1].strip()
            elif line.startswith("Version: "): current_pkg["version"] = line.split(":", 1)[1].strip()
            elif line.startswith("Filename: "): current_pkg["filename"] = line.split(":", 1)[1].strip()
            elif line.startswith("Depends: "):
                current_pkg.setdefault("depends_raw", []).append(line.split(":", 1)[1].strip())
                depends_continuation = True
            elif line.startswith("Pre-Depends: "):
                current_pkg.setdefault("depends_raw", []).append(line.split(":", 1)[1].strip())
                depends_continuation = True
    except Exception as e:
        print(f"Error parsing Debian index: {e}")

def parse_arch_index(mirror_url: str, merged_index: dict):
    for repo in ("core", "extra"):
        url = normalize(mirror_url) + f"{repo}/os/{_host_arch()}/{repo}.db"
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
                            name, version, arch = "", "", _host_arch()
                            dependencies = []
                            provides = []
                            groups = []
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
                                elif line == "%PROVIDES%":
                                    j = i + 1
                                    while j < len(lines) and lines[j] and not lines[j].startswith("%"):
                                        prov = lines[j]
                                        for char in ('<', '>', '='):
                                            prov = prov.split(char)[0]
                                        provides.append(prov)
                                        j += 1
                                elif line == "%GROUPS%":
                                    j = i + 1
                                    while j < len(lines) and lines[j] and not lines[j].startswith("%"):
                                        groups.append(lines[j])
                                        j += 1

                            if name:
                                # don't overwrite a higher-priority repo entry
                                merged_index["packages"].setdefault(name, {}).setdefault("arch", {
                                    "version": version,
                                    "mirror": mirror_url,
                                    "format": "arch",
                                    "dependencies": dependencies,
                                    "provides": provides,
                                    "download_path": f"{repo}/os/{_host_arch()}/{name}-{version}-{arch}.pkg.tar.zst"
                                })
                                for prov in provides:
                                    merged_index.setdefault("arch_provides", {}).setdefault(prov, name)
                                for grp in groups:
                                    merged_index.setdefault("arch_groups", {}).setdefault(grp, [])
                                    if name not in merged_index["arch_groups"][grp]:
                                        merged_index["arch_groups"][grp].append(name)
        except Exception as e:
            print(f"Error parsing Arch {repo} index: {e}")

def parse_nix_index(merged_index: dict):
    if not _nix_available():
        print("  Skipping NixOS index (nix-env not found)")
        return
    print("Fetching NixOS package index...")
    batch_size = 5000
    after = None
    total_fetched = 0
    while True:
        body = {
            "query": {"term": {"type": "package"}},
            "size": batch_size,
            "sort": [{"_doc": "asc"}],
            "_source": [
                "package_attr_name", "package_pversion",
                "package_description", "package_programs",
                "package_system", "package_outputs"
            ]
        }
        if after is not None:
            body["search_after"] = after
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            _NIX_SEARCH_URL, data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Basic " + _NIX_AUTH,
                "User-Agent": "yapm/1.0"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"Error fetching NixOS index: {e}")
            break
        hits = result.get("hits", {}).get("hits", [])
        if not hits:
            break
        for hit in hits:
            src = hit.get("_source", {})
            attr_name = src.get("package_attr_name", "")
            version = src.get("package_pversion", "")
            if not attr_name:
                continue
            merged_index["packages"].setdefault(attr_name, {})["nix"] = {
                "version": version,
                "description": src.get("package_description", ""),
                "mirror": "https://search.nixos.org",
                "format": "nix",
                "attr": attr_name
            }
        total_fetched += len(hits)
        after = hits[-1].get("sort")
        print(f"  Fetched {total_fetched} NixOS packages...")
    print(f"  NixOS index complete: {total_fetched} packages")

def get_pkg_info(idx: dict, pkg: str, version: Optional[str] = None, arch_mode: bool = False) -> Optional[dict]:
    packages = idx.get("packages", {})
    entry = packages.get(pkg)
    if not entry and arch_mode:
        provided_by = idx.get("arch_provides", {}).get(pkg)
        if provided_by:
            entry = packages.get(provided_by)
    if not entry:
        return None

    # pick the right per-format sub-entry (arch mode, or yapm > arch > deb > nix)
    if arch_mode:
        sub = entry.get("arch")
        if not sub:
            return None
        entry = sub
    else:
        for fmt in ("yapm", "arch", "deb", "nix"):
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

def update_index(hall: Optional[str] = None):
    if config_flag("yapm.yapm"):
        print("found 0 updates")
        time.sleep(1)
        print("just kidding")
    print(f"  {_action('updating package index')}...")
    merged_index = {"packages": {}}
    mirrors = resolve_hall(hall) if hall else sorted_mirrors()
    if hall:
        print(f"    {Color.DIM}(filtered to hall '{hall}' — {len(mirrors)} mirror(s)){Color.RESET}")
    for mirror in mirrors:
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

    if _nix_available():
        parse_nix_index(merged_index)

    with open(INDEX_FILE, "w") as f:
        json.dump(merged_index, f, indent=4)
    print(f"  {_ok('Index updated.')}")

def load_index() -> Dict:
    if not INDEX_FILE.exists():
        print("Warning: Local index not found. Run 'yapm update' first.")
        return {"packages": {}}
    with open(INDEX_FILE) as f:
        idx = json.load(f)
    new_pkgs = {}
    for k, v in idx.get("packages", {}).items():
        name = k.split("/", 1)[-1] if "/" in k else k
        if any(fmt in v for fmt in ("yapm", "arch", "deb", "nix")):
            normalized = v
        else:
            fmt = v.get("format", "yapm")
            normalized = {fmt: v}

        if name in new_pkgs:
            for fmt, entry in normalized.items():
                if fmt not in new_pkgs[name]:
                    new_pkgs[name][fmt] = entry
        else:
            new_pkgs[name] = dict(normalized)
    idx["packages"] = new_pkgs
    return idx
