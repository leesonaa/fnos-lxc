import datetime
import hashlib
import http.server
import json
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "server"))
import updater  # noqa: E402


class VersionTests(unittest.TestCase):
    def test_compare(self):
        self.assertTrue(updater.is_newer("v1.0.28", "1.0.27"))
        self.assertTrue(updater.is_newer("1.1", "1.0.9"))
        self.assertTrue(updater.is_newer("2.0.0", "1.9.9.9"))
        self.assertFalse(updater.is_newer("1.0.27", "1.0.27"))
        self.assertFalse(updater.is_newer("1.0.26", "v1.0.27"))
        self.assertFalse(updater.is_newer("1.0", "1.0.0"))
        self.assertFalse(updater.is_newer("abc", "1.0.0"))
        self.assertFalse(updater.is_newer("1.0.1", ""))

    def test_current_version_reads_manifest(self):
        v = updater.current_version()
        self.assertRegex(v, r"^\d+\.\d+")


class SettingsTests(unittest.TestCase):
    def test_validate(self):
        s, err = updater.validate_settings({})
        self.assertEqual((err, s["mode"], s["hours"]), ("", "interval", 24))
        self.assertTrue(updater.validate_settings({"hours": 0})[1])        # 最短 1 小时
        self.assertTrue(updater.validate_settings({"hours": 1000})[1])
        self.assertTrue(updater.validate_settings({"hours": "x"})[1])
        self.assertTrue(updater.validate_settings({"mode": "x"})[1])
        self.assertTrue(updater.validate_settings({"mode": "daily", "time": "25:00"})[1])
        self.assertEqual(updater.validate_settings({"mode": "daily", "time": "04:30"})[1], "")
        self.assertTrue(updater.validate_settings({"proxy": "custom", "custom_proxy": "ftp://x/"})[1])
        self.assertTrue(updater.validate_settings({"proxy": "custom", "custom_proxy": "https://x.com"})[1])  # 缺结尾 /
        s, err = updater.validate_settings({"proxy": "custom", "custom_proxy": "https://my.proxy/"})
        self.assertEqual((err, s["custom_proxy"]), ("", "https://my.proxy/"))

    def test_chain(self):
        self.assertEqual(updater.proxy_chain({"proxy": "direct"}), [""])
        self.assertEqual(updater.proxy_chain({"proxy": "auto"})[0], "")
        self.assertEqual(len(updater.proxy_chain({"proxy": "auto"})), 3)
        self.assertEqual(updater.proxy_chain({"proxy": "custom", "custom_proxy": "https://p/"}), ["https://p/", ""])


class DueTests(unittest.TestCase):
    def test_interval(self):
        s = {"mode": "interval", "hours": 6}
        self.assertTrue(updater.due(s, 0))
        self.assertFalse(updater.due(s, time.time() - 3600))
        self.assertTrue(updater.due(s, time.time() - 7 * 3600))
        self.assertFalse(updater.due({"mode": "off", "hours": 1}, 0))

    def test_interval_floor_one_hour(self):
        self.assertFalse(updater.due({"mode": "interval", "hours": 1}, time.time() - 1800))

    def test_daily(self):
        s = {"mode": "daily", "time": "04:00"}
        day = datetime.datetime(2026, 11, 24, 12, 0).timestamp()          # 当天 12:00，已过 04:00
        self.assertTrue(updater.due(s, day - 86400, now=day))             # 昨天检查过 → 今天该检查
        self.assertFalse(updater.due(s, day - 3600, now=day))             # 今天 11:00 已检查过
        early = datetime.datetime(2026, 11, 24, 3, 0).timestamp()
        self.assertFalse(updater.due(s, early - 86400, now=early))        # 还没到 04:00


