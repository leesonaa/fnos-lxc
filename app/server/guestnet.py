"""容器内部网络配置同步。

LXC 的 lxc.net.0.ipv4.address 只会在宿主机侧给 eth0 加上静态地址，
而容器镜像自带的网络服务（ifupdown / systemd-networkd / netplan）仍会在 eth0 上跑 DHCP，
于是同一块网卡出现「静态地址 + 自动分配地址」。

这里在 IPv4 设为静态时关闭容器内 eth0 的 DHCP，改回 DHCP 时再还原。
原文件首次修改前备份为 <file>.fnos-orig。
"""
from __future__ import annotations

import re
from pathlib import Path

BAK = ".fnos-orig"
_ROOTFS_RE = re.compile(r"^\s*lxc\.rootfs\.path\s*=\s*(\S+)", re.M)


def rootfs_path(config_path: Path) -> Path | None:
    try:
        text = config_path.read_text(errors="ignore")
    except OSError:
        return None
    m = _ROOTFS_RE.search(text)
    if m:
        val = m.group(1)
        if val.startswith("dir:"):
            val = val[4:]
        if val.startswith("/"):
            p = Path(val)
            return p if p.is_dir() else None
        return None
    p = config_path.parent / "rootfs"
    return p if p.is_dir() else None


def _read(p: Path) -> str | None:
    try:
        return p.read_text(errors="ignore")
    except OSError:
        return None


def _sync(path: Path, static: bool, transform) -> None:
    """static=True：备份并写入改写后的内容；static=False：从备份还原。"""
    bak = path.with_name(path.name + BAK)
    if static:
        text = _read(bak) if bak.exists() else _read(path)
        if text is None:
            return
        new = transform(text)
        if new == text:
            return
        if not bak.exists():
            bak.write_text(text)
        path.write_text(new)
    elif bak.exists():
        text = _read(bak)
        if text is not None:
            path.write_text(text)
        bak.unlink()


def _files(d: Path, patterns: tuple[str, ...]) -> list[Path]:
    if not d.is_dir():
        return []
    out = []
    for pat in patterns:
        out += [p for p in sorted(d.glob(pat)) if p.is_file() and not p.is_symlink() and not p.name.endswith(BAK)]
    return out


def _ifupdown(text: str) -> str:
    # ifupdown-ng 的 inet6 dhcp 会直接调用 dhcpcd（双栈），同样会拿到 IPv4 地址，所以一并关闭
    return re.sub(r"^(\s*iface\s+eth0\s+inet6?\s+)dhcp\b", r"\1manual", text, flags=re.M)


def _networkd(gateway: str):
    def fn(text: str) -> str:
        def dhcp(m):
            v = m.group(2).strip().lower()
            nv = {"yes": "ipv6", "true": "ipv6", "ipv4": "no"}.get(v, v)
            return f"{m.group(1)}{nv}"

        new = re.sub(r"^(\s*DHCP\s*=\s*)(\S+)", dhcp, text, flags=re.M | re.I)
        if gateway and not re.search(r"^\s*DNS\s*=", new, re.M | re.I):
            new = re.sub(r"^(\[Network\][^\n]*\n)", rf"\1DNS={gateway}\n", new, count=1, flags=re.M)
        return new

    return fn


def _netplan(text: str) -> str:
    return re.sub(r"^(\s*dhcp4\s*:\s*)(true|yes)\b", r"\1false", text, flags=re.M | re.I)


def _nm_unmanaged(root: Path, static: bool) -> None:
    """NetworkManager 系发行版（Fedora / Rocky / Alma 等）：静态时不再托管 eth0。"""
    p = root / "etc" / "NetworkManager" / "conf.d" / "99-fnos-static.conf"
    if static:
        if (root / "etc" / "NetworkManager").is_dir():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("[keyfile]\nunmanaged-devices=interface-name:eth0\n")
    elif p.exists():
        p.unlink()


def _resolv(root: Path, static: bool, gateway: str) -> None:
    """DHCP 关闭后容器没有 DNS：resolv.conf 为空时用网关兜底，改回 DHCP 时清理。"""
    p = root / "etc" / "resolv.conf"
    mark = "# fnos-lxc static\n"
    if p.is_symlink():
        return
    cur = _read(p) if p.exists() else ""
    if static and gateway:
        if cur is not None and not re.search(r"^\s*nameserver\s", cur, re.M):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(mark + f"nameserver {gateway}\n")
    elif not static and cur and cur.startswith(mark):
        p.write_text("")


def sync_guest_network(config_path: Path, ipv4_static: bool, gateway: str = "") -> None:
    root = rootfs_path(config_path)
    if root is None:
        return
    etc = root / "etc"
    for f in _files(etc / "network", ("interfaces", "interfaces.d/*")):
        _sync(f, ipv4_static, _ifupdown)
    for f in _files(etc / "systemd" / "network", ("*.network",)):
        _sync(f, ipv4_static, _networkd(gateway))
    for f in _files(etc / "netplan", ("*.yaml", "*.yml")):
        _sync(f, ipv4_static, _netplan)
    _nm_unmanaged(root, ipv4_static)
    _resolv(root, ipv4_static, gateway)
