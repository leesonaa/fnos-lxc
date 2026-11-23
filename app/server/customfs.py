"""自定义 rootfs 创建 / 导出的纯函数部分（便于单测）。"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

TAR_EXTS = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.zst", ".tar.bz2", ".tbz2")
SQUASH_EXTS = (".squashfs", ".sqfs", ".squash")
ROOT_DIRS = {"bin", "boot", "dev", "etc", "home", "lib", "lib32", "lib64", "media", "mnt", "nix", "opt",
             "proc", "root", "run", "sbin", "srv", "sys", "tmp", "usr", "var"}
ARCH_MAP = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64",
            "armhf": "armv7l", "armv7l": "armv7l", "i386": "i686", "i686": "i686",
            "riscv64": "riscv64", "ppc64el": "ppc64le", "s390x": "s390x"}
_SYSTEM_SRC = ("/proc", "/sys", "/dev", "/run")


def classify(path_or_url: str) -> str:
    """返回 'tar' / 'squashfs' / ''（不支持）。"""
    p = path_or_url.split("?", 1)[0].split("#", 1)[0].lower()
    if p.endswith(TAR_EXTS):
        return "tar"
    if p.endswith(SQUASH_EXTS):
        return "squashfs"
    return ""


def ext_of(path_or_url: str) -> str:
    p = path_or_url.split("?", 1)[0].split("#", 1)[0].lower()
    for e in sorted(TAR_EXTS + SQUASH_EXTS, key=len, reverse=True):
        if p.endswith(e):
            return e
    return ""


def check_source_path(p: str) -> tuple[str, str]:
    """本地文件路径：返回 (规范化路径, 错误)。"""
    p = str(p or "").strip()
    if not p.startswith("/"):
        return "", "请填写压缩包的绝对路径，例如 /vol1/1000/images/rootfs.tar.xz"
    if any(ord(c) < 32 or c == "\x7f" for c in p) or ".." in p.split("/"):
        return "", "路径含非法字符"
    for d in _SYSTEM_SRC:
        if p == d or p.startswith(d + "/"):
            return "", "不能使用系统目录下的文件"
    if not classify(p):
        return "", "不支持的文件类型（支持 .tar / .tar.gz / .tar.xz / .tar.zst / .tar.bz2 / .squashfs）"
    return p, ""


def check_url(u: str) -> tuple[str, str]:
    u = str(u or "").strip()
    if not re.match(r"^https?://[^\s/]+(/\S*)?$", u):
        return "", "URL 必须是 http(s):// 开头的地址"
    if not classify(u):
        return "", "URL 指向的文件类型不支持（需 .tar.* 或 .squashfs）"
    return u, ""


def check_members(names: list[str]) -> tuple[bool, str]:
    """tar 条目预检：拒绝绝对路径与 .. 穿越。"""
    for n in names:
        if n.startswith("/"):
            return False, f"压缩包含绝对路径条目：{n[:80]}"
        if ".." in PurePosixPath(n).parts:
            return False, f"压缩包含路径穿越条目：{n[:80]}"
    return True, ""


def strip_components(names: list[str]) -> int:
    """全部条目都在同一个非系统目录下（如 rootfs/、debian-12/）时返回 1，否则 0。"""
    tops = set()
    for n in names:
        parts = [x for x in PurePosixPath(n).parts if x not in (".", "/")]
        if parts:
            tops.add(parts[0])
        if len(tops) > 1:
            return 0
    if len(tops) == 1 and next(iter(tops)) not in ROOT_DIRS:
        return 1
    return 0


def init_kind(exists) -> str:
    """exists(相对 rootfs 的路径) -> bool。返回 'standard' / 'init' (仅 /init，如 NixOS) / ''。"""
    for p in ("sbin/init", "usr/sbin/init", "lib/systemd/systemd", "usr/lib/systemd/systemd", "bin/busybox", "sbin/openrc-init"):
        if exists(p):
            return "standard"
    if exists("init"):
        return "init"
    return ""


def build_config(name: str, rootfs: str, distro: str, release: str, arch: str,
                 default_conf: str, include_path: str, init_cmd: str = "") -> str:
    lines = [
        "# Template used to create this container: custom-rootfs (fnos-lxc)",
        f"# Parameters passed to the template: -d {distro or 'linux'}" + (f" -r {release}" if release else ""),
        f"lxc.include = {include_path}",
        f"lxc.arch = {ARCH_MAP.get(arch, arch) or 'x86_64'}",
    ]
    for ln in default_conf.splitlines():
        s = ln.strip()
        if s and not s.startswith("#") and not s.startswith(("lxc.rootfs", "lxc.uts", "lxc.arch", "lxc.include")):
            lines.append(s)
    lines += [f"lxc.rootfs.path = dir:{rootfs}", f"lxc.uts.name = {name}"]
    if init_cmd:
        lines.append(f"lxc.init.cmd = {init_cmd}")
    return "\n".join(lines) + "\n"


# ELF e_machine -> 内核架构名
_ELF_MACHINE = {0x3E: "x86_64", 0xB7: "aarch64", 0x28: "armv7l", 0x03: "i686", 0xF3: "riscv64"}
_ELF_PROBES = ("bin/sh", "usr/bin/env", "bin/busybox", "sbin/init", "usr/bin/sh", "bin/ls", "usr/bin/ls")


def resolve_in_root(root, rel: str, depth: int = 12):
    """在 rootfs 内解析符号链接（绝对链接按 rootfs 根解释），返回真实文件 Path 或 None。"""
    from pathlib import Path
    root = Path(root)
    cur = root / rel
    for _ in range(depth):
        if not cur.is_symlink():
            return cur if cur.is_file() else None
        t = cur.readlink() if hasattr(cur, "readlink") else Path(__import__("os").readlink(cur))
        cur = root / str(t).lstrip("/") if str(t).startswith("/") else cur.parent / t
    return None


def elf_arch(root) -> str:
    """读取 rootfs 内常见可执行文件的 ELF 头，返回 'x86_64' / 'aarch64' / ...；识别不了返回 ''。"""
    for rel in _ELF_PROBES:
        f = resolve_in_root(root, rel)
        if not f:
            continue
        try:
            with open(f, "rb") as fh:
                h = fh.read(20)
        except OSError:
            continue
        if h[:4] != b"\x7fELF" or len(h) < 20:
            continue
        little = h[5] == 1
        em = int.from_bytes(h[18:20], "little" if little else "big")
        return _ELF_MACHINE.get(em, "")
    return ""


def arch_compatible(host: str, guest: str) -> bool:
    """宿主机能否原生运行该架构的 rootfs（不含 qemu 模拟）。"""
    host, guest = ARCH_MAP.get(host, host), ARCH_MAP.get(guest, guest)
    if not guest or host == guest:
        return True
    return host == "x86_64" and guest == "i686"  # x86_64 内核可运行 32 位 x86 用户态
