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

import guestnet
import lxcconf

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

def run(cmd, timeout=120, input_text=None):
    try:
        r = subprocess.run(
            cmd,
            shell=isinstance(cmd, str),
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
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
    return run(f"command -v {bin_name}").get("ok", False)


# ---------------------------------------------------------------------------
# Setup / init steps
# ---------------------------------------------------------------------------

def detect_bridges() -> list:
    """列出可用网桥：OVS 桥 + Linux 桥，*-ovs 排最前；每次调用实时探测。"""
    found = []
    if which("ovs-vsctl"):
        r = run("ovs-vsctl list-br", timeout=10)
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
    env_cmd = (
        "DEBIAN_FRONTEND=noninteractive apt-get install -y "
        "-o Dpkg::Options::=--force-confdef "
        "-o Dpkg::Options::=--force-confold "
        + " ".join(pkgs)
    )
    r = run("DEBIAN_FRONTEND=noninteractive apt-get update -qq", timeout=180)
    if not r["ok"]:
        return {"ok": False, "step": "apt-update", "detail": r, "has_lxc": False}
    r2 = run(env_cmd, timeout=600)
    ok = which("lxc-ls") and which("lxc-create")
    if not r2["ok"] and not ok:
        return {"ok": False, "step": "apt-install", "detail": r2, "has_lxc": False}
    return {"ok": ok, "step": "packages", "detail": r2, "has_lxc": ok}


def step_configure_storage(lxcpath: str) -> dict:
    lxcpath = lxcpath.strip().rstrip("/")
    if not lxcpath.startswith("/") or lxcpath.count("/") < 2:
        return {"ok": False, "error": "请填写数据卷下的绝对路径，例如 /vol1/1000/lxc"}
    cache = Path(lxcpath) / "cache"
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "error": f"无法创建目录: {e}"}
    warn = ""
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
            warn = "/var/lib/lxc 下已有内容，未迁移；新容器将创建在所选路径"
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
    Path("/etc/lxc/lxc.conf").write_text(lxc_conf)
    Path("/etc/lxc/default.conf").write_text(default_conf)
    # also keep a copy in data dir
    (DATA_DIR / "lxc.conf").write_text(lxc_conf)
    (DATA_DIR / "default.conf").write_text(default_conf)
    return {"ok": True}


def finalize_init(lxcpath: str, bridge: str, timeout: int = 15) -> dict:
    r1 = step_configure_storage(lxcpath)
    if not r1["ok"]:
        return r1
    r2 = step_write_lxc_conf(lxcpath, bridge, timeout)
    if not r2["ok"]:
        return r2
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


