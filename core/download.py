# download + archive helpers
import ast
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import urllib.error
import zipfile
from pathlib import Path
from typing import Optional

from .color import Color
from .config import config_flag

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

# ZIP or ZSTD magic, so 404 HTML pages aren't treated as packages
def is_valid_zip(data: bytes) -> bool:
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

    content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('//'):
            continue

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