class ReleaseTests(unittest.TestCase):
    def rel(self, **kw):
        base = {"tag_name": "v1.0.28", "name": "v1.0.28", "body": "notes", "html_url": "https://github.com/x",
                "assets": [
                    {"name": "fnos-lxc-1.0.28-unsigned.fpk", "size": 10, "digest": "sha256:" + "a" * 64,
                     "browser_download_url": "https://github.com/leesonaa/fnos-lxc/releases/download/v1.0.28/fnos-lxc-1.0.28-unsigned.fpk"},
                    {"name": "evil.fpk", "size": 1, "browser_download_url": "https://evil.com/x.fpk"},
                    {"name": "fnos-lxc-1.0.28.fpk", "size": 20,
                     "browser_download_url": "https://github.com/leesonaa/fnos-lxc/releases/download/v1.0.28/fnos-lxc-1.0.28.fpk"},
                ]}
        base.update(kw)
        return updater.parse_release(base)

    def test_parse_filters_foreign_urls(self):
        r = self.rel()
        self.assertEqual(r["version"], "1.0.28")
        self.assertEqual([a["name"] for a in r["assets"]], ["fnos-lxc-1.0.28-unsigned.fpk", "fnos-lxc-1.0.28.fpk"])

    def test_prefers_signed(self):
        self.assertEqual(updater.choose_asset(self.rel()["assets"])["name"], "fnos-lxc-1.0.28.fpk")
        only = [a for a in self.rel()["assets"] if "unsigned" in a["name"]]
        self.assertEqual(updater.choose_asset(only)["name"], "fnos-lxc-1.0.28-unsigned.fpk")
        self.assertIsNone(updater.choose_asset([]))

    def test_bad_tag(self):
        self.assertIsNone(updater.parse_release({"tag_name": "nightly"}))


class _H(http.server.BaseHTTPRequestHandler):
    BODY = b"FPK" * 1000
    ETAG = '"abc"'
    hits = 0

    def do_GET(self):
        _H.hits += 1
        if self.path == "/api":
            if self.headers.get("If-None-Match") == self.ETAG:
                self.send_response(304); self.end_headers(); return
            body = json.dumps({"tag_name": "v9.9.9", "assets": [{
                "name": "a.fpk", "size": len(self.BODY),
                "digest": "sha256:" + hashlib.sha256(self.BODY).hexdigest(),
                "browser_download_url": "https://github.com/leesonaa/fnos-lxc/releases/download/v9.9.9/a.fpk"}]}).encode()
            self.send_response(200); self.send_header("ETag", self.ETAG); self.end_headers(); self.wfile.write(body); return
        if self.path == "/file":
            self.send_response(200); self.send_header("Content-Length", str(len(self.BODY))); self.end_headers()
            self.wfile.write(self.BODY); return
        self.send_response(404); self.end_headers()

    def log_message(self, *a):
        pass


class NetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.orig_api = updater.API_LATEST
        cls.orig_re = updater.ASSET_URL_RE
        updater.API_LATEST = cls.base + "/api"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        updater.API_LATEST = cls.orig_api
        updater.ASSET_URL_RE = cls.orig_re

    def test_fetch_etag_and_check_cooldown(self):
        s = {"proxy": "direct"}
        rel, etag, err = updater.fetch_latest(s)
        self.assertEqual((err, rel["version"], etag), ("", "9.9.9", '"abc"'))
        rel2, etag2, err2 = updater.fetch_latest(s, etag)          # 304：没有变化也没有错误
        self.assertEqual((rel2, err2, etag2), (None, "", etag))
        st = updater.check(s, {}, force=True)
        self.assertEqual(st["latest"]["version"], "9.9.9")
        n = _H.hits
        st2 = updater.check(s, st, force=True)                      # 冷却期内不再请求
        self.assertTrue(st2.get("cooldown"))
        self.assertEqual(_H.hits, n)

    def test_check_error_recorded(self):
        updater.API_LATEST = "http://127.0.0.1:1/api"
        try:
            st = updater.check({"proxy": "direct"}, {}, force=False)
        finally:
            updater.API_LATEST = self.base + "/api"
        self.assertIn("检查失败", st["last_error"])
        self.assertGreater(st["last_check"], 0)                      # 失败也记录时间，避免疯狂重试

    def test_download_verify(self):
        updater.ASSET_URL_RE = re.compile(r".*")                     # 测试用本地地址
        with tempfile.TemporaryDirectory() as d:
            good = {"name": "a.fpk", "size": len(_H.BODY), "url": self.base + "/file",
                    "digest": "sha256:" + hashlib.sha256(_H.BODY).hexdigest()}
            seen = []
            p, err = updater.download(good, {"proxy": "direct"}, Path(d), lambda g, t, l: seen.append(g))
            self.assertEqual(err, "")
            self.assertEqual(p.read_bytes(), _H.BODY)
            self.assertTrue(seen and seen[-1] == len(_H.BODY))
            bad = dict(good, digest="sha256:" + "0" * 64)
            p, err = updater.download(bad, {"proxy": "direct"}, Path(d))
            self.assertIsNone(p)
            self.assertIn("sha256", err)
            p, err = updater.download(dict(good, size=5), {"proxy": "direct"}, Path(d))
            self.assertIn("大小不符", err)
            self.assertEqual([f.name for f in Path(d).iterdir() if f.suffix == ".part"], [])   # 失败不留半成品
            p, err = updater.download(good, {"proxy": "direct"}, Path(d), cancelled=lambda: True)
            self.assertEqual(err, "已取消")

    def test_download_rejects_foreign_url(self):
        updater.ASSET_URL_RE = self.orig_re
        with tempfile.TemporaryDirectory() as d:
            p, err = updater.download({"name": "a.fpk", "size": 1, "url": "https://evil.com/a.fpk"}, {"proxy": "direct"}, Path(d))
            self.assertIsNone(p)
            self.assertIn("不合法", err)


