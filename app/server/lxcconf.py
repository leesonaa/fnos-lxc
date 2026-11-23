"""LXC 容器 config 的解析与受管区块写入（纯函数，无副作用，方便测试）。

策略：
- UI 表单管理的项写入文件末尾的受管区块（BEGIN/END 标记之间）。
- 保存时，区块外与表单项冲突的行会被移除（避免重复/互相覆盖），其余行原样保留。
- 其余任何自定义项请走「高级：原始配置」。
"""
from __future__ import annotations

import ipaddress
import math
import re

BEGIN = "# >>> fnos-lxc managed (由管理界面生成，请勿手动编辑此区块) >>>"
END = "# <<< fnos-lxc managed <<<"

# 区块外出现就直接移除的单值键（由表单接管）
_STRIP_KEYS = {
    "lxc.start.auto",
    "lxc.start.delay",
    "lxc.uts.name",
    "lxc.net.0.type",
    "lxc.net.0.flags",
    "lxc.net.0.link",
    "lxc.net.0.name",
    "lxc.net.0.hwaddr",
    "lxc.net.0.ipv4.address",
    "lxc.net.0.ipv4.gateway",
    "lxc.net.0.ipv6.address",
    "lxc.net.0.ipv6.gateway",
    "lxc.cgroup2.cpu.max",
    "lxc.cgroup2.memory.max",
}

_DEV_TUN = "c 10:200 rwm"
_DEV_FUSE = "c 10:229 rwm"
_DEV_DRI = "c 226:* rwm"

_WEBUI_RE = re.compile(r"^#\s*fnos\.webui\s*=\s*(.*)$", re.M)
_ICON_RE = re.compile(r"^#\s*fnos\.icon\s*=\s*(\S+)\s*$", re.M)
# 图标取值：空=按发行版自动识别；builtin:<内置图标名>；custom:<上传文件名>
_ICON_VAL = re.compile(r"^(?:builtin:[a-z0-9_-]{1,40}|custom:[A-Za-z0-9._-]{1,100}\.(?:png|jpg|webp|gif|svg))$")
_OPT_RE = re.compile(r"^[A-Za-z0-9_=.:-]+$")
_PLAIN_OPTS = {"bind", "ro", "rw", "create=dir", "create=file"}
MAX_CPU_CORES = 1024
MAX_MEM_MB = 16 * 1024 * 1024  # 16 TB，只用来挡住荒谬输入
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,63}$")
_BRIDGE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,15}$")
_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")

DEFAULTS = {
    "autostart": False,
    "start_delay": 5,
    "hostname": "",
    "webui": "",  # 点击图标打开的地址，支持 [IP] 占位符
    "icon": "",  # 自定义图标，空则按发行版自动识别
    "bridge": "",
    "hwaddr": "",
    "ipv4_mode": "dhcp",  # dhcp | static
    "ipv4_address": "",
    "ipv4_gateway": "",
    "ipv6_mode": "auto",  # auto | static
    "ipv6_address": "",
    "ipv6_gateway": "",
    "cpu_cores": 0,  # 0 = 不限
    "memory_mb": 0,  # 0 = 不限
    "tun": False,
    "fuse": False,
    "gpu": False,
    "docker": False,
    "mounts": [],  # [{"host": "/vol1/x", "container": "/data", "readonly": False}]
}


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def _kv_lines(text: str):
    """yield (index, key, value) for non-comment key=value lines."""
    for i, line in enumerate(text.splitlines()):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        yield i, k.strip(), v.strip()


def _esc(p: str) -> str:
    return p.replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011")


def _unesc(p: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), p)


def _parse_mem(v: str) -> int:
    m = re.match(r"^(\d+)([KkMmGg]?)$", v.strip())
    if not m:
        return 0
    n = int(m.group(1))
    u = m.group(2).upper()
    if u == "G":
        return n * 1024
    if u == "M":
        return n
    if u == "K":
        return n // 1024
    return n // (1024 * 1024)


