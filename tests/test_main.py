import io
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

_TMP = tempfile.mkdtemp()
os.environ["TRIM_PKGVAR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "server"))
import main  # noqa: E402


def frame(payload: bytes, op=1, fin=True, masked=True, rsv=0) -> bytes:
    b1 = (0x80 if fin else 0) | rsv | op
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", b1, (0x80 if masked else 0) | n)
    else:
        head = struct.pack("!BBH", b1, (0x80 if masked else 0) | 126, n)
    if not masked:
        return head + payload
    mask = b"\x01\x02\x03\x04"
    return head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload))


class WebSocketTests(unittest.TestCase):
    def recv(self, raw: bytes):
        return main._ws_recv(io.BufferedReader(io.BytesIO(raw)))

    def test_masked_text(self):
        self.assertEqual(self.recv(frame(b"hello")), (1, b"hello"))

    def test_large_masked(self):
        data = bytes(range(256)) * 20
        self.assertEqual(self.recv(frame(data, op=2)), (2, data))

    def test_unmasked_rejected(self):
        self.assertEqual(self.recv(frame(b"x", masked=False))[0], 8)

    def test_rsv_rejected(self):
        self.assertEqual(self.recv(frame(b"x", rsv=0x40))[0], 8)

    def test_fragments_reassembled(self):
        raw = frame(b"ab", op=1, fin=False) + frame(b"cd", op=0, fin=True)
        self.assertEqual(self.recv(raw), (1, b"abcd"))

    def test_fragment_total_capped(self):
        chunk = b"x" * 60000
        raw = b"".join(frame(chunk, op=2 if i == 0 else 0, fin=False) for i in range(30))
        self.assertEqual(self.recv(raw)[0], 8)

    def test_truncated(self):
        self.assertEqual(self.recv(frame(b"hello")[:-2])[0], 8)

    def test_fragmented_control_rejected(self):
        self.assertEqual(self.recv(frame(b"", op=9, fin=False))[0], 8)


class PathTests(unittest.TestCase):
    def test_system_dirs_rejected(self):
        for p in ("/etc/lxc", "/usr/local/x", "/proc/1", "/boot/a"):
            self.assertTrue(main.check_lxcpath(p)[1], p)

    def test_control_chars_rejected(self):
        self.assertTrue(main.check_lxcpath("/vol1/a\nlxc.hook=x")[1])

    def test_vol_ok_and_warning(self):
        self.assertEqual(main.check_lxcpath("/vol1/1000/lxc"), ("/vol1/1000/lxc", "", ""))
        p, err, warn = main.check_lxcpath("/mnt/disk/lxc")
        self.assertEqual(err, "")
        self.assertTrue(warn)

    def test_clamp(self):
        self.assertEqual(main._clamp_int("abc", 15, 1, 600), 15)
        self.assertEqual(main._clamp_int("1e309", 15, 1, 600), 15)
        self.assertEqual(main._clamp_int(99999, 15, 1, 600), 600)


class ContainerFileTests(unittest.TestCase):
    def setUp(self):
        self.lxc = Path(tempfile.mkdtemp())
        main.save_config({**main.DEFAULT_CONFIG, "initialized": True, "lxcpath": str(self.lxc)})
        d = self.lxc / "c1"
        (d / "rootfs").mkdir(parents=True)
        (d / "config").write_text(f"lxc.rootfs.path = dir:{d}/rootfs\nlxc.net.0.link = br0\n")

    def test_raw_comment_only_rootfs_rejected(self):
        r = main.save_container_raw("c1", "# lxc.rootfs.path = dir:/x\nlxc.arch = linux64\n")
        self.assertFalse(r["ok"])

    def test_raw_valid_saved_atomically(self):
        r = main.save_container_raw("c1", "lxc.rootfs.path = dir:/x\nlxc.arch = linux64\n")
        self.assertTrue(r["ok"], r)
        self.assertFalse(list((self.lxc / "c1").glob("*.fnos-tmp")))

    def test_autostart_ignores_missing_mount(self):
        p = self.lxc / "c1" / "config"
        p.write_text(p.read_text() + "lxc.mount.entry = /vol9/gone mnt/x none bind,create=dir 0 0\n")
        r = main.save_container_settings("c1", {"bridge": "br0", "mounts": [{"host": "/vol9/gone", "container": "/mnt/x"}]})
        self.assertFalse(r["ok"])  # 表单保存仍然校验
        r = main.set_autostart("c1", True)
        self.assertTrue(r["ok"], r)  # 但切换自启不应被缺失的挂载路径拦住

    def test_find_rootfs_fixed_depth(self):
        d = self.lxc / "cache" / "download" / "alpine" / "3.21" / "amd64" / "default" / "20260101_00:00"
        d.mkdir(parents=True)
        (d / "rootfs.tar.xz").write_bytes(b"x")
        self.assertEqual(main._find_rootfs_download("alpine", "3.21", "amd64"), str(d / "rootfs.tar.xz"))