if __name__ == "__main__":
    unittest.main()


class LaunchInstallTests(unittest.TestCase):
    def test_volume_index(self):
        self.assertEqual(updater.volume_index("/vol2/@appcenter/x"), 2)
        self.assertEqual(updater.volume_index("/tmp/nope", "/vol12"), 12)
        self.assertEqual(updater.volume_index("/tmp/nope"), 0)

    def test_refuses_without_volume(self):
        import os, tempfile
        b = Path(tempfile.mkdtemp())
        (b / "appcenter-cli").write_text("#!/bin/sh\nexit 0\n")
        (b / "appcenter-cli").chmod(0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{b}:{old}"
        try:
            self.assertIn("存储空间", updater.launch_install(Path("/tmp/x.fpk"), Path("/tmp/x.log"), 0))
        finally:
            os.environ["PATH"] = old

    def test_install_local_runs_with_extracted_dir(self):
        import os, tarfile, tempfile, time
        d = Path(tempfile.mkdtemp())
        (d / "src").mkdir()
        (d / "src" / "manifest").write_text("version=9.9.9\n")
        fpk = d / "a.fpk"
        with tarfile.open(fpk, "w:gz") as t:
            t.add(d / "src" / "manifest", arcname="manifest")
        cli = d / "bin" / "appcenter-cli"
        cli.parent.mkdir()
        cli.write_text('#!/bin/sh\necho "ARGS $*" \nls "$3"\necho "KEEP=$wizard_keep_data"\n')
        cli.chmod(0o755)
        log = d / "install.log"
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{cli.parent}:{old}"
        real_which = updater.shutil.which
        # 固定走「直接启动」分支：CI 机器上有 systemd，systemd-run 会把任务丢到别的上下文里，测试读不到日志
        updater.shutil.which = lambda n, *a, **k: None if n == "systemd-run" else real_which(n, *a, **k)
        try:
            self.assertEqual(updater.launch_install(fpk, log, 3), "")
            for _ in range(100):
                if log.exists() and "EXIT=" in log.read_text():
                    break
                time.sleep(0.1)
        finally:
            updater.shutil.which = real_which
            os.environ["PATH"] = old
        self.assertTrue(log.exists(), "install.log 没有生成")
        txt = log.read_text()
        self.assertIn("install-local -d /tmp/fnos-lxc-upgrade.", txt)
        self.assertIn("-v 3", txt)
        self.assertIn("manifest", txt)       # 解压出来的目录里有 manifest
        self.assertIn("KEEP=true", txt)      # 卸载环节要求保留数据
        self.assertIn("EXIT=0", txt)
