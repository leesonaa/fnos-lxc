import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "server"))
import guestnet  # noqa: E402


def make_ct(files: dict) -> Path:
    d = Path(tempfile.mkdtemp())
    root = d / "rootfs"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    cfg = d / "config"
    cfg.write_text(f"lxc.rootfs.path = dir:{root}\n")
    return cfg


def rd(cfg: Path, rel: str) -> str:
    return (cfg.parent / "rootfs" / rel).read_text()


NETPLAN = "network:\n  version: 2\n  ethernets:\n    eth0:\n      dhcp4: true\n    eth1:\n      dhcp4: true\n"
IFACES = "auto lo\niface lo inet loopback\n\nauto eth0\niface eth0 inet dhcp\n\nauto eth1\niface eth1 inet dhcp\n"
NETWORKD = "[Match]\nName=eth0\n\n[Network]\nDHCP=true\n\n[DHCPv4]\nClientIdentifier=mac\n"


class NetplanTests(unittest.TestCase):
    def test_only_eth0_changes(self):
        cfg = make_ct({"etc/netplan/10-lxc.yaml": NETPLAN})
        guestnet.sync_guest_network(cfg, True, "192.168.1.1")
        t = rd(cfg, "etc/netplan/10-lxc.yaml")
        self.assertIn("eth0:\n      # fnos-lxc: dhcp4 was true\n      dhcp4: false", t)
        self.assertIn("eth1:\n      dhcp4: true", t)  # 其它网卡不动

    def test_roundtrip_exact_and_idempotent(self):
        cfg = make_ct({"etc/netplan/10-lxc.yaml": NETPLAN})
        guestnet.sync_guest_network(cfg, True)
        once = rd(cfg, "etc/netplan/10-lxc.yaml")
        guestnet.sync_guest_network(cfg, True)
        self.assertEqual(once, rd(cfg, "etc/netplan/10-lxc.yaml"))
        guestnet.sync_guest_network(cfg, False)
        self.assertEqual(NETPLAN, rd(cfg, "etc/netplan/10-lxc.yaml"))


class IfupdownTests(unittest.TestCase):
    def test_only_eth0_and_restore(self):
        cfg = make_ct({"etc/network/interfaces": IFACES})
        guestnet.sync_guest_network(cfg, True)
        t = rd(cfg, "etc/network/interfaces")
        self.assertIn("iface eth0 inet manual", t)
        self.assertIn("iface eth1 inet dhcp", t)
        guestnet.sync_guest_network(cfg, False)
        self.assertEqual(IFACES, rd(cfg, "etc/network/interfaces"))

    def test_user_edit_survives_resync(self):
        """回归：容器启动时重复同步不能覆盖用户在容器里改的内容。"""
        cfg = make_ct({"etc/network/interfaces": IFACES})
        guestnet.sync_guest_network(cfg, True)
        p = cfg.parent / "rootfs" / "etc/network/interfaces"
        p.write_text(p.read_text() + "# my edit\n")
        guestnet.sync_guest_network(cfg, True)
        self.assertIn("# my edit", p.read_text())
        guestnet.sync_guest_network(cfg, False)
        self.assertIn("# my edit", p.read_text())

    def test_no_backup_files_left(self):
        cfg = make_ct({"etc/network/interfaces": IFACES, "etc/network/interfaces.d/x": "iface eth0 inet dhcp\n"})
        guestnet.sync_guest_network(cfg, True)
        names = [p.name for p in (cfg.parent / "rootfs/etc/network").rglob("*") if p.is_file()]
        self.assertFalse([n for n in names if "orig" in n or "bak" in n])


class NetworkdTests(unittest.TestCase):
    def test_eth0_file_only(self):
        cfg = make_ct({
            "etc/systemd/network/eth0.network": NETWORKD,
            "etc/systemd/network/eth1.network": NETWORKD.replace("eth0", "eth1"),
        })
        guestnet.sync_guest_network(cfg, True, "192.168.1.1")
        e0 = rd(cfg, "etc/systemd/network/eth0.network")
        self.assertIn("DHCP=ipv6", e0)
        self.assertIn("DNS=192.168.1.1", e0)
        self.assertEqual(NETWORKD.replace("eth0", "eth1"), rd(cfg, "etc/systemd/network/eth1.network"))

    def test_restore(self):
        cfg = make_ct({"etc/systemd/network/eth0.network": NETWORKD})
        guestnet.sync_guest_network(cfg, True, "192.168.1.1")
        guestnet.sync_guest_network(cfg, True, "192.168.1.1")
        guestnet.sync_guest_network(cfg, False)
        self.assertEqual(NETWORKD, rd(cfg, "etc/systemd/network/eth0.network"))


class ResolvTests(unittest.TestCase):
    def test_keeps_search_and_options(self):
        orig = "search example.local\noptions timeout:2\n"
        cfg = make_ct({"etc/resolv.conf": orig, "etc/network/interfaces": IFACES})
        guestnet.sync_guest_network(cfg, True, "192.168.1.1")
        t = rd(cfg, "etc/resolv.conf")
        self.assertIn("search example.local", t)
        self.assertIn("nameserver 192.168.1.1", t)
        guestnet.sync_guest_network(cfg, False)
        self.assertEqual(orig, rd(cfg, "etc/resolv.conf"))

    def test_existing_nameserver_untouched(self):
        orig = "nameserver 8.8.8.8\n"
        cfg = make_ct({"etc/resolv.conf": orig, "etc/network/interfaces": IFACES})
        guestnet.sync_guest_network(cfg, True, "192.168.1.1")
        self.assertEqual(orig, rd(cfg, "etc/resolv.conf"))


if __name__ == "__main__":
    unittest.main()
