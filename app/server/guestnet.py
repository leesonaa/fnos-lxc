"""容器内部网络配置同步。

LXC 的 lxc.net.0.ipv4.address 只会在宿主机侧给 eth0 加上静态地址，
而容器镜像自带的网络服务（ifupdown / systemd-networkd / netplan）仍会在 eth0 上跑 DHCP，
于是同一块网卡出现「静态地址 + 自动分配地址」。

做法：IPv4 设为静态时，只关闭 **eth0** 的 DHCP；改回 DHCP 时再还原。
- 每处改动前插入一行 `# fnos-lxc: ...` 标记，记录原值；还原时只撤销带标记的改动。
- 因此：幂等（重复执行不变）、不依赖备份文件、不会覆盖用户在容器里的其它修改，
  也不会碰 eth0 以外的网卡。
"""
from __future__ import annotations

import re
from pathlib import Path

MARK = "# fnos-lxc:"
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


def _edit(path: Path, fn) -> None:
    text = _read(path)
    if text is None:
        return
    new = fn(text)
    if new != text:
        path.write_text(new)  # 原地截断写入，保留属主和权限


def _files(d: Path, patterns: tuple[str, ...]) -> list[Path]:
    if not d.is_dir():
        return []
    out = []
    for pat in patterns:
        out += [p for p in sorted(d.glob(pat)) if p.is_file() and not p.is_symlink()]
    return out


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


# ---------------------------------------------------------------- ifupdown

_IFACE = re.compile(r"^(\s*iface\s+eth0\s+inet6?\s+)(dhcp|manual)(\s*)$")


def _ifupdown(lines: list[str], static: bool) -> list[str]:
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        m = _IFACE.match(ln)
        marked = bool(out) and out[-1].strip() == f"{MARK} was dhcp"
        if static and m and m.group(2) == "dhcp":
            out += [f"{MARK} was dhcp", f"{m.group(1)}manual{m.group(3)}"]
        elif not static and m and m.group(2) == "manual" and marked:
            out.pop()
            out.append(f"{m.group(1)}dhcp{m.group(3)}")
        else:
            out.append(ln)
        i += 1
    return out


# ---------------------------------------------------------------- networkd

def _networkd(lines: list[str], static: bool, dns: str) -> list[str]:
    # 只处理 [Match] 里明确写了 Name=eth0 的文件
    sect, match_ok = "", False
    for ln in lines:
        s = ln.strip()
        if s.startswith("["):
            sect = s.lower()
        elif sect == "[match]":
            m = re.match(r"^Name\s*=\s*(.*)$", s, re.I)
            if m and "eth0" in m.group(1).split():
                match_ok = True
    if not match_ok:
        return lines

    out, sect = [], ""
    i = 0
    have_dns = any(re.match(r"^\s*DNS\s*=", x, re.I) for x in lines)
    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if s.startswith("["):
            sect = s.lower()
            out.append(ln)
            if static and sect == "[network]" and dns and not have_dns:
                out += [f"{MARK} dns", f"DNS={dns}"]
                have_dns = True
            i += 1
            continue
        if sect == "[network]":
            m = re.match(r"^(\s*DHCP\s*=\s*)(\S+)\s*$", ln, re.I)
            marked = bool(out) and out[-1].strip().startswith(f"{MARK} DHCP was ")
            if m and static and not marked:
                new = {"yes": "ipv6", "true": "ipv6", "ipv4": "no"}.get(m.group(2).lower())
                if new:
                    out += [f"{MARK} DHCP was {m.group(2)}", f"{m.group(1)}{new}"]
                    i += 1
                    continue
            elif m and not static and marked:
                orig = out.pop().strip()[len(f"{MARK} DHCP was "):]
                out.append(f"{m.group(1)}{orig}")
                i += 1
                continue
            if not static and s == f"{MARK} dns":
                # 去掉我们加的 DNS 行（标记行 + 下一行）
                if i + 1 < len(lines) and lines[i + 1].strip().upper().startswith("DNS="):
                    i += 2
                    continue
        out.append(ln)
        i += 1
    return out


# ---------------------------------------------------------------- netplan

def _netplan(lines: list[str], static: bool) -> list[str]:
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(r"^(\s*)eth0:\s*(#.*)?$", ln)
        if not m:
            out.append(ln)
            i += 1
            continue
        base = len(m.group(1))
        out.append(ln)
        i += 1
        while i < len(lines):  # eth0 这一块：缩进比 eth0 深的行
            b = lines[i]
            if b.strip() and _indent(b) <= base:
                break
            d = re.match(r"^(\s*)dhcp4:\s*(true|yes)\s*(#.*)?$", b, re.I)
            marked = bool(out) and out[-1].strip() == f"{MARK} dhcp4 was true"
            r = re.match(r"^(\s*)dhcp4:\s*false\s*$", b, re.I)
            if static and d:
                out += [f"{d.group(1)}{MARK} dhcp4 was true", f"{d.group(1)}dhcp4: false"]
            elif not static and r and marked:
                out.pop()
                out.append(f"{r.group(1)}dhcp4: true")
            else:
                out.append(b)
            i += 1
    return out


# ---------------------------------------------------------------- resolv.conf

def _resolv(root: Path, static: bool, dns: str) -> None:
    """DHCP 关闭后容器没有 DNS：没有任何 nameserver 时追加网关；改回 DHCP 时只撤销自己加的两行。"""
    p = root / "etc" / "resolv.conf"
    if p.is_symlink():
        return
    cur = _read(p) if p.exists() else ""
    if cur is None:
        return
    lines = cur.splitlines()
    mark = f"{MARK} static"
    if static:
        if dns and mark not in lines and not re.search(r"^\s*nameserver\s", cur, re.M):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("".join(x + "\n" for x in lines) + f"{mark}\nnameserver {dns}\n")
    elif mark in lines:
        i = lines.index(mark)
        del lines[i : i + 2 if i + 1 < len(lines) and lines[i + 1].startswith("nameserver ") else i + 1]
        p.write_text("".join(x + "\n" for x in lines))


# ---------------------------------------------------------------- NetworkManager

def _nm_unmanaged(root: Path, static: bool) -> None:
    """NetworkManager 系发行版（Fedora / Rocky / Alma 等）：静态时不再托管 eth0。"""
    p = root / "etc" / "NetworkManager" / "conf.d" / "99-fnos-static.conf"
    if static:
        if (root / "etc" / "NetworkManager").is_dir():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("[keyfile]\nunmanaged-devices=interface-name:eth0\n")
    elif p.exists():
        p.unlink()


# ---------------------------------------------------------------- 入口

def _lines(fn):
    def wrap(text: str) -> str:
        res = fn(text.splitlines())
        return "\n".join(res) + ("\n" if text.endswith("\n") else "")
    return wrap


def sync_guest_network(config_path: Path, ipv4_static: bool, gateway: str = "") -> None:
    root = rootfs_path(config_path)
    if root is None:
        return
    etc = root / "etc"
    for f in _files(etc / "network", ("interfaces", "interfaces.d/*")):
        _edit(f, _lines(lambda L: _ifupdown(L, ipv4_static)))
    for f in _files(etc / "systemd" / "network", ("*.network",)):
        _edit(f, _lines(lambda L: _networkd(L, ipv4_static, gateway)))
    for f in _files(etc / "netplan", ("*.yaml", "*.yml")):
        _edit(f, _lines(lambda L: _netplan(L, ipv4_static)))
    _nm_unmanaged(root, ipv4_static)
    _resolv(root, ipv4_static, gateway)
