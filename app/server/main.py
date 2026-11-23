#!/usr/bin/env python3
"""fnOS LXC Manager — native LXC manager, step-by-step setup."""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import pty
import re
import secrets
import select
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import termios
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import customfs
import guestnet
import lxcconf
import oplog
import rootstore

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("TRIM_PKGVAR", "/tmp/fnos-lxc-data"))
CONFIG_FILE = DATA_DIR / "config.json"
STATIC_DIR = Path(__file__).resolve().parent.parent / "ui"
SYS_NET = Path(os.environ.get("FNLXC_SYS_NET", "/sys/class/net"))
_CONF_LOCK = threading.Lock()
GATEWAY_PREFIX = "/app/fnos-lxc"
CSRF_HEADER = ("X-Requested-With", "fnos-lxc")
_TERM_TOKENS: dict = {}
_TERM_LOCK = threading.Lock()


def issue_term_token(name: str) -> str:
    """一次性、30 秒有效，把 WebSocket 绑定到已通过认证的 POST 请求（防跨站 WebSocket 劫持）。"""
    tok = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _TERM_LOCK:
        for k in [k for k, v in _TERM_TOKENS.items() if v[1] < now]:
            del _TERM_TOKENS[k]
        _TERM_TOKENS[tok] = (name, now + 30)
    return tok


def consume_term_token(tok: str, name: str) -> bool:
    with _TERM_LOCK:
        item = _TERM_TOKENS.pop(tok, None)
    return bool(item) and item[0] == name and item[1] >= time.monotonic()
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

DEFAULT_CONFIG = {
    "initialized": False,
    "lxcpath": "",
    "bridge": "",
    "timeout": 15,
    "autostart_delay": 5,
    "service_enabled": True,
    "image_mirror": "official",
}


def load_config() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            return {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text())}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------

def run(cmd, timeout=120, input_text=None, env=None):
    """Run a command. *cmd* must be a list of arguments (shell=False always)."""
    if isinstance(cmd, str):
        raise TypeError("run() only accepts a list of arguments, not a shell string")
    try:
        r = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            env=env,
        )
        return {
            "ok": r.returncode == 0,
            "code": r.returncode,
            "stdout": (r.stdout or "").strip(),
            "stderr": (r.stderr or "").strip(),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1, "stdout": "", "stderr": "timeout"}
    except Exception as e:
        return {"ok": False, "code": -1, "stdout": "", "stderr": str(e)}


def which(bin_name: str) -> bool:
    return shutil.which(bin_name) is not None


def _clamp_int(v, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(float(v))))
    except (TypeError, ValueError, OverflowError):
        return default


def _atomic_write(p: Path, text: str) -> None:
    """先写临时文件再 rename，避免写到一半断电/崩溃留下残缺的 config。"""
    tmp = p.with_name(p.name + ".fnos-tmp")
    tmp.write_text(text)
    try:
        os.chmod(tmp, p.stat().st_mode & 0o777)
    except OSError:
        pass
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Setup / init steps
# ---------------------------------------------------------------------------

def detect_bridges() -> list:
    """列出可用网桥：OVS 桥 + Linux 桥，*-ovs 排最前；每次调用实时探测。"""
    found = []
    if which("ovs-vsctl"):
        r = run(["ovs-vsctl", "list-br"], timeout=10)
        if r["ok"]:
            found += [x.strip() for x in r["stdout"].splitlines() if x.strip()]
    try:
        for p in sorted(SYS_NET.iterdir()):
            n = p.name
            if (p / "bridge").exists() or n.endswith("-ovs"):
                found.append(n)
    except OSError:
        pass
    skip = re.compile(r"^(docker0|virbr\d+|veth.*|br-[0-9a-f]{12}|ovs-system)$")
    seen, out = set(), []
    for n in found:
        if n in seen or skip.match(n):
            continue
        seen.add(n)
        out.append(n)
    out.sort(key=lambda n: (not n.endswith("-ovs"), n))
    return out


def detect_data_volumes() -> list:
    """Suggest storage under /vol*."""
    vols = []
    for p in sorted(Path("/").glob("vol*")):
        if p.is_dir():
            vols.append(str(p))
    # common user data
    for v in list(vols):
        for sub in Path(v).glob("*/"):
            if sub.is_dir() and sub.name.isdigit():
                vols.append(str(sub))
    return vols[:20] or ["/vol1"]


def start_install_job() -> dict:
    """apt 安装可能超过网关/浏览器超时：放后台线程，前端轮询 /api/job/<id>。"""
    with _JOBS_LOCK:
        for j in _JOBS.values():
            if j.get("kind") == "install" and j["status"] == "running":
                return {"ok": True, "job": j["id"]}
        jid = f"{int(time.time() * 1000)}"
        job = {"id": jid, "kind": "install", "name": "lxc", "status": "running", "progress": 5,
               "stage": "安装 LXC 软件包（可能需要几分钟）", "log": [], "elapsed": 0, "error": "", "result": None}
        _JOBS[jid] = job

    def work():
        t0 = time.time()
        try:
            res = step_install_packages()
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "step": "exception", "detail": {"stderr": str(e)}, "has_lxc": False}
        err = ""
        if not res.get("ok"):
            d = res.get("detail") or {}
            err = f"{res.get('step', '')}: {(d.get('stderr') or d.get('stdout') or '').splitlines()[-1:] or ['失败']}"[:300]
        _job_set(job, status="done" if res.get("ok") else "error", progress=100, result=res, error=err,
                 elapsed=int(time.time() - t0))
        oplog.log("setup.install_packages", ok=bool(res.get("ok")), detail=err or "ok")

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "job": jid}


def step_install_packages() -> dict:
    """apt install native lxc stack."""
    if which("lxc-ls") and which("lxc-create"):
        return {"ok": True, "step": "packages", "detail": {"stdout": "already installed"}, "has_lxc": True}
    pkgs = [
        "lxc",
        "lxc-templates",
        "lxcfs",
        "bridge-utils",
        "debootstrap",
        "uidmap",
    ]
    # noninteractive; keep existing conf if we already wrote default.conf
    apt_env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    r = run(["apt-get", "update", "-qq"], timeout=180, env=apt_env)
    if not r["ok"]:
        return {"ok": False, "step": "apt-update", "detail": r, "has_lxc": False}
    r2 = run(
        [
            "apt-get", "install", "-y",
            "-o", "Dpkg::Options::=--force-confdef",
            "-o", "Dpkg::Options::=--force-confold",
            *pkgs,
        ],
        timeout=600,
        env=apt_env,
    )
    ok = which("lxc-ls") and which("lxc-create")
    if not r2["ok"] and not ok:
        return {"ok": False, "step": "apt-install", "detail": r2, "has_lxc": False}
    return {"ok": ok, "step": "packages", "detail": r2, "has_lxc": ok}


_SYSTEM_DIRS = ("/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/proc", "/sys", "/dev", "/run", "/root")


def check_lxcpath(lxcpath: str) -> tuple[str, str, str]:
    """返回 (规范化路径, 错误, 警告)。"""
    p = str(lxcpath or "").strip().rstrip("/")
    if not p.startswith("/") or p.count("/") < 2:
        return "", "请填写数据卷下的绝对路径，例如 /vol1/1000/lxc", ""
    if any(ord(c) < 32 or c == "\x7f" for c in p):
        return "", "路径含非法字符", ""
    p = os.path.normpath(p)
    if any(p == d or p.startswith(d + "/") for d in _SYSTEM_DIRS):
        return "", "不能把容器放在系统目录下，请选择数据卷（/vol1 等）", ""
    warn = ""
    if not re.match(r"^/vol\d+(/|$)", p):
        warn = "所选路径不在飞牛数据卷（/volN）下，容器会占用该路径所在磁盘的空间"
    return p, "", warn


def step_configure_storage(lxcpath: str) -> dict:
    lxcpath, err, warn0 = check_lxcpath(lxcpath)
    if err:
        return {"ok": False, "error": err}
    cache = Path(lxcpath) / "cache"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        if not os.access(lxcpath, os.W_OK):
            return {"ok": False, "error": "所选路径不可写"}
    except OSError as e:
        return {"ok": False, "error": f"无法创建目录: {e}"}
    warn = warn0
    # /var/lib/lxc、/var/cache/lxc 仅在为空/不存在/已是软链时指向用户路径；
    # 真正生效的是 /etc/lxc/lxc.conf 里的 lxc.lxcpath
    for link, target in (("/var/lib/lxc", lxcpath), ("/var/cache/lxc", str(cache))):
        lp = Path(link)
        lp.parent.mkdir(parents=True, exist_ok=True)
        if lp.is_symlink() or not lp.exists():
            run(["ln", "-sfn", target, link])
        elif lp.is_dir() and not any(lp.iterdir()):
            lp.rmdir()
            run(["ln", "-sfn", target, link])
        elif link == "/var/lib/lxc":
            warn = (warn + "；" if warn else "") + "/var/lib/lxc 下已有内容，未迁移；新容器将创建在所选路径"
    return {"ok": True, "lxcpath": lxcpath, "warning": warn}


def step_write_lxc_conf(lxcpath: str, bridge: str, timeout: int = 15) -> dict:
    Path("/etc/lxc").mkdir(parents=True, exist_ok=True)
    lxc_conf = f"""# managed by fnos-lxc
lxc.lxcpath = {lxcpath}
"""
    default_conf = f"""# managed by fnos-lxc — default network
lxc.net.0.type = veth
lxc.net.0.flags = up
lxc.net.0.link = {bridge}
lxc.net.0.name = eth0
lxc.apparmor.profile = generated
lxc.apparmor.allow_nesting = 1
"""
    # 覆盖前先备份宿主机原有配置（只在首次、且原文件不是本程序写的时候备份），卸载时可还原
    orig = DATA_DIR / "orig"
    backed = []
    for name, text in (("lxc.conf", lxc_conf), ("default.conf", default_conf)):
        dst = Path("/etc/lxc") / name
        try:
            old = dst.read_text()
        except OSError:
            old = None
        if old is not None and "managed by fnos-lxc" not in old and not (orig / name).exists():
            orig.mkdir(parents=True, exist_ok=True)
            (orig / name).write_text(old)
            backed.append(name)
        if dst.exists():
            _atomic_write(dst, text)
        else:
            dst.write_text(text)
    # also keep a copy in data dir
    (DATA_DIR / "lxc.conf").write_text(lxc_conf)
    (DATA_DIR / "default.conf").write_text(default_conf)
    return {"ok": True, "backed_up": backed}


