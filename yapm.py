#!/usr/bin/env python3

import argparse
import ast
import base64
import fcntl
import gzip
import io
import itertools
import json
import os
import platform
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
from typing import List, Dict, Optional, Any

VIRTUAL_PROVIDERS = frozenset({"sh", "awk", "perl", "python", "ruby"})

_SYSTEM_ARCH = platform.machine()

_DEB_ARCH_MAP = {
    "x86_64": "amd64",
    "aarch64": "arm64",
    "armv7l": "armhf",
    "i686": "i386",
    "i386": "i386",
}

def _host_arch():
    return _SYSTEM_ARCH

def _deb_arch():
    return _DEB_ARCH_MAP.get(_SYSTEM_ARCH, _SYSTEM_ARCH)

# ============================================================
# COLOR OUTPUT
# ============================================================

_COLOR_ENABLED = sys.stdout.isatty()

class Color:
    RESET   = "\033[0m"  if _COLOR_ENABLED else ""
    BOLD    = "\033[1m"   if _COLOR_ENABLED else ""
    DIM     = "\033[2m"   if _COLOR_ENABLED else ""
    UNDER   = "\033[4m"   if _COLOR_ENABLED else ""
    RED     = "\033[31m"  if _COLOR_ENABLED else ""
    GREEN   = "\033[32m"  if _COLOR_ENABLED else ""
    YELLOW  = "\033[33m"  if _COLOR_ENABLED else ""
    BLUE    = "\033[34m"  if _COLOR_ENABLED else ""
    MAGENTA = "\033[35m"  if _COLOR_ENABLED else ""
    CYAN    = "\033[36m"  if _COLOR_ENABLED else ""
    WHITE   = "\033[37m"  if _COLOR_ENABLED else ""
    BG_RED  = "\033[41m"  if _COLOR_ENABLED else ""
    BROWN   = "\033[38;2;160;120;90m" if _COLOR_ENABLED else ""
    DEB_RED = "\033[38;2;170;33;33m"  if _COLOR_ENABLED else ""
    ARCH_BLUE = "\033[38;2;23;147;209m" if _COLOR_ENABLED else ""
    YAPM_BROWN = "\033[38;2;160;120;90m" if _COLOR_ENABLED else ""

def _pkg(name):      return f"{Color.BOLD}{name}{Color.RESET}"
def _ver(v):         return f"{Color.DIM}{v}{Color.RESET}"
def _ok(msg):        return f"{Color.GREEN}{msg}{Color.RESET}"
def _warn(msg):      return f"{Color.YELLOW}{msg}{Color.RESET}"
def _err(msg):       return f"{Color.RED}{msg}{Color.RESET}"
def _info(msg):      return f"{Color.CYAN}{msg}{Color.RESET}"
def _action(msg):    return f"{Color.BOLD}{Color.CYAN}::{Color.RESET} {msg}"
def _title(msg):     return f"{Color.BOLD}{msg}{Color.RESET}"
def _fmt(fmt_name):
    colors = {"deb": Color.DEB_RED, "arch": Color.ARCH_BLUE, "yapm": Color.YAPM_BROWN, "nix": Color.MAGENTA}
    c = colors.get(fmt_name, Color.CYAN)
    return f"{c}{fmt_name.upper()}{Color.RESET}"


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

APP_VERSION = "0.5.2"
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
LIB_DIR     = Path("/usr/local/lib")
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
    "yapm.fuckaround": False,
    "yapm.yapm": False,
}

_DEB_DISTROS = {"debian", "ubuntu", "linuxmint", "pop", "kali", "raspbian", "deepin", "elementary", "zorin"}
_ARCH_DISTROS = {"arch", "endeavouros", "manjaro", "garuda", "arco"}

def _detect_host_distro() -> str:
    """Read /etc/os-release to detect the host distribution family."""
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("ID="):
                    return line.split("=", 1)[1].strip().strip('"').lower()
    except Exception:
        pass
    return ""

def _check_cross_distro(fmt: str):
    """Block installing packages from an incompatible distro format."""
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

DEFAULT_CONFIG = {
    "version": CURRENT_VERSION,
    "mirrors": [
        {"url": "https://yapm.pages.dev/", "priority": 0},
        {"url": "https://mirror.rackspace.com/archlinux/", "priority": 10},
        {"url": "https://deb.debian.org/debian/", "priority": 20},
        {"url": "https://archive.ubuntu.com/ubuntu/", "priority": 30}
    ]
}

def _pager(lines: List[str]):
    """Pipe a list of lines through a pager (less), or print directly if unavailable/TTY is a pipe."""
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

def require_root():
    """Abort immediately if not running as root."""
    if os.getuid() != 0:
        print("Error: yapm must be run with sudo.")
        print("  Try: sudo yapm <command>")
        sys.exit(1)