if __name__ == "__main__":
    unittest.main()


class SnapshotTests(unittest.TestCase):
    def test_parse(self):
        out = ("snap1 (/vol1/lxc/web/snaps) 2026:11:22 10:05:00\n"
               "snap0 (/vol1/lxc/web/snaps) 2026:11:21 09:00:01\n"
               "garbage line\n")
        r = main.parse_snapshots(out)
        self.assertEqual([x["name"] for x in r], ["snap0", "snap1"])
        self.assertEqual(r[0]["time"], "2026-11-21 09:00:01")
        self.assertEqual(r[0]["path"], "/vol1/lxc/web/snaps")
        self.assertEqual(main.parse_snapshots("No snapshots"), [])

    def test_validation(self):
        base = Path(tempfile.mkdtemp())
        d = base / "t1"
        d.mkdir()
        (d / "config").write_text("lxc.rootfs.path = dir:/x\n")
        main.save_config({**main.load_config(), "lxcpath": str(base)})
        self.assertFalse(main.snapshot_op("t1", {"op": "bogus"})["ok"])
        self.assertFalse(main.snapshot_op("t1", {"op": "delete", "snap": "snap0"})["ok"])  # 缺 confirm
        self.assertFalse(main.snapshot_op("t1", {"op": "delete", "snap": "../x", "confirm": True})["ok"])
        self.assertFalse(main.snapshot_op("t1", {"op": "restore", "snap": "snap0", "confirm": True, "as_new": "a b"})["ok"])
        self.assertFalse(main.snapshot_op("nope", {"op": "create"})["ok"])


class IconTests(unittest.TestCase):
    def setUp(self):
        import base64, tempfile
        self.b64 = base64.b64encode
        self.tmp = Path(tempfile.mkdtemp())
        self._data_dir = main.DATA_DIR
        main.DATA_DIR = self.tmp / "data"
        base = self.tmp / "lxc"
        (base / "web").mkdir(parents=True)
        (base / "web" / "config").write_text("lxc.rootfs.path = dir:/x\n")
        self._cfg = main.load_config()
        main.save_config({**self._cfg, "lxcpath": str(base)})

    def tearDown(self):
        main.save_config(self._cfg)
        main.DATA_DIR = self._data_dir

    PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32

    def test_upload_and_read(self):
        r = main.save_container_icon("web", {"ext": "png", "data": self.b64(self.PNG).decode()})
        self.assertTrue(r["ok"], r)
        fn = r["icon"][len("custom:"):]
        data, ctype = main.read_icon(fn)
        self.assertEqual((data, ctype), (self.PNG, "image/png"))
        # 换新图后旧文件被清理
        r2 = main.save_container_icon("web", {"ext": "png", "data": self.b64(self.PNG).decode()})
        main.cleanup_icons("web", r2["icon"])
        self.assertIsNone(main.read_icon(fn))
        self.assertIsNotNone(main.read_icon(r2["icon"][7:]))
        main.cleanup_icons("web", "")
        self.assertIsNone(main.read_icon(r2["icon"][7:]))

    def test_rejects(self):
        ok = lambda **kw: main.save_container_icon("web", kw)["ok"]  # noqa: E731
        self.assertFalse(ok(ext="png", data=self.b64(b"not a png").decode()))            # 魔数不符
        self.assertFalse(ok(ext="exe", data=self.b64(self.PNG).decode()))                # 扩展名
        self.assertFalse(ok(ext="png", data="!!!"))                                      # 非 base64
        self.assertFalse(ok(ext="png", data=self.b64(self.PNG + b"0" * 400000).decode()))  # 太大
        self.assertFalse(ok(ext="svg", data=self.b64(b"<html>").decode()))               # 不是 svg
        self.assertTrue(ok(ext="svg", data=self.b64(b'<svg xmlns="http://www.w3.org/2000/svg"/>').decode()))
        self.assertFalse(main.save_container_icon("nope", {"ext": "png", "data": self.b64(self.PNG).decode()})["ok"])
        for bad in ("../x.png", "a/b.png", "x.exe", ".." + "/" * 2 + "etc.png"):
            self.assertIsNone(main.read_icon(bad))
        main.cleanup_icons("web", "")

    def test_url_fetch(self):
        import http.server, threading
        PNG = self.PNG
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = {"/a": PNG, "/t.txt": b"hello", "/big": PNG + b"0" * 400000}.get(self.path)
                if body is None:
                    self.send_response(404); self.end_headers(); return
                self.send_response(200); self.end_headers(); self.wfile.write(body)
            def log_message(self, *a): pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            # 回环地址默认被拒绝
            self.assertFalse(main.save_container_icon("web", {"url": base + "/a"})["ok"])
            orig = main._icon_host_ok
            main._icon_host_ok = lambda u: ""
            try:
                r = main.save_container_icon("web", {"url": base + "/a"})
                self.assertTrue(r["ok"], r)
                self.assertTrue(r["icon"].endswith(".png"))
                self.assertFalse(main.save_container_icon("web", {"url": base + "/t.txt"})["ok"])   # 不是图片
                self.assertFalse(main.save_container_icon("web", {"url": base + "/big"})["ok"])     # 太大
                self.assertFalse(main.save_container_icon("web", {"url": base + "/none"})["ok"])    # 404
            finally:
                main._icon_host_ok = orig
            self.assertFalse(main.save_container_icon("web", {"url": "ftp://x/a.png"})["ok"])
        finally:
            srv.shutdown()
            main.cleanup_icons("web", "")