def container_stats(name: str, pid: int, mem_limit_mb: int = 0) -> dict:
    ncpu = os.cpu_count() or 1
    st = {"pid": pid, "cpus": f"0-{ncpu - 1}" if ncpu > 1 else "0", "cpu": None,
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
    try:
        eff = (d / "cpuset.cpus.effective").read_text().strip()
        if eff:
            st["cpus"] = eff
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
        st["cpu"] = round(min(100.0, (usage - prev[1]) / 1e6 / (now - prev[0]) / ncpu * 100), 1)
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
        mem_mb = 0
        try:
            cs = lxcconf.parse_settings(p.read_text(errors="ignore")) if p else {}
            c["webui"] = cs.get("webui", "")
            mem_mb = cs.get("memory_mb", 0)
        except OSError:
            c["webui"] = ""
        if running and not c["pid"]:
            r = run(["lxc-info", "-n", c["name"], "-pH"], timeout=10)
            c["pid"] = int(r["stdout"]) if r["ok"] and r["stdout"].isdigit() else 0
        c["stats"] = container_stats(c["name"], c["pid"], mem_mb) if running and c["pid"] else None
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
    return save_container_settings(name, s)


def container_state(name: str) -> str:
    r = run(["lxc-info", "-n", name, "-sH"], timeout=10)
    return r["stdout"].strip().upper() if r["ok"] else "UNKNOWN"


def container_action(name: str, action: str) -> dict:
    name = clean_name(name)
    if not name:
        return {"ok": False, "error": "invalid name"}
    t = str(int(load_config().get("timeout", 15)))
    mapping = {
        "start": ["lxc-start", "-n", name, "-d"],
        "stop": ["lxc-stop", "-n", name, "-t", t],
        "kill": ["lxc-stop", "-n", name, "-k"],
        "freeze": ["lxc-freeze", "-n", name],
        "unfreeze": ["lxc-unfreeze", "-n", name],
        "destroy": ["lxc-destroy", "-n", name, "-f"],
    }
    if action not in mapping:
        return {"ok": False, "error": "unknown action"}
    if action == "start":
        p = ct_config_path(name)
        if p:
            _sync_guest_net(p, p.read_text(errors="ignore"))
    r = run(mapping[action], timeout=180)
    err = r["stderr"].splitlines()[-1] if r["stderr"] else ""
    return {"ok": r["ok"], "error": "" if r["ok"] else err, "detail": r}


def host_arch() -> str:
    a = run("dpkg --print-architecture").get("stdout", "")
    if a:
        return a
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(run("uname -m").get("stdout", ""), "amd64")


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
    import time
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
    import glob
    best = None
    for base in ("/var/cache/lxc", str(lxc_base()), "/var/cache"):
        for f in glob.glob(f"{base}/**/{distro}/{release}/{arch}/default/**/rootfs.tar.*", recursive=True):
            try:
                if best is None or os.path.getmtime(f) > os.path.getmtime(best):
                    best = f
            except OSError:
                pass
        if best:
            break
    return best


def _create_worker(job: dict, name: str, distro: str, release: str, arch: str, autostart: bool, mid: str) -> None:
    import time
    t0 = time.time()
    argv = ["lxc-create", "-n", name, "-t", "download", "--", "-d", distro, "-r", release, "-a", arch]
    if mid != "official":
        argv += ["--server", image_server(mid).split("://", 1)[-1]]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError as e:
        _job_set(job, status="error", error=f"无法执行 lxc-create：{e}")
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
        return

    p = ct_config_path(name)
    if p:
        text = p.read_text()
        s = lxcconf.parse_settings(text)
        s["autostart"] = bool(autostart)
        s["bridge"] = s["bridge"] or load_config().get("bridge") or "br0"
        p.write_text(lxcconf.apply_settings(text, s))
    _job_set(job, status="done", progress=100, stage="完成")


def create_container(body: dict) -> dict:
    import time
    name = clean_name(body.get("name", ""))
    distro = re.sub(r"[^a-z0-9]", "", str(body.get("distro", "alpine")).lower())
    release = re.sub(r"[^A-Za-z0-9._-]", "", str(body.get("release", "3.21")))
    arch = re.sub(r"[^a-z0-9_]", "", str(body.get("arch") or host_arch()))
    if not name:
        return {"ok": False, "error": "名称无效（字母/数字/. _ -，以字母或数字开头）"}
    if ct_config_path(name):
        return {"ok": False, "error": "容器已存在"}
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
        target=_create_worker, args=(job, name, distro, release, arch, bool(body.get("autostart")), mid), daemon=True
    ).start()
    return {"ok": True, "job": jid, "name": name}


def get_job(jid: str) -> dict:
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        return {"ok": True, **j} if j else {"ok": False, "error": "任务不存在"}


# ----- 容器配置（表单 / 原始） -----

def _prepare_host_devices(s: dict) -> None:
    """TUN / FUSE 需要宿主机有对应设备节点。"""
    if s.get("tun"):
        run("modprobe tun 2>/dev/null; mkdir -p /dev/net; [ -e /dev/net/tun ] || mknod /dev/net/tun c 10 200; chmod 666 /dev/net/tun")
    if s.get("fuse"):
        run("modprobe fuse 2>/dev/null")


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


def save_container_settings(name: str, raw: dict) -> dict:
    name = clean_name(name)
    p = ct_config_path(name) if name else None
    if not p:
        return {"ok": False, "error": "容器不存在"}
    s, err = lxcconf.validate(raw)
    if err:
        return {"ok": False, "error": err}
    for m in s["mounts"]:
        if not Path(m["host"]).exists():
            return {"ok": False, "error": f"宿主机路径不存在：{m['host']}"}
    with _CONF_LOCK:
        text = p.read_text(errors="ignore")
        new = lxcconf.apply_settings(text, s, bind_is_file=lambda x: Path(x).is_file())
        (p.parent / "config.fnos-bak").write_text(text)
        p.write_text(new)
    _sync_guest_net(p, new)
    _prepare_host_devices(s)
    return {"ok": True, "needs_restart": container_state(name) in ("RUNNING", "FROZEN")}


def _sync_guest_net(p: Path, conf_text: str) -> None:
    """IPv4 静态时关闭容器内 eth0 的 DHCP，避免出现额外的自动分配地址。"""
    s = lxcconf.parse_settings(conf_text)
    try:
        guestnet.sync_guest_network(p, s["ipv4_mode"] == "static", s["ipv4_gateway"])
    except OSError:
        pass


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
    if "lxc.rootfs.path" not in text:
        return {"ok": False, "error": "配置中缺少 lxc.rootfs.path，已拒绝保存"}
    with _CONF_LOCK:
        (p.parent / "config.fnos-bak").write_text(p.read_text(errors="ignore"))
        p.write_text(text.replace("\r\n", "\n").rstrip("\n") + "\n")
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


