# hall cmds
from .color import Color
from .config import load_config, save_config, sorted_mirrors
from .mirrors import parse_selection

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
