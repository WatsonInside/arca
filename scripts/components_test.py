#!/usr/bin/env python3
"""组件更新与 7-Zip 双源回归（v3 任务书 T14 / T15）。

覆盖：
  * 7-Zip 主源（官网页面）解析、GitHub Releases 兜底与资产名校验
  * 可信 HTTPS 主机 / 资产名匹配（拒绝 http、非白名单域、路径伪装）
  * 预发布 / draft 排除
  * 组件更新 force 绕过 6h 缓存、网络失败时 stale 缓存只用于展示
  * API 响应大小上限、源码包下载上限与截断检测
  * 安装二进制“临时文件 → 校验 → 原子替换”，失败保留旧版
运行：
    build/venv/bin/python -S -E -B scripts/components_test.py
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from arca import components, par2, updates, zst  # noqa: E402
from arca.components import ComponentError, InstallJob, Par2, SevenZip, Zstd  # noqa: E402
from _paths import build_dir

PAGE_HTML = (b'<html><body><a href="download.html">'
             b'Download 7-Zip 26.00 (2026-01-01)</a> '
             b'<a href="a/7z2600-mac.tar.xz">Download</a></body></html>')

RELEASES = [
    {"tag_name": "v1.5.7", "draft": True, "prerelease": False,
     "published_at": "2026-01-03T00:00:00Z", "body": "draft"},
    {"tag_name": "v1.5.6", "draft": False, "prerelease": True,
     "published_at": "2026-01-02T00:00:00Z", "body": "prerelease"},
    {"tag_name": "v1.5.5", "draft": False, "prerelease": False,
     "published_at": "2026-01-01T00:00:00Z", "body": "stable"},
]

API_JSON = {
    "tag_name": "v26.00",
    "published_at": "2026-01-01T00:00:00Z",
    "draft": False,
    "prerelease": False,
    "assets": [{
        "name": "7z2600-mac.tar.xz",
        "browser_download_url": (
            "https://github.com/ip7z/7zip/releases/download/26.00/"
            "7z2600-mac.tar.xz"),
    }],
}


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, headers: dict | None = None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


class FakeOpener:
    def __init__(self, response):
        self.response = response

    def open(self, request, timeout=0):
        return self.response


class SourceTests(unittest.TestCase):
    """T14：官网为主源、API 兜底、白名单与资产名校验。"""

    def test_trusted_asset_url_rules(self):
        ok = updates._trusted_asset_url(
            "https://www.7-zip.org/a/7z2600-mac.tar.xz",
            "7z2600-mac.tar.xz")
        self.assertTrue(ok)
        self.assertTrue(updates._trusted_asset_url(
            "https://github.com/ip7z/7zip/releases/download/26.00/"
            "7z2600-mac.tar.xz", "7z2600-mac.tar.xz"))
        self.assertFalse(updates._trusted_asset_url(
            "http://www.7-zip.org/a/7z2600-mac.tar.xz", "7z2600-mac.tar.xz"))
        self.assertFalse(updates._trusted_asset_url(
            "https://evil.example/7z2600-mac.tar.xz", "7z2600-mac.tar.xz"))
        self.assertFalse(updates._trusted_asset_url(
            "https://www.7-zip.org/a/7z2599-mac.tar.xz", "7z2600-mac.tar.xz"))
        self.assertFalse(updates._trusted_asset_url(
            "https://user:pass@www.7-zip.org/a/7z2600-mac.tar.xz",
            "7z2600-mac.tar.xz"))

    def test_page_is_primary_source(self):
        with patch.object(updates, "_get", lambda url, timeout: PAGE_HTML):
            info = updates.fetch_latest()
        self.assertEqual(info["source"], "page")
        self.assertEqual(info["version"], "26.00")
        self.assertEqual(info["archive"], "7z2600-mac.tar.xz")
        self.assertTrue(info["urls"][0].startswith("https://www.7-zip.org/"))

    def test_page_failure_falls_back_to_github(self):
        def fake_get(url, timeout):
            if url == updates.API_LATEST:
                return json.dumps(API_JSON).encode()
            raise updates.UpdateError("HTTP 503")

        with patch.object(updates, "_get", fake_get):
            info = updates.fetch_latest()
        self.assertEqual(info["source"], "github")
        self.assertEqual(info["version"], "26.00")
        self.assertTrue(info["url"].endswith("7z2600-mac.tar.xz"))

    def test_github_asset_mismatch_rejected(self):
        payload = json.loads(json.dumps(API_JSON))
        payload["assets"][0]["name"] = "7z2599-mac.tar.xz"

        def fake_get(url, timeout):
            if url == updates.API_LATEST:
                return json.dumps(payload).encode()
            raise updates.UpdateError("HTTP 503")

        with patch.object(updates, "_get", fake_get), self.assertRaises(updates.UpdateError):
            updates.fetch_latest()

    def test_github_prerelease_rejected(self):
        payload = dict(API_JSON, prerelease=True)

        def fake_get(url, timeout):
            if url == updates.API_LATEST:
                return json.dumps(payload).encode()
            raise updates.UpdateError("HTTP 503")

        with patch.object(updates, "_get", fake_get), self.assertRaises(updates.UpdateError):
            updates.fetch_latest()

    def test_page_ignores_untrusted_links(self):
        html = (b'<html><body><a href="download.html">'
                b'Download 7-Zip 26.00 (2026-01-01)</a> '
                b'<a href="https://evil.example/7z2600-mac.tar.xz">D</a>'
                b'</body></html>')
        with patch.object(updates, "_get", lambda url, timeout: html):
            info = updates.fetch_latest()
        for url in info["urls"]:
            self.assertTrue(url.startswith("https://www.7-zip.org/"), url)
        self.assertNotIn("evil.example", " ".join(info["urls"]))


class ComponentRemoteTests(unittest.TestCase):
    """T15：缓存/force/draft 过滤/stale 展示。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=build_dir())
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name).resolve()
        self.mock = patch.object(components.util, "state_path",
                                 side_effect=lambda name: str(self.root / name))
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def test_force_bypasses_cache(self):
        fetch = MagicMock(return_value=RELEASES)
        with patch.object(components, "_fetch_json", fetch):
            first = Zstd().remote()
            self.assertEqual(first["latest"], "1.5.5")
            cached = Zstd().remote()
            self.assertEqual(cached["latest"], "1.5.5")
            self.assertEqual(fetch.call_count, 1)
            forced = Zstd().remote(force=True)
        self.assertEqual(forced["latest"], "1.5.5")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(forced["source"], "github")

    def test_draft_and_prerelease_excluded(self):
        with patch.object(components, "_fetch_json", return_value=RELEASES):
            info = Par2().remote(force=True)
        self.assertEqual(info["latest"], "1.5.5")
        self.assertEqual([v["version"] for v in info["versions"]], ["1.5.5"])

    def test_network_failure_returns_stale_cache(self):
        with patch.object(components, "_fetch_json", return_value=RELEASES):
            Zstd().remote(force=True)
        with patch.object(components, "_fetch_json",
                          side_effect=ComponentError("HTTP 403")):
            info = Zstd().remote(force=True)
        self.assertTrue(info["ok"])
        self.assertTrue(info["stale"])
        self.assertEqual(info["latest"], "1.5.5")
        self.assertIn("HTTP 403", info["error"])

    def test_json_response_cap(self):
        body = b"x" * (components.MAX_JSON_BYTES + 1)
        with patch.object(components.urllib.request, "build_opener",
                          lambda *a, **k: FakeOpener(FakeResponse(body))), \
                self.assertRaises(ComponentError):
            components._fetch_json("https://api.github.com/x")

    def test_sevenzip_install_uses_fresh_metadata(self):
        check = MagicMock(return_value={"ok": True, "latest": "26.00"})
        with patch.object(components.updates, "check", check):
            job = SevenZip().install("26.00")
        check.assert_called_once_with(None, force=True)
        self.assertEqual(job.meta["latest"], "26.00")