def finalize_init(lxcpath: str, bridge: str, timeout: int = 15) -> dict:
    r1 = step_configure_storage(lxcpath)
    if not r1["ok"]:
        return r1
    r2 = step_write_lxc_conf(r1["lxcpath"], bridge, timeout)
    if not r2["ok"]:
        return r2
    if r2.get("backed_up"):
        r1["warning"] = (r1.get("warning", "") + "；" if r1.get("warning") else "") + \
            f"已备份原有 LXC 配置（{', '.join(r2['backed_up'])}）到 {DATA_DIR / 'orig'}，卸载时可还原"
    cfg = load_config()
    cfg.update(
        {
            "initialized": True,
            "lxcpath": r1["lxcpath"],
            "bridge": bridge,
            "timeout": timeout,
            "service_enabled": True,
        }
    )
    save_config(cfg)
    oplog.log("setup.finalize", ok=True, detail=f"lxcpath={cfg.get('lxcpath')} bridge={cfg.get('bridge')}")
    return {"ok": True, "config": cfg, "warning": r1.get("warning", "")}


# ---------------------------------------------------------------------------
# LXC operations
# ---------------------------------------------------------------------------

def lxc_base() -> Path:
    return Path(load_config().get("lxcpath") or "/var/lib/lxc")


def clean_name(name: str) -> str:
    name = (name or "").strip()
    return name if _NAME_RE.match(name) else ""


def ct_config_path(name: str) -> Path | None:
    for base in (lxc_base(), Path("/var/lib/lxc")):
        p = base / name / "config"
        if p.exists():
            return p
    return None


def detect_distro(name: str) -> tuple[str, str]:
    p = ct_config_path(name)
    distro, release = "", ""
    if p:
        try:
            distro, release = lxcconf.parse_distro(p.read_text(errors="ignore"))
        except OSError:
            pass
        if not distro:
            osr = p.parent / "rootfs" / "etc" / "os-release"
            try:
                m = re.search(r'^ID="?([A-Za-z0-9._-]+)"?', osr.read_text(errors="ignore"), re.M)
                if m:
                    distro = m.group(1).lower()
            except OSError:
                pass
    if not distro:
        for d in ("debian", "ubuntu", "alpine", "centos", "fedora", "archlinux", "opensuse",
                  "rockylinux", "almalinux", "kali", "mint", "gentoo", "nixos", "openwrt"):
            if d in name.lower():
                distro = d
                break
    return distro or "linux", release


def container_ips(name: str) -> tuple[list, list]:
    r = run(["lxc-info", "-n", name, "-iH"], timeout=10)
    v4, v6 = [], []
    for ip in (r["stdout"].split() if r["ok"] else []):
        if ":" in ip:
            if not ip.lower().startswith("fe80") and ip != "::1":
                v6.append(ip)
        elif not ip.startswith("127."):
            v4.append(ip)
    return v4, v6


CGROUP_ROOT = Path(os.environ.get("FNLXC_CGROUP", "/sys/fs/cgroup"))
_CPU_PREV: dict = {}
_NET_PREV: dict = {}
_CPU_LOCK = threading.Lock()


def _read_int(p: Path):
    try:
        v = p.read_text().strip()
        return None if v == "max" else int(v)
    except (OSError, ValueError):
        return None


def _host_mem_total() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return 0


def _cgroup_dir(pid: int, name: str) -> Path | None:
    """容器 init 的 cgroup v2 目录（取到 lxc.payload.* 这一级）。"""
    cands = []
    try:
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                parts = [x for x in line[3:].split("/") if x]
                for i, part in enumerate(parts):
                    if part.startswith("lxc.payload"):
                        cands.append("/".join(parts[: i + 1]))
                        break
    except OSError:
        pass
    cands.append(f"lxc.payload.{name}")
    for c in cands:
        d = CGROUP_ROOT / c
        if (d / "cpu.stat").exists():
            return d
    return None


def _net_bytes(pid: int):
    rx = tx = 0
    try:
        for line in Path(f"/proc/{pid}/net/dev").read_text().splitlines()[2:]:
            iface, _, rest = line.partition(":")
            if iface.strip() == "lo":
                continue
            f = rest.split()
            rx += int(f[0])
            tx += int(f[8])
    except (OSError, ValueError, IndexError):
        return None, None
    return rx, tx


def _uptime_s(pid: int):
    try:
        st = Path(f"/proc/{pid}/stat").read_text()
        start = int(st.rsplit(")", 1)[1].split()[19]) / os.sysconf("SC_CLK_TCK")
        up = float(Path("/proc/uptime").read_text().split()[0])
        return max(0, int(up - start))
    except (OSError, ValueError, IndexError):
        return None


def _cpuset_count(value: str) -> int:
    count = 0
    try:
        for item in value.split(","):
            a, sep, b = item.partition("-")
            count += int(b) - int(a) + 1 if sep else 1
    except ValueError:
        return 0
    return count


def container_stats(name: str, pid: int, mem_limit_mb: int = 0, cpu_limit_cores: float = 0) -> dict:
    host_cpus = os.cpu_count() or 1
    st = {"pid": pid, "cpus": f"0-{host_cpus - 1}" if host_cpus > 1 else "0", "cpu": None,
          "cpu_limit": cpu_limit_cores or None,
          "mem": None, "mem_max": None, "rx": None, "tx": None, "uptime": _uptime_s(pid)}
    st["rx"], st["tx"] = _net_bytes(pid)
    st["rx_rate"] = st["tx_rate"] = None
    if st["rx"] is not None:
        t = time.monotonic()
        with _CPU_LOCK:
            prev = _NET_PREV.get(name)
            if prev and t - prev[0] < 1.0:
                st["rx_rate"], st["tx_rate"] = prev[3], prev[4]  # 短时间内重复请求，沿用上次速率
            else:
                if prev and t > prev[0] and st["rx"] >= prev[1] and st["tx"] >= prev[2]:
                    st["rx_rate"] = (st["rx"] - prev[1]) / (t - prev[0])
                    st["tx_rate"] = (st["tx"] - prev[2]) / (t - prev[0])
                _NET_PREV[name] = (t, st["rx"], st["tx"], st["rx_rate"], st["tx_rate"])
    d = _cgroup_dir(pid, name)
    if not d:
        return st
    capacity = _cpuset_count(st["cpus"]) or host_cpus
    try:
        cpu_max = (d / "cpu.max").read_text().split()
        if len(cpu_max) >= 2 and cpu_max[0] != "max" and int(cpu_max[1]) > 0:
            st["cpu_limit"] = int(cpu_max[0]) / int(cpu_max[1])
    except (OSError, ValueError):
        pass
    if st["cpu_limit"]:
        capacity = min(capacity, st["cpu_limit"])
    try:
        eff = (d / "cpuset.cpus.effective").read_text().strip()
        if eff:
            st["cpus"] = eff
            capacity = _cpuset_count(eff) or capacity
            if st["cpu_limit"]:
                capacity = min(capacity, st["cpu_limit"])
    except OSError:
        pass
    st["mem"] = _read_int(d / "memory.current")
    limit = _read_int(d / "memory.max")
    if not limit and mem_limit_mb:
        limit = mem_limit_mb * 1024 * 1024
    st["mem_max"] = limit or _host_mem_total() or None
    try:
        usage = next(int(x.split()[1]) for x in (d / "cpu.stat").read_text().splitlines() if x.startswith("usage_usec"))
    except (OSError, StopIteration, ValueError, IndexError):
        return st
    now = time.monotonic()
    with _CPU_LOCK:
        prev = _CPU_PREV.get(name)
        _CPU_PREV[name] = (now, usage)
    if prev and now > prev[0] and usage >= prev[1]:
        st["cpu"] = round(min(100.0, (usage - prev[1]) / 1e6 / (now - prev[0]) / max(capacity, 0.01) * 100), 1)
    return st


def _lxc_ls_rows() -> list:
    r = run(["lxc-ls", "-f", "-F", "NAME,STATE,AUTOSTART,PID"])
    has_pid = r["ok"]
    if not has_pid:
        r = run(["lxc-ls", "-f", "-F", "NAME,STATE,AUTOSTART"])
        if not r["ok"]:
            return []
    rows = []
    for line in r["stdout"].splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        pid = parts[3] if has_pid and len(parts) > 3 else ""
        rows.append(
            {
                "name": parts[0],
                "state": parts[1].upper(),
                "autostart": len(parts) > 2 and parts[2] == "1",
                "pid": int(pid) if pid.isdigit() else 0,
            }
        )
    return rows


def list_containers() -> list:
    if not which("lxc-ls"):
        return []
    rows = _lxc_ls_rows()

    def fill(c):
        running = c["state"] in ("RUNNING", "FROZEN")
        c["ipv4"], c["ipv6"] = container_ips(c["name"]) if c["state"] == "RUNNING" else ([], [])
        c["distro"], c["release"] = detect_distro(c["name"])
        p = ct_config_path(c["name"])
        c["rootfs_kind"], c["img_gb"] = "dir", 0
        if p:
            try:
                img = rootstore.image_path(p.read_text(errors="ignore"))
            except OSError:
                img = None
            if img:
                c["rootfs_kind"], c["img_gb"] = "loop", round(rootstore.apparent_bytes(img) / (1 << 30), 1)
        mem_mb = 0
        cpu_cores = 0
        try:
            cs = lxcconf.parse_settings(p.read_text(errors="ignore")) if p else {}
            c["webui"] = cs.get("webui", "")
            mem_mb = cs.get("memory_mb", 0)
            cpu_cores = cs.get("cpu_cores", 0)
            if p:  # 运行中的容器 lxc-ls 返回的是启动时加载的配置，以磁盘上的 config 为准
                c["autostart"] = bool(cs.get("autostart"))
        except OSError:
            c["webui"] = ""
        if running and not c["pid"]:
            r = run(["lxc-info", "-n", c["name"], "-pH"], timeout=10)
            c["pid"] = int(r["stdout"]) if r["ok"] and r["stdout"].isdigit() else 0
        c["stats"] = container_stats(c["name"], c["pid"], mem_mb, cpu_cores) if running and c["pid"] else None
        return c

    with ThreadPoolExecutor(max_workers=8) as ex:
        out = list(ex.map(fill, rows))
    names = {c["name"] for c in out}
    with _CPU_LOCK:
        for k in [k for k in _CPU_PREV if k not in names]:
            del _CPU_PREV[k]
        for k in [k for k in _NET_PREV if k not in names]:
            del _NET_PREV[k]
    return out


