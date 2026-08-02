# misc utils
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List

from .paths import _yapm_entry

# parse "1.2.3" -> (1, 2, 3) for comparison
def _parse_ver(v: str):
    v = v.strip()
    parts = []
    for p in v.split("."):
        parts.append(int(''.join(c for c in p if c.isdigit()) or '0'))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])

# pipe lines through a pager, or print if not a tty
def _pager(lines: List[str]):
    if not sys.stdout.isatty():
        for line in lines:
            print(line)
        return
    pager = os.environ.get("PAGER", "less -R")
    try:
        proc = subprocess.Popen(pager, shell=True, stdin=subprocess.PIPE, text=True)
        proc.communicate(input="\n".join(lines) + "\n")
    except Exception:
        for line in lines:
            print(line)

# abort if not root
def require_root():
    if os.getuid() != 0:
        print("Error: yapm must be run with sudo.")
        print("  Try: sudo yapm <command>")
        sys.exit(1)

# set up passwordless sudo via a sudoers drop-in
def su_exec(extra_args: List[str]):
    if os.getuid() == 0:
        # as root — write the sudoers rule
        user = os.environ.get("SUDO_USER") or os.environ.get("USER")
        if not user or user == "root":
            print("Error: could not determine original user.")
            sys.exit(1)

        yapm_path = _yapm_entry()
        rule = f"{user} ALL=(root) NOPASSWD: {yapm_path} *\n"
        rule_file = Path(f"/etc/sudoers.d/yapm-{user}")

        if rule_file.exists():
            existing = rule_file.read_text()
            if yapm_path in existing:
                print(f"yapm is already set up for passwordless use ({rule_file}).")
                sys.exit(0)

        rule_file.write_text(rule)
        rule_file.chmod(0o440)

        result = subprocess.run(["visudo", "-c"], capture_output=True, text=True)
        if result.returncode != 0:
            rule_file.unlink(missing_ok=True)
            print("Error: sudoers validation failed. Rule not applied.")
            print(result.stderr.strip())
            sys.exit(1)

        print(f"Done. {user} can now run yapm without sudo.")
        print(f"  Rule: {rule_file}")
        print("  You may need to open a new shell for changes to take effect.")
        sys.exit(0)

    # not root — re-exec with sudo
    yapm_path = _yapm_entry()
    cmd = ["sudo", yapm_path, "su"] + extra_args
    print("Re-executing with sudo...")
    os.execvp("sudo", cmd)

def normalize(url: str) -> str:
    return url if url.endswith("/") else url + "/"

# strip author prefix for filename guessing
def pkg_basename(key: str) -> str:
    return key.split("/", 1)[-1]

# internal key -> display form (author/name -> author@name)
def format_key(key: str) -> str:
    if "/" in key:
        a, n = key.split("/", 1)
        return f"{a}@{n}"
    return key
