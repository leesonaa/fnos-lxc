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
        t = customfs.build_config("n1", "dir:/vol1/lxc/n1/rootfs", "nixos", "26.05", "arm64",
                                  "# c\nlxc.net.0.type = veth\nlxc.net.0.link = br0\nlxc.rootfs.path = x\n",
                                  "/usr/share/lxc/config/common.conf", "/init")
        self.assertIn("lxc.arch = aarch64", t)
        self.assertIn("lxc.net.0.link = br0", t)
        self.assertEqual(t.count("lxc.rootfs.path"), 1)
        self.assertIn("lxc.init.cmd = /init", t)
        self.assertIn("-d nixos -r 26.05", t)

    def test_elf_arch(self):
        import tempfile, os
        d = Path(tempfile.mkdtemp())
        (d / "bin").mkdir()
        (d / "nix" / "store").mkdir(parents=True)
        hdr = b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8) + (2).to_bytes(2, "little") + (0xB7).to_bytes(2, "little")
        (d / "nix/store/sh").write_bytes(hdr + bytes(40))
        os.symlink("/nix/store/sh", d / "bin/sh")  # 绝对链接应按 rootfs 根解析
        self.assertEqual(customfs.elf_arch(d), "aarch64")
        self.assertTrue(customfs.arch_compatible("arm64", "aarch64"))
        self.assertFalse(customfs.arch_compatible("amd64", "aarch64"))
        self.assertTrue(customfs.arch_compatible("amd64", "i686"))
        self.assertFalse(customfs.arch_compatible("arm64", "armv7l"))
        self.assertEqual(customfs.elf_arch(Path(tempfile.mkdtemp())), "")

    def test_guess_distro(self):
        g = customfs.guess_distro
        self.assertEqual(g("https://x/images/nixos/26.05/arm64/default/rootfs.squashfs"), "nixos")
        self.assertEqual(g("", "my-debian12"), "debian")
        self.assertEqual(g("aarch64-rootfs.tar"), "")          # aarch64 不是 arch
        self.assertEqual(g("arch-bootstrap.tar.zst"), "archlinux")
        self.assertEqual(g("immortalwrt-armsr.tar.gz"), "openwrt")
        self.assertEqual(g("minting"), "")
        self.assertEqual(customfs.distro_from_os_release({"ID": "opensuse-leap"}), "opensuse")
        self.assertEqual(customfs.distro_from_os_release({"ID": "weirdos"}), "weirdos")

    def test_read_os_release_nixos_symlinks(self):
        import tempfile, os
        d = Path(tempfile.mkdtemp())
        (d / "etc").mkdir()
        (d / "nix/store/abc-etc/etc").mkdir(parents=True)
        (d / "nix/store/abc-etc/etc/os-release").write_text('ID=nixos\nVERSION_ID="26.05"\nNAME=NixOS\n')
        os.symlink("/nix/store/abc-etc/etc", d / "etc/static")            # 目录符号链接，宿主机上悬空
        os.symlink("/etc/static/os-release", d / "etc/os-release")        # 链接再经过链接
        info = customfs.read_os_release(d)
        self.assertEqual(info.get("ID"), "nixos")
        self.assertEqual(info.get("VERSION_ID"), "26.05")
        # .. 不能越出 rootfs
        os.symlink("../../../../../../etc/passwd", d / "etc/evil")
        self.assertIsNone(customfs.resolve_in_root(d, "etc/evil"))

    def test_config_without_distro(self):
        t = customfs.build_config("n", "dir:/r", "", "", "amd64", "", "/c.conf")
        self.assertNotIn("Parameters passed", t)


if __name__ == "__main__":
    unittest.main()