def su_exec(extra_args: List[str]):
    """Set up passwordless sudo for yapm via a sudoers drop-in rule.

    run once with sudo, then yapm never needs sudo again.
    Creates /etc/sudoers.d/yapm-<user> allowing the current user to run
    yapm as root without a password.
    """
    if os.getuid() == 0:
        # Running as root — write the sudoers rule
        user = os.environ.get("SUDO_USER") or os.environ.get("USER")
        if not user or user == "root":
            print("Error: could not determine original user.")
            sys.exit(1)

        yapm_path = shutil.which("yapm") or str(Path(__file__).resolve())
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

    # Not root — re-exec with sudo
    yapm_path = shutil.which("yapm") or str(Path(__file__).resolve())
    cmd = ["sudo", yapm_path, "su"] + extra_args
    print("Re-executing with sudo...")
    os.execvp("sudo", cmd)

# ============================================================
# SHELL COMPLETIONS
# ============================================================

_BASH_COMPLETION = '''\
_yapm() {
    local cur prev commands
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    commands="install remove list info search update upgrade fetch version uninstall riot build submit outdated files why clean repair mirror hall su fetch-count completions"

    if [[ ${cur} == -* ]]; then
        case "${COMP_WORDS[1]}" in
            install)  COMPREPLY=( $(compgen -W "-m -r -y -n -H -f --mirror --root --noconfirm --dry-run --hall --format" -- ${cur}) ) ;;
            remove)   COMPREPLY=( $(compgen -W "-y --noconfirm" -- ${cur}) ) ;;
            list)     COMPREPLY=( $(compgen -W "-o -j --outdated --json" -- ${cur}) ) ;;
            upgrade)  COMPREPLY=( $(compgen -W "-y -n --refresh --dry-run" -- ${cur}) ) ;;
            mirror)   COMPREPLY=( $(compgen -W "add list remove sync test show" -- ${cur}) ) ;;
            hall)     COMPREPLY=( $(compgen -W "add list remove show" -- ${cur}) ) ;;
            completions) COMPREPLY=( $(compgen -W "bash zsh fish" -- ${cur}) ) ;;
            *)        COMPREPLY=( $(compgen -W "--help" -- ${cur}) ) ;;
        esac
        return 0
    fi

    if [[ ${COMP_CWORD} -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "${commands}" -- ${cur}) )
        return 0
    fi

    case "${COMP_WORDS[1]}" in
        install|info|files|why|repair|hall)
            if [[ ${COMP_CWORD} -eq 2 ]]; then
                local pkgs
                pkgs=$(yapm list --json 2>/dev/null | python3 -c "import sys,json; print(' '.join(json.load(sys.stdin).keys()))" 2>/dev/null)
                COMPREPLY=( $(compgen -W "${pkgs}" -- ${cur}) )
            fi
            ;;
        mirror)
            if [[ ${COMP_CWORD} -eq 2 ]]; then
                COMPREPLY=( $(compgen -W "add list remove sync test show" -- ${cur}) )
            elif [[ "${COMP_WORDS[2]}" == "add" ]]; then
                COMPREPLY=( $(compgen -f -X '!*.url' -- ${cur}) )
            elif [[ "${COMP_WORDS[2]}" == "remove" ]]; then
                COMPREPLY=( $(compgen -W "$(yapm mirror list 2>/dev/null | grep -oP 'https?://\\S+')" -- ${cur}) )
            elif [[ "${COMP_WORDS[2]}" == "show" ]]; then
                COMPREPLY=( $(compgen -W "$(yapm list --json 2>/dev/null | python3 -c "import sys,json; print(' '.join(json.load(sys.stdin).keys()))" 2>/dev/null)" -- ${cur}) )
            fi
            ;;
        hall)
            if [[ ${COMP_CWORD} -eq 2 ]]; then
                COMPREPLY=( $(compgen -W "add list remove show" -- ${cur}) )
            elif [[ "${COMP_WORDS[2]}" == "remove" || "${COMP_WORDS[2]}" == "show" ]]; then
                : # hall names are static, could be added later
            fi
            ;;
    esac
    return 0
}
complete -F _yapm yapm
'''

