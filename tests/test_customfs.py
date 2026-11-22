import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "server"))
import customfs  # noqa: E402


class CustomFsTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(customfs.classify("/a/b/rootfs.tar.xz"), "tar")
        self.assertEqual(customfs.classify("https://x/y/rootfs.squashfs?sig=1"), "squashfs")
        self.assertEqual(customfs.classify("/a/disk.qcow2"), "")
        self.assertEqual(customfs.ext_of("x.TAR.ZST"), ".tar.zst")

    def test_paths(self):
        self.assertEqual(customfs.check_source_path("/vol1/a/r.tar.gz")[1], "")
        for bad in ("rel/r.tar.gz", "/vol1/../etc/r.tar.gz", "/proc/x.tar", "/vol1/a/r.qcow2", "/vol1/a\n/r.tar"):
            self.assertNotEqual(customfs.check_source_path(bad)[1], "", bad)
        self.assertEqual(customfs.check_url("https://h/x/rootfs.tar.xz")[1], "")
        self.assertNotEqual(customfs.check_url("ftp://h/x.tar")[1], "")
        self.assertNotEqual(customfs.check_url("https://h/x.iso")[1], "")

    def test_members(self):
        self.assertTrue(customfs.check_members(["./etc/passwd", "usr/bin/ls"])[0])
        self.assertFalse(customfs.check_members(["/etc/passwd"])[0])
        self.assertFalse(customfs.check_members(["a/../../etc/passwd"])[0])
        self.assertTrue(customfs.check_members(["a/b..c/d"])[0])

    def test_strip(self):
        self.assertEqual(customfs.strip_components(["rootfs/", "rootfs/etc/os-release"]), 1)
        self.assertEqual(customfs.strip_components(["./etc/", "./usr/bin/ls"]), 0)
        self.assertEqual(customfs.strip_components(["etc/passwd", "usr/x"]), 0)
        self.assertEqual(customfs.strip_components(["usr/bin/ls", "usr/lib/x"]), 0)  # 只有 usr 一个顶级但属系统目录

    def test_init_kind(self):
        self.assertEqual(customfs.init_kind(lambda p: p == "sbin/init"), "standard")
        self.assertEqual(customfs.init_kind(lambda p: p == "init"), "init")
        self.assertEqual(customfs.init_kind(lambda p: False), "")

    def test_build_config(self):
        t = customfs.build_config("n1", "/vol1/lxc/n1/rootfs", "nixos", "26.05", "arm64",
                                  "# c\nlxc.net.0.type = veth\nlxc.net.0.link = br0\nlxc.rootfs.path = x\n",
                                  "/usr/share/lxc/config/common.conf", "/init")
        self.assertIn("lxc.arch = aarch64", t)
        self.assertIn("lxc.net.0.link = br0", t)
        self.assertEqual(t.count("lxc.rootfs.path"), 1)
        self.assertIn("lxc.init.cmd = /init", t)
        self.assertIn("-d nixos -r 26.05", t)


if __name__ == "__main__":
    unittest.main()
