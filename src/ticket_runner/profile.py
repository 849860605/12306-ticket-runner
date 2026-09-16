"""Lease the dedicated profile and remove dead Docker singleton symlinks only."""

import fcntl
import re
import socket
from contextlib import contextmanager
from pathlib import Path

from .domain import NeedsAttention


def clear_container_singletons(profile: Path):
    # Chromium refuses a profile marked with a previous container's hostname even
    # when its process and /tmp socket disappeared. Production callers also hold
    # runner.lock for the data volume; the profile lease covers direct adapters.
    if not Path("/.dockerenv").exists():
        return
    lock = profile / "SingletonLock"
    if not lock.is_symlink():
        return
    target = str(lock.readlink())
    match = re.fullmatch(r"([0-9a-f]{12,64})-(\d+)", target)
    if not match or match[1] == socket.gethostname():
        return
    markers = [profile / name for name in ("SingletonLock", "SingletonSocket", "SingletonCookie")]
    if any(p.exists() for p in markers):
        raise NeedsAttention("浏览器配置目录仍有活动锁，请关闭占用该目录的浏览器后重试")
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            arguments = (process / "cmdline").read_bytes().split(b"\0")
        except FileNotFoundError:
            continue
        except PermissionError:
            raise NeedsAttention("无法确认浏览器配置目录是否被占用，保留原锁") from None
        if f"--user-data-dir={profile}".encode() in arguments:
            raise NeedsAttention("已有浏览器占用配置目录，请关闭后重试")
    for marker in markers:
        if marker.is_symlink():
            marker.unlink()


@contextmanager
def profile_lease(data_dir: Path):
    with (data_dir / "browser-profile.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NeedsAttention("已有浏览器占用此配置目录") from None
        try:
            clear_container_singletons(data_dir / "browser-profile")
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