def set_autostart(name: str, enabled: bool) -> dict:
    name = clean_name(name)
    p = ct_config_path(name) if name else None
    if not p:
        return {"ok": False, "error": "容器不存在"}
    s = lxcconf.parse_settings(p.read_text(errors="ignore"))
    if not s["bridge"]:
        s["bridge"] = load_config().get("bridge") or "br0"
    s["autostart"] = bool(enabled)
    r = save_container_settings(name, s, check_mounts=False)
    oplog.log("container.autostart", ok=bool(r.get("ok")), name=name, detail="" if r.get("ok") else r.get("error", ""), enabled=bool(enabled))
    return r


def container_state(name: str) -> str:
    r = run(["lxc-info", "-n", name, "-sH"], timeout=10)
    return r["stdout"].strip().upper() if r["ok"] else "UNKNOWN"


def container_action(name: str, action: str, confirm: bool = False) -> dict:
    name = clean_name(name)
    if not name:
        return {"ok": False, "error": "invalid name"}
    if action == "destroy" and not confirm:
        return {"ok": False, "error": "删除容器需要 confirm=true 二次确认"}
    t = str(int(load_config().get("timeout", 15)))
    mapping = {
        "start": ["lxc-start", "-n", name, "-d"],
        "stop": ["lxc-stop", "-n", name, "-t", t],
        "kill": ["lxc-stop", "-n", name, "-k"],
        "freeze": ["lxc-freeze", "-n", name],
        "unfreeze": ["lxc-unfreeze", "-n", name],
        "destroy": ["lxc-destroy", "-n", name, "-f", "-s"],
    }
    if action not in mapping:
        return {"ok": False, "error": "unknown action"}
    if action == "start":
        p = ct_config_path(name)
        if p:
            _sync_guest_net(p, p.read_text(errors="ignore"))
    r = run(mapping[action], timeout=180)
    err = r["stderr"].splitlines()[-1] if r["stderr"] else ""
    ok = r["ok"]
    oplog.log("container." + action, ok=ok, name=name, detail="" if ok else (err or r.get("stderr") or r.get("stdout") or f"exit={r.get('code')}"))
    return {"ok": ok, "error": "" if ok else err, "detail": r}


# ----- 快照 -----

_SNAP_RE = re.compile(r"^snap\d+$")
_SNAP_LINE = re.compile(r"^(snap\d+)\s+\((.*?)\)\s+(\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2})\s*$")


def parse_snapshots(text: str) -> list:
    """解析 `lxc-snapshot -L` 输出：snapN (路径) 2026:11:22 10:00:00"""
    out = []
    for ln in (text or "").splitlines():
        m = _SNAP_LINE.match(ln.strip())
        if m:
            out.append({"name": m.group(1), "path": m.group(2), "time": m.group(3).replace(":", "-", 2)})
    out.sort(key=lambda x: int(x["name"][4:]))
    return out


def _snap_comment(path: str, snap: str) -> str:
    for d in (Path(path) / snap, Path(path) / snap / "snap"):
        try:
            return (d / "comment").read_text(errors="ignore").strip()[:200]
        except OSError:
            continue
    return ""


def list_snapshots(name: str) -> dict:
    name = clean_name(name)
    if not name or not ct_config_path(name):
        return {"ok": False, "error": "容器不存在"}
    r = run(["lxc-snapshot", "-n", name, "-L"], timeout=30)
    if not r["ok"] and "No snapshots" not in r["stderr"]:
        return {"ok": False, "error": r["stderr"].splitlines()[-1] if r["stderr"] else "lxc-snapshot 失败"}
    snaps = parse_snapshots(r["stdout"])
    for sn in snaps:
        sn["comment"] = _snap_comment(sn["path"], sn["name"])
    return {"ok": True, "snapshots": snaps}


def _container_state(name: str) -> str:
    r = run(["lxc-info", "-n", name, "-sH"], timeout=10)
    return r["stdout"].strip().upper() if r["ok"] else ""


def _free_bytes(path: Path) -> int:
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except OSError:
        return 0


def _dir_bytes(path: Path) -> int:
    r = run(["du", "-sb", str(path)], timeout=120)
    try:
        return int(r["stdout"].split()[0])
    except (ValueError, IndexError):
        return 0


def _snap_err(r: dict) -> str:
    """把 lxc 命令的失败原因尽量完整返回给前端（默认经常 stderr 为空）。"""
    parts = []
    code = r.get("code")
    if code not in (None, 0):
        parts.append(f"exit={code}")
    body = (r.get("stderr") or r.get("stdout") or "").strip()
    if body:
        lines = [ln for ln in body.splitlines() if ln.strip()]
        parts.append("\n".join(lines[-6:]) if lines else body)
    return " | ".join(parts) if parts else "失败"


def _snapshot_worker(job: dict, name: str, op: str, snap: str, comment: str, freeze: bool, as_new: str) -> None:
    """dir 后端的 lxc-snapshot 是 copy-clone，不支持运行中容器。
    --allowrunning 只存在于 lxc-copy，lxc-snapshot 不认该参数。
    因此创建/原地回滚时若容器在跑：先停止 → 操作 → 再启动（兼容运行中一键操作）。
    """
    tmp = None
    was_running = False
    stopped_by_us = False
    try:
        if op in ("create", "restore"):
            cp = ct_config_path(name)
            if cp and _fstype(cp.parent) not in ("btrfs", "zfs"):
                _job_set(job, stage="检查磁盘空间", progress=3)
                img = rootstore.image_path(cp.read_text(errors="ignore"))
                need = rootstore.alloc_bytes(img) if img else _dir_bytes(cp.parent / "rootfs")
                free = _free_bytes(cp.parent)
                if need and free and need > free:
                    _job_set(job, status="error", error=f"磁盘空间不足：容器约 {need >> 20} MB，剩余 {free >> 20} MB")
                    return
        if op == "create":
            state = _container_state(name)
            # lxc-snapshot 无法对 RUNNING/FROZEN 做 dir 快照，必须先停
            if state in ("RUNNING", "FROZEN"):
                was_running = True
                _job_set(job, stage="停止容器以创建快照", progress=8)
                if state == "FROZEN":
                    run(["lxc-unfreeze", "-n", name], timeout=30)
                t = str(int(load_config().get("timeout", 15)))
                r_stop = run(["lxc-stop", "-n", name, "-t", t], timeout=max(60, int(t) + 30))
                if not r_stop["ok"]:
                    r_stop = run(["lxc-stop", "-n", name, "-k"], timeout=30)
                st2 = _container_state(name)
                if st2 not in ("STOPPED", ""):
                    _job_set(job, status="error", error=f"无法停止容器（状态 {st2}），dir 存储下必须停止后才能创建快照。{_snap_err(r_stop)}")
                    return
                stopped_by_us = True

            _job_set(job, stage="创建快照", progress=20)
            argv = ["lxc-snapshot", "-n", name]
            if comment:
                import tempfile
                fd, tmp = tempfile.mkstemp(prefix="fnlxc-snap-")
                with os.fdopen(fd, "w") as f:
                    f.write(comment)
                argv += ["-c", tmp]
            r = run(argv, timeout=3600)

            if r["ok"] and stopped_by_us and was_running:
                _job_set(job, stage="重新启动容器", progress=90)
                run(["lxc-start", "-n", name, "-d"], timeout=120)
                stopped_by_us = False

            if r["ok"]:
                _job_set(job, status="done", progress=100, stage="完成")
                oplog.log("snapshot.create", ok=True, name=name, detail=comment or "")
            else:
                msg = _snap_err(r)
                if was_running:
                    msg = f"快照失败。详情: {msg}"
                _job_set(job, status="error", error=msg[:500])
                oplog.log("snapshot.create", ok=False, name=name, detail=msg[:500])
            return

        elif op == "restore":
            # 原地回滚需要容器停止；克隆为新容器不要求停原容器
            if not as_new:
                state = _container_state(name)
                if state in ("RUNNING", "FROZEN"):
                    was_running = True
                    _job_set(job, stage="停止容器以回滚", progress=8)
                    if state == "FROZEN":
                        run(["lxc-unfreeze", "-n", name], timeout=30)
                    t = str(int(load_config().get("timeout", 15)))
                    r_stop = run(["lxc-stop", "-n", name, "-t", t], timeout=max(60, int(t) + 30))
                    if not r_stop["ok"]:
                        r_stop = run(["lxc-stop", "-n", name, "-k"], timeout=30)
                    st2 = _container_state(name)
                    if st2 not in ("STOPPED", ""):
                        _job_set(job, status="error", error=f"无法停止容器（状态 {st2}），回滚前必须停止。{_snap_err(r_stop)}")
                        return
                    stopped_by_us = True
            _job_set(job, stage="回滚快照" if not as_new else "从快照克隆", progress=20)
            argv = ["lxc-snapshot", "-n", name, "-r", snap] + (["-N", as_new] if as_new else [])
            r = run(argv, timeout=3600)
            if r["ok"] and stopped_by_us and was_running and not as_new:
                _job_set(job, stage="重新启动容器", progress=90)
                run(["lxc-start", "-n", name, "-d"], timeout=120)
                stopped_by_us = False
        else:  # delete
            _job_set(job, stage="删除快照", progress=10)
            r = run(["lxc-snapshot", "-n", name, "-d", snap], timeout=600)
        if r["ok"]:
            _job_set(job, status="done", progress=100, stage="完成")
            if op == "restore":
                oplog.log("snapshot.restore" if not as_new else "snapshot.clone", ok=True, name=name, snap=snap, as_new=as_new or "")
            elif op == "delete":
                oplog.log("snapshot.delete", ok=True, name=name, snap=snap)
        else:
            err = _snap_err(r)[:500]
            _job_set(job, status="error", error=err)
            if op == "restore":
                oplog.log("snapshot.restore" if not as_new else "snapshot.clone", ok=False, name=name, snap=snap, detail=err, as_new=as_new or "")
            elif op == "delete":
                oplog.log("snapshot.delete", ok=False, name=name, snap=snap, detail=err)
    finally:
        if stopped_by_us and was_running:
            run(["lxc-start", "-n", name, "-d"], timeout=120)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass



