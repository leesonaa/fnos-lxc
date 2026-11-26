"""检查更新 / 下载 fpk / 调用飞牛命令升级。

设计要点
- 版本信息来自 GitHub Releases latest；只读、尽量少请求：定时任务有最小间隔，手动检查有冷却，
  并带 ETag（304 不消耗额度）。
- 下载地址只接受本仓库 releases/download 下的 .fpk，可经 gh-proxy 一类加速前缀；
  GitHub 给出 digest 时校验 sha256，始终校验大小。
- 升级通过 `appcenter-cli install-fpk <文件>`（同版本即覆盖安装）。安装会重启本服务，
  所以放进 systemd-run / setsid 独立进程里执行。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = "leesonaa/fnos-lxc"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_URL_RE = re.compile(r"^https://github\.com/" + re.escape(REPO) + r"/releases/download/[^/\s]+/[^/\s]+\.fpk$")
BUILTIN_PROXIES = {"gh-proxy": "https://gh-proxy.com/", "ghfast": "https://ghfast.top/"}
MIN_AUTO_SECONDS = 3600        # 自动检查最短间隔 1 小时
MANUAL_COOLDOWN = 30           # 手动检查冷却，防止连点
UA = "fnos-lxc-updater"

DEFAULT_SETTINGS = {"mode": "interval", "hours": 24, "time": "04:00", "proxy": "auto", "custom_proxy": ""}
_MODES = ("off", "interval", "daily")
_PROXY_KEYS = ("auto", "direct", "gh-proxy", "ghfast", "custom")


# ---------------------------------------------------------------- 版本 ---

def current_version(app_dir: Path | None = None) -> str:
    """优先读打包时写入的 VERSION，其次读 manifest，最后给 0.0.0。"""
    here = Path(__file__).resolve().parent
    cands = [here / "VERSION", Path("/var/apps/fnos-lxc/manifest"), here.parent / "manifest", here.parent.parent / "manifest"]
    if app_dir:
        cands.insert(0, Path(app_dir) / "VERSION")
    for p in cands:
        try:
            t = p.read_text().strip()
        except OSError:
            continue
        if p.name == "VERSION":
            if t:
                return t.splitlines()[0].strip().lstrip("v")
        else:
            m = re.search(r"^version\s*=\s*(\S+)", t, re.M)
            if m:
                return m.group(1).lstrip("v")
    return "0.0.0"


def parse_version(v: str):
    m = re.match(r"^v?(\d+(?:\.\d+){0,3})", str(v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


# ---------------------------------------------------------------- 设置 ---

def validate_settings(raw: dict) -> tuple[dict, str]:
    s = dict(DEFAULT_SETTINGS)
    mode = str(raw.get("mode", s["mode"]))
    if mode not in _MODES:
        return s, "检查方式无效"
    s["mode"] = mode
    try:
        hours = int(raw.get("hours", s["hours"]))
    except (TypeError, ValueError):
        return s, "间隔小时数必须是整数"
    if not 1 <= hours <= 24 * 30:
        return s, "间隔必须在 1–720 小时之间（为避免频繁访问 GitHub，最短 1 小时）"
    s["hours"] = hours
    t = str(raw.get("time", s["time"]))
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", t):
        return s, "每天检查的时间格式应为 HH:MM"
    s["time"] = t
    proxy = str(raw.get("proxy", s["proxy"]))
    if proxy not in _PROXY_KEYS:
        return s, "加速源取值无效"
    s["proxy"] = proxy
    custom = str(raw.get("custom_proxy", "")).strip()
    if proxy == "custom":
        if not re.match(r"^https?://[^\s]+/$", custom):
            return s, "自定义加速源必须是 http(s):// 开头、以 / 结尾的前缀，例如 https://gh-proxy.com/"
        s["custom_proxy"] = custom
    else:
        s["custom_proxy"] = custom if re.match(r"^https?://[^\s]+/$", custom) else ""
    return s, ""


def proxy_chain(s: dict) -> list:
    """按顺序尝试的前缀；"" 表示直连。"""
    p = s.get("proxy", "auto")
    if p == "direct":
        return [""]
    if p == "auto":
        return ["", BUILTIN_PROXIES["gh-proxy"], BUILTIN_PROXIES["ghfast"]]
    if p == "custom" and s.get("custom_proxy"):
        return [s["custom_proxy"], ""]
    return [BUILTIN_PROXIES.get(p, ""), ""]


def due(s: dict, last_check: float, now: float | None = None) -> bool:
    """自动检查是否到点。last_check 为 0 表示从未检查。"""
    now = time.time() if now is None else now
    if s.get("mode") == "off":
        return False
    if s["mode"] == "interval":
        return now - last_check >= max(MIN_AUTO_SECONDS, int(s["hours"]) * 3600)
    hh, mm = (int(x) for x in s["time"].split(":"))
    ln = _dt.datetime.fromtimestamp(now)
    sched = ln.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()
    return now >= sched and last_check < sched and now - last_check >= MIN_AUTO_SECONDS


# ------------------------------------------------------------ GitHub 请求 ---

def parse_release(j: dict) -> dict | None:
    tag = str(j.get("tag_name") or "")
    if not parse_version(tag):
        return None
    assets = []
    for a in j.get("assets") or []:
        url = str(a.get("browser_download_url") or "")
        if ASSET_URL_RE.match(url):
            dg = str(a.get("digest") or "")
            assets.append({"name": str(a.get("name") or ""), "size": int(a.get("size") or 0), "url": url,
                           "digest": dg if re.match(r"^sha256:[0-9a-f]{64}$", dg) else ""})
    return {"tag": tag, "version": tag.lstrip("v"), "name": str(j.get("name") or tag),
            "notes": str(j.get("body") or "")[:3000], "html_url": str(j.get("html_url") or ""),
            "published_at": str(j.get("published_at") or ""), "assets": assets}


def choose_asset(assets: list) -> dict | None:
    signed = [a for a in assets if "unsigned" not in a["name"].lower()]
    return (signed or assets or [None])[0]


def _get(url: str, headers: dict, timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_latest(s: dict, etag: str = "") -> tuple[dict | None, str, str]:
    """返回 (release, etag, 错误)。release 为 None 且无错误表示 304 未变化。"""
    err = ""
    for prefix in proxy_chain(s):
        hdr = {"Accept": "application/vnd.github+json"}
        if etag and not prefix:
            hdr["If-None-Match"] = etag
        try:
            with _get(prefix + API_LATEST, hdr) as r:
                rel = parse_release(json.loads(r.read(2_000_000).decode("utf-8", "replace")))
                if rel:
                    return rel, (r.headers.get("ETag") or "") if not prefix else "", ""
                err = "响应里没有有效的版本号"
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return None, etag, ""
            err = f"HTTP {e.code}" + ("（GitHub API 额度用尽，稍后再试）" if e.code in (403, 429) else "")
        except Exception as e:  # noqa: BLE001
            err = str(e)[:120]
    return None, etag, "检查失败：" + err


# ------------------------------------------------------------- 状态文件 ---

def load_state(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, st: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False))
        os.replace(tmp, path)
    except OSError:
        pass


def check(s: dict, st: dict, force: bool, now: float | None = None) -> dict:
    """执行一次检查并返回新的状态（不改原对象）。force=手动：有冷却；自动：调用方已用 due() 判过。"""
    now = time.time() if now is None else now
    st = dict(st)
    last = float(st.get("last_check") or 0)
    if force and now - last < MANUAL_COOLDOWN and st.get("latest"):
        st["cooldown"] = True
        return st
    st.pop("cooldown", None)
    rel, etag, err = fetch_latest(s, st.get("etag", ""))
    st["last_check"] = now
    if err:
        st["last_error"] = err
        return st
    st["last_error"] = ""
    st["etag"] = etag
    if rel:
        st["latest"] = rel
    return st


# --------------------------------------------------------------- 下载 ---

def download(asset: dict, s: dict, dest_dir: Path, progress=None, cancelled=None) -> tuple[Path | None, str]:
    """下载到 dest_dir，校验大小与 sha256（若有）。依次尝试加速前缀。"""
    if not ASSET_URL_RE.match(asset.get("url", "")):
        return None, "下载地址不合法"
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", asset["name"]) or "update.fpk"
    final, part = dest_dir / name, dest_dir / (name + ".part")
    last = ""
    for prefix in proxy_chain(s):
        label = prefix or "GitHub 直连"
        try:
            h = hashlib.sha256()
            got = 0
            with _get(prefix + asset["url"], {}, timeout=30) as r, open(part, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0) or asset.get("size", 0)
                while True:
                    if cancelled and cancelled():
                        part.unlink(missing_ok=True)
                        return None, "已取消"
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if progress:
                        progress(got, total, label)
            if asset.get("size") and got != asset["size"]:
                raise OSError(f"大小不符（{got}/{asset['size']}）")
            if asset.get("digest") and "sha256:" + h.hexdigest() != asset["digest"]:
                raise OSError("sha256 校验失败，文件可能被篡改或损坏")
            if got < 1024:
                raise OSError("文件过小")
            os.replace(part, final)
            return final, ""
        except Exception as e:  # noqa: BLE001
            part.unlink(missing_ok=True)
            last = f"{label}：{str(e)[:100]}"
    return None, "下载失败（已尝试全部线路）：" + last


# --------------------------------------------------------------- 安装 ---

def installer_cmd() -> str:
    return shutil.which("appcenter-cli") or ""


def launch_install(fpk: Path, log: Path) -> str:
    """在独立进程里执行 appcenter-cli install-fpk（安装会重启本服务，不能依附在当前进程上）。返回错误，空串表示已启动。"""
    cli = installer_cmd()
    if not cli:
        return "找不到 appcenter-cli，无法自动安装。请把下载好的安装包手动安装：" + str(fpk)
    log.parent.mkdir(parents=True, exist_ok=True)
    script = (f"sleep 2; echo \"== $(date) install-fpk {shlex.quote(str(fpk))}\" >> {shlex.quote(str(log))}; "
              f"{shlex.quote(cli)} install-fpk {shlex.quote(str(fpk))} >> {shlex.quote(str(log))} 2>&1; "
              f"echo \"EXIT=$?\" >> {shlex.quote(str(log))}")
    if shutil.which("systemd-run") and os.path.isdir("/run/systemd/system"):
        argv = ["systemd-run", "--collect", f"--unit=fnos-lxc-upgrade-{int(time.time())}", "sh", "-c", script]
    else:
        argv = ["setsid", "sh", "-c", script] if shutil.which("setsid") else ["sh", "-c", script]
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except OSError as e:
        return f"无法启动安装：{e}"
    return ""


def log_tail(log: Path, n: int = 12) -> str:
    try:
        return "\n".join(log.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def jitter(seed: str, span: int = 600) -> int:
    """给定时检查加一个固定的随机偏移，避免所有用户在同一秒访问 GitHub。"""
    return random.Random(seed).randint(0, span)