class InstallSafetyTests(unittest.TestCase):
    """T15：下载上限/截断/原子替换。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=build_dir())
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name).resolve()

    def test_download_content_length_cap(self):
        job = InstallJob(Zstd(), "1.5.5", meta={})
        response = FakeResponse(b"tiny", {"Content-Length": str(components.MAX_SOURCE_BYTES + 1)})
        dest = str(self.root / "pkg.tar.gz")
        with patch.object(components.urllib.request, "build_opener",
                          lambda *a, **k: FakeOpener(response)), \
                self.assertRaises(ComponentError):
            job._download("https://github.com/x/y", dest)
        self.assertFalse(os.path.exists(dest + ".part"))

    def test_download_truncation_detected(self):
        job = InstallJob(Zstd(), "1.5.5", meta={})
        response = FakeResponse(b"0123456789", {"Content-Length": "100"})
        dest = str(self.root / "pkg.tar.gz")
        with patch.object(components.urllib.request, "build_opener",
                          lambda *a, **k: FakeOpener(response)), \
                self.assertRaises(ComponentError):
            job._download("https://github.com/x/y", dest)
        self.assertFalse(os.path.exists(dest))

    def test_install_binary_is_atomic(self):
        source = self.root / "new"
        target = self.root / "bin"
        source.write_bytes(b"new-binary")
        target.write_bytes(b"old-binary")
        components._install_binary(str(source), str(target))
        self.assertEqual(target.read_bytes(), b"new-binary")
        self.assertTrue(os.access(target, os.X_OK))
        self.assertEqual(os.stat(target).st_mode & 0o100, 0o100)
        self.assertFalse(os.path.exists(str(target) + ".new"))

    def test_install_binary_failure_keeps_old(self):
        source = self.root / "new"
        target = self.root / "bin"
        source.write_bytes(b"new-binary")
        target.write_bytes(b"old-binary")
        with patch.object(components.shutil, "copyfile", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            components._install_binary(str(source), str(target))
        self.assertEqual(target.read_bytes(), b"old-binary")
        self.assertFalse(os.path.exists(str(target) + ".new"))


class HelperBinarySelectionTests(unittest.TestCase):
    """旧的用户组件不得覆盖更新的应用内置组件。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=build_dir())
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name).resolve()

    def _binary(self, module, role, version):
        path = self.root / (module.__name__.rpartition(".")[2] + "-" + role)
        if module is par2:
            output = "par2cmdline version " + version
        else:
            output = "zstd command line interface v" + version
        if version == "unknown":
            output = "version cannot be identified"
        path.write_text("#!/bin/sh\necho '%s'\n" % output)
        path.chmod(0o755)
        return path

    def _select(self, module, user_version, bundled_version):
        user = self._binary(module, "user", user_version)
        bundled = self._binary(module, "bundled", bundled_version)
        support = self.root / (module.__name__.rpartition(".")[2] + "-support")
        components_dir = support / "components"
        components_dir.mkdir(parents=True)
        user = user.replace(components_dir / user.name)
        with patch.object(module.util, "app_support_dir", return_value=str(support)), \
                patch.object(module, "candidates", return_value=[str(user), str(bundled)]):
            return module.find_binary(), user, bundled

    def test_newer_bundled_binary_wins_over_older_user_component(self):
        for module, old, new in ((par2, "1.4.0", "2.0.0"), (zst, "1.5.6", "1.5.7")):
            with self.subTest(module=module.__name__):
                (path, source), _user, bundled = self._select(module, old, new)
                self.assertEqual((path, source), (str(bundled), "bundled"))

    def test_newer_user_component_still_wins(self):
        for module, bundled_version, user_version in (
                (par2, "2.0.0", "2.1.0"), (zst, "1.5.7", "1.5.8")):
            with self.subTest(module=module.__name__):
                (path, source), user, _bundled = self._select(module, user_version, bundled_version)
                self.assertEqual((path, source), (str(user), "user"))

    def test_same_version_keeps_user_component(self):
        for module, version in ((par2, "2.0.0"), (zst, "1.5.7")):
            with self.subTest(module=module.__name__):
                (path, source), user, _bundled = self._select(module, version, version)
                self.assertEqual((path, source), (str(user), "user"))

    def test_unknown_user_version_preserves_existing_selection(self):
        for module, bundled_version in ((par2, "2.0.0"), (zst, "1.5.7")):
            with self.subTest(module=module.__name__):
                (path, source), user, _bundled = self._select(module, "unknown", bundled_version)
                self.assertEqual((path, source), (str(user), "user"))

    def test_explicit_binary_path_still_wins_without_version_comparison(self):
        for module in (par2, zst):
            with self.subTest(module=module.__name__):
                explicit = self._binary(module, "explicit", "0.1.0")
                support = self.root / (module.__name__.rpartition(".")[2] + "-explicit-support")
                support.mkdir()
                with patch.object(module.util, "app_support_dir", return_value=str(support)):
                    path, source = module.find_binary(str(explicit))
                self.assertEqual((path, source), (str(explicit), "user"))


