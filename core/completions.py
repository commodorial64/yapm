# shell completions
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, List

_BASH_COMPLETION = '''\
_yapm() {
    local cur prev commands
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    commands="install remove list info search update upgrade fetch version uninstall riot build submit outdated files why clean repair mirror hall su fetch-count completions config"

    if [[ ${cur} == -* ]]; then
        case "${COMP_WORDS[1]}" in
            install)  COMPREPLY=( $(compgen -W "-m -r -y -n -H -f --mirror --root --noconfirm --dry-run --hall --format" -- ${cur}) ) ;;
            remove)   COMPREPLY=( $(compgen -W "-y --noconfirm" -- ${cur}) ) ;;
            list)     COMPREPLY=( $(compgen -W "-o -j --outdated --json" -- ${cur}) ) ;;
            upgrade)  COMPREPLY=( $(compgen -W "-y -n --refresh --dry-run" -- ${cur}) ) ;;
            mirror)   COMPREPLY=( $(compgen -W "add list remove sync test show" -- ${cur}) ) ;;
            hall)     COMPREPLY=( $(compgen -W "add list remove show" -- ${cur}) ) ;;
            completions) COMPREPLY=( $(compgen -W "bash zsh fish" -- ${cur}) ) ;;
            config)   COMPREPLY=( $(compgen -W "list enable disable" -- ${cur}) ) ;;
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
        'config:View and toggle yapm configuration flags'
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
                config)
                    _arguments '1:subcommand:(list enable disable)' && ret=0
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
complete -c yapm -n '__fish_use_subcommand' -a config -d 'View and toggle yapm configuration flags'
complete -c yapm -n '__fish_seen_subcommand_from config' -a 'list enable disable'

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
    scripts = {
        "bash": _BASH_COMPLETION,
        "zsh": _ZSH_COMPLETION,
        "fish": _FISH_COMPLETION,
    }
    if shell not in scripts:
        print(f"Error: unsupported shell '{shell}'. Choose from: bash, zsh, fish")
        sys.exit(1)
    print(scripts[shell])

# detect user shell, using /etc/passwd when running via sudo
def _detect_shell() -> str:
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

# non-root user who invoked this
def _detect_user() -> str:
    for var in ("SUDO_USER", "LOGNAME", "USER"):
        u = os.environ.get(var, "")
        if u and u != "root":
            return u
    return "root"

# real user's home (resolves through SUDO_USER)
def _user_home() -> Path:
    user = _detect_user()
    if user != "root":
        return Path(f"/home/{user}")
    return Path.home()

def _install_completions_bash(yapm_path: str):
    script = _BASH_COMPLETION
    sys_dir = Path("/etc/bash_completion.d")
    if sys_dir.is_dir() and os.access(sys_dir, os.W_OK):
        (sys_dir / "yapm").write_text(script)
        print(f"  Installed bash completions → {sys_dir / 'yapm'}")
        return
    local_dir = _user_home() / ".local/share/bash-completion/completions"
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / "yapm").write_text(script)
    print(f"  Installed bash completions → {local_dir / 'yapm'}")

def _install_completions_zsh(yapm_path: str):
    script = _ZSH_COMPLETION
    sys_dir = Path("/usr/share/zsh/site-functions")
    if sys_dir.is_dir() and os.access(sys_dir, os.W_OK):
        (sys_dir / "_yapm").write_text(script)
        print(f"  Installed zsh completions → {sys_dir / '_yapm'}")
        return
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
    sys_dir = Path("/usr/share/fish/vendor_completions.d")
    if sys_dir.is_dir() and os.access(sys_dir, os.W_OK):
        (sys_dir / "yapm.fish").write_text(script)
        print(f"  Installed fish completions → {sys_dir / 'yapm.fish'}")
        return
    local_dir = _user_home() / ".config/fish/completions"
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / "yapm.fish").write_text(script)
    print(f"  Installed fish completions → {local_dir / 'yapm.fish'}")

# patch neofetch/fastfetch to count yapm packages
def _install_fetch_count(shell: str):
    _patch_neofetch()
    _patch_fastfetch()

def _patch_neofetch():
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
    fastfetch_path = shutil.which("fastfetch")
    if not fastfetch_path:
        return

    config_file = _user_home() / ".config/fastfetch/config.jsonc"
    config_file.parent.mkdir(parents=True, exist_ok=True)

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

    default_modules: List[Any] = ["title", "separator", "os", "kernel", "packages", "shell"]
    modules: List[Any] = default_modules

    if content.strip():
        try:
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
