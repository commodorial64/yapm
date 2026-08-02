# chaos mode
import itertools
import random
import sys
import time

from .config import config_flag

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