class AptParseTests(unittest.TestCase):
    def test_parse(self):
        p = main.parse_apt_line
        self.assertEqual(p("pmstatus:lxc:42.5:Installing lxc (amd64)\n")["pct"], 42.5)
        self.assertEqual(p("pmstatus:lxc:42.5:Installing lxc (amd64)")["pkg"], "lxc")
        self.assertEqual(p("dlstatus:1:10.0:Downloading file")["kind"], "dl")
        self.assertEqual(p("Setting up lxc-templates (3.0.4-1) ...")["pkg"], "lxc-templates")
        self.assertEqual(p("Setting up libc6:amd64 (2.36-9) ...")["pkg"], "libc6")
        self.assertEqual(p("Unpacking lxcfs (5.0.3) ...")["kind"], "unpack")
        self.assertEqual(p("random text")["kind"], "log")
        self.assertEqual(p("pmstatus:x:abc:y")["kind"], "log")      # 坏数据不抛异常
        self.assertEqual(p("pmstatus:x:150:y")["pct"], 100.0)        # 夹紧到 0-100


class HostTermTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(main.term_name("_host"), "_host")
        self.assertEqual(main.term_name(" _host "), "_host")
        self.assertEqual(main.term_name("test1"), "test1")
        self.assertEqual(main.term_name("../x"), "")
        self.assertEqual(main.term_name("_host/x"), "")
        self.assertEqual(main.clean_name("_host"), "")     # 容器名不可能与保留名冲突

    def test_enabled_and_idle_follow_config(self):
        with mock.patch.object(main, "load_config", return_value={"host_terminal": False}):
            self.assertFalse(main.host_term_enabled())
        with mock.patch.object(main, "load_config", return_value={"host_terminal": True, "host_term_idle": 1800}):
            self.assertTrue(main.host_term_enabled())
            self.assertEqual(main.host_term_idle(), 1800)
        with mock.patch.object(main, "load_config", return_value={"host_term_idle": 5}):
            self.assertEqual(main.host_term_idle(), 3600)   # 非法值回落到默认

    def test_argv_uses_own_rc_and_clean_env(self):
        argv = main._host_term_argv()
        self.assertEqual(argv[:3], ["/usr/bin/env", "-i", main._ENV[0]])
        self.assertIn("--noprofile", argv)
        self.assertTrue(argv[argv.index("--rcfile") + 1].endswith("hostterm.rc"))
        self.assertIn("COLORTERM=truecolor", argv)

    def test_real_shell_has_colors_and_home(self):
        import time
        c = TermSessionTests.FakeClient()
        with mock.patch.object(main, "load_config", return_value={"host_terminal": True, "host_term_idle": 1800}):
            sess = main.TermSession(main.HOST_TERM, main._host_term_argv(), 100, 30, slot=False)
        self.assertEqual(sess.ttl, 1800)
        try:
            sess.attach(c)
            w = TermSessionTests.wait
            self.assertTrue(w(self, lambda: b"\x1b[1;32m" in c.data(), 8))      # 绿色提示符
            sess.write(b"echo HOME=$HOME; ls --color=always /etc | head -3; alias ls; echo done\n")
            self.assertTrue(w(self, lambda: b"\r\ndone\r\n" in c.data(), 8))
            d = c.data()
            self.assertIn(b"HOME=/root", d)
            self.assertIn(b"\x1b[", d.split(b"HOME=/root", 1)[1])               # ls 输出带颜色转义
            self.assertIn(b"alias ls=", d)
        finally:
            sess.finish()