class Par2TwoSourceRecipeTests(unittest.TestCase):
    """PAR2 2.0 的 CMake 源码布局仍可走既有的轻量编译管线。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=build_dir())
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name).resolve()

    def test_runtime_and_bundle_recipes_cover_cmake_layout(self):
        import build_thirdparty

        runtime = components.SOURCE_RECIPES["par2"]
        bundled = build_thirdparty.RECIPES["par2"]
        self.assertEqual(bundled["version"], "2.0.0")
        for spec in (runtime, bundled):
            self.assertIn("include", spec["includes"])
            self.assertIn("libs/cxxopts", spec["includes"])
            self.assertIn("-D_FILE_OFFSET_BITS=64", spec["flags"])
            self.assertIn("version_h", spec)

    def test_source_build_generates_par2_version_header(self):
        archive = self.root / "par2cmdline-2.0.0.tar.gz"
        with components.tarfile.open(archive, "w:gz") as tf:
            payload = b"// minimal source fixture\n"
            info = components.tarfile.TarInfo("par2cmdline-2.0.0/src/dummy.cpp")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))

        fake_binary = self.root / "fake-par2"
        fake_binary.write_text("#!/bin/sh\necho 'par2cmdline version 2.0.0'\n")
        fake_binary.chmod(0o755)
        work = self.root / "work"
        work.mkdir()
        spec = components.SOURCE_RECIPES["par2"].copy()
        spec["version"] = "2.0.0"
        job = InstallJob(Par2(), "2.0.0")
        generated = {}

        def inspect_source(_spec, src, _work):
            header = pathlib.Path(src) / "include" / "par2" / "version.h"
            generated["version_h"] = header.read_text() if header.exists() else ""
            return str(fake_binary)

        with patch.object(job, "_compile", side_effect=inspect_source), \
                patch.object(components, "bin_dir", return_value=str(self.root / "bin")), \
                patch.object(components, "_save_records_merge"):
            job._build_source(spec, str(work), str(archive))

        self.assertIn('#define PAR2_VERSION "2.0.0"', generated["version_h"])
        self.assertIn("#define PAR2_VERSION_MAJOR 2", generated["version_h"])
        self.assertIn("#define PAR2_VERSION_MINOR 0", generated["version_h"])
        self.assertIn("#define PAR2_VERSION_PATCH 0", generated["version_h"])
        self.assertEqual(job.phase, "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
