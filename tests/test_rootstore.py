import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "server"))
import rootstore  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(rootstore.parse_rootfs("lxc.rootfs.path = dir:/a/b\n"), ("dir", "/a/b"))
        self.assertEqual(rootstore.parse_rootfs("lxc.rootfs.path = /a/b\n"), ("dir", "/a/b"))
        self.assertEqual(rootstore.parse_rootfs("lxc.rootfs.path = loop:/a/x.img\n"), ("loop", "/a/x.img"))
        self.assertEqual(rootstore.parse_rootfs("lxc.rootfs.path = overlayfs:/a:/b\n")[0], "other")
        self.assertEqual(rootstore.parse_rootfs("# lxc.rootfs.path = dir:/x\n"), ("", ""))
        # 多条时取最后一条生效的
        self.assertEqual(rootstore.parse_rootfs("lxc.rootfs.path = dir:/a\nlxc.rootfs.path = loop:/b.img\n"), ("loop", "/b.img"))
        self.assertEqual(rootstore.image_path("lxc.rootfs.path = loop:/b.img\n"), Path("/b.img"))
        self.assertIsNone(rootstore.image_path("lxc.rootfs.path = dir:/b\n"))

    def test_sizes(self):
        self.assertEqual(rootstore.valid_size_gb("12")[0], 12)
        for bad in (0, -1, "abc", None, 5000, "nan"):
            self.assertNotEqual(rootstore.valid_size_gb(bad)[1], "", bad)
        self.assertEqual(rootstore.check_size_vs_free(10, 11 << 30), "")
        self.assertNotEqual(rootstore.check_size_vs_free(12, 11 << 30), "")
        # 扩容只算新增部分
        self.assertEqual(rootstore.check_size_vs_free(12, 3 << 30, already=10 << 30), "")


@unittest.skipUnless(os.geteuid() == 0 and shutil.which("mkfs.ext4") and shutil.which("losetup"), "需要 root 与 loop 设备")
class LoopTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.img = self.d / "r.img"
        if rootstore.mkfs_image(self.img, 1, "t"):
            self.skipTest("本环境无法创建 ext4 镜像")
        try:
            with rootstore.mounted_image(self.img) as m:
                (m / "f").write_text("x")
        except rootstore.RootfsError:
            self.skipTest("本环境无法 loop 挂载")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_mount_roundtrip_and_resize(self):
        with rootstore.mounted_image(self.img, rw=False) as m:
            self.assertEqual((m / "f").read_text(), "x")
        self.assertNotIn(str(self.img), open("/proc/mounts").read())
        self.assertNotEqual(rootstore.resize_image(self.img, 1), "")  # 不能缩小/不变
        self.assertEqual(rootstore.resize_image(self.img, 2), "")
        self.assertEqual(self.img.stat().st_size, 2 << 30)
        with rootstore.mounted_image(self.img) as m:
            self.assertEqual((m / "f").read_text(), "x")  # 数据保留

    def test_double_mount_refused(self):
        with rootstore.mounted_image(self.img):
            with self.assertRaises(rootstore.RootfsError):
                with rootstore.mounted_image(self.img):
                    pass


if __name__ == "__main__":
    unittest.main()
