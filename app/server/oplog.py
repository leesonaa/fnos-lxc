"""操作日志：所有关键动作的成功/失败落盘，便于排查。

格式：每行一条 JSON（JSONL），字段：
  ts, level, action, ok, name, detail, extra...

文件：DATA_DIR/ops.log（默认轮转约 2MB）
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_path: Path | None = None
_MAX_BYTES = 2 * 1024 * 1024
_KEEP_BYTES = 1 * 1024 * 1024


def init(data_dir: Path) -> None:
    global _path
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    _path = data_dir / "ops.log"


def _rotate_if_needed(p: Path) -> None:
    try:
        if not p.is_file() or p.stat().st_size < _MAX_BYTES:
            return
        data = p.read_bytes()
        p.write_bytes(data[-_KEEP_BYTES:])
    except OSError:
        pass


def log(action: str, ok: bool = True, detail: str = "", level: str = "", name: str = "", **extra) -> None:
    """写一条操作日志。ok=False 时默认 level=error。"""
    if not level:
        level = "info" if ok else "error"
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "level": level,
        "action": action,
        "ok": bool(ok),
    }
    if name:
        rec["name"] = name
    if detail:
        rec["detail"] = str(detail)[:800]
    for k, v in extra.items():
        if v is None or k in rec:
            continue
        if isinstance(v, (str, int, float, bool)):
            rec[k] = v if not isinstance(v, str) else v[:400]
        else:
            rec[k] = str(v)[:400]
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    p = _path
    if p is None:
        # 尚未 init 时仍打到 stderr，避免丢信息
        print(f"[oplog] {line}", end="", flush=True)
        return
    with _lock:
        try:
            _rotate_if_needed(p)
            with p.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            print(f"[oplog] write failed: {e} | {line}", end="", flush=True)


def read_tail(max_lines: int = 200) -> list[dict]:
    """读最近 max_lines 条，按时间正序返回。"""
    max_lines = max(1, min(int(max_lines or 200), 1000))
    p = _path
    if p is None or not p.is_file():
        return []
    with _lock:
        try:
            # 大文件只读尾部
            size = p.stat().st_size
            with p.open("rb") as f:
                if size > 256 * 1024:
                    f.seek(max(0, size - 256 * 1024))
                    f.readline()  # 丢弃可能截断的首行
                raw = f.read().decode("utf-8", errors="replace")
        except OSError:
            return []
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    lines = lines[-max_lines:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            out.append({"ts": "", "level": "info", "action": "raw", "ok": True, "detail": ln[:200]})
    return out


def clear() -> None:
    p = _path
    if p is None:
        return
    with _lock:
        try:
            p.write_text("")
        except OSError:
            pass