_ZSH_COMPLETION = '''\
#compdef yapm

_yapm() {
    local -a commands
    commands=(
        'install:Install a package from a mirror or local file'
        'remove:Remove an installed package'
        'list:List installed packages'
        'info:Show package details'
        'search:Search the local package index'
        'update:Refresh the package index from mirrors'
        'upgrade:Upgrade installed packages'
        'fetch:Update yapm itself'
        'version:Print yapm version information'
        'uninstall:Uninstall yapm from the system'
        'riot:Bootstrap the system by installing bash'
        'build:Build a .yapm package from source'
        'submit:Submit a package to yapm-contrib'
        'outdated:Show packages with newer versions'
        'files:List files installed by a package'
        'why:Show reverse dependencies'
        'clean:Remove cached index/download files'
        'repair:Re-create missing symlinks'
        'mirror:Manage package mirrors'
        'hall:Manage mirror groups'
        'su:Re-run a command with sudo'
        'fetch-count:Print package count for fetch tools'
        'completions:Generate shell completion scripts'
    )

    _arguments -C \
        '1:command:->cmd' \
        '*::arg:->args'

    case $state in
        cmd)
            _describe 'command' commands
            ;;
        args)
            case $words[1] in
                install)
                    _arguments \
                        '-m[Pin to mirror by index]:mirror index:' \
                        '--mirror[Pin to mirror by index]:mirror index:' \
                        '-H[Only use mirrors from named hall]:hall name:' \
                        '--hall[Only use mirrors from named hall]:hall name:' \
                        '-r[Install to different root]:root path:_files' \
                        '--root[Install to different root]:root path:_files' \
                        '-y[Skip confirmation]' \
                        '--noconfirm[Skip confirmation]' \
                        '-n[Dry run]' \
                        '--dry-run[Dry run]' \
                        '-f[Package format]:format:(yapm deb arch)' \
                        '*:package:_files -g "*.(yapm|deb|pkg.tar.zst)"' && ret=0
                    ;;
                remove)
                    _arguments '-y[Skip confirmation]' '--noconfirm[Skip confirmation]' '*:package:_yapm_installed' && ret=0
                    ;;
                list)
                    _arguments '-o[Show outdated only]' '--outdated[Show outdated only]' '-j[JSON output]' '--json[JSON output]' && ret=0
                    ;;
                info)
                    _arguments '*:package:_yapm_installed' && ret=0
                    ;;
                mirror)
                    _arguments '1:subcommand:(add list remove sync test show)' && ret=0
                    ;;
                hall)
                    _arguments '1:subcommand:(add list remove show)' && ret=0
                    ;;
                completions)
                    _arguments '1:shell:(bash zsh fish)' && ret=0
                    ;;
            esac
            ;;
    esac
}

_yapm_installed() {
    local -a pkgs
    pkgs=(${(f)"$(yapm list --json 2>/dev/null | python3 -c 'import sys,json; print("\\n".join(json.load(sys.stdin).keys()))' 2>/dev/null)"})
    _describe 'installed package' pkgs
}

_yapm "$@"
'''

_FISH_COMPLETION = '''\
# yapm fish completions

function __yapm_mirrors
    yapm mirror list 2>/dev/null | grep -oP 'https?://\\S+'
end

function __yapm_halls
    echo "add\tCreate a hall from mirror indices"
    echo "list\tList all halls"
    echo "remove\tRemove a hall"
    echo "show\tShow mirrors in a hall"
end

function __yapm_mirror_sub
    echo "add\tAdd a new mirror"
    echo "list\tList all mirrors"
    echo "remove\tRemove a mirror"
    echo "sync\tTest and remove unreachable mirrors"
    echo "test\tTest mirrors without removing"
    echo "show\tShow all packages in the index"
end

function __yapm_installed
    yapm list --json 2>/dev/null | python3 -c 'import sys,json; print("\n".join(json.load(sys.stdin).keys()))' 2>/dev/null
end

# Subcommands
complete -c yapm -n '__fish_use_subcommand' -a install -d 'Install a package'
complete -c yapm -n '__fish_use_subcommand' -a remove -d 'Remove an installed package'
complete -c yapm -n '__fish_use_subcommand' -a list -d 'List installed packages'
complete -c yapm -n '__fish_use_subcommand' -a info -d 'Show package details'
complete -c yapm -n '__fish_use_subcommand' -a search -d 'Search the local package index'
complete -c yapm -n '__fish_use_subcommand' -a update -d 'Refresh the package index'
complete -c yapm -n '__fish_use_subcommand' -a upgrade -d 'Upgrade installed packages'
complete -c yapm -n '__fish_use_subcommand' -a fetch -d 'Update yapm itself'
complete -c yapm -n '__fish_use_subcommand' -a version -d 'Print version information'
complete -c yapm -n '__fish_use_subcommand' -a uninstall -d 'Uninstall yapm'
complete -c yapm -n '__fish_use_subcommand' -a riot -d 'Bootstrap with bash'
complete -c yapm -n '__fish_use_subcommand' -a build -d 'Build .yapm from source'
complete -c yapm -n '__fish_use_subcommand' -a submit -d 'Submit package to yapm-contrib'
complete -c yapm -n '__fish_use_subcommand' -a outdated -d 'Show outdated packages'
complete -c yapm -n '__fish_use_subcommand' -a files -d 'List files in a package'
complete -c yapm -n '__fish_use_subcommand' -a why -d 'Show reverse dependencies'
complete -c yapm -n '__fish_use_subcommand' -a clean -d 'Remove cached files'
complete -c yapm -n '__fish_use_subcommand' -a repair -d 'Re-create missing symlinks'
complete -c yapm -n '__fish_use_subcommand' -a mirror -d 'Manage mirrors'
complete -c yapm -n '__fish_use_subcommand' -a hall -d 'Manage mirror groups'
complete -c yapm -n '__fish_use_subcommand' -a su -d 'Re-run with sudo'
complete -c yapm -n '__fish_use_subcommand' -a fetch-count -d 'Package count for fetch tools'
complete -c yapm -n '__fish_use_subcommand' -a completions -d 'Generate shell completions'

# install flags
complete -c yapm -n '__fish_seen_subcommand_from install' -s m -l mirror -d 'Pin to mirror by index' -r
complete -c yapm -n '__fish_seen_subcommand_from install' -s H -l hall -d 'Only use mirrors from named hall' -r
complete -c yapm -n '__fish_seen_subcommand_from install' -s r -l root -d 'Install to different root' -r -F
complete -c yapm -n '__fish_seen_subcommand_from install' -s y -l noconfirm -d 'Skip confirmation'
complete -c yapm -n '__fish_seen_subcommand_from install' -s n -l dry-run -d 'Dry run'
complete -c yapm -n '__fish_seen_subcommand_from install' -s f -l format -d 'Package format' -r -a 'yapm deb arch'

# remove/list/upgrade flags
complete -c yapm -n '__fish_seen_subcommand_from remove' -s y -l noconfirm -d 'Skip confirmation'
complete -c yapm -n '__fish_seen_subcommand_from list' -s o -l outdated -d 'Show outdated only'
complete -c yapm -n '__fish_seen_subcommand_from list' -s j -l json -d 'JSON output'
complete -c yapm -n '__fish_seen_subcommand_from upgrade' -s y -l refresh -d 'Refresh index first'
complete -c yapm -n '__fish_seen_subcommand_from upgrade' -s n -l dry-run -d 'Dry run'

# mirror/hall subcompletions
complete -c yapm -n '__fish_seen_subcommand_from mirror' -a '(__yapm_mirror_sub)'
complete -c yapm -n '__fish_seen_subcommand_from hall' -a '(__yapm_halls)'
complete -c yapm -n '__fish_seen_subcommand_from completions' -a 'bash zsh fish'
'''


