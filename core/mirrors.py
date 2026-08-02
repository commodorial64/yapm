# mirror cmds
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from .color import Color, _ok, _pkg
from .config import load_config, resolve_hall, save_config, sorted_mirrors
from .db import load_db
from .index import load_index
from .paths import DEFAULT_CONFIG
from .utils import _pager, normalize

def validate_mirror(url: str) -> bool:
    try:
        if url.startswith("file://"):
            return Path(url[7:]).exists()
        req = urllib.request.Request(normalize(url), method="HEAD", headers={'User-Agent': 'yapm/1.0'})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status < 400
    except Exception:
        return False

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

def mirror_test():
    config = load_config()
    print("Testing mirrors...")
    for m in config["mirrors"]:
        ok = validate_mirror(m["url"])
        status = f"{Color.GREEN}OK{Color.RESET}" if ok else f"{Color.RED}FAILED{Color.RESET}"
        print(f"  {m['url']} -> {status}")

def mirror_show(hall: Optional[str] = None, mirror_filter: Optional[str] = None):
    idx = load_index()
    packages = idx.get("packages", {})
    if not packages:
        print("No packages in index. Run 'yapm update' first.")
        return

    db = load_db()

    hall_urls = set()
    if hall:
        config = load_config()
        halls = config.get("halls", {})
        if hall not in halls:
            print(f"Hall '{hall}' not found. Available halls: {', '.join(sorted(halls.keys())) or '(none)'}")
            return
        hall_urls = set(halls[hall])

    name_ver_parts = []
    for pkg_key, formats_entry in packages.items():
        for fmt_name in ("yapm", "arch", "deb", "nix"):
            entry = formats_entry.get(fmt_name)
            if not entry:
                continue
            pkg_mirror = entry.get("mirror", "")
            if hall_urls and pkg_mirror not in hall_urls:
                continue
            if mirror_filter and mirror_filter not in pkg_mirror:
                continue
            if "versions" in entry:
                latest = entry.get("latest", "")
                ver_str = f"{pkg_key} (v{latest})"
            else:
                ver_str = f"{pkg_key} (v{entry.get('version', '?')})"
            name_ver_parts.append(ver_str)
            break

    if not name_ver_parts:
        label = f"hall '{hall}'" if hall else f"mirror '{mirror_filter}'" if mirror_filter else "index"
        print(f"No packages found for {label}.")
        return

    col1_width = max((len(s) for s in name_ver_parts), default=30) + 4

    lines = []
    for pkg_key in sorted(packages):
        formats_entry = packages[pkg_key]
        entry = None
        fmt_name = None
        for fmt in ("yapm", "arch", "deb", "nix"):
            if formats_entry.get(fmt):
                entry = formats_entry[fmt]
                fmt_name = fmt
                break
        if not entry:
            continue

        pkg_mirror = entry.get("mirror", "")
        if hall_urls and pkg_mirror not in hall_urls:
            continue
        if mirror_filter and mirror_filter not in pkg_mirror:
            continue

        if "versions" in entry:
            latest = entry.get("latest", "")
            ver_info = entry["versions"].get(latest, {})
            ver_str = f"v{latest}"
        else:
            ver_str = f"v{entry.get('version', '?')}"
            ver_info = entry

        desc = ver_info.get("description", entry.get("description", ""))
        author = ver_info.get("author", entry.get("author", ""))
        license_ = ver_info.get("license", entry.get("license", ""))

        left = f"{pkg_key} ({ver_str})"
        if len(left) < col1_width:
            padding = " " * (col1_width - len(left))
        else:
            padding = " "

        desc_display = desc if len(desc) <= 50 else desc[:47] + "..."

        installed_mark = ""
        if pkg_key in db:
            installed_mark = f" {_ok('[installed]')}"

        lines.append(f"  {_pkg(left)}{padding}{Color.DIM}{desc_display}{Color.RESET}{installed_mark}")
        lines.append(f"    {Color.DIM}{author}  {license_}{Color.RESET}")

    _pager(lines)

# parse "1-3", "[1,5]", or "3"; indices are 1-based like 'yapm mirror list'
def parse_selection(sel: str, mirrors: List[Dict]) -> List[Dict]:
    sel = sel.strip()
    results = []

    if sel.startswith("[") and sel.endswith("]"):
        inner = sel[1:-1]
        parts = [p.strip() for p in inner.split(",") if p.strip()]
        for p in parts:
            idx = int(p)
            if idx < 1 or idx > len(mirrors):
                print(f"Error: mirror index {idx} is out of range (1-{len(mirrors)}).")
                sys.exit(1)
            results.append(mirrors[idx - 1])
    elif "-" in sel and not sel.startswith("-"):
        parts = sel.split("-", 1)
        start = int(parts[0])
        end = int(parts[1])
        if start < 1 or end > len(mirrors) or start > end:
            print(f"Error: range {sel} is out of bounds (1-{len(mirrors)}).")
            sys.exit(1)
        results = mirrors[start - 1 : end]
    else:
        idx = int(sel)
        if idx < 1 or idx > len(mirrors):
            print(f"Error: mirror index {idx} is out of range (1-{len(mirrors)}).")
            sys.exit(1)
        results = [mirrors[idx - 1]]

    return results
