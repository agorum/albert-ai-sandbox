"""Per-sandbox disk images (hard disk limit).

Every sandbox gets a sparse ext4 image file of its configured size that is
loop-mounted on the host and bind-mounted into the container:

  <storage>/disks/<name>.img          sparse image file (grows on demand)
  <storage>/mnt/<name>/               mount point (immutable while unmounted)
  <storage>/mnt/<name>/home        -> /home/ubuntu in the container
  <storage>/mnt/<name>/files       -> /tmp/albert-files (upload directory)

"Full" means full: writes beyond the size fail with ENOSPC inside the
sandbox and can never fill the host disk. The mount point directory carries
the immutable attribute while nothing is mounted, so a container that is
started without its image mounted fails instead of silently writing to the
host root filesystem. Mounting uses ``discard`` so deleted files give space
back to the host.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import dockerctl
from .config import settings

GIB = 1024 ** 3
MOUNT_OPTIONS = "loop,nodev,nosuid,discard"


class StorageError(RuntimeError):
    pass


def _cmd(args: List[str], timeout: float = 120, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise StorageError(f"{args[0]} not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise StorageError(f"{args[0]} timed out") from exc
    if check and proc.returncode != 0:
        raise StorageError(f"{' '.join(args[:2])} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def image_path(name: str) -> Path:
    return settings().disks_dir / f"{name}.img"


def mount_path(name: str) -> Path:
    return settings().mounts_dir / name


def home_path(name: str) -> Path:
    return mount_path(name) / "home"


def files_path(name: str) -> Path:
    return mount_path(name) / "files"


def is_mounted(name: str) -> bool:
    return os.path.ismount(str(mount_path(name)))


def _set_immutable(path: Path, flag: bool) -> None:
    _cmd(["chattr", "+i" if flag else "-i", str(path)], check=False)


def _ensure_dirs() -> None:
    s = settings()
    for path in (s.disks_dir, s.mounts_dir, s.skel_dir):
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)


def _prepare_mountpoint(name: str) -> Path:
    target = mount_path(name)
    if not target.exists():
        target.mkdir(parents=True)
    if not is_mounted(name):
        _set_immutable(target, True)
    return target


def mount(name: str) -> None:
    if is_mounted(name):
        return
    img = image_path(name)
    if not img.exists():
        raise StorageError(f"disk image missing: {img}")
    target = _prepare_mountpoint(name)
    _cmd(["mount", "-o", MOUNT_OPTIONS, str(img), str(target)])


def umount(name: str) -> None:
    if not is_mounted(name):
        return
    proc = _cmd(["umount", str(mount_path(name))], check=False)
    if proc.returncode != 0 and is_mounted(name):
        # Busy (e.g. container still shutting down): detach lazily.
        _cmd(["umount", "-l", str(mount_path(name))])


def create(name: str, size_gb: int) -> None:
    """Create, format and mount a fresh disk image."""
    _ensure_dirs()
    img = image_path(name)
    if img.exists():
        raise StorageError(f"disk image already exists: {img}")
    with open(img, "wb") as fh:
        fh.truncate(int(size_gb) * GIB)
    os.chmod(img, 0o600)
    try:
        _cmd(["mkfs.ext4", "-q", "-F", "-m", "0", "-L", name[-16:],
              "-E", "lazy_itable_init=1,lazy_journal_init=1,root_owner=0:0", str(img)], timeout=300)
        mount(name)
        home = home_path(name)
        files = files_path(name)
        home.mkdir(exist_ok=True)
        files.mkdir(exist_ok=True)
        os.chmod(files, 0o1777)
        lost = mount_path(name) / "lost+found"
        if lost.exists():
            os.chmod(lost, 0o700)
    except BaseException:
        remove(name)
        raise


def _skeleton(image: str) -> Path:
    """Tar of /home/ubuntu from the image, cached per image id."""
    _ensure_dirs()
    image_id = dockerctl.image_id(image)
    if not image_id:
        raise StorageError(f"docker image not found: {image}")
    cache = settings().skel_dir / f"{image_id.replace(':', '_')}.tar"
    if not cache.exists():
        data = dockerctl.export_home_tar(image)
        tmp = cache.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(cache)
        # Drop skeletons of older images.
        for old in settings().skel_dir.glob("*.tar"):
            if old != cache:
                old.unlink(missing_ok=True)
    return cache


def populate_home(name: str, image: str) -> None:
    """Copy the image's /home/ubuntu into the (empty) sandbox home.

    GNU tar also applies owner and mode of the archive's "./" entry to the
    target directory, so the home ends up owned by the container user.
    """
    skeleton = _skeleton(image)
    _cmd(["tar", "-xpf", str(skeleton), "--numeric-owner", "-C", str(home_path(name))], timeout=300)


def remove(name: str) -> None:
    """Unmount and delete the disk image and mount point (idempotent)."""
    umount(name)
    target = mount_path(name)
    if target.exists() and not is_mounted(name):
        _set_immutable(target, False)
        try:
            target.rmdir()
        except OSError:
            # Should be empty when unmounted; if not, something wrote to the
            # host fs - remove it anyway, it is not sandbox data.
            shutil.rmtree(target, ignore_errors=True)
    image_path(name).unlink(missing_ok=True)


def exists(name: str) -> bool:
    return image_path(name).exists()


def usage(name: str) -> Optional[Tuple[int, int]]:
    """(used_bytes, size_bytes) of a sandbox disk, or None if unknown."""
    if is_mounted(name):
        st = os.statvfs(str(mount_path(name)))
        size = st.f_blocks * st.f_frsize
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        return used, size
    img = image_path(name)
    if img.exists():
        st = img.stat()
        return st.st_blocks * 512, st.st_size
    return None


def resize(name: str, new_size_gb: int) -> None:
    """Grow a disk image online (shrinking is not supported)."""
    img = image_path(name)
    if not img.exists():
        raise StorageError("disk image missing")
    current = img.stat().st_size
    new_size = int(new_size_gb) * GIB
    if new_size < current:
        raise StorageError("shrinking a sandbox disk is not supported")
    if new_size == current:
        return
    with open(img, "r+b") as fh:
        fh.truncate(new_size)
    if is_mounted(name):
        source = _cmd(["findmnt", "-n", "-o", "SOURCE", str(mount_path(name))]).stdout.strip()
        if source.startswith("/dev/loop"):
            _cmd(["losetup", "-c", source])
            _cmd(["resize2fs", source], timeout=600)
            return
    _cmd(["e2fsck", "-f", "-p", str(img)], check=False, timeout=600)
    _cmd(["resize2fs", str(img)], timeout=600)


def list_names() -> Dict[str, Dict[str, bool]]:
    """All disk images / mount points found on the host."""
    s = settings()
    names: Dict[str, Dict[str, bool]] = {}
    if s.disks_dir.exists():
        for img in s.disks_dir.glob("*.img"):
            names.setdefault(img.stem, {})["image"] = True
    if s.mounts_dir.exists():
        for entry in s.mounts_dir.iterdir():
            if entry.is_dir():
                names.setdefault(entry.name, {})["mountpoint"] = True
    return names


def filesystem_usage(path: Path) -> Optional[Dict[str, int]]:
    try:
        st = os.statvfs(str(path))
    except OSError:
        return None
    return {
        "totalBytes": st.f_blocks * st.f_frsize,
        "freeBytes": st.f_bavail * st.f_frsize,
        "usedBytes": (st.f_blocks - st.f_bfree) * st.f_frsize,
    }


def dir_size(path: str) -> Optional[int]:
    proc = _cmd(["du", "-sb", path], check=False, timeout=600)
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.split()[0])
    except (IndexError, ValueError):
        return None