def parse_settings(text: str) -> dict:
    s = dict(DEFAULTS)
    s["mounts"] = []
    allow = []
    m = _WEBUI_RE.search(text)
    if m:
        s["webui"] = m.group(1).strip()
    m = _ICON_RE.search(text)
    if m and _ICON_VAL.match(m.group(1)) and ".." not in m.group(1):
        s["icon"] = m.group(1)
    for _, k, v in _kv_lines(text):
        if k == "lxc.start.auto":
            s["autostart"] = v == "1"
        elif k == "lxc.start.delay":
            s["start_delay"] = int(v) if v.isdigit() else 5
        elif k == "lxc.uts.name":
            s["hostname"] = v
        elif k == "lxc.net.0.link":
            s["bridge"] = v
        elif k == "lxc.net.0.hwaddr":
            s["hwaddr"] = "" if "x" in v.lower() else v
        elif k == "lxc.net.0.ipv4.address":
            if not s["ipv4_address"]:
                s["ipv4_mode"] = "static"
                s["ipv4_address"] = v
        elif k == "lxc.net.0.ipv4.gateway":
            s["ipv4_gateway"] = v
        elif k == "lxc.net.0.ipv6.address":
            if not s["ipv6_address"]:
                s["ipv6_mode"] = "static"
                s["ipv6_address"] = v
        elif k == "lxc.net.0.ipv6.gateway":
            s["ipv6_gateway"] = v
        elif k == "lxc.cgroup2.cpu.max":
            m = re.match(r"^(\d+)\s+(\d+)$", v)
            if m and int(m.group(2)) > 0:
                s["cpu_cores"] = round(int(m.group(1)) / int(m.group(2)), 2)
        elif k == "lxc.cgroup2.memory.max":
            s["memory_mb"] = _parse_mem(v)
        elif k == "lxc.apparmor.profile":
            s["docker"] = v == "unconfined"
        elif k == "lxc.cgroup2.devices.allow":
            allow.append(v)
        elif k == "lxc.mount.entry":
            parts = v.split()
            if len(parts) < 4:
                continue
            host, ct, _fs, opts = _unesc(parts[0]), _unesc(parts[1]), parts[2], parts[3]
            if "bind" not in opts.split(","):
                continue
            if host == "/dev/dri":
                s["gpu"] = True
            elif host == "/dev/net/tun":
                s["tun"] = True
            elif host == "/dev/fuse":
                s["fuse"] = True
            else:
                s["mounts"].append(
                    {
                        "host": host,
                        "container": "/" + ct.lstrip("/"),
                        "readonly": "ro" in opts.split(","),
                        # 表单不认识的选项（optional、nosuid 等）原样保留，避免保存后丢失
                        "options": [o for o in opts.split(",") if o and o not in _PLAIN_OPTS],
                    }
                )
    if _DEV_TUN in allow:
        s["tun"] = True
    if _DEV_FUSE in allow:
        s["fuse"] = True
    if _DEV_DRI in allow:
        s["gpu"] = True
    return s


def parse_distro(text: str) -> tuple[str, str]:
    """从 lxc-create download 模板写入的注释里取 (distro, release)。"""
    m = re.search(r"Parameters passed to the template:(.*)", text)
    if m:
        args = m.group(1)
        d = re.search(r"(?:^|\s)(?:-d|--dist)\s+(\S+)", args)
        r = re.search(r"(?:^|\s)(?:-r|--release)\s+(\S+)", args)
        if d:
            return d.group(1).lower(), (r.group(1) if r else "")
    m = re.search(r"lxc\.include\s*=\s*\S*/config/([A-Za-z0-9_-]+)\.common\.conf", text)
    if m:
        return m.group(1).lower(), ""
    return "", ""


# ---------------------------------------------------------------------------
# 校验 + 写入
# ---------------------------------------------------------------------------