def completions_generate(shell: str):
    """Output shell-specific completion script."""
    scripts = {
        "bash": _BASH_COMPLETION,
        "zsh": _ZSH_COMPLETION,
        "fish": _FISH_COMPLETION,
    }
    if shell not in scripts:
        print(f"Error: unsupported shell '{shell}'. Choose from: bash, zsh, fish")
        sys.exit(1)
    print(scripts[shell])

SETUP_MARKER = DATA_DIR / ".setup_done"

def _detect_shell() -> str:
    """Detect the user's shell, using /etc/passwd when running via sudo."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        try:
            import pwd
            pw = pwd.getpwnam(sudo_user)
            name = Path(pw.pw_shell).name
            if name in ("bash", "zsh", "fish"):
                return name
        except (KeyError, ImportError):
            pass
    full = os.environ.get("SHELL", "")
    name = Path(full).name if full else ""
    if name in ("bash", "zsh", "fish"):
        return name
    return "bash"

def _detect_user() -> str:
    """Get the non-root user who invoked this (via sudo or directly)."""
    for var in ("SUDO_USER", "LOGNAME", "USER"):
        u = os.environ.get(var, "")
        if u and u != "root":
            return u
    return "root"

def _user_home() -> Path:
    """Get the real user's home directory (resolves through SUDO_USER)."""
    user = _detect_user()
    if user != "root":
        return Path(f"/home/{user}")
    return Path.home()

def _install_completions_bash(yapm_path: str):
    script = _BASH_COMPLETION
    # system-wide
    sys_dir = Path("/etc/bash_completion.d")
    if sys_dir.is_dir() and os.access(sys_dir, os.W_OK):
        (sys_dir / "yapm").write_text(script)
        print(f"  Installed bash completions → {sys_dir / 'yapm'}")
        return
    # user-local fallback
    local_dir = _user_home() / ".local/share/bash-completion/completions"
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / "yapm").write_text(script)
    print(f"  Installed bash completions → {local_dir / 'yapm'}")

def _install_completions_zsh(yapm_path: str):
    script = _ZSH_COMPLETION
    # system-wide
    sys_dir = Path("/usr/share/zsh/site-functions")
    if sys_dir.is_dir() and os.access(sys_dir, os.W_OK):
        (sys_dir / "_yapm").write_text(script)
        print(f"  Installed zsh completions → {sys_dir / '_yapm'}")
        return
    # user-local fallback
    local_dir = _user_home() / ".zsh/functions"
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / "_yapm").write_text(script)
    print(f"  Installed zsh completions → {local_dir / '_yapm'}")
    # add to fpath in .zshrc if not already there
    zshrc = _user_home() / ".zshrc"
    fpath_line = f'fpath=({local_dir} $fpath)'
    if zshrc.exists() and fpath_line in zshrc.read_text():
        return
    if zshrc.exists():
        with open(zshrc, "a") as f:
            f.write(f"\n# yapm completions\n{fpath_line}\nautoload -Uz compinit && compinit\n")

def _install_completions_fish(yapm_path: str):
    script = _FISH_COMPLETION
    # system-wide
    sys_dir = Path("/usr/share/fish/vendor_completions.d")
    if sys_dir.is_dir() and os.access(sys_dir, os.W_OK):
        (sys_dir / "yapm.fish").write_text(script)
        print(f"  Installed fish completions → {sys_dir / 'yapm.fish'}")
        return
    # user-local fallback
    local_dir = _user_home() / ".config/fish/completions"
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / "yapm.fish").write_text(script)
    print(f"  Installed fish completions → {local_dir / 'yapm.fish'}")

def _install_fetch_count(shell: str):
    """Patch neofetch/fastfetch to count yapm packages."""
    _patch_neofetch()
    _patch_fastfetch()