def snapshot_op(name: str, body: dict) -> dict:
    name = clean_name(name)
    if not name or not ct_config_path(name):
        return {"ok": False, "error": "容器不存在"}
    op = body.get("op")
    snap = str(body.get("snap") or "")
    comment = str(body.get("comment") or "").strip()[:200]
    comment = "".join(c for c in comment if c.isprintable())
    as_new = ""
    if op not in ("create", "restore", "delete"):
        return {"ok": False, "error": "unknown op"}
    if op in ("restore", "delete"):
        if not _SNAP_RE.match(snap):
            return {"ok": False, "error": "快照名无效"}
        if not body.get("confirm"):
            return {"ok": False, "error": "该操作需要 confirm=true 二次确认"}
    if op == "restore":
        as_new = clean_name(str(body.get("as_new") or "")) if body.get("as_new") else ""
        if body.get("as_new") and not as_new:
            return {"ok": False, "error": "新容器名称无效"}
        if as_new and ct_config_path(as_new):
            return {"ok": False, "error": "新容器名称已存在"}
        # 原地回滚若容器在跑，由 worker 内自动停止→回滚→再启动
    with _JOBS_LOCK:
        if any(j["name"] == name and j["status"] == "running" for j in _JOBS.values()):
            return {"ok": False, "error": "该容器正有任务在执行"}
        jid = f"{int(time.time() * 1000)}"
        job = {"id": jid, "name": name, "status": "running", "progress": 1, "stage": "开始",
               "log": [], "elapsed": 0, "downloaded": 0, "total": 0, "error": ""}
        _JOBS[jid] = job
    threading.Thread(
        target=_snapshot_worker,
        args=(job, name, op, snap, comment, bool(body.get("freeze", True)), as_new),
        daemon=True,
    ).start()
    return {"ok": True, "job": jid}


def _fstype(path: Path) -> str:
    r = run(["stat", "-f", "-c", "%T", str(path)], timeout=5)
    return r["stdout"].strip() if r["ok"] else ""


def host_arch() -> str:
    a = run(["dpkg", "--print-architecture"]).get("stdout", "")
    if a:
        return a
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(run(["uname", "-m"]).get("stdout", ""), "amd64")


# ----- 创建容器（后台任务 + 进度） -----

# 镜像站目录结构与官方一致（/meta/1.0/index-system、/images/...）
IMAGE_MIRRORS = {
    "official": ("官方 images.linuxcontainers.org", "https://images.linuxcontainers.org"),
    "tuna": ("清华大学 TUNA", "https://mirrors.tuna.tsinghua.edu.cn/lxc-images"),
    "nju": ("南京大学 NJU", "https://mirror.nju.edu.cn/lxc-images"),
    "bfsu": ("北京外国语大学 BFSU", "https://mirrors.bfsu.edu.cn/lxc-images"),
}


def mirror_id(mid: str = "") -> str:
    mid = mid or load_config().get("image_mirror") or "official"
    return mid if mid in IMAGE_MIRRORS else "official"


def image_server(mid: str = "") -> str:
    return IMAGE_MIRRORS[mirror_id(mid)][1]


_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()
_IDX_CACHE: dict = {}

# 下载模板输出的阶段 -> 大致进度
_STAGES = [
    ("Setting up the GPG keyring", 3, "准备密钥环"),
    ("Downloading the image index", 6, "下载镜像索引"),
    ("Downloading the rootfs", 10, "下载根文件系统"),
    ("Downloading the metadata", 76, "下载元数据"),
    ("image cache is now ready", 80, "镜像缓存就绪"),
    ("Unpacking the rootfs", 85, "解包根文件系统"),
    ("You just created", 98, "完成"),
]


def _http(url: str, method="GET", timeout=15):
    import urllib.request
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "fnos-lxc"})
    return urllib.request.urlopen(req, timeout=timeout)


def image_index(mid: str = "") -> list:
    """镜像站的完整镜像索引（按镜像站缓存 1 小时）。"""
    mid = mirror_id(mid)
    now = time.time()
    c = _IDX_CACHE.get(mid)
    if c and c["rows"] and now - c["t"] < 3600:
        return c["rows"]
    rows = []
    try:
        with _http(image_server(mid) + "/meta/1.0/index-system") as r:
            for line in r.read().decode("utf-8", "ignore").splitlines():
                p = line.split(";")
                if len(p) >= 6:
                    rows.append({"distro": p[0], "release": p[1], "arch": p[2], "variant": p[3], "path": p[5]})
    except Exception:
        return c["rows"] if c else []
    _IDX_CACHE[mid] = {"t": now, "rows": rows}
    return rows


def _natkey(s: str):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", s)]


def list_templates(arch: str = "", mid: str = "") -> dict:
    arch = arch or host_arch()
    mid = mirror_id(mid)
    rows = [r for r in image_index(mid) if r["variant"] == "default" and r["arch"] == arch]
    info = {
        "mirror": mid,
        "mirrors": [{"id": k, "name": v[0]} for k, v in IMAGE_MIRRORS.items()],
        "arch": arch,
    }
    if not rows:
        return {**info, "templates": list_templates_hint(), "online": False}
    rows.sort(key=lambda r: (r["distro"], _natkey(r["release"])))
    return {**info, "templates": [{"distro": r["distro"], "release": r["release"]} for r in rows], "online": True}


def _job_set(job: dict, **kw) -> None:
    with _JOBS_LOCK:
        job.update(kw)


def _find_rootfs_download(distro: str, release: str, arch: str):
    """下载模板的缓存目录层级固定：<cache>/download/<distro>/<release>/<arch>/default/<build>/rootfs.*
    只做定深度的 glob，绝不递归遍历容器目录。"""
    import glob
    best = None
    for base in ("/var/cache/lxc", str(lxc_base() / "cache")):
        for f in glob.glob(f"{base}/download/{distro}/{release}/{arch}/default/*/rootfs.*"):
            try:
                if best is None or os.path.getmtime(f) > os.path.getmtime(best):
                    best = f
            except OSError:
                pass
        if best:
            break
    return best


def _create_worker(job: dict, name: str, distro: str, release: str, arch: str, autostart: bool, mid: str,
                   storage: str = "dir", size_gb: int = 0) -> None:
    t0 = time.time()
    argv = ["lxc-create", "-n", name, "-t", "download"]
    if storage == "loop":
        argv += ["-B", "loop", "--fssize", f"{size_gb}G", "--fstype", "ext4"]
    argv += ["--", "-d", distro, "-r", release, "-a", arch]
    if mid != "official":
        argv += ["--server", image_server(mid).split("://", 1)[-1]]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError as e:
        _job_set(job, status="error", error=f"无法执行 lxc-create：{e}")
        oplog.log("container.create", ok=False, name=name, detail=str(e), distro=distro, release=release)
        return

    def reader():
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            with _JOBS_LOCK:
                job["log"] = (job["log"] + [line])[-30:]
                for key, pct, label in _STAGES:
                    if key in line:
                        job["progress"], job["stage"] = max(job["progress"], pct), label

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    total = None
    asked = False
    while proc.poll() is None:
        time.sleep(1)
        job["elapsed"] = int(time.time() - t0)
        if job["stage"] == "下载根文件系统":
            if not asked:
                asked = True
                for r in image_index(mid):
                    if (r["distro"], r["release"], r["arch"], r["variant"]) == (distro, release, arch, "default"):
                        try:
                            with _http(image_server(mid) + r["path"] + "rootfs.tar.xz", "HEAD") as h:
                                total = int(h.headers.get("Content-Length") or 0) or None
                        except Exception:
                            total = None
                        break
            f = _find_rootfs_download(distro, release, arch)
            if f:
                try:
                    done = os.path.getsize(f)
                    _job_set(job, downloaded=done, total=total or 0)
                    if total:
                        _job_set(job, progress=max(job["progress"], 10 + min(65, int(done * 65 / total))))
                except OSError:
                    pass
    th.join(timeout=3)
    if proc.returncode != 0:
        tail = job["log"][-1] if job["log"] else ""
        _job_set(job, status="error", error="lxc-create 失败：" + tail)
        oplog.log("container.create", ok=False, name=name, detail="lxc-create 失败：" + tail, distro=distro, release=release)
        return

    p = ct_config_path(name)
    if p:
        text = p.read_text()
        s = lxcconf.parse_settings(text)
        s["autostart"] = bool(autostart)
        s["bridge"] = s["bridge"] or load_config().get("bridge") or "br0"
        _atomic_write(p, lxcconf.apply_settings(text, s))
    _job_set(job, status="done", progress=100, stage="完成")
    oplog.log("container.create", ok=True, name=name, distro=distro, release=release, arch=arch)


def create_container(body: dict) -> dict:
    name = clean_name(body.get("name", ""))
    distro = re.sub(r"[^a-z0-9]", "", str(body.get("distro", "alpine")).lower())
    release = re.sub(r"[^A-Za-z0-9._-]", "", str(body.get("release", "3.21")))
    arch = re.sub(r"[^a-z0-9_]", "", str(body.get("arch") or host_arch()))
    if not name:
        return {"ok": False, "error": "名称无效（字母/数字/. _ -，以字母或数字开头）"}
    if ct_config_path(name):
        return {"ok": False, "error": "容器已存在"}
    storage, size_gb, err = _parse_storage(body)
    if err:
        return {"ok": False, "error": err}
    mid = mirror_id(str(body.get("mirror") or ""))
    if mid != load_config().get("image_mirror"):
        cfg = load_config()
        cfg["image_mirror"] = mid
        save_config(cfg)
    with _JOBS_LOCK:
        if any(j["name"] == name and j["status"] == "running" for j in _JOBS.values()):
            return {"ok": False, "error": "该名称正在创建中"}
        jid = f"{int(time.time() * 1000)}"
        job = {"id": jid, "name": name, "status": "running", "progress": 1, "stage": "开始创建",
               "log": [], "elapsed": 0, "downloaded": 0, "total": 0, "error": ""}
        _JOBS[jid] = job
        for k in [k for k, j in _JOBS.items() if j["status"] != "running"][:-10]:
            _JOBS.pop(k, None)
    threading.Thread(
        target=_create_worker,
        args=(job, name, distro, release, arch, bool(body.get("autostart")), mid, storage, size_gb),
        daemon=True,
    ).start()
    oplog.log("container.create.start", ok=True, name=name, distro=distro, release=release, arch=arch, mirror=mid)
    return {"ok": True, "job": jid, "name": name}


