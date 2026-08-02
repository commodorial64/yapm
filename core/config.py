# config
import json
import sys
from pathlib import Path
from typing import Dict, List

from .paths import CONFIG_FILE, DEFAULT_CONFIG, KNOWN_FLAGS, YAPM_CONF_SYSTEM, YAPM_CONF_USER

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

def sorted_mirrors() -> List[Dict]:
    config = load_config()
    official = [m for m in config["mirrors"] if "yapm.pages.dev" in m["url"]]
    others = sorted((m for m in config["mirrors"] if "yapm.pages.dev" not in m["url"]),
                     key=lambda x: x["priority"])
    return official + others

# hall name -> list of mirror dicts
def resolve_hall(hall_name: str) -> List[Dict]:
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
