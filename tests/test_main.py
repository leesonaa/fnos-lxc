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