def _patch_neofetch():
    """Inject yapm into neofetch's get_packages() function."""
    neofetch_path = shutil.which("neofetch")
    if not neofetch_path:
        return
    try:
        content = Path(neofetch_path).read_text()
    except (OSError, PermissionError):
        return

    marker = "# yapm package manager"
    if marker in content:
        print(f"  neofetch already patched → {neofetch_path}")
        return

    # inject after the pacman line
    anchor = 'has pacman-key && tot pacman -Qq --color never'
    if anchor not in content:
        print(f"  Warning: could not find insertion point in {neofetch_path}")
        return

    injection = f'{anchor}\n            {marker}\n            has yapm && tot yapm list'
    content = content.replace(anchor, injection, 1)
    try:
        Path(neofetch_path).write_text(content)
        print(f"  Patched neofetch → {neofetch_path}")
    except (OSError, PermissionError) as e:
        print(f"  Warning: could not patch neofetch: {e}")

def _patch_fastfetch():
    """Add yapm to fastfetch's packages config."""
    fastfetch_path = shutil.which("fastfetch")
    if not fastfetch_path:
        return

    config_file = _user_home() / ".config/fastfetch/config.jsonc"
    config_file.parent.mkdir(parents=True, exist_ok=True)

    # read existing or create default
    if config_file.exists():
        try:
            content = config_file.read_text()
        except (OSError, PermissionError):
            return
    else:
        content = ""

    if "yapm" in content:
        print(f"  fastfetch already configured → {config_file}")
        return

    # parse existing modules or use defaults
    default_modules: List[Any] = ["title", "separator", "os", "kernel", "packages", "shell"]
    modules: List[Any] = default_modules

    if content.strip():
        try:
            import re
            # strip comments for parsing
            cleaned = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
            cleaned = re.sub(r'/\*.*?\*/', '', cleaned, flags=re.DOTALL)
            cfg = json.loads(cleaned)
            modules = cfg.get("modules", default_modules)
        except (json.JSONDecodeError, OSError):
            pass

    # insert yapm module before the last entry (usually "shell")
    yapm_module = {
        "type": "command",
        "key": "Packages (yapm)",
        "command": "yapm fetch-count 2>/dev/null"
    }
    if yapm_module not in modules:
        # put it after "packages" if present, else before the last entry
        if "packages" in modules:
            idx = modules.index("packages") + 1
            modules.insert(idx, yapm_module)
        else:
            modules.insert(-1, yapm_module)

    config = {"modules": modules}
    try:
        config_file.write_text(json.dumps(config, indent=4) + "\n")
        print(f"  Patched fastfetch → {config_file}")
    except (OSError, PermissionError) as e:
        print(f"  Warning: could not patch fastfetch: {e}")