def validate(raw: dict) -> tuple[dict | None, str]:
    """归一化并校验表单；返回 (settings, error)。"""
    s = dict(DEFAULTS)
    s["mounts"] = []
    s["autostart"] = bool(raw.get("autostart"))
    try:
        s["start_delay"] = max(0, min(600, int(raw.get("start_delay") or 0)))
    except (TypeError, ValueError):
        return None, "自启延迟必须是整数"

    host = str(raw.get("hostname") or "").strip()
    if host and not _HOST_RE.match(host):
        return None, "主机名只能包含字母、数字、. _ -"
    s["hostname"] = host

    webui = str(raw.get("webui") or "").strip()
    if webui and (not re.match(r"^https?://[^\s]+$", webui) or len(webui) > 500):
        return None, "WebUI 地址需以 http:// 或 https:// 开头，且不含空格"
    s["webui"] = webui

    icon = str(raw.get("icon") or "").strip()
    if icon and (not _ICON_VAL.match(icon) or ".." in icon):
        return None, "图标取值无效"
    s["icon"] = icon

    br = str(raw.get("bridge") or "").strip()
    if not br or not _BRIDGE_RE.match(br):
        return None, "网桥名称无效"
    s["bridge"] = br

    mac = str(raw.get("hwaddr") or "").strip()
    if mac and not _MAC_RE.match(mac):
        return None, "MAC 地址格式应为 aa:bb:cc:dd:ee:ff"
    s["hwaddr"] = mac.lower()

    if raw.get("ipv4_mode") == "static":
        try:
            iface = ipaddress.IPv4Interface(str(raw.get("ipv4_address") or "").strip())
            if "/" not in str(raw.get("ipv4_address")):
                return None, "IPv4 地址需带掩码长度，例如 192.168.1.50/24"
        except ValueError:
            return None, "IPv4 地址无效，格式例如 192.168.1.50/24"
        gw = str(raw.get("ipv4_gateway") or "").strip()
        if gw:
            try:
                ipaddress.IPv4Address(gw)
            except ValueError:
                return None, "IPv4 网关无效"
        s.update(ipv4_mode="static", ipv4_address=str(iface), ipv4_gateway=gw)

    if raw.get("ipv6_mode") == "static":
        try:
            iface6 = ipaddress.IPv6Interface(str(raw.get("ipv6_address") or "").strip())
            if "/" not in str(raw.get("ipv6_address")):
                return None, "IPv6 地址需带前缀长度，例如 fd00::50/64"
        except ValueError:
            return None, "IPv6 地址无效，格式例如 fd00::50/64"
        gw6 = str(raw.get("ipv6_gateway") or "").strip()
        if gw6:
            try:
                ipaddress.IPv6Address(gw6)
            except ValueError:
                return None, "IPv6 网关无效"
        s.update(ipv6_mode="static", ipv6_address=str(iface6), ipv6_gateway=gw6)

    try:
        cores = float(raw.get("cpu_cores") or 0)
        mem_f = float(raw.get("memory_mb") or 0)
    except (TypeError, ValueError):
        return None, "CPU / 内存必须是数字"
    if not (math.isfinite(cores) and math.isfinite(mem_f)):
        return None, "CPU / 内存必须是有限的数字"
    if cores < 0 or mem_f < 0:
        return None, "CPU / 内存不能为负数"
    if cores > MAX_CPU_CORES or mem_f > MAX_MEM_MB:
        return None, "CPU / 内存数值过大"
    mem = int(mem_f)
    s["cpu_cores"] = cores
    s["memory_mb"] = mem

    for k in ("tun", "fuse", "gpu", "docker"):
        s[k] = bool(raw.get(k))

    for m in raw.get("mounts") or []:
        h = str(m.get("host") or "").strip()
        c = str(m.get("container") or "").strip()
        if not h and not c:
            continue
        if not h.startswith("/") or not c.startswith("/"):
            return None, "挂载路径必须是绝对路径（以 / 开头）"
        if any(ch in h + c for ch in "\n\r"):
            return None, "挂载路径含非法字符"
        if c.strip("/") == "":
            return None, "容器内路径不能是根目录 /"
        extra = [str(o) for o in (m.get("options") or []) if str(o) not in _PLAIN_OPTS]
        if any(not _OPT_RE.match(o) for o in extra):
            return None, "挂载选项含非法字符"
        s["mounts"].append({"host": h, "container": c, "readonly": bool(m.get("readonly")), "options": extra})
    return s, ""


def _should_strip(key: str, value: str) -> bool:
    if key in _STRIP_KEYS:
        return True
    if key == "lxc.apparmor.profile" and value == "unconfined":
        return True
    if key == "lxc.cap.drop" and value == "":
        return True
    if key == "lxc.mount.auto" and value.startswith("proc:rw sys:rw"):
        return True
    if key == "lxc.cgroup2.devices.allow" and value in (_DEV_TUN, _DEV_FUSE, _DEV_DRI):
        return True
    if key == "lxc.mount.entry":
        parts = value.split()
        if len(parts) >= 4 and "bind" in parts[3].split(","):
            return True
    return False


