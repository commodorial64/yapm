# qol cmds
import json
import os
import shutil
import sys
from pathlib import Path

from .color import Color, _action, _err, _fmt, _ok, _pkg, _title, _ver, _warn
from .db import load_db
from .index import load_index
from .paths import BIN_DIR, CACHE_DIR, ROOT_DIR
from .utils import _parse_ver, format_key

def fetch_count():
    # package count for neofetch/fastfetch
    try:
        db = load_db()
    except (OSError, PermissionError):
        print("0 (yapm)")
        return
    count = len(db)
    print(f"{count} (yapm)")

def list_installed(outdated: bool = False, json_output: bool = False):
    db = load_db()
    if not db:
        if json_output:
            print("[]")
        else:
            print(f"  {_action('no packages installed')}")
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
                print(f"  {_pkg(pkg)} {_ver(local_ver)} -> {_ok(remote_ver)}")
                found = True
        if not found:
            print(f"  {_ok('Everything is up to date.')}")
        return

    print()
    for pkg, info in sorted(db.items()):
        ver = info.get("version", "0.0.0")
        fmt = info.get("format", "yapm")
        print(f"  {_pkg(pkg)}  {_ver(ver)}  {_fmt(fmt)}")
    print(f"\n  {Color.DIM}{len(db)} package(s) installed{Color.RESET}\n")

def info_package(pkg: str):
    idx = load_index()
    db = load_db()

    pkg_key = pkg

    print(f"\n  {_title(format_key(pkg_key))}")

    if pkg_key in db:
        ver = db[pkg_key].get('version', '0.0.0')
        fmt = db[pkg_key].get('format', 'yapm')
        print(f"  {_action('status')} {_ok('Installed')} {_ver(f'v{ver}')} [{_fmt(fmt)}]")
        meta = db[pkg_key].get("metadata", {})
        if "description" in meta:
            print(f"  {_action('description')} {meta['description']}")
        if "dependencies" in meta and meta["dependencies"]:
            print(f"  {_action('depends on')} {', '.join(meta['dependencies'])}")
    else:
        print(f"  {_action('status')} Not installed")

    if pkg_key in idx.get("packages", {}):
        formats_entry = idx["packages"][pkg_key]
        for fmt_name in ("yapm", "arch", "deb", "nix"):
            entry = formats_entry.get(fmt_name)
            if not entry:
                continue
            print(f"\n  {_fmt(fmt_name)}")
            if "versions" in entry:
                vers = ', '.join(_ver(v) for v in sorted(entry['versions'].keys()))
                print(f"  {_action('versions')} {vers}")
                print(f"  {_action('latest')} {_ver(entry.get('latest', 'unknown'))}")
                ver_info = entry["versions"].get(entry.get("latest", ""), {})
                if "dependencies" in ver_info and ver_info["dependencies"]:
                    print(f"  {_action('depends on')} {', '.join(ver_info['dependencies'])}")
            else:
                print(f"  {_action('version')} {_ver(entry.get('version', '0.0.0'))}")
                if "dependencies" in entry and entry["dependencies"]:
                    print(f"  {_action('depends on')} {', '.join(entry['dependencies'])}")
    else:
        print(f"  {_action('remote')} Not found in index.")
    print()

def search_package(term: str):
    idx = load_index()
    db = load_db()
    found = False
    term_lower = term.lower()

    for pkg_key, formats_entry in idx.get("packages", {}).items():
        display = pkg_key
        display_lower = display.lower()

        for fmt_name in ("yapm", "arch", "deb", "nix"):
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
                    installed_mark = f"  {_ok(f'[installed {local_ver}]')}"
                print(f"  {_pkg(display)} {_ver(f'v{latest_ver}')} - {ver_info.get('description', 'No description')}{installed_mark}")
                found = True
                break

    if not found:
        print(f"  {_warn('No matches found in local index.')} Try 'yapm update' first.")

def outdated_packages():
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
    # which installed packages depend on this one
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
    if not CACHE_DIR.exists():
        print("Cache is already clean.")
        return
    size = sum(f.stat().st_size for f in CACHE_DIR.rglob("*") if f.is_file())
    shutil.rmtree(CACHE_DIR)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Cache cleaned ({size / 1024:.1f} KB freed).")

def repair_package(pkg: str):
    # re-create missing symlinks for an installed package
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