def setup():
    """One-time setup: install shell completions and fetch-count integration."""
    marker_user = _user_home() / ".yapm" / ".setup_done"
    if SETUP_MARKER.exists() or marker_user.exists():
        print("yapm is already set up. To re-run: rm ~/.yapm/.setup_done && yapm setup")
        return

    shell = _detect_shell()
    yapm_path = shutil.which("yapm") or str(Path(__file__).resolve())

    print(f"Setting up yapm for {shell}...")

    if shell == "bash":
        _install_completions_bash(yapm_path)
    elif shell == "zsh":
        _install_completions_zsh(yapm_path)
    elif shell == "fish":
        _install_completions_fish(yapm_path)

    _install_fetch_count(shell)

    # rebuild zsh completion cache so new completions are picked up
    if shell == "zsh":
        user_home = _user_home()
        for f in user_home.glob(".zcompdump*"):
            try:
                f.unlink()
            except OSError:
                pass

    # make installed.json world-readable so neofetch/fastfetch can count packages
    try:
        DB_FILE.chmod(0o644)
        DB_FILE.parent.chmod(0o755)
    except (OSError, PermissionError):
        pass

    for marker in (SETUP_MARKER, marker_user):
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"shell": shell, "user": _detect_user()}))
        except (OSError, PermissionError):
            pass

    print(f"\nSetup complete. Open a new shell or run 'source ~/.{shell}rc' to activate.")


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
    try:
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
    except (OSError, PermissionError):
        # non-root read-only fallback (e.g. neofetch counting packages)
        try:
            with open(DB_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

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
    official = [m for m in config["mirrors"] if "yapm.pages.dev" in m["url"]]
    others = sorted((m for m in config["mirrors"] if "yapm.pages.dev" not in m["url"]),
                     key=lambda x: x["priority"])
    return official + others

def resolve_hall(hall_name: str) -> List[Dict]:
    """Resolve a hall name to its list of mirror dicts."""
    config = load_config()
    halls = config.get("halls", {})
    if hall_name not in halls:
        print(f"Error: Hall '{hall_name}' not found.")
        print(f"Available halls: {', '.join(sorted(halls.keys())) or '(none)'}")
        sys.exit(1)
    urls = halls[hall_name]
    mirrors = sorted_mirrors()
    url_to_mirror = {m["url"]: m for m in mirrors}
    result = []
    for url in urls:
        m = url_to_mirror.get(url)
        if m:
            result.append(m)
        else:
            result.append({"url": url, "priority": 99})
    return result

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
                        
                        print(f"\r\033[K{Color.CYAN}{desc}{Color.RESET} [{brown}{bar}{Color.RESET}] {Color.GREEN}{percent:3d}%{Color.RESET} {Color.DIM}({sz_str}){Color.RESET}", end="", flush=True)
                
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

def mirror_show(hall: Optional[str] = None, mirror_filter: Optional[str] = None):
    idx = load_index()
    packages = idx.get("packages", {})
    if not packages:
        print("No packages in index. Run 'yapm update' first.")
        return

    db = load_db()

    # resolve hall to a set of mirror URLs
    hall_urls = set()
    if hall:
        config = load_config()
        halls = config.get("halls", {})
        if hall not in halls:
            print(f"Hall '{hall}' not found. Available halls: {', '.join(sorted(halls.keys())) or '(none)'}")
            return
        hall_urls = set(halls[hall])

    # determine column widths from the data
    name_ver_parts = []
    for pkg_key, formats_entry in packages.items():
        for fmt_name in ("yapm", "arch", "deb", "nix"):
            entry = formats_entry.get(fmt_name)
            if not entry:
                continue
            # filter by hall or mirror
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

        # filter by hall or mirror
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

def parse_selection(sel: str, mirrors: List[Dict]) -> List[Dict]:
    """Parse a mirror selection string and return the matching mirrors.

    Supports:
        1-3     range (mirrors 1 through 3 inclusive)
        [1,5]   pinpoint (mirrors 1 and 5)
        3       single mirror
    Indices are 1-based, matching 'yapm mirror list' output.
    """
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

# ============================================================
# HALL COMMANDS
# ============================================================

def hall_add(selection: str, name: str):
    config = load_config()
    halls = config.get("halls", {})
    if name in halls:
        print(f"Hall '{name}' already exists. Use 'yapm hall remove {name}' first.")
        return
    mirrors = sorted_mirrors()
    chosen = parse_selection(selection, mirrors)
    if not chosen:
        print("No mirrors selected.")
        return
    halls[name] = [m["url"] for m in chosen]
    config["halls"] = halls
    save_config(config)
    print(f"Hall '{name}' created with {len(chosen)} mirror(s):")
    for m in chosen:
        print(f"  {m['url']}")

def hall_list():
    config = load_config()
    halls = config.get("halls", {})
    if not halls:
        print("No halls defined. Create one with 'yapm hall add <selection> <name>'.")
        return
    for name, urls in sorted(halls.items()):
        print(f"{Color.BOLD}{name}{Color.RESET} ({len(urls)} mirror(s))")

def hall_remove(name: str):
    config = load_config()
    halls = config.get("halls", {})
    if name not in halls:
        print(f"Hall '{name}' not found.")
        return
    del halls[name]
    config["halls"] = halls
    save_config(config)
    print(f"Hall '{name}' removed.")

def hall_show(name: str):
    config = load_config()
    halls = config.get("halls", {})
    if name not in halls:
        print(f"Hall '{name}' not found.")
        return
    urls = halls[name]
    mirrors = sorted_mirrors()
    url_to_mirror = {m["url"]: m for m in mirrors}
    print(f"Hall '{name}' — {len(urls)} mirror(s):")
    for url in urls:
        m = url_to_mirror.get(url)
        if m:
            print(f"  {url} (priority {m['priority']})")
        else:
            print(f"  {url} {Color.DIM}(not currently configured){Color.RESET}")

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
                # continuation line (e.g. multi-line Depends)
                if depends_continuation and current_pkg is not None:
                    current_pkg.setdefault("depends_raw", []).append(line.strip())
                continue

            depends_continuation = False

            if not line.strip():
                if current_pkg and "name" in current_pkg:
                    name = current_pkg["name"]
                    # Parse deb Depends: "pkg (>= ver), pkg2 | pkg3"
                    deps = []
                    for dep_str in current_pkg.get("depends_raw", []):
                        for part in dep_str.split(","):
                            # Take first alternative, drop version constraints
                            pkg_name = part.split("|")[0].strip().split("(")[0].strip()
                            # Skip virtual packages, alternatives markers, pre-deps
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
                                    "download_path": f"{repo}/os/{_host_arch()}/{name}-{version}-{arch}.pkg.tar.zst"
                                })
        except Exception as e:
            print(f"Error parsing Arch {repo} index: {e}")

_NIX_SEARCH_URL = "https://search.nixos.org/backend/latest-44-nixos-unstable/_search"
_NIX_AUTH = base64.b64encode(b"aWVSALXpZv:X8gPHnzL52wFEekuxsfQ9cSh").decode()

