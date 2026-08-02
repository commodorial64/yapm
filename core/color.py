# color helpers
import sys

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
