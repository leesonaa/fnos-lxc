"""容器 rootfs 的存储形态：目录（dir:）或 ext4 镜像（loop:）。

为什么需要 ext4 镜像：fnOS 的 Btrfs 卷带 trimacl 挂载选项，创建目录时不遵循 umask
（普通用户创建出 0775），NixOS 这类对权限位很严格的系统会拒绝构建结果。
把 rootfs 放进 loop 挂载的 ext4 镜像后行为恢复标准 POSIX。
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_KV = re.compile(r"^\s*lxc\.rootfs\.path\s*=\s*(\S+)\s*$")
MIN_IMG_GB = 1
MAX_IMG_GB = 4096


class RootfsError(Exception):
    pass


def parse_rootfs(config_text: str) -> tuple[str, str]:
    """返回 (kind, path)：kind 为 dir / loop / other / ''（没有 rootfs 配置）。取最后一条生效的非注释行。"""
    val = ""
    for ln in config_text.splitlines():
        m = _KV.match(ln)
        if m:
            val = m.group(1)
    if not val:
        return "", ""
    if val.startswith("loop:"):
        return "loop", val[5:]
    if val.startswith("dir:"):
        return "dir", val[4:]
    if val.startswith("/"):
        return "dir", val
    return "other", val


def image_path(config_text: str) -> Path | None:
    kind, v = parse_rootfs(config_text)
    return Path(v) if kind == "loop" and v.startswith("/") else None


def valid_size_gb(v) -> tuple[int, str]:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return 0, "镜像大小无效"
    if n < MIN_IMG_GB or n > MAX_IMG_GB:
        return 0, f"镜像大小需在 {MIN_IMG_GB}–{MAX_IMG_GB} GB 之间"
    return n, ""


def check_size_vs_free(size_gb: int, free_bytes: int, already: int = 0) -> str:
    """稀疏镜像是「超卖」的：写满宿主盘会让镜像内的 ext4 出现 I/O 错误，所以不允许超过剩余空间。"""
    need = size_gb * (1 << 30) - already
    if need > free_bytes:
        return f"镜像最大 {size_gb} GB 超过磁盘剩余空间 {free_bytes >> 30} GB（稀疏文件写满宿主盘会损坏镜像内文件系统）"
    return ""


def alloc_bytes(path: Path) -> int:
    """已实际占用的字节数（稀疏文件按块计算）。"""
    try:
        return os.stat(path).st_blocks * 512
    except OSError:
        return 0


def apparent_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _run(argv, timeout=120):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)


def is_attached(img: Path) -> bool:
    """镜像是否已被某个 loop 设备占用（容器运行中、或被手动挂载）。"""
    rc, out, _ = _run(["losetup", "-j", str(img)], 15)
    return rc == 0 and bool(out.strip())


def mkfs_image(img: Path, size_gb: int, label: str = "rootfs") -> str:
    """创建稀疏 ext4 镜像。返回错误信息（空串成功）。Btrfs 上对空文件设 +C（关闭写时复制，避免严重碎片）。"""
    try:
        img.parent.mkdir(parents=True, exist_ok=True)
        with open(img, "xb") as f:
            pass
        _run(["chattr", "+C", str(img)], 10)  # 失败（非 btrfs 等）忽略
        with open(img, "r+b") as f:
            f.truncate(size_gb * (1 << 30))
    except OSError as e:
        return f"创建镜像文件失败：{e}"
    # -i 8192：每 8KB 一个 inode，小文件多的系统（Nix / node_modules）不会先耗尽 inode
    rc, _, err = _run(["mkfs.ext4", "-q", "-F", "-m", "0", "-i", "8192", "-L", label[:16], str(img)], 600)
    if rc != 0:
        with contextlib.suppress(OSError):
            img.unlink()
        return "mkfs.ext4 失败：" + err.strip()[-200:]
    return ""


@contextlib.contextmanager
def mounted_image(img: Path, rw: bool = True):
    """把镜像临时 loop 挂载到一个私有目录，用完卸载。镜像已被占用时拒绝（避免同一 ext4 双重读写挂载造成损坏）。"""
    if is_attached(img):
        raise RootfsError("镜像正被占用（容器运行中或已被挂载），请先停止容器")
    mnt = tempfile.mkdtemp(prefix="fnlxc-img-")
    opts = "loop" + ("" if rw else ",ro")
    rc, _, err = _run(["mount", "-t", "ext4", "-o", opts, str(img), mnt], 120)
    if rc != 0 and not rw:
        # 日志未回放的镜像无法只读挂载，退而用 noload（只读、不回放日志）
        rc, _, err = _run(["mount", "-t", "ext4", "-o", "loop,ro,noload", str(img), mnt], 120)
    if rc != 0:
        shutil.rmtree(mnt, ignore_errors=True)
        raise RootfsError("挂载镜像失败：" + err.strip()[-200:])
    try:
        yield Path(mnt)
    finally:
        rc, _, _ = _run(["umount", mnt], 120)
        if rc != 0:
            _run(["umount", "-l", mnt], 60)
        with contextlib.suppress(OSError):
            os.rmdir(mnt)


@contextlib.contextmanager
def access(config_path: Path, pid: int = 0, rw: bool = True):
    """取得可读写的 rootfs 路径。
    - dir：直接用目录（运行与否都一样，保持原有行为）
    - loop 运行中：/proc/<pid>/root（镜像已被 LXC 挂载，不能再挂一次）
    - loop 已停止：临时挂载镜像
    没有可用 rootfs 时 yield None。
    """
    try:
        text = config_path.read_text(errors="ignore")
    except OSError:
        yield None
        return
    kind, val = parse_rootfs(text)
    if kind == "dir":
        p = Path(val)
        yield p if p.is_dir() else None
    elif kind == "loop" and pid:
        p = Path(f"/proc/{pid}/root")
        yield p if p.is_dir() else None
    elif kind == "loop" and val.startswith("/") and Path(val).is_file():
        with mounted_image(Path(val), rw) as m:
            yield m
    elif kind == "":
        p = config_path.parent / "rootfs"
        yield p if p.is_dir() else None
    else:
        yield None


def resize_image(img: Path, new_gb: int) -> str:
    """离线扩容：truncate → e2fsck → resize2fs。返回错误信息（空串成功）。调用方保证容器已停止。"""
    if is_attached(img):
        return "镜像正被占用，请先停止容器"
    cur = apparent_bytes(img)
    if new_gb * (1 << 30) <= cur:
        return f"只支持扩容：新大小需大于当前的 {cur >> 30} GB"
    try:
        with open(img, "r+b") as f:
            f.truncate(new_gb * (1 << 30))
    except OSError as e:
        return f"扩展镜像文件失败：{e}"
    rc, _, err = _run(["e2fsck", "-f", "-y", str(img)], 3600)
    if rc not in (0, 1):  # 1 = 已修复
        return "e2fsck 失败：" + err.strip()[-200:]
    rc, _, err = _run(["resize2fs", str(img)], 3600)
    if rc != 0:
        return "resize2fs 失败：" + err.strip()[-200:]
    return ""