def _nix_available():
    return shutil.which("nix-env") is not None

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
    """Look up a specific version of a package.

    The index stores per-format entries as ``{pkg_name: {format: entry_dict}}``.
    When *arch_mode* is ``True`` only the ``"arch"`` sub-entry is considered;
    otherwise the priority order is ``yapm > arch > deb > nix``.
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
        # Normalize to nested-by-format structure
        if any(fmt in v for fmt in ("yapm", "arch", "deb", "nix")):
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

def fetch_package(pkg: str, mirror_url: Optional[str] = None, version: Optional[str] = None, arch_mode: bool = False, hall: Optional[str] = None) -> Optional[bytes]:
    idx = load_index()
    packages = idx.get("packages", {})
    pkg_entry = packages.get(pkg, {})

    # when a mirror is explicitly pinned, find the format that belongs to it
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

def _install_single(pkg_name: str, db: Dict, data: bytes, fmt: str):
    if config_flag("yapm.nativenationality") and fmt != "yapm":
        print("yapm.nativenationality is enabled — only native .yapm packages allowed")
        sys.exit(1)

    _check_cross_distro(fmt)

    # Determine extraction target:
    # - .yapm packages always go to sandbox (they have manifests)
    # - arch/deb always extract to ROOT_DIR (they're self-contained filesystem trees)
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

        # Link executables from non-standard bin dirs to /usr/local/bin
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

        # Also link usr/share subtree (data files, man pages, etc.)
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
            # when mirror is pinned, find the format that belongs to it
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
        print(f"  {_action('removed')} {_pkg(format_key(pkg_key))} ({removed} files).")
        # Clean up any symlinks in BIN_DIR pointing into this package's tree
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
            # For root-extracted packages, check against file_list
            for link in BIN_DIR.iterdir():
                if link.is_symlink():
                    try:
                        target = str(link.resolve())
                        if any(str((Path("/") / f.lstrip("./")).resolve()) == target for f in file_list):
                            link.unlink()
                    except Exception:
                        pass
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

def fetch_count():
    """Print installed package count for neofetch/fastfetch integration."""
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

def generate_yapm_data(target_dir: str):
    """Generate a template yapm.data file with all fields documented."""
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

    # check gh is available
    if not shutil.which("gh"):
        print("Error: 'gh' CLI is required. Install it from https://cli.github.com/")
        sys.exit(1)

    # When running via sudo, gh commands need to run as the original user
    # since gh auth is stored in the user's home directory.
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

        # get fork owner
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
    p_install.add_argument("-H", "--hall", type=str, default=None, metavar="NAME",
                           help="Only use mirrors from the named hall (see 'yapm hall add')")

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
    p_update = sub.add_parser(
        "update",
        help="Refresh the package index from all mirrors",
        description="Fetch and merge package lists from all configured mirrors into a local\n"
                    "index cache. Supports Debian/Ubuntu (Packages.gz), Arch (core.db),\n"
                    "and native YAPM (index.json) mirror formats.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_update.add_argument("-H", "--hall", type=str, default=None, metavar="NAME",
                          help="Only update from mirrors in the named hall (see 'yapm hall add')")
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

    # fetch-count
    sub.add_parser(
        "fetch-count",
        help="Print package count for neofetch/fastfetch",
        description="Output the number of installed packages in a format suitable\n"
                    "for neofetch/fastfetch package display lines.\n\n"
                    "Example neofetch config:\n"
                    "  info \"Packages\" fetch-count",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # completions
    p_completions = sub.add_parser(
        "completions",
        help="Generate shell completion scripts",
        description="Output a shell completion script for yapm.\n\n"
                    "Usage:\n"
                    "  eval \"$(yapm completions bash)\"   # bash (~/.bashrc)\n"
                    "  eval \"$(yapm completions zsh)\"    # zsh (~/.zshrc)\n"
                    "  yapm completions fish | source     # fish (~/.config/fish/config.fish)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_completions.add_argument("shell", choices=["bash", "zsh", "fish"],
                                help="Shell to generate completions for")

    # setup
    sub.add_parser(
        "setup",
        help="One-time setup: install completions and fetch-count",
        description="Detects your shell and installs:\n"
                    "  - Tab completion scripts\n"
                    "  - Package count for neofetch/fastfetch\n\n"
                    "Runs automatically after the first 'yapm install'.\n"
                    "To re-run: rm /var/lib/yapm/.setup_done && yapm setup",
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

    # riot (only available when yapm.riot is enabled)
    if config_flag("yapm.riot"):
        p_riot = sub.add_parser(
            "riot",
            help="Bootstrap the system by installing bash (riot mode)",
            description="Ensure bash is installed on the system. Intended for first-run\n"
                        "bootstrapping on Riot live ISOs. Requires yapm.riot to be enabled.",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        p_riot.add_argument("-r", "--root", type=str, default=None, metavar="PATH",
                            help="Install to a different root directory (requires yapm.insroot)")

    # build
    p_build = sub.add_parser(
        "build",
        help="Build a .yapm package from a source directory",
        description="Package a directory into a distributable .yapm file (tar.zst format).\n\n"
                    "  yapm build <dir>      — build a package from <dir>/yapm.data\n"
                    "  yapm build -f <dir>   — generate a template yapm.data in <dir>",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_build.add_argument("directory", metavar="DIR", nargs="?", default=".",
                         help="Path to the directory (default: current directory)")
    p_build.add_argument("-f", "--file", action="store_true",
                         help="Generate a template yapm.data instead of building")

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

    m_show = mirror_sub.add_parser(
        "show",
        help="Show all packages available in the mirror index",
        description="Display every package in the local index with version,\n"
                    "description, author, and license.\n\n"
                    "Optionally filter by hall name or mirror URL substring.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    m_show.add_argument("filter", nargs="?", default=None, metavar="hall|mirror",
                        help="Hall name or mirror URL substring to filter by")

    # hall
    p_hall = sub.add_parser(
        "hall",
        help="Manage mirror groups (halls)",
        description="A hall is a named group of mirrors. Use halls to quickly\n"
                    "switch between mirror subsets when installing or updating.\n\n"
                    "Mirror indices match 'yapm mirror list' output.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    hall_sub = p_hall.add_subparsers(dest="hall_cmd", required=True, metavar="<subcommand>")

    h_add = hall_sub.add_parser(
        "add",
        help="Create a hall from mirror indices",
        description="Select mirrors by range (1-3) or pinpoint ([1,5]) and\n"
                    "save them under a name.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    h_add.add_argument("selection", metavar="SELECTION",
                        help="Mirror selection: 1-3 (range), [1,5] (pinpoint), or 3 (single)")
    h_add.add_argument("name", metavar="NAME",
                        help="Name for this hall")

    hall_sub.add_parser(
        "list",
        help="List all halls",
        description="Print all defined halls with their mirror count.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    h_remove = hall_sub.add_parser(
        "remove",
        help="Remove a hall by name",
        description="Delete a hall. Does not remove the mirrors themselves.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    h_remove.add_argument("name", metavar="NAME",
                           help="Name of the hall to remove")

    h_show = hall_sub.add_parser(
        "show",
        help="Show mirrors in a hall",
        description="List all mirrors belonging to a named hall.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    h_show.add_argument("name", metavar="NAME",
                         help="Name of the hall to inspect")

    # su
    p_su = sub.add_parser(
        "su",
        help="Set up passwordless sudo for yapm",
        description="One-time setup: creates a sudoers rule so yapm never needs sudo again.\n\n"
                    "  yapm su              — set up passwordless sudo (run once)\n"
                    "  yapm su <cmd> [args] — re-run a yapm command with sudo\n\n"
                    "Creates /etc/sudoers.d/yapm-<user> so yapm can run as root\n"
                    "without prompting for a password.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_su.add_argument("extra", nargs=argparse.REMAINDER,
                       metavar="...",
                       help="Command and arguments to re-run as root (optional)")

    args = parser.parse_args()

    if args.command not in ("submit", "su", "completions", "fetch-count", "version", "setup", "list"):
        if args.command == "build" and getattr(args, "file", False):
            pass  # template generation doesn't need root
        else:
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
        install_package(args.package, args.format, mirror_index=args.mirror, root=args.root, noconfirm=args.noconfirm, dry_run=args.dry_run, hall=args.hall)
    elif args.command == "remove":
        remove_package(args.package, noconfirm=args.noconfirm)
    elif args.command == "list":
        list_installed(outdated=args.outdated, json_output=args.json)
    elif args.command == "info":
        info_package(args.package)
    elif args.command == "search":
        search_package(args.term)
    elif args.command == "update":
        update_index(hall=args.hall)
    elif args.command == "upgrade":
        upgrade_packages(refresh=args.refresh, dry_run=args.dry_run)
    elif args.command == "build":
        if args.file:
            generate_yapm_data(args.directory)
        else:
            build_package(args.directory)
    elif args.command == "submit":
        submit_package(args.package)
    elif args.command == "su":
        su_exec(args.extra)
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
        print(f"""
{Color.YAPM_BROWN}  __ _____ ____  __ _{Color.RESET}
{Color.YAPM_BROWN} / // / _ `/ _ \\/  ' \\{Color.RESET}
{Color.YAPM_BROWN} \\_, /\\_,_/ .__/_/_/_/{Color.RESET}
{Color.YAPM_BROWN}/___/    /_/{Color.RESET}          {Color.BOLD}v{ver}{Color.RESET}
""")
        print(f"  {_action('installed')} {Color.BOLD}yapm{_ver(f' v{ver}')}")
        pkgs = load_db()
        print(f"  {_action('packages')} {_pkg(str(len(pkgs)))} installed")
        if not config_flag("yapm.riot"):
            print(f"  {_action('hint')} riot features available via {Color.BOLD}yapm.conf{Color.RESET}")
    elif args.command == "fetch-count":
        fetch_count()
    elif args.command == "completions":
        completions_generate(args.shell)
    elif args.command == "setup":
        setup()
    elif args.command == "uninstall":
        uninstall_yapm()
    elif args.command == "riot":
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
        elif args.mirror_cmd == "show":
            f = getattr(args, "filter", None)
            # if it looks like a hall name (no dots/slashes), try hall first
            hall = None
            mirror_filter = None
            if f:
                config = load_config()
                if f in config.get("halls", {}):
                    hall = f
                else:
                    mirror_filter = f
            mirror_show(hall=hall, mirror_filter=mirror_filter)

    elif args.command == "hall":
        if args.hall_cmd == "add":
            hall_add(args.selection, args.name)
        elif args.hall_cmd == "list":
            hall_list()
        elif args.hall_cmd == "remove":
            hall_remove(args.name)
        elif args.hall_cmd == "show":
            hall_show(args.name)

    elif args.command == "config":
        if args.config_cmd == "list":
            yapm_config_list()
        elif args.config_cmd == "enable":
            yapm_config_enable(args.flag)
        elif args.config_cmd == "disable":
            yapm_config_disable(args.flag)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
