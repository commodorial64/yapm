# config cmds
import sys

from .config import load_yapm_conf, save_yapm_conf
from .paths import KNOWN_FLAGS

HIDDEN_FLAGS = {"yapm.yapm"}

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
