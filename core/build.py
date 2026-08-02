# build/submit cmds
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from .download import parse_yapm_data

YAPM_CONTRIB_REPO = "commodorial64/yapm-contrib"

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

    meta = y_data.get("METADATA", {})
    required = ["name", "version", "description", "author", "license"]
    missing = [f for f in required if not meta.get(f)]
    if missing:
        print(f"Error: yapm.data [METADATA] is missing required fields: {', '.join(missing)}")
        sys.exit(1)

    content = y_data.get("CONTENT", {})
    if not content.get("Uninstall"):
        print("Warning: yapm.data [CONTENT] has no Uninstall script.")
        print("         Packages that install addons (desktop entries, services, etc.)")
        print("         should provide an Uninstall script to clean up on removal.")

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

def generate_yapm_data(target_dir: str):
    path = Path(target_dir) / "yapm.data"
    if path.exists():
        print(f"Error: {path} already exists. Remove it first or use a different directory.")
        sys.exit(1)

    template = """\
// YAPM Package Definition File
// Similar to a Debian CONTROL file or Arch PKGBUILD.
// Lines starting with // are comments. /* ... */ for multi-line comments.

[METADATA]
// ─── REQUIRED ──────────────────────────────────────────────
// These fields MUST be filled in or 'yapm build' will fail.

name = "my-package"                    // Unique package name (no spaces)
version = "1.0.0"                      // Semantic version (major.minor.patch)
description = "A short description"    // One-line summary (shown in 'yapm search')
author = "your-name"                   // Your name or handle
license = "MIT"                        // SPDX license identifier

// ─── OPTIONAL ──────────────────────────────────────────────

// Dependencies: other packages that must be installed first.
// Use package names as they appear in 'yapm search'.
// dependencies = ["python3", "zstd"]

[CONTENT]
// ─── RECOMMENDED ──────────────────────────────────────────
// Uninstall script cleans up addons (desktop entries, services, config files,
// etc.) when the package is removed. A warning is shown at build time if
// no Uninstall script is provided.
//
// Uninstall = uninstall.sh          // Runs before the package is removed

// ─── OPTIONAL ──────────────────────────────────────────────
// These point to files inside your package's run/ or build/ folders.
// YAPM links RunFile to /usr/local/bin/ automatically.

// RunFile = my-program             // Primary executable (linked to PATH)
// BuildFile = build.sh             // Build/compile script (run before install)
// PreInstall = pre-install.sh      // Runs before files are copied
// PostInstall = post-install.sh    // Runs after files are copied

[FILES]
// ─── OPTIONAL ──────────────────────────────────────────────
// Maps extra files from inside the package to locations on the system.
// Format: "source_in_package" = "destination_on_system"

// "config/default.conf" = "/etc/my-package/config.conf"
// "assets/icon.png" = "/usr/share/my-package/icon.png"
// "service/my-package.service" = "/etc/systemd/system/my-package.service"
"""

    path.write_text(template)
    print(f"Generated {path}")
    print()
    print("Fill in the [METADATA] section, then run:")
    print(f"  yapm build {target_dir}")

def submit_package(package_path: str):
    # fork yapm-contrib, push the .yapm, open a PR
    pkg = Path(package_path).resolve()
    if not pkg.exists():
        print(f"Error: {pkg} does not exist.")
        sys.exit(1)
    if not pkg.name.endswith(".yapm"):
        print(f"Error: {pkg.name} is not a .yapm file.")
        sys.exit(1)

    # validate it's a tar.zst
    with open(pkg, "rb") as f:
        magic = f.read(4)
    if magic != b'\x28\xb5\x2f\xfd':
        print(f"Error: {pkg.name} is not a valid tar.zst archive.")
        sys.exit(1)

    # check yapm.data is inside
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

    if not shutil.which("gh"):
        print("Error: 'gh' CLI is required. Install it from https://cli.github.com/")
        sys.exit(1)

    # gh commands must run as the original user (auth lives in their home)
    real_user = os.environ.get("SUDO_USER")
    gh_cmd = ["sudo", "-u", real_user, "gh"] if real_user else ["gh"]

    branch = f"submit-{pkg.stem}"
    tmpdir = tempfile.mkdtemp()

    try:
        print("Forking yapm-contrib...")
        result = subprocess.run(gh_cmd + ["repo", "fork", YAPM_CONTRIB_REPO, "--clone=false"],
                               capture_output=True, text=True)
        if result.returncode != 0:
            if "already exists" in result.stderr:
                print("  Fork already exists, continuing...")
            else:
                print(f"Error forking: {result.stderr.strip()}")
                sys.exit(1)

        result = subprocess.run(gh_cmd + ["api", "user", "--jq", ".login"],
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
            gh_cmd + ["pr", "create",
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