def get_job(jid: str) -> dict:
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        return {"ok": True, **j} if j else {"ok": False, "error": "任务不存在"}


# ----- 自定义 rootfs 创建 / 导出 -----

def _custom_fail(job: dict, name: str, msg: str, cdir: Path | None = None) -> None:
    if cdir and cdir.exists():
        shutil.rmtree(cdir, ignore_errors=True)
    _job_set(job, status="error", error=msg[:500])
    oplog.log("container.create_custom", ok=False, name=name, detail=msg[:500])


def _extract_squash(job: dict, src: str, dst: Path) -> str:
    """返回错误信息，空串表示成功。优先 unsquashfs，没有则 loop 挂载后拷贝。"""
    if which("unsquashfs"):
        r = run(["unsquashfs", "-f", "-d", str(dst), src], timeout=7200)
        return "" if r["ok"] else _snap_err(r)
    import tempfile
    mnt = tempfile.mkdtemp(prefix="fnlxc-sq-")
    try:
        r = run(["mount", "-t", "squashfs", "-o", "loop,ro", src, mnt], timeout=60)
        if not r["ok"]:
            return "没有 unsquashfs，且 loop 挂载 squashfs 失败（可 apt install squashfs-tools）：" + _snap_err(r)
        try:
            p1 = subprocess.Popen(["tar", "--numeric-owner", "--xattrs", "-C", mnt, "-cf", "-", "."], stdout=subprocess.PIPE)
            p2 = subprocess.run(["tar", "--numeric-owner", "-C", str(dst), "-xpf", "-"], stdin=p1.stdout,
                                capture_output=True, text=True, timeout=7200)
            p1.stdout.close()
            p1.wait()
            return "" if p2.returncode == 0 and p1.returncode == 0 else (p2.stderr or "拷贝失败")[-300:]
        finally:
            run(["umount", mnt], timeout=60)
    finally:
        shutil.rmtree(mnt, ignore_errors=True)


def _custom_worker(job: dict, name: str, src: str, url: str, distro: str, release: str, arch: str, autostart: bool,
                   storage: str = "dir", size_gb: int = 0) -> None:
    t0 = time.time()
    cdir = lxc_base() / name
    downloaded = None
    try:
        # 1) 取得本地文件
        if url:
            ext = customfs.ext_of(url)
            cache = lxc_base() / "cache" / "custom"
            cache.mkdir(parents=True, exist_ok=True)
            downloaded = cache / f"{name}{ext}"
            _job_set(job, stage="下载 rootfs", progress=5)
            try:
                with _http(url, "GET", timeout=60) as resp, open(downloaded, "wb") as f:
                    total = int(resp.headers.get("Content-Length") or 0)
                    _job_set(job, total=total)
                    got = 0
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        _job_set(job, downloaded=got, elapsed=int(time.time() - t0),
                                 progress=5 + (min(45, int(got * 45 / total)) if total else 0))
            except Exception as e:  # noqa: BLE001
                return _custom_fail(job, name, f"下载失败：{e}")
            src = str(downloaded)
        kind = customfs.classify(src)
        size = os.path.getsize(src)
        free = _free_bytes(lxc_base())
        if free and size > free:
            return _custom_fail(job, name, f"磁盘空间不足：文件 {size >> 20} MB，剩余 {free >> 20} MB")
        if storage == "loop":
            msg = rootstore.check_size_vs_free(size_gb, free)
            if msg:
                return _custom_fail(job, name, msg)

        # 2) 预检
        strip = 0
        if kind == "tar":
            _job_set(job, stage="检查压缩包", progress=52)
            if customfs.ext_of(src) == ".tar.zst" and not which("zstd"):
                return _custom_fail(job, name, "缺少 zstd，请先 apt install zstd")
            lst = run(["tar", "-tf", src], timeout=1800)
            if not lst["ok"]:
                return _custom_fail(job, name, "无法读取压缩包：" + _snap_err(lst))
            names = [x for x in lst["stdout"].splitlines() if x]
            ok, why = customfs.check_members(names)
            if not ok:
                return _custom_fail(job, name, why)
            strip = customfs.strip_components(names)
        # 3) 解包（dir：直接解到目录；loop：先建 ext4 镜像并挂载，再解进去）
        def populate(rootfs: Path) -> str:
            """解包 + 架构/init 检查；返回错误信息，成功返回 ''。结果通过 nonlocal 带出。"""
            nonlocal distro, init_kind
            _job_set(job, stage="解包 rootfs（可能需要几分钟）", progress=55)
            if kind == "tar":
                argv = ["tar", "--numeric-owner", "--xattrs", "-xpf", src, "-C", str(rootfs)]
                if strip:
                    argv.append(f"--strip-components={strip}")
                r = run(argv, timeout=7200)
                if not r["ok"] and "xattr" in (r["stderr"] or "").lower():
                    r = run([a for a in argv if a != "--xattrs"], timeout=7200)
                if not r["ok"]:
                    return "解包失败：" + _snap_err(r)
            else:
                err = _extract_squash(job, src, rootfs)
                if err:
                    return "解包失败：" + err
            # 架构检查（LXC 只能原生运行与宿主机相同架构的容器）
            guest_arch = customfs.elf_arch(rootfs)
            if guest_arch and not customfs.arch_compatible(arch, guest_arch):
                return f"rootfs 是 {guest_arch} 架构，而本机是 {customfs.ARCH_MAP.get(arch, arch)}：LXC 不能运行异构架构的容器"
            init_kind = customfs.init_kind(lambda rel: (rootfs / rel).exists() or (rootfs / rel).is_symlink())
            if not distro:
                try:
                    m = re.search(r'^ID="?([A-Za-z0-9._-]+)"?', (rootfs / "etc/os-release").read_text(errors="ignore"), re.M)
                    distro = m.group(1).lower() if m else ""
                except OSError:
                    pass
            return ""

        init_kind = ""
        cdir.mkdir(parents=True)
        if storage == "loop":
            img = cdir / "rootfs.img"
            _job_set(job, stage=f"创建 {size_gb} GB ext4 镜像", progress=53)
            err = rootstore.mkfs_image(img, size_gb, name)
            if err:
                return _custom_fail(job, name, err, cdir)
            try:
                with rootstore.mounted_image(img) as mnt:
                    err = populate(mnt)
            except rootstore.RootfsError as e:
                err = str(e)
            if err:
                return _custom_fail(job, name, err, cdir)
            rootfs_spec = f"loop:{img}"
        else:
            rootfs = cdir / "rootfs"
            rootfs.mkdir()
            err = populate(rootfs)
            if err:
                return _custom_fail(job, name, err, cdir)
            rootfs_spec = f"dir:{rootfs}"
        # 4) 写配置
        _job_set(job, stage="生成容器配置", progress=92)
        if not init_kind:
            with _JOBS_LOCK:
                job["log"] = (job["log"] + ["警告：rootfs 里没有找到 init（/sbin/init 等），容器可能无法启动。Docker 导出的镜像通常不适用。"])[-30:]
        inc = Path("/usr/share/lxc/config")
        include = next((str(inc / f) for f in (f"{distro}.common.conf", "common.conf") if (inc / f).exists()), str(inc / "common.conf"))
        try:
            default_conf = Path("/etc/lxc/default.conf").read_text()
        except OSError:
            default_conf = ""
        text = customfs.build_config(name, rootfs_spec, distro, release, arch or host_arch(), default_conf, include,
                                     "/init" if init_kind == "init" else "")
        s = lxcconf.parse_settings(text)
        s["autostart"] = bool(autostart)
        s["bridge"] = s["bridge"] or load_config().get("bridge") or "br0"
        _atomic_write(cdir / "config", lxcconf.apply_settings(text, s))
        _job_set(job, status="done", progress=100, stage="完成")
        oplog.log("container.create_custom", ok=True, name=name, distro=distro, detail=Path(src).name, storage=storage)
    except Exception as e:  # noqa: BLE001
        _custom_fail(job, name, f"{type(e).__name__}: {e}", cdir)
    finally:
        if downloaded:
            try:
                downloaded.unlink()
            except OSError:
                pass


def _new_job(name: str) -> tuple[dict | None, str]:
    with _JOBS_LOCK:
        if any(j["name"] == name and j["status"] == "running" for j in _JOBS.values()):
            return None, "该名称正有任务在执行"
        jid = f"{int(time.time() * 1000)}"
        job = {"id": jid, "name": name, "status": "running", "progress": 1, "stage": "开始",
               "log": [], "elapsed": 0, "downloaded": 0, "total": 0, "error": ""}
        _JOBS[jid] = job
        for k in [k for k, j in _JOBS.items() if j["status"] != "running"][:-10]:
            _JOBS.pop(k, None)
    return job, ""


def _parse_storage(body: dict) -> tuple[str, int, str]:
    """前端传入的 storage=dir|loop 与 size_gb；返回 (storage, size_gb, 错误)。"""
    storage = "loop" if str(body.get("storage") or "dir") == "loop" else "dir"
    if storage == "dir":
        return "dir", 0, ""
    if not (which("mkfs.ext4") and which("losetup")):
        return "", 0, "宿主机缺少 mkfs.ext4 / losetup（apt install e2fsprogs mount）"
    size_gb, err = rootstore.valid_size_gb(body.get("size_gb"))
    if err:
        return "", 0, err
    err = rootstore.check_size_vs_free(size_gb, _free_bytes(lxc_base()))
    return "loop", size_gb, err


def create_custom_container(body: dict) -> dict:
    name = clean_name(body.get("name", ""))
    if not name:
        return {"ok": False, "error": "名称无效（字母/数字/. _ -，以字母或数字开头）"}
    if ct_config_path(name) or (lxc_base() / name).exists():
        return {"ok": False, "error": "容器已存在"}
    src, url = "", ""
    if str(body.get("url") or "").strip():
        url, err = customfs.check_url(body.get("url"))
    else:
        src, err = customfs.check_source_path(body.get("path"))
        if not err and not os.path.isfile(src):
            err = "文件不存在或不是普通文件"
    if err:
        return {"ok": False, "error": err}
    distro = re.sub(r"[^a-z0-9]", "", str(body.get("distro") or "").lower())
    release = re.sub(r"[^A-Za-z0-9._-]", "", str(body.get("release") or ""))
    arch = host_arch()  # LXC 只能原生运行本机架构，忽略前端传入的架构
    storage, size_gb, err = _parse_storage(body)
    if err:
        return {"ok": False, "error": err}
    job, err = _new_job(name)
    if not job:
        return {"ok": False, "error": err}
    threading.Thread(target=_custom_worker,
                     args=(job, name, src, url, distro, release, arch, bool(body.get("autostart")), storage, size_gb),
                     daemon=True).start()
    oplog.log("container.create_custom.start", ok=True, name=name, detail=url or src)
    return {"ok": True, "job": job["id"], "name": name}


