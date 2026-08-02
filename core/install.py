# install cmds
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

from .chaos import chaos_confirm, chaos_delay, chaos_interrupt, chaos_opinion, chaos_post_operation, chaos_spinner, chaos_wrong_name, chaos_yap_on_extract
from .color import Color, _action, _err, _ok, _pkg, _ver
from .completions import _user_home
from .config import config_flag, resolve_hall, sorted_mirrors
from .db import load_db, save_db
from .download import parse_pkginfo, parse_yapm_data, safe_extract
from .extract import extract_arch, extract_deb, get_arch_file_list, get_deb_file_list, run_pkg_install_hook
from .fetch import fetch_from_github, fetch_package, resolve_dependencies
from .index import get_pkg_info, load_index, update_index
from .paths import BIN_DIR, INSTALL_DIR, LIB_DIR, ROOT_DIR, VIRTUAL_PROVIDERS, set_root_dir
from .setup import SETUP_MARKER, setup
from .utils import _parse_ver, format_key

_DEB_DISTROS = {"debian", "ubuntu", "linuxmint", "pop", "kali", "raspbian", "deepin", "elementary", "zorin"}
_ARCH_DISTROS = {"arch", "endeavouros", "manjaro", "garuda", "arco"}

def _detect_host_distro() -> str:
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("ID="):
                    return line.split("=", 1)[1].strip().strip('"').lower()
    except Exception:
        pass
    return ""

# block installing a foreign distro format
def _check_cross_distro(fmt: str):
    if config_flag("yapm.fuckaround"):
        return
    host = _detect_host_distro()
    if not host:
        return
    if fmt == "deb" and host not in _DEB_DISTROS:
        print(f"BLOCKED: Installing Debian packages on {host} is NOT recommended.")
        print("Trust me, I fucked around. (And found out.) - commodore.")
        print("Set yapm.fuckaround to true if you know about the config flags. but i'd NOTTTTTT reccomend.")
        sys.exit(1)
    if fmt == "arch" and host not in _ARCH_DISTROS:
        print(f"BLOCKED: I can't really tell you what'd happen if you installed")
        print(f"an Arch package on {host}, but don't try it. - commodore.")
        print("Set yapm.fuckaround to true if you know about the config flags. but i'd NOTTTTTT reccomend.")
        sys.exit(1)