def _strip_existing(text: str) -> list[str]:
    out, inside = [], False
    for line in text.splitlines():
        s = line.strip()
        if s == BEGIN:
            inside = True
            continue
        if s == END:
            inside = False
            continue
        if inside:
            continue
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            if _should_strip(k.strip(), v.strip()):
                continue
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return out


def build_block(s: dict, bind_is_file=lambda p: False) -> list[str]:
    L = [BEGIN]
    L.append(f"lxc.start.auto = {1 if s['autostart'] else 0}")
    if s["autostart"]:
        L.append(f"lxc.start.delay = {s['start_delay']}")
    if s["hostname"]:
        L.append(f"lxc.uts.name = {s['hostname']}")
    if s["webui"]:
        L.append(f"# fnos.webui = {s['webui']}")
    if s.get("icon"):
        L.append(f"# fnos.icon = {s['icon']}")
    L += [
        "lxc.net.0.type = veth",
        "lxc.net.0.flags = up",
        f"lxc.net.0.link = {s['bridge']}",
        "lxc.net.0.name = eth0",
    ]
    if s["hwaddr"]:
        L.append(f"lxc.net.0.hwaddr = {s['hwaddr']}")
    if s["ipv4_mode"] == "static":
        L.append(f"lxc.net.0.ipv4.address = {s['ipv4_address']}")
        if s["ipv4_gateway"]:
            L.append(f"lxc.net.0.ipv4.gateway = {s['ipv4_gateway']}")
    if s["ipv6_mode"] == "static":
        L.append(f"lxc.net.0.ipv6.address = {s['ipv6_address']}")
        if s["ipv6_gateway"]:
            L.append(f"lxc.net.0.ipv6.gateway = {s['ipv6_gateway']}")
    if s["cpu_cores"] and s["cpu_cores"] > 0:
        L.append(f"lxc.cgroup2.cpu.max = {int(round(s['cpu_cores'] * 100000))} 100000")
    if s["memory_mb"] and s["memory_mb"] > 0:
        L.append(f"lxc.cgroup2.memory.max = {s['memory_mb']}M")
    if s["tun"]:
        L.append(f"lxc.cgroup2.devices.allow = {_DEV_TUN}")
        L.append("lxc.mount.entry = /dev/net/tun dev/net/tun none bind,optional,create=file 0 0")
    if s["fuse"]:
        L.append(f"lxc.cgroup2.devices.allow = {_DEV_FUSE}")
        L.append("lxc.mount.entry = /dev/fuse dev/fuse none bind,optional,create=file 0 0")
    if s["gpu"]:
        L.append(f"lxc.cgroup2.devices.allow = {_DEV_DRI}")
        L.append("lxc.mount.entry = /dev/dri dev/dri none bind,optional,create=dir 0 0")
    if s["docker"]:
        L += [
            "lxc.apparmor.profile = unconfined",
            "lxc.cgroup2.devices.allow = a",
            "lxc.cap.drop =",
            "lxc.mount.auto = proc:rw sys:rw",
        ]
    for m in s["mounts"]:
        opts = "bind," + ("ro," if m["readonly"] else "") + "".join(o + "," for o in m.get("options", [])) + (
            "create=file" if bind_is_file(m["host"]) else "create=dir"
        )
        L.append(
            f"lxc.mount.entry = {_esc(m['host'])} {_esc(m['container'].lstrip('/'))} none {opts} 0 0"
        )
    L.append(END)
    return L


def apply_settings(text: str, s: dict, bind_is_file=lambda p: False) -> str:
    base = _strip_existing(text)
    return "\n".join(base + [""] + build_block(s, bind_is_file)) + "\n"


def valid_bridge(name: str) -> bool:
    return bool(_BRIDGE_RE.match(str(name or "")))


def kv_effective(text: str):
    """真正生效（非注释）的 (key, value) 行。"""
    for _, k, v in _kv_lines(text):
        yield k, v
