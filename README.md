# yapm
## yet another package manager

**Made in** ![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54) **with love and convienience**

YAPM is a package manager (duh) aimed to be an all-in-one tool for package management across all platforms (by parsing their seperate package types or like formats idk the term).

I've made it open source. I will accept and HAPPILY take any contributions.

# Adding packages
Now getting packages to the yapm mirror by yourself is impossible as of current. the easiest way is to go to the [**contribution repo**](https://github.com/galaxyg144/yapm-contrib) make a PR and wait for me or any approved "admins" to review it. Then it'll be validated and copied!

*orrrr.*

Just make your own mirror. I dont mind.


# Usage
```
yapm — Yet Another Package Manager
Supports native .yapm packages as well as .deb (Debian/Ubuntu) and
Arch Linux packages (.pkg.tar.zst) via upstream mirrors.

Run 'yapm update' first to build the local package index.

positional arguments:
  <command>
    install            Install a package from a mirror or a local file
    remove             Remove an installed package
    list               List all installed packages
    info               Show details about a package
    search             Search the local package index
    update             Refresh the package index from all mirrors
    upgrade            Upgrade all installed packages to their latest versions
    fetch              Update yapm itself.
    version            Print yapm version information
    uninstall          Uninstall yapm itself from the system
    build              Build a .yapm package from a source directory
    mirror             Manage package mirrors

options:
  -h, --help           show this help message and exit
  -f, --format FORMAT  Override the package format for local installs (yapm | deb | arch). Auto-detected from file extension when installing a local file.
```
(yes that is the help menu. bite me.)

# Installation
It's honestly simple.

If you want to use it alongside your main package manager its as simple as running the [install script](https://github.com/galaxyg144/yapm/blob/main/install.sh) when you clone the repo

Any other use case thats out of my knowledge.