def _export_worker(job: dict, name: str, dest: Path) -> None:
    cp = ct_config_path(name)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
        try:
            with rootstore.access(cp, rw=False) as rootfs:
                if not rootfs:
                    _job_set(job, status="error", error="找不到可用的 rootfs（不支持该存储类型）")
                    return
                _job_set(job, stage="打包 rootfs（可能需要几分钟）", progress=10)
                r = run(["tar", "--numeric-owner", "--xattrs", "-czpf", str(out), "-C", str(rootfs), "."], timeout=7200)
                if not r["ok"] and "xattr" in (r["stderr"] or "").lower():
                    r = run(["tar", "--numeric-owner", "-czpf", str(out), "-C", str(rootfs), "."], timeout=7200)
        except rootstore.RootfsError as e:
            _job_set(job, status="error", error=str(e))
            return
        if r["ok"] or r["code"] == 1:  # tar 退出码 1 = 文件在读取时有变化，仍然可用
            with _JOBS_LOCK:
                job["log"] = [str(out)]
            _job_set(job, status="done", progress=100, stage="完成：" + str(out))
            oplog.log("container.export", ok=True, name=name, detail=str(out))
        else:
            out.unlink(missing_ok=True)
            _job_set(job, status="error", error="打包失败：" + _snap_err(r))
            oplog.log("container.export", ok=False, name=name, detail=_snap_err(r)[:300])
    except Exception as e:  # noqa: BLE001
        _job_set(job, status="error", error=f"{type(e).__name__}: {e}")


def export_container(name: str, body: dict) -> dict:
    name = clean_name(name)
    cp = ct_config_path(name) if name else None
    if not cp:
        return {"ok": False, "error": "容器不存在"}
    if _container_state(name) not in ("STOPPED", ""):
        return {"ok": False, "error": "导出前请先停止容器（保证文件一致）"}
    d = str(body.get("dest") or "").strip()
    if d:
        d, err, _ = check_lxcpath(d)
        if err:
            return {"ok": False, "error": err}
        dest = Path(d)
    else:
        dest = lxc_base() / "exports"
    job, err = _new_job(name)
    if not job:
        return {"ok": False, "error": err}
    threading.Thread(target=_export_worker, args=(job, name, dest), daemon=True).start()
    return {"ok": True, "job": job["id"], "dest": str(dest)}


# ----- ext4 镜像：信息 / 扩容 -----

def storage_info() -> dict:
    base = lxc_base()
    free = _free_bytes(base)
    return {"ok": True, "path": str(base), "fstype": _fstype(base), "free": free,
            "can_loop": bool(which("mkfs.ext4") and which("losetup"))}


def _resize_worker(job: dict, name: str, img: Path, new_gb: int) -> None:
    _job_set(job, stage="扩容镜像（truncate → e2fsck → resize2fs）", progress=20)
    err = rootstore.resize_image(img, new_gb)
    if err:
        _job_set(job, status="error", error=err)
        oplog.log("container.resize", ok=False, name=name, detail=err)
    else:
        _job_set(job, status="done", progress=100, stage="完成")
        oplog.log("container.resize", ok=True, name=name, detail=f"{new_gb}GB")


def resize_container(name: str, body: dict) -> dict:
    name = clean_name(name)
    cp = ct_config_path(name) if name else None
    if not cp:
        return {"ok": False, "error": "容器不存在"}
    img = rootstore.image_path(cp.read_text(errors="ignore"))
    if not img or not img.is_file():
        return {"ok": False, "error": "该容器不是 ext4 镜像存储"}
    if _container_state(name) not in ("STOPPED", ""):
        return {"ok": False, "error": "扩容前请先停止容器"}
    if rootstore.is_attached(img):
        return {"ok": False, "error": "镜像正被占用（容器运行中或已被挂载），请先停止容器并卸载"}
    new_gb, err = rootstore.valid_size_gb(body.get("size_gb"))
    if err:
        return {"ok": False, "error": err}
    cur = rootstore.apparent_bytes(img)
    if new_gb * (1 << 30) <= cur:
        return {"ok": False, "error": f"只支持扩容：新大小需大于当前的 {cur >> 30} GB"}
    # 超卖检查：新增的上限不能超过磁盘剩余空间
    err = rootstore.check_size_vs_free(new_gb, _free_bytes(img.parent), already=cur)
    if err:
        return {"ok": False, "error": err}
    job, err = _new_job(name)
    if not job:
        return {"ok": False, "error": err}
    threading.Thread(target=_resize_worker, args=(job, name, img, new_gb), daemon=True).start()
    return {"ok": True, "job": job["id"]}


# ----- 容器配置（表单 / 原始） -----

def _prepare_host_devices(s: dict) -> None:
    """TUN / FUSE 需要宿主机有对应设备节点。"""
    if s.get("tun"):
        run(["modprobe", "tun"], timeout=10)
        Path("/dev/net").mkdir(parents=True, exist_ok=True)
        if not Path("/dev/net/tun").exists():
            run(["mknod", "/dev/net/tun", "c", "10", "200"], timeout=5)
            run(["chmod", "666", "/dev/net/tun"], timeout=5)
    if s.get("fuse"):
        run(["modprobe", "fuse"], timeout=10)


def get_container_settings(name: str) -> dict:
    name = clean_name(name)
    p = ct_config_path(name) if name else None
    if not p:
        return {"ok": False, "error": "容器不存在"}
    text = p.read_text(errors="ignore")
    s = lxcconf.parse_settings(text)
    cfg = load_config()
    if not s["bridge"]:
        s["bridge"] = cfg.get("bridge") or ""
    distro, release = detect_distro(name)
    return {
        "ok": True,
        "name": name,
        "state": container_state(name),
        "distro": distro,
        "release": release,
        "settings": s,
        "bridges": detect_bridges(),
    }


def save_container_settings(name: str, raw: dict, check_mounts: bool = True) -> dict:
    name = clean_name(name)
    p = ct_config_path(name) if name else None
    if not p:
        return {"ok": False, "error": "容器不存在"}
    s, err = lxcconf.validate(raw)
    if err:
        oplog.log("container.settings", ok=False, name=name, detail=err)
        return {"ok": False, "error": err}
    for m in s["mounts"] if check_mounts else []:
        if "optional" in m.get("options", []):
            continue
        if not Path(m["host"]).exists():
            return {"ok": False, "error": f"宿主机路径不存在：{m['host']}"}
    with _CONF_LOCK:
        text = p.read_text(errors="ignore")
        new = lxcconf.apply_settings(text, s, bind_is_file=lambda x: Path(x).is_file())
        (p.parent / "config.fnos-bak").write_text(text)
        _atomic_write(p, new)
    _sync_guest_net(p, new)
    _prepare_host_devices(s)
    out = {"ok": True, "needs_restart": container_state(name) in ("RUNNING", "FROZEN")}
    if s.get("docker"):
        out["warning"] = (
            "已开启 Docker/嵌套容器支持：AppArmor 已关闭、设备与 capability 全部放开，"
            "容器隔离性显著降低，仅在确实需要时使用。"
        )
    oplog.log("container.settings", ok=True, name=name)
    return out


def _sync_guest_net(p: Path, conf_text: str) -> None:
    """IPv4 静态时关闭容器内 eth0 的 DHCP，避免出现额外的自动分配地址。"""
    s = lxcconf.parse_settings(conf_text)
    name = p.parent.name
    pid = 0
    if rootstore.parse_rootfs(conf_text)[0] == "loop":
        with _JOBS_LOCK:  # 快照/扩容/导出等任务正在读写镜像时，不去挂载它
            if any(j["name"] == name and j["status"] == "running" for j in _JOBS.values()):
                oplog.log("container.guestnet", ok=False, name=name, detail="有任务正在使用镜像，已跳过容器内网络同步")
                return
    if container_state(name) in ("RUNNING", "FROZEN"):
        r = run(["lxc-info", "-n", name, "-pH"], timeout=10)
        pid = int(r["stdout"]) if r["ok"] and r["stdout"].strip().isdigit() else 0
        if not pid and rootstore.parse_rootfs(conf_text)[0] == "loop":
            return  # 运行中但取不到 PID：loop 镜像不能再挂载一次
    try:
        with rootstore.access(p, pid=pid) as root:
            if root is not None:
                guestnet.sync_guest_network(p, s["ipv4_mode"] == "static", s["ipv4_gateway"], root=root)
    except (OSError, rootstore.RootfsError) as e:
        oplog.log("container.guestnet", ok=False, name=name, detail=str(e)[:300])


def get_container_raw(name: str) -> dict:
    name = clean_name(name)
    p = ct_config_path(name) if name else None
    if not p:
        return {"ok": False, "error": "容器不存在"}
    return {"ok": True, "config": p.read_text(errors="ignore")}


def save_container_raw(name: str, text: str) -> dict:
    name = clean_name(name)
    p = ct_config_path(name) if name else None
    if not p:
        return {"ok": False, "error": "容器不存在"}
    text = text.replace("\r\n", "\n")
    if "\0" in text or len(text) > 256 * 1024:
        return {"ok": False, "error": "配置内容无效或过大"}
    if not any(k == "lxc.rootfs.path" and v for k, v in lxcconf.kv_effective(text)):
        oplog.log("container.raw_config", ok=False, name=name, detail="缺少 lxc.rootfs.path")
        return {"ok": False, "error": "配置中缺少有效的 lxc.rootfs.path（注释行不算），已拒绝保存"}
    with _CONF_LOCK:
        (p.parent / "config.fnos-bak").write_text(p.read_text(errors="ignore"))
        _atomic_write(p, text.rstrip("\n") + "\n")
    _sync_guest_net(p, p.read_text(errors="ignore"))
    return {"ok": True, "needs_restart": container_state(name) in ("RUNNING", "FROZEN")}


