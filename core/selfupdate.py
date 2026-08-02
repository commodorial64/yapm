# yapm self update/uninstall
import os
import re
import shutil
import sys
from pathlib import Path

from .download import download
from .paths import APP_VERSION, _yapm_entry
from .utils import _parse_ver

YAPM_SOURCE_URL = "https://raw.githubusercontent.com/commodorial64/yapm/main/yapm.py"

def uninstall_yapm():
    # require_root() has already run before this point
    print("Uninstalling system-wide yapm...")
    script_path = Path(_yapm_entry())
    if "bin/yapm" in str(script_path):
        os.unlink(script_path)
    else:
        std_bin = Path("/usr/local/bin/yapm")
        if std_bin.exists():
            os.unlink(std_bin)

    shutil.rmtree("/etc/yapm", ignore_errors=True)
    shutil.rmtree("/var/lib/yapm", ignore_errors=True)
    print("Successfully uninstalled yapm.")

def update_yapm(force: bool = False):
    print(f"Fetching latest yapm from {YAPM_SOURCE_URL} ...")
    data = download(YAPM_SOURCE_URL, desc="Downloading yapm")
    if not data:
        print("Error: failed to download the latest yapm.")
        sys.exit(1)

    new_src = data.decode("utf-8", errors="replace")

    # parse APP_VERSION from the downloaded script
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

    # atomic replace: temp file beside the target, then rename
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
