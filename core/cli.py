# cli
import argparse
import sys

from .build import build_package, generate_yapm_data, submit_package
from .color import Color, _action, _pkg, _ver
from .completions import completions_generate
from .config import config_flag, load_config
from .configcmd import yapm_config_disable, yapm_config_enable, yapm_config_list
from .db import load_db
from .halls import hall_add, hall_list, hall_remove, hall_show
from .index import update_index
from .install import init_package, install_package, remove_package, upgrade_packages
from .mirrors import mirror_add, mirror_list, mirror_remove, mirror_preset, mirror_refresh, mirror_show, mirror_test
from .paths import APP_VERSION, LOCK_FILE
from .query import clean_cache, fetch_count, info_package, list_files, list_installed, outdated_packages, repair_package, search_package, why_package
from .selfupdate import uninstall_yapm, update_yapm
from .setup import ensure_dirs, setup
from .utils import require_root, su_exec

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
                           help="Install to a different root directory")
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

    # riot
    p_riot = sub.add_parser(
        "riot",
        help="Bootstrap the system by installing bash (riot mode)",
        description="Ensure bash is installed on the system. Intended for first-run\n"
                    "bootstrapping on Riot live ISOs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_riot.add_argument("-r", "--root", type=str, default=None, metavar="PATH",
                        help="Install to a different root directory")

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

    # config
    p_config = sub.add_parser(
        "config",
        help="View and toggle yapm configuration flags",
        description="List or toggle yapm configuration flags in ~/.config/yapm/yapm.conf.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    config_sub = p_config.add_subparsers(dest="config_cmd", required=True, metavar="<action>")

    p_config_list = config_sub.add_parser("list", help="List configuration flags and their state")
    p_config_enable = config_sub.add_parser("enable", help="Turn a configuration flag on")
    p_config_enable.add_argument("flag", metavar="FLAG", help="Name of the flag to enable")
    p_config_disable = config_sub.add_parser("disable", help="Turn a configuration flag off")
    p_config_disable.add_argument("flag", metavar="FLAG", help="Name of the flag to disable")

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
        "reset",
        help="Restore the default mirror list",
        description="Discard all custom mirrors and restore yapm's built-in defaults.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

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

    if args.command not in ("submit", "su", "completions", "fetch-count", "version", "setup", "list", "config"):
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
        elif args.mirror_cmd == "reset":
            mirror_preset()
        elif args.mirror_cmd == "sync":
            mirror_refresh()
        elif args.mirror_cmd == "test":
            mirror_test()
        elif args.mirror_cmd == "list":
            mirror_list()
        elif args.mirror_cmd == "show":
            f = getattr(args, "filter", None)
            # if it looks like a hall name, try hall first
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
