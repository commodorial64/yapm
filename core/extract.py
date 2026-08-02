# pkg extract engines
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import List

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
        # not under <root>/tmp — arch-chroot bind-mounts host /tmp over it
        hook_dir = root / "var" / "lib" / "yapm"
        tmp_hook = hook_dir / "_install_hook.sh"
        hook_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(install_file, tmp_hook)
        os.chmod(tmp_hook, 0o755)
        script = f"source /var/lib/yapm/_install_hook.sh && if type {phase} >/dev/null 2>&1; then {phase}; fi"
        print(f"  Running {phase} hook...")
        if str(root) != "/":
            if not shutil.which("arch-chroot"):
                print(f"  Warning: arch-chroot not found, skipping {phase} hook.")
            else:
                subprocess.run(["arch-chroot", str(root), "bash", "-c", script], check=False)
        else:
            subprocess.run(["bash", "-c", script], check=False)
        tmp_hook.unlink(missing_ok=True)