def _ws_recv(rfile):
    """返回 (opcode, payload)；连接断开返回 (8, b"")。支持分片。"""
    data, op0 = b"", 0
    while True:
        h = rfile.read(2)
        if len(h) < 2:
            return 8, b""
        b1, b2 = h
        fin, op = b1 & 0x80, b1 & 0x0F
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", rfile.read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", rfile.read(8))[0]
        mask = rfile.read(4) if b2 & 0x80 else b""
        if n > 4 * 1024 * 1024:
            return 8, b""
        buf = rfile.read(n) if n else b""
        if len(buf) < n:
            return 8, b""
        if mask:
            buf = bytes(c ^ mask[i % 4] for i, c in enumerate(buf))
        if op in (8, 9, 10):
            return op, buf
        if op != 0:
            op0 = op
        data += buf
        if fin:
            return op0, data


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


def terminal_session(handler, name: str, cols: int, rows: int) -> None:
    """已完成 WebSocket 握手后，把 PTY 与 WebSocket 对接。"""
    rfile, wfile = handler.rfile, handler.wfile
    lock = threading.Lock()
    master, slave = pty.openpty()
    _set_winsize(master, rows, cols)
    argv = _terminal_argv(name)

    def _ctty():
        # 让 PTY 成为子进程的控制终端，shell 才有作业控制、Ctrl+C 才能生效
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, start_new_session=True, close_fds=True, preexec_fn=_ctty)
    os.close(slave)
    alive = threading.Event()
    alive.set()

    def pump():
        try:
            while alive.is_set():
                data = os.read(master, 65536)
                if not data:
                    break
                _ws_send(wfile, lock, 2, data)
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
                except ValueError:
                    continue
                if msg.get("type") == "resize":
                    _set_winsize(master, max(1, int(msg.get("rows", 24))), max(1, int(msg.get("cols", 80))))
    except (OSError, struct.error):
        pass
    finally:
        alive.clear()
        try:
            os.killpg(proc.pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
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

    def _gateway_denied(self) -> bool:
        if not self.gateway or self.headers.get("X-Trim-Isadmin", "").lower() == "true":
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
        if not str(fs).startswith(str(STATIC_DIR.resolve())) or not fs.is_file():
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

    def do_GET(self):
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

    def do_POST(self):
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

        m = re.match(r"^/api/container/([^/]+)/(settings|config)$", path)
        if m:
            name, what = m.groups()
            self._json(200, get_container_settings(name) if what == "settings" else get_container_raw(name))
            return

        self._json(404, {"error": "not found"})

    def handle_api_post(self, path, body):
        if path == "/api/setup/install-packages":
            self._json(200, step_install_packages())
            return

        if path == "/api/setup/finalize":
            lxcpath = body.get("lxcpath") or "/vol1/lxc"
            bridge = body.get("bridge") or "br0"
            timeout = int(body.get("timeout") or 15)
            self._json(200, finalize_init(lxcpath, bridge, timeout))
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

        if path == "/api/container/action":
            self._json(200, container_action(body.get("name", ""), body.get("action", "")))
            return

        m = re.match(r"^/api/container/([^/]+)/(settings|config)$", path)
        if m:
            name, what = m.groups()
            if what == "settings":
                self._json(200, save_container_settings(name, body))
            else:
                self._json(200, save_container_raw(name, str(body.get("config", ""))))
            return

        if path == "/api/config/save":
            cfg = load_config()
            for k in ("timeout", "autostart_delay", "bridge", "lxcpath", "service_enabled"):
                if k in body:
                    cfg[k] = body[k]
            if cfg.get("lxcpath") and cfg.get("bridge"):
                r = step_configure_storage(cfg["lxcpath"])
                if not r["ok"]:
                    self._json(200, r)
                    return
                step_write_lxc_conf(cfg["lxcpath"], cfg["bridge"], int(cfg.get("timeout") or 15))
            save_config(cfg)
            self._json(200, {"ok": True, "config": cfg})
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
    os.chmod(path, 0o666)  # 网关进程需要能连接
    return srv


def main():
    global DATA_DIR, CONFIG_FILE
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--socket", default="", help="统一网关 Unix Socket 路径")
    args = parser.parse_args()
    DATA_DIR = Path(args.data_dir)
    CONFIG_FILE = DATA_DIR / "config.json"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    threading.Thread(target=boot_autostart, daemon=True).start()
    # 指定 --socket 时只走飞牛统一网关（由网关校验登录），不再开放任何 TCP 端口
    if args.socket:
        server = make_unix_server(args.socket)
        print(f"fnos-lxc gateway socket {args.socket}, data={DATA_DIR}", flush=True)
    else:
        server = ThreadingHTTPServer((args.bind, args.port), Handler)
        print(f"fnos-lxc listening on {args.bind}:{args.port}, data={DATA_DIR}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