class TermSessionTests(unittest.TestCase):
    class FakeClient:
        def __init__(self):
            self.frames, self.kicked = [], False
        def send(self, op, payload):
            self.frames.append((op, bytes(payload)))
        def kick(self, graceful=False):
            self.kicked = True
        def data(self):
            return b"".join(p for o, p in self.frames if o == 2)

    def wait(self, pred, t=5):
        import time
        end = time.time() + t
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.05)
        return False

    def test_survives_detach_and_replays(self):
        sess = main.TermSession("t", ["sh", "-c", "echo hello; sleep 0.4; echo world; sleep 30"], 80, 24, slot=False)
        try:
            c1 = self.FakeClient()
            self.assertTrue(sess.attach(c1))
            self.assertTrue(self.wait(lambda: b"hello" in c1.data()))
            sess.detach(c1)                                  # 页面断线：任务继续
            self.assertTrue(sess.alive)
            self.assertTrue(self.wait(lambda: b"world" in bytes(sess.buf)))   # 断线期间的输出被缓存
            c2 = self.FakeClient()
            self.assertTrue(sess.attach(c2))                 # 重连：回放历史
            d = c2.data()
            self.assertIn(b"hello", d)
            self.assertIn(b"world", d)
            self.assertEqual(c2.frames[0][0], 1)             # 先收到 hello(sid)
            self.assertIn(sess.sid.encode(), c2.frames[0][1])
        finally:
            sess.finish()
        self.assertNotIn(sess.sid, main._TERMS)
        self.assertFalse(sess.alive)

    def test_new_attach_kicks_old_and_exit_notifies(self):
        sess = main.TermSession("t", ["sh", "-c", "sleep 0.5"], 80, 24, slot=False)
        c1, c2 = self.FakeClient(), self.FakeClient()
        sess.attach(c1)
        sess.attach(c2)
        self.assertTrue(c1.kicked)                           # 同一会话只保留最新连接
        self.assertTrue(self.wait(lambda: not sess.alive))   # shell 退出 → 会话结束
        self.assertTrue(any(o == 1 and b"exit" in p for o, p in c2.frames))
        self.assertFalse(main.close_term_session(sess.sid))

    def test_close_by_sid(self):
        sess = main.TermSession("t", ["sh", "-c", "sleep 30"], 80, 24, slot=False)
        self.assertFalse(main.close_term_session(sess.sid, "other"))   # 名字不符不能关
        self.assertTrue(main.close_term_session(sess.sid, "t"))
        self.assertFalse(sess.alive)


class CancelTests(unittest.TestCase):
    def test_run_job_cancel_kills_group(self):
        import threading, time
        job = {"id": "t1", "name": "x", "status": "running", "stage": "s"}
        with main._JOBS_LOCK:
            main._JOBS["t1"] = job
        res = {}
        t = threading.Thread(target=lambda: res.update(main.run_job(job, ["sh", "-c", "sleep 60 & sleep 60"], 120)))
        t.start()
        for _ in range(50):
            if job.get("pid"):
                break
            time.sleep(0.05)
        t0 = time.time()
        self.assertTrue(main.cancel_job("t1")["ok"])
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertLess(time.time() - t0, 5)                 # 立刻返回，不是等 60 秒
        self.assertEqual(res.get("stderr"), "已取消")
        self.assertFalse(main.run_job(job, ["true"])["ok"])  # 已取消的任务不再启动新进程
        main._JOBS.pop("t1", None)

    def test_cancel_finished_job_refused(self):
        main._JOBS["t2"] = {"id": "t2", "name": "x", "status": "done"}
        self.assertFalse(main.cancel_job("t2")["ok"])
        self.assertFalse(main.cancel_job("nope")["ok"])
        main._JOBS.pop("t2", None)

    def test_remove_container_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            c = Path(d) / "c1"
            (c / "rootfs" / "a").mkdir(parents=True)
            (c / "rootfs" / "a" / "f").write_text("x")
            main._remove_container_dir(c)
            self.assertFalse(c.exists())
            main._remove_container_dir(c)                    # 不存在也不报错


class LxcPathTests(unittest.TestCase):
    def test_volume_missing(self):
        self.assertIn("/vol9", main.check_volume_user_dir("/vol9/1000/lxc"))

    def test_user_dir_missing_message(self):
        orig = os.path.isdir
        os.path.isdir = lambda x, _o=orig: True if x == "/vol2" else (False if x == "/vol2/1000" else _o(x))
        try:
            err = main.check_volume_user_dir("/vol2/1000/lxc")
        finally:
            os.path.isdir = orig
        self.assertIn("文件管理", err)

    def test_other_paths_unaffected(self):
        self.assertEqual(main.check_volume_user_dir("/mnt/data/lxc"), "")
        self.assertEqual(main.check_volume_user_dir("/vol1/lxc"), "")     # 卷根下的目录不受影响
