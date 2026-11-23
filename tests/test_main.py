import io
import os
import struct
import sys
import tempfile
import unittest
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