def _install_single(pkg_name: str, db: Dict, data: bytes, fmt: str):
    if config_flag("yapm.nativenationality") and fmt != "yapm":
        print("yapm.nativenationality is enabled — only native .yapm packages allowed")
        sys.exit(1)

    _check_cross_distro(fmt)

    # .yapm goes to sandbox (has manifests); arch/deb extract to ROOT_DIR
    use_root = fmt in ("arch", "deb")
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
        # manifest-driven install
        yapm_data_path = extract_target / "yapm.data"
        if yapm_data_path.exists():
            with open(yapm_data_path) as f:
                y_data = parse_yapm_data(f.read())

            meta = y_data.get("METADATA", {})
            pkg_meta["version"] = meta.get("version", "0.0.0")
            if "description" in meta: pkg_meta["description"] = meta["description"]
            if "dependencies" in meta: pkg_meta["dependencies"] = meta["dependencies"]

            content_info = y_data.get("CONTENT", {})

            uninstall_script = content_info.get("Uninstall")
            if uninstall_script:
                pkg_meta["uninstall_script"] = uninstall_script

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

            # FILES — map package paths to absolute ROOT_DIR destinations
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
            # no-manifest fallback: link executables out of bin dirs
            bin_source_dirs = [extract_target / "src", extract_target / "usr" / "bin", extract_target / "bin",
                               extract_target / "usr" / "games", extract_target / "usr" / "sbin"]
            for src_dir in bin_source_dirs:
                if src_dir.exists() and src_dir.is_dir():
                    for item in src_dir.iterdir():
                        if (item.is_file() or item.is_symlink()) and os.access(item, os.X_OK):
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

        # link executables from non-standard bin dirs to BIN_DIR
        _standard_bin = {"/bin", "/usr/bin", "/sbin", "/usr/sbin"}
        for fpath in file_list:
            full = ROOT_DIR / (fpath[2:] if fpath.startswith("./") else fpath)
            parent = str(full.parent)
            if parent not in _standard_bin and full.exists() and (full.is_file() or full.is_symlink()) and os.access(full, os.X_OK):
                dest = BIN_DIR / full.name
                if dest.is_symlink() and not dest.exists():
                    dest.unlink(missing_ok=True)
                if not dest.exists() and not dest.is_symlink():
                    try:
                        os.symlink(full, dest)
                        print(f"  Linked {full.name} -> {dest}")
                    except Exception:
                        pass

        # grab metadata from the package
        if fmt == "arch":
            pkginfo = parse_pkginfo(data)
            if pkginfo:
                pkg_meta["version"] = pkginfo.get("pkgver", "0.0.0")
                pkg_meta["dependencies"] = pkginfo.get("depends", [])
                pkg_meta["description"] = pkginfo.get("pkgdesc", "")
        elif fmt == "deb":
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
        # arch/deb in sandbox mode on the host — fallback linking
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        LIB_DIR.mkdir(parents=True, exist_ok=True)

        ldso_conf = Path("/etc/ld.so.conf.d/yapm.conf")
        if not ldso_conf.exists():
            try:
                ldso_conf.write_text("/usr/local/lib\n")
                print(f"  Created {ldso_conf}")
            except Exception:
                pass

        bin_source_dirs = [extract_target / "src", extract_target / "usr" / "bin", extract_target / "bin",
                           extract_target / "usr" / "games", extract_target / "usr" / "sbin"]
        for src_dir in bin_source_dirs:
            if src_dir.exists() and src_dir.is_dir():
                for item in src_dir.iterdir():
                    if (item.is_file() or item.is_symlink()) and os.access(item, os.X_OK):
                        dest = BIN_DIR / item.name
                        if dest.exists() or dest.is_symlink():
                            os.unlink(dest)
                        symlink_src = ROOT_DIR / item.relative_to(ROOT_DIR)
                        os.symlink(symlink_src, dest)
                        print(f"  Linked {item.name} -> {dest}")

        lib_source_dirs = [extract_target / "usr" / "lib", extract_target / "lib"]
        for src_dir in lib_source_dirs:
            if src_dir.exists() and src_dir.is_dir():
                for item in src_dir.iterdir():
                    if item.is_file() or item.is_symlink():
                        if item.suffix in ('.so', '.a') or '.so.' in item.name:
                            dest = LIB_DIR / item.name
                            if dest.exists() or dest.is_symlink():
                                os.unlink(dest)
                            symlink_src = ROOT_DIR / item.relative_to(ROOT_DIR)
                            os.symlink(symlink_src, dest)
                            print(f"  Linked lib {item.name} -> {dest}")

        # also link usr/share subtree (data files, man pages, etc.)
        share_src = extract_target / "usr" / "share"
        if share_src.exists() and share_src.is_dir():
            for item in share_src.iterdir():
                dest = Path("/") / "usr" / "share" / item.name
                if not dest.exists() and not dest.is_symlink():
                    try:
                        os.symlink(item, dest)
                        print(f"  Linked share/{item.name} -> {dest}")
                    except Exception:
                        pass

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