# ----- 网页终端（PTY + WebSocket） -----

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_send(wfile, lock, opcode: int, payload: bytes) -> None:
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    with lock:
        wfile.write(head + payload)
        wfile.flush()


WS_MAX_MESSAGE = 1024 * 1024  # 单条消息（含分片累计）上限
WS_IDLE_TIMEOUT = 90  # 秒；服务端每 30 秒发 ping，浏览器会自动回 pong


def _read_exact(rfile, n: int) -> bytes:
    buf = rfile.read(n)
    if buf is None or len(buf) < n:
        raise EOFError
    return buf


def _ws_recv(rfile):
    """返回 (opcode, payload)；连接断开/协议错误返回 (8, b"")。支持分片，强制客户端掩码。"""
    data, op0 = bytearray(), 0
    try:
        while True:
            b1, b2 = _read_exact(rfile, 2)
            fin, rsv, op = b1 & 0x80, b1 & 0x70, b1 & 0x0F
            n = b2 & 0x7F
            if rsv or not (b2 & 0x80):  # RSV 位必须为 0；客户端帧必须带掩码（RFC 6455 §5.1）
                return 8, b""
            if n == 126:
                n = struct.unpack("!H", _read_exact(rfile, 2))[0]
            elif n == 127:
                n = struct.unpack("!Q", _read_exact(rfile, 8))[0]
            mask = _read_exact(rfile, 4)
            if op >= 8 and (n > 125 or not fin):  # 控制帧不能分片、载荷 ≤125
                return 8, b""
            if n > WS_MAX_MESSAGE or len(data) + n > WS_MAX_MESSAGE:
                return 8, b""
            buf = _read_exact(rfile, n) if n else b""
            if n:
                # 用大整数异或代替逐字节循环
                key = (mask * (n // 4 + 1))[:n]
                buf = (int.from_bytes(buf, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")
            if op in (8, 9, 10):
                return op, buf
            if op != 0:
                op0 = op
            data += buf
            if fin:
                return op0, bytes(data)
    except (EOFError, struct.error, OSError):
        return 8, b""


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


_SHELL = 'cd /root 2>/dev/null; command -v bash >/dev/null 2>&1 && exec bash -l; exec sh -l'


_ENV = [
    "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME=/root", "TERM=xterm-256color", "LANG=C.UTF-8",
]


def _terminal_argv(name: str) -> list:
    """优先用 nsenter 进入容器命名空间（不依赖 lxc-attach 自带的 PTY 转发），失败时回退 lxc-attach。"""
    pid = run(["lxc-info", "-n", name, "-pH"]).get("stdout", "").strip()
    if pid.isdigit() and int(pid) > 1 and which("nsenter"):
        argv = ["nsenter", "-t", pid, "-m", "-u", "-i", "-n", "-p", "-C"]
        try:
            if os.readlink(f"/proc/{pid}/ns/user") != os.readlink("/proc/self/ns/user"):
                argv.append("-U")
        except OSError:
            pass
        return argv + ["-r", "--", "/usr/bin/env", "-i", *_ENV, "/bin/sh", "-c", _SHELL]
    argv = ["lxc-attach", "-n", name, "--clear-env"]
    for e in _ENV:
        argv += ["-v", e]
    return argv + ["--", "/bin/sh", "-c", _SHELL]


_TERM_SLOTS = threading.BoundedSemaphore(8)  # 同时最多 8 个终端


def terminal_session(handler, name: str, cols: int, rows: int) -> None:
    """已完成 WebSocket 握手后，把 PTY 与 WebSocket 对接。"""
    rfile, wfile = handler.rfile, handler.wfile
    lock = threading.Lock()
    master, slave = pty.openpty()
    _set_winsize(master, rows, cols)
    argv = _terminal_argv(name)
    # 用 setsid -c 让 PTY 成为控制终端（shell 才有作业控制、Ctrl+C 才生效）。
    # 不用 preexec_fn：它在多线程服务里可能死锁。没有 setsid 时退化为仅新会话。
    use_setsid = which("setsid")
    if use_setsid:
        argv = ["setsid", "-c"] + argv
    try:
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                start_new_session=not use_setsid, close_fds=True)
    except OSError:
        os.close(master)
        raise
    finally:
        os.close(slave)
    alive = threading.Event()
    alive.set()
    try:
        handler.connection.settimeout(WS_IDLE_TIMEOUT)
    except OSError:
        pass

    def pump():
        last_ping = time.monotonic()
        try:
            while alive.is_set():
                r, _, _ = select.select([master], [], [], 5)
                if r:
                    data = os.read(master, 65536)
                    if not data:
                        break
                    _ws_send(wfile, lock, 2, data)
                elif time.monotonic() - last_ping > 30:
                    _ws_send(wfile, lock, 9, b"")
                    last_ping = time.monotonic()
                if proc.poll() is not None and not r:
                    break
        except OSError:
            pass
        finally:
            alive.clear()
            try:
                _ws_send(wfile, lock, 8, struct.pack("!H", 1000))
                handler.connection.shutdown(2)
            except OSError:
                pass

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    try:
        while alive.is_set():
            op, payload = _ws_recv(rfile)
            if op == 8:
                break
            if op == 9:
                _ws_send(wfile, lock, 10, payload)
            elif op == 2:
                os.write(master, payload)
            elif op == 1:
                try:
                    msg = json.loads(payload.decode("utf-8"))
                    if msg.get("type") == "resize":
                        _set_winsize(master, max(1, min(500, int(msg.get("rows", 24)))),
                                     max(1, min(1000, int(msg.get("cols", 80)))))
                except (ValueError, TypeError, AttributeError):
                    continue
    except OSError:
        pass
    finally:
        alive.clear()
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except OSError:
                break
            try:
                proc.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                continue
        try:
            os.close(master)
        except OSError:
            pass


def list_templates_hint() -> list:
    """Common download templates (names follow images.linuxcontainers.org)."""
    return [
        {"distro": "alpine", "release": "3.21", "label": "Alpine 3.21"},
        {"distro": "alpine", "release": "3.20", "label": "Alpine 3.20"},
        {"distro": "debian", "release": "bookworm", "label": "Debian 12 (bookworm)"},
        {"distro": "debian", "release": "bullseye", "label": "Debian 11 (bullseye)"},
        {"distro": "ubuntu", "release": "noble", "label": "Ubuntu 24.04 (noble)"},
        {"distro": "ubuntu", "release": "jammy", "label": "Ubuntu 22.04 (jammy)"},
        {"distro": "fedora", "release": "41", "label": "Fedora 41"},
        {"distro": "fedora", "release": "40", "label": "Fedora 40"},
        {"distro": "rockylinux", "release": "9", "label": "Rocky Linux 9"},
        {"distro": "almalinux", "release": "9", "label": "AlmaLinux 9"},
        {"distro": "archlinux", "release": "current", "label": "Arch Linux"},
        {"distro": "opensuse", "release": "15.6", "label": "openSUSE 15.6"},
        {"distro": "kali", "release": "current", "label": "Kali Linux"},
    ]


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    # 统一网关 Unix Socket：飞牛网关已校验登录，并通过 X-Trim-* 头转发用户；仅管理员可用
    gateway = False

    def log_message(self, fmt, *args):
        pass  # quiet

    def _strip_prefix(self, path: str) -> str:
        if path == GATEWAY_PREFIX or path.startswith(GATEWAY_PREFIX + "/"):
            return path[len(GATEWAY_PREFIX):] or "/"
        return path

    def _peer_uid(self):
        try:
            creds = self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", creds)[1]
        except (OSError, AttributeError, struct.error):
            return None

    def _gateway_denied(self) -> bool:
        if not self.gateway:
            return False
        # 先验证「谁连进来的」：X-Trim-* 头可以被本机任何能连上 socket 的用户伪造，
        # 只有来自受信任 uid（默认 root，即飞牛网关）的连接才采信这些头。
        uid = self._peer_uid()
        allowed = {0, os.getuid()}
        for x in load_config().get("allowed_uids") or []:
            if isinstance(x, int):
                allowed.add(x)
        if uid not in allowed:
            print(f"[auth] rejected socket peer uid={uid}; add it to allowed_uids in {CONFIG_FILE} if it is the gateway", flush=True)
            oplog.log("auth.reject", ok=False, detail=f"peer uid={uid}")
            msg = f"拒绝访问：连接进程 uid={uid} 不受信任。".encode("utf-8")
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return True
        if self.headers.get("X-Trim-Isadmin", "").lower() == "true":
            return False
        body = "仅飞牛管理员可使用 LXC 管理。".encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def _json(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _same_origin(self) -> bool:
        """拒绝跨站请求（本服务以 root 运行，防止被网页 CSRF）。
        自定义请求头无法被跨站表单/脚本在不经预检的情况下发送，因此必须带上；
        直连端口时再额外比对 Origin 与 Host（经网关时 Host 不可靠，只靠自定义头）。"""
        if self.headers.get(CSRF_HEADER[0]) != CSRF_HEADER[1]:
            return False
        if self.gateway:
            return True
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urlparse(origin).netloc == (self.headers.get("Host") or "")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _serve_static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        fs = (STATIC_DIR / path.lstrip("/")).resolve()
        if not fs.is_relative_to(STATIC_DIR.resolve()) or not fs.is_file():
            self.send_error(404)
            return
        ctype = "text/html; charset=utf-8"
        if fs.suffix == ".js":
            ctype = "application/javascript"
        elif fs.suffix == ".css":
            ctype = "text/css"
        elif fs.suffix == ".png":
            ctype = "image/png"
        elif fs.suffix == ".svg":
            ctype = "image/svg+xml"
        data = fs.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _guard(self, fn):
        try:
            fn()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            try:
                self._json(500, {"ok": False, "error": f"内部错误：{type(e).__name__}: {e}"})
            except OSError:
                pass

    def do_GET(self):
        self._guard(self._do_GET)

    def do_POST(self):
        self._guard(self._do_POST)

    def _do_GET(self):
        if self._gateway_denied():
            return
        u = urlparse(self.path)
        if u.path == GATEWAY_PREFIX:  # 补上结尾斜杠，页面里的相对地址才能解析到网关路径下
            self.send_response(301)
            self.send_header("Location", GATEWAY_PREFIX.rsplit("/", 1)[1] + "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = self._strip_prefix(u.path)

        mt = re.match(r"^/api/container/([^/]+)/term$", path)
        if mt:
            self.handle_term(mt.group(1), parse_qs(u.query))
            return
        if path.startswith("/api/"):
            self.handle_api_get(path, parse_qs(u.query))
            return
        self._serve_static(path)

    def _do_POST(self):
        if self._gateway_denied():
            return
        u = urlparse(self.path)
        path = self._strip_prefix(u.path)
        if not path.startswith("/api/"):
            self.send_error(404)
            return
        if not self._same_origin():
            self._json(403, {"ok": False, "error": "cross-origin request refused"})
            return
        body = self._read_json()
        self.handle_api_post(path, body)

    def handle_term(self, name, qs):
        name = clean_name(name)
        key = self.headers.get("Sec-WebSocket-Key")
        if not name or not key or "websocket" not in (self.headers.get("Upgrade") or "").lower():
            self._json(400, {"ok": False, "error": "websocket required"})
            return
        if not consume_term_token((qs.get("token") or [""])[0], name):
            self._json(403, {"ok": False, "error": "invalid or expired token"})
            return
        if container_state(name) != "RUNNING":
            self._json(409, {"ok": False, "error": "容器未运行"})
            return
        if not _TERM_SLOTS.acquire(blocking=False):
            self._json(429, {"ok": False, "error": "终端数量已达上限，请先关闭其它终端"})
            return
        try:
            self._handle_term_ws(name, key, qs)
        finally:
            _TERM_SLOTS.release()

    def _handle_term_ws(self, name, key, qs):
        accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        try:
            cols = int(qs.get("cols", ["80"])[0])
            rows = int(qs.get("rows", ["24"])[0])
        except ValueError:
            cols, rows = 80, 24
        self.close_connection = True
        terminal_session(self, name, cols, rows)

    def handle_api_get(self, path, qs):
        cfg = load_config()

        if path == "/api/logs":
            try:
                n = int((qs.get("limit") or ["200"])[0])
            except ValueError:
                n = 200
            self._json(200, {"ok": True, "logs": oplog.read_tail(n)})
            return

        if path == "/api/status":
            self._json(
                200,
                {
                    "initialized": cfg.get("initialized", False),
                    "has_lxc": which("lxc-ls"),
                    "config": {
                        "lxcpath": cfg.get("lxcpath"),
                        "bridge": cfg.get("bridge"),
                        "timeout": cfg.get("timeout"),
                    },
                },
            )
            return

        if path == "/api/setup/info":
            self._json(
                200,
                {
                    "has_lxc": which("lxc-ls"),
                    "bridges": detect_bridges(),
                    "volumes": detect_data_volumes(),
                    "arch": host_arch(),
                },
            )
            return

        if path == "/api/bridges":
            self._json(200, {"bridges": detect_bridges()})
            return

        if path == "/api/containers":
            if not cfg.get("initialized"):
                self._json(200, {"containers": [], "initialized": False})
                return
            self._json(200, {"containers": list_containers(), "initialized": True})
            return

        if path == "/api/templates":
            self._json(200, list_templates(mid=(qs.get("mirror") or [""])[0]))
            return

        m = re.match(r"^/api/job/(\d+)$", path)
        if m:
            self._json(200, get_job(m.group(1)))
            return

        if path == "/api/storage":
            self._json(200, storage_info())
            return

        m = re.match(r"^/api/container/([^/]+)/snapshots$", path)
        if m:
            self._json(200, list_snapshots(m.group(1)))
            return

        m = re.match(r"^/api/container/([^/]+)/(settings|config)$", path)
        if m:
            name, what = m.groups()
            self._json(200, get_container_settings(name) if what == "settings" else get_container_raw(name))
            return

        self._json(404, {"error": "not found"})

    def handle_api_post(self, path, body):
        if path == "/api/setup/install-packages":
            self._json(200, start_install_job())
            return

        if path == "/api/setup/finalize":
            lxcpath = body.get("lxcpath") or "/vol1/lxc"
            bridge = body.get("bridge") or "br0"
            if not lxcconf.valid_bridge(bridge):
                self._json(200, {"ok": False, "error": "网桥名称无效"})
                return
            self._json(200, finalize_init(lxcpath, bridge, _clamp_int(body.get("timeout"), 15, 1, 600)))
            return

        if path == "/api/container/create-custom":
            self._json(200, create_custom_container(body))
            return

        m = re.match(r"^/api/container/([^/]+)/resize$", path)
        if m:
            self._json(200, resize_container(m.group(1), body))
            return

        m = re.match(r"^/api/container/([^/]+)/export$", path)
        if m:
            self._json(200, export_container(m.group(1), body))
            return

        if path == "/api/container/create":
            self._json(200, create_container(body))
            return

        if path == "/api/container/term-token":
            name = clean_name(body.get("name", ""))
            if not name:
                self._json(400, {"ok": False, "error": "invalid name"})
                return
            self._json(200, {"ok": True, "token": issue_term_token(name)})
            return

        if path == "/api/container/autostart":
            self._json(200, set_autostart(body.get("name", ""), body.get("enabled")))
            return

        m = re.match(r"^/api/container/([^/]+)/snapshot$", path)
        if m:
            self._json(200, snapshot_op(m.group(1), body))
            return

        if path == "/api/container/action":
            self._json(
                200,
                container_action(
                    body.get("name", ""),
                    body.get("action", ""),
                    confirm=bool(body.get("confirm")),
                ),
            )
            return

        m = re.match(r"^/api/container/([^/]+)/(settings|config)$", path)
        if m:
            name, what = m.groups()
            if what == "settings":
                self._json(200, save_container_settings(name, body))
            else:
                self._json(200, save_container_raw(name, str(body.get("config", ""))))
            return

        if path == "/api/logs/clear":
            oplog.clear()
            oplog.log("logs.clear", ok=True)
            self._json(200, {"ok": True})
            return

        if path == "/api/config/save":

            cfg = load_config()
            if "bridge" in body:
                if body["bridge"] and not lxcconf.valid_bridge(body["bridge"]):
                    self._json(200, {"ok": False, "error": "网桥名称无效"})
                    return
                cfg["bridge"] = body["bridge"]
            for k, lo, hi in (("timeout", 1, 600), ("autostart_delay", 0, 600)):
                if k in body:
                    cfg[k] = _clamp_int(body[k], cfg.get(k, 15), lo, hi)
            if "service_enabled" in body:
                cfg["service_enabled"] = bool(body["service_enabled"])
            warning = ""
            if "lxcpath" in body:
                cfg["lxcpath"] = body["lxcpath"]
            if cfg.get("lxcpath") and cfg.get("bridge"):
                r = step_configure_storage(cfg["lxcpath"])
                if not r["ok"]:
                    self._json(200, r)
                    return
                cfg["lxcpath"], warning = r["lxcpath"], r.get("warning", "")
                step_write_lxc_conf(cfg["lxcpath"], cfg["bridge"], int(cfg.get("timeout") or 15))
            save_config(cfg)
            self._json(200, {"ok": True, "config": cfg, "warning": warning})
            return

        self._json(404, {"error": "not found"})


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _autostart_pending() -> list:
    return [c["name"] for c in list_containers() if c["autostart"] and c["state"] != "RUNNING"]


def boot_autostart(wait_total: int = 600) -> None:
    """每次开机只执行一次：等数据卷和网桥就绪后，启动所有 lxc.start.auto=1 的容器。"""
    boot = _boot_id()
    marker = DATA_DIR / "autostart.boot"
    try:
        if boot and marker.read_text().strip() == boot:
            return
    except OSError:
        pass
    waited = 0
    while waited < wait_total:
        cfg = load_config()
        if cfg.get("initialized") and cfg.get("lxcpath") and Path(cfg["lxcpath"]).is_dir():
            bridge = cfg.get("bridge")
            if not bridge or (SYS_NET / bridge).exists():
                break
        threading.Event().wait(5)
        waited += 5
    else:
        print("autostart: lxcpath/bridge not ready, skipped", flush=True)
        return
    if boot:
        try:
            marker.write_text(boot)
        except OSError:
            pass
    if not which("lxc-autostart"):
        return
    for attempt in range(3):
        if not _autostart_pending():
            break
        print(f"autostart: lxc-autostart (attempt {attempt + 1})", flush=True)
        r = run(["lxc-autostart", "-P", lxc_base().as_posix()], timeout=3600)
        if not r["ok"]:
            print(f"autostart: {r['stderr']}", flush=True)
        threading.Event().wait(5)
    left = _autostart_pending()
    if left:
        print(f"autostart: still stopped: {', '.join(left)}", flush=True)


class GatewayHandler(Handler):
    gateway = True


class UnixHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)  # 跳过 HTTPServer 对 (host, port) 的解包
        self.server_name = "localhost"
        self.server_port = 0


def make_unix_server(path: str) -> UnixHTTPServer:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    srv = UnixHTTPServer(path, GatewayHandler)
    # 0666 只是让「任何进程都能连上并拿到明确的 403 提示」；真正的准入在 Handler._gateway_denied
    # 里用 SO_PEERCRED 校验对端 uid，不信任客户端自报的 X-Trim-* 头。
    os.chmod(path, 0o666)
    return srv


def main():
    global DATA_DIR, CONFIG_FILE
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--bind", default="127.0.0.1", help="仅调试用的 TCP 模式；该模式没有身份认证，勿绑定到局域网")
    parser.add_argument("--socket", default="", help="统一网关 Unix Socket 路径")
    args = parser.parse_args()
    DATA_DIR = Path(args.data_dir)
    CONFIG_FILE = DATA_DIR / "config.json"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    oplog.init(DATA_DIR)
    oplog.log("server.start", ok=True, detail=f"data={DATA_DIR}")

    threading.Thread(target=boot_autostart, daemon=True).start()
    # 指定 --socket 时只走飞牛统一网关（由网关校验登录），不再开放任何 TCP 端口
    if args.socket:
        server = make_unix_server(args.socket)
        print(f"fnos-lxc gateway socket {args.socket}, data={DATA_DIR}", flush=True)
    else:
        if args.bind not in ("127.0.0.1", "::1", "localhost"):
            print("WARNING: TCP 模式没有身份认证，绑定到非本机地址会把 root 级容器管理暴露给局域网", flush=True)
        server = ThreadingHTTPServer((args.bind, args.port), Handler)
        print(f"fnos-lxc listening on {args.bind}:{args.port}, data={DATA_DIR}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
