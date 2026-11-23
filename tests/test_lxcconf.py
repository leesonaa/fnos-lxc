import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "server"))
import lxcconf  # noqa: E402

BASE = """# Template used to create this container: /usr/share/lxc/templates/lxc-download
# Parameters passed to the template: -d alpine -r 3.21 -a amd64
lxc.include = /usr/share/lxc/config/common.conf
lxc.rootfs.path = dir:/vol1/lxc/t/rootfs
lxc.uts.name = t
lxc.net.0.type = veth
lxc.net.0.link = br0
lxc.net.0.hwaddr = 00:16:3e:aa:bb:cc
"""


def form(**kw):
    s = {"bridge": "br0"}
    s.update(kw)
    return s


class ValidateTests(unittest.TestCase):
    def test_rejects_non_finite_and_huge(self):
        for k, v in (("cpu_cores", "1e309"), ("cpu_cores", "nan"), ("cpu_cores", "inf"),
                     ("memory_mb", "1e309"), ("memory_mb", "nan"), ("cpu_cores", 10**6), ("memory_mb", 10**12)):
            s, err = lxcconf.validate(form(**{k: v}))
            self.assertIsNone(s, (k, v))
            self.assertTrue(err)

    def test_accepts_normal(self):
        s, err = lxcconf.validate(form(cpu_cores=1.5, memory_mb=2048))
        self.assertEqual(err, "")
        self.assertIn("lxc.cgroup2.cpu.max = 150000 100000", lxcconf.build_block(s))

    def test_bridge_validation(self):
        self.assertTrue(lxcconf.valid_bridge("enP3p49s0-ovs"))
        self.assertFalse(lxcconf.valid_bridge("br0\nlxc.hook.pre-start=/x"))
        self.assertFalse(lxcconf.valid_bridge(""))


class RoundTripTests(unittest.TestCase):
    def test_idempotent(self):
        s, _ = lxcconf.validate(lxcconf.parse_settings(BASE))
        a = lxcconf.apply_settings(BASE, s)
        s2, _ = lxcconf.validate(lxcconf.parse_settings(a))
        self.assertEqual(a, lxcconf.apply_settings(a, s2))

    def test_mount_options_preserved(self):
        text = BASE + "lxc.mount.entry = /vol1/media mnt/media none bind,optional,nosuid,create=dir 0 0\n"
        s = lxcconf.parse_settings(text)
        self.assertEqual(s["mounts"][0]["options"], ["optional", "nosuid"])
        v, err = lxcconf.validate(s)
        self.assertEqual(err, "")
        out = lxcconf.apply_settings(text, v)
        self.assertIn("bind,optional,nosuid,create=dir", out)
        self.assertEqual(out.count("/vol1/media"), 1)  # 区块外的旧行被接管，没有重复

    def test_manual_devices_allow_a_kept(self):
        text = BASE + "lxc.cgroup2.devices.allow = a\n"
        v, _ = lxcconf.validate(lxcconf.parse_settings(text))
        self.assertIn("lxc.cgroup2.devices.allow = a", lxcconf.apply_settings(text, v).split(lxcconf.BEGIN)[0])

    def test_distro(self):
        self.assertEqual(lxcconf.parse_distro(BASE), ("alpine", "3.21"))

    def test_toggle_off_removes_block_lines(self):
        s = lxcconf.parse_settings(BASE)
        s.update(tun=True, docker=True)
        on = lxcconf.apply_settings(BASE, lxcconf.validate(s)[0])
        self.assertIn("/dev/net/tun", on)
        s2 = lxcconf.parse_settings(on)
        s2.update(tun=False, docker=False)
        off = lxcconf.apply_settings(on, lxcconf.validate(s2)[0])
        self.assertNotIn("/dev/net/tun", off)
        self.assertNotIn("unconfined", off)


if __name__ == "__main__":
    unittest.main()


class IconSettingTests(unittest.TestCase):
    def _roundtrip(self, icon):
        s = lxcconf.parse_settings("lxc.rootfs.path = dir:/x\n")
        raw = dict(s, icon=icon, bridge="br0")
        v, err = lxcconf.validate(raw)
        return v, err

    def test_valid_and_roundtrip(self):
        for ic in ("", "builtin:debian", "custom:web-1a2b3c4d.png", "custom:a.b-c_d.svg"):
            v, err = self._roundtrip(ic)
            self.assertEqual(err, "", ic)
            text = lxcconf.apply_settings("lxc.rootfs.path = dir:/x\n", v)
            self.assertEqual(lxcconf.parse_settings(text)["icon"], ic)

    def test_invalid(self):
        for ic in ("custom:../x.png", "custom:x.exe", "builtin:a b", "http://x/y.png", "custom:/etc/passwd.png"):
            self.assertNotEqual(self._roundtrip(ic)[1], "", ic)


class Ipv6OffTests(unittest.TestCase):
    def test_off_roundtrip(self):
        base = "lxc.rootfs.path = dir:/x\n"
        s = lxcconf.parse_settings(base)
        v, err = lxcconf.validate(dict(s, ipv6_mode="off", ipv6_address="fd00::1/64", bridge="br0"))
        self.assertEqual(err, "")
        text = lxcconf.apply_settings(base, v)
        self.assertIn("lxc.sysctl.net.ipv6.conf.all.disable_ipv6 = 1", text)
        p = lxcconf.parse_settings(text)
        self.assertEqual((p["ipv6_mode"], p["ipv6_address"]), ("off", ""))
        # 改回自动：sysctl 行被清除
        v2, _ = lxcconf.validate(dict(p, ipv6_mode="auto", bridge="br0"))
        text2 = lxcconf.apply_settings(text, v2)
        self.assertNotIn("disable_ipv6", text2)
        self.assertEqual(lxcconf.parse_settings(text2)["ipv6_mode"], "auto")