def install_package(packages: List[str], fmt: str, mirror_index: Optional[int] = None, root: Optional[str] = None, noconfirm: bool = False, dry_run: bool = False, hall: Optional[str] = None):
    if config_flag("yapm.yapm"):
        chaos_spinner(3)
    if config_flag("yapm.autoupdate"):
        update_index(hall=hall)

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

        if arch_mode and pkg_name not in idx.get("packages", {}) and pkg_name in idx.get("arch_groups", {}):
            members = idx["arch_groups"][pkg_name]
            print(f"  {_action('group')} {_pkg(pkg_name)} expands to: {', '.join(members)}")
            for member in members:
                pin_version[member] = None
                pin_mirror[member] = global_pinned_mirror
                resolve_dependencies(member, idx, db, to_install_merged, seen, visited, version=None, arch_mode=arch_mode)
            continue

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

        print(f"  {_action('installing')} {_pkg(pkg_name)} from local {Color.DIM}{pkg_path}{Color.RESET}")
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
        print(f"  {_action('installed')} {_pkg(pkg_name)}.")

    if not to_install_merged:
        if not local_installs:
            print(f"{_action('nothing to do')}")
        return

    print(f"{_action('resolving dependencies')}...")
    pkg_list = ', '.join(_pkg(p) for p in to_install_merged)
    print(f"  {pkg_list}")

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

    needs_ldconfig = False
    for p in to_install_merged:
        p_ver = pin_version.get(p)
        p_mirror = pin_mirror.get(p)
        chaos_interrupt()
        display_p = chaos_wrong_name(p)
        print(f"  {_action('installing')} {_pkg(display_p)}...")

        if arch_mode:
            fetched_fmt = "arch"
        elif p_mirror:
            # pinned mirror — find the format that belongs to it
            pkg_entry = idx.get("packages", {}).get(p, {})
            fetched_fmt = "yapm"
            for fmt in ("yapm", "arch", "deb", "nix"):
                sub = pkg_entry.get(fmt)
                if sub and sub.get("mirror", "") == p_mirror:
                    fetched_fmt = fmt
                    break
        else:
            fetched_fmt = (get_pkg_info(idx, p, p_ver) or {}).get("format", "yapm")

        if fetched_fmt == "nix":
            nix_info = (get_pkg_info(idx, p, p_ver) or {})
            attr_name = nix_info.get("attr", p)
            print(f"  Delegating to nix-env (nixpkgs.{attr_name})...")
            result = subprocess.run(
                ["nix-env", "-iA", f"nixpkgs.{attr_name}"],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                print(f"  nix-env failed: {result.stderr.strip()}")
                sys.exit(1)
            db[p] = {
                "version": p_ver or nix_info.get("version", "0.0.0"),
                "path": f"nixpkgs.{attr_name}",
                "dependencies": [],
                "format": "nix",
                "metadata": {"description": nix_info.get("description", "")}
            }
            save_db(db)
            print(f"  {_action('installed')} {_pkg(display_p)}.")
            continue

        if p in pre_fetched_data:
            data = pre_fetched_data[p]
        else:
            data = fetch_package(p, mirror_url=p_mirror, version=p_ver, arch_mode=arch_mode, hall=hall)

        if not data:
            print(f"Failed to fetch {p}. Aborting.")
            sys.exit(1)

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
        needs_ldconfig = True
        if fetched_fmt == "arch" and config_flag("yapm.hooks"):
            run_pkg_install_hook(data, ROOT_DIR, "post_install")
        print(f"  {_action('installed')} {_pkg(chaos_wrong_name(p))}.")

    if "linux" in to_install_merged and str(ROOT_DIR) != "/":
        print("Running mkinitcpio for bootstrapped system...")
        subprocess.run(["arch-chroot", str(ROOT_DIR), "mkinitcpio", "-P"], check=False)

    if needs_ldconfig:
        print(f"  {_action('updating library cache')}...")
        subprocess.run(["ldconfig"], capture_output=True, check=False)

    if config_flag("yapm.yapm"):
        print("something may or may have gone wrong. who can say really")

    if not SETUP_MARKER.exists() and not (_user_home() / ".yapm" / ".setup_done").exists():
        try:
            setup()
        except Exception:
            pass  # non-fatal — completions are nice-to-have

    chaos_post_operation()

def remove_package(pkg: str, noconfirm: bool = False):
    db = load_db()

    pkg_key = pkg

    if pkg_key not in db:
        print(f"  {_err(f'Package {format_key(pkg_key)} not installed.')}")
        return

    if not noconfirm and not config_flag("yapm.noconfirm"):
        try:
            choice = input(f"  {_action('remove')} {_pkg(format_key(pkg_key))}? [{Color.GREEN}y{Color.RESET}/N] ").strip().lower()
            if choice not in ('y', 'yes'):
                print("Aborted.")
                return
        except (ValueError, EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return

    pkg_info = db[pkg_key]
    fmt = pkg_info.get("format", "yapm")

    # run uninstall script before removing files (native packages only)
    if fmt == "yapm":
        metadata = pkg_info.get("metadata", {})
        uninstall_script = metadata.get("uninstall_script")
        if uninstall_script:
            target = Path(pkg_info["path"])
            script_path = target / uninstall_script
            # script must exist and be inside the package dir
            if script_path.exists() and script_path.resolve().is_relative_to(target.resolve()):
                print(f"  Running uninstall script...")
                os.chmod(script_path, 0o755)
                try:
                    subprocess.run([str(script_path)], cwd=target, timeout=30)
                except subprocess.TimeoutExpired:
                    print(f"  Warning: uninstall script timed out (30s), continuing...")
                except subprocess.CalledProcessError as e:
                    print(f"  Warning: uninstall script failed ({e.returncode}), continuing...")
            elif uninstall_script:
                print(f"  Warning: uninstall script '{uninstall_script}' not found or unsafe, skipping.")

    if fmt == "nix":
        attr_name = pkg_info.get("path", pkg_key)
        print(f"  {_action('removing')} {_pkg(format_key(pkg_key))}...")
        result = subprocess.run(
            ["nix-env", "-e", pkg_key],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"  {_err('nix-env removal failed')}: {result.stderr.strip()}")
            return
        del db[pkg_key]
        save_db(db)
        print(f"  {_action('removed')} {_pkg(format_key(pkg_key))}.")
        return

    file_list = pkg_info.get("files", [])

    if file_list:
        # file-list removal (packages extracted to ROOT_DIR)
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
                    pass
        # clean up empty parent dirs
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
        print(f"  {_action('removed')} {_pkg(format_key(pkg_key))} ({removed} files).")
        # clean up BIN_DIR symlinks pointing into this package's tree
        pkg_path = pkg_info.get("path", "")
        if pkg_path and pkg_path != "/":
            for link in BIN_DIR.iterdir():
                if link.is_symlink():
                    try:
                        target = str(link.resolve())
                        if target.startswith(str(Path(pkg_path))):
                            link.unlink()
                    except Exception:
                        pass
        else:
            # for root-extracted packages, check against file_list
            for link in BIN_DIR.iterdir():
                if link.is_symlink():
                    try:
                        target = str(link.resolve())
                        if any(str((Path("/") / f.lstrip("./")).resolve()) == target for f in file_list):
                            link.unlink()
                    except Exception:
                        pass
    else:
        # directory removal (sandbox packages)
        target = Path(pkg_info["path"])
        bin_source_dirs = [target / "src", target / "usr" / "bin", target / "bin"]
        for src_dir in bin_source_dirs:
            if src_dir.exists() and src_dir.is_dir():
                for item in src_dir.iterdir():
                    dest = BIN_DIR / item.name
                    if dest.is_symlink() and str(dest.resolve()) == str(item.resolve()):
                        os.unlink(dest)
                        print(f"  {_action('removed')} symlink {dest.name}")

        lib_source_dirs = [target / "usr" / "lib", target / "lib"]
        for src_dir in lib_source_dirs:
            if src_dir.exists() and src_dir.is_dir():
                for item in src_dir.iterdir():
                    if item.is_file() or item.is_symlink():
                        if item.suffix in ('.so', '.a') or '.so.' in item.name:
                            dest = LIB_DIR / item.name
                            if dest.is_symlink() and str(dest.resolve()) == str(item.resolve()):
                                os.unlink(dest)
                                print(f"  {_action('removed')} lib symlink {dest.name}")

        shutil.rmtree(target, ignore_errors=True)
        print(f"  {_action('removed')} {_pkg(format_key(pkg_key))}.")

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
        print(f"  {_ok('Everything is up to date.')}")
        return

    print(f"  {_action('upgrades available')}:")
    for pkg, ver in to_upgrade:
        print(f"    {_pkg(pkg)} {_ver(db[pkg].get('version', '0.0.0'))} -> {_ok(ver)}")

    if dry_run:
        print(f"\n  {Color.DIM}(dry run — no changes made){Color.RESET}")
        return

    for pkg, ver in to_upgrade:
        chaos_interrupt()
        print(f"  {_action('upgrading')} {_pkg(pkg)}...")
        installed_fmt = db[pkg].get("format", "yapm")
        if installed_fmt == "nix":
            idx_entry = (get_pkg_info(idx, pkg) or {})
            attr_name = idx_entry.get("attr", pkg)
            result = subprocess.run(
                ["nix-env", "-iA", f"nixpkgs.{attr_name}"],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                print(f"    {_err('nix-env failed')}: {result.stderr.strip()}. Skipping.")
                continue
            db[pkg]["version"] = ver
            save_db(db)
            print(f"  {_action('upgraded')} {_pkg(pkg)}.")
            continue
        data = fetch_package(pkg, version=ver)
        if not data:
            print(f"    {_err('failed to fetch')} {_pkg(pkg)}. Skipping.")
            continue
        _install_single(pkg, db, data, installed_fmt)
        print(f"  {_action('upgraded')} {_pkg(pkg)}.")

    chaos_post_operation()

def init_package(noconfirm: bool = False, root: Optional[str] = None):
    # bootstrap a Riot system by ensuring bash is installed
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
