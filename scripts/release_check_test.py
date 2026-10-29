#!/usr/bin/env python3
"""发布预检（T09）回归：资产/摘要/tar 安全/provenance/import 副作用/文案。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import plistlib
import sys
import tarfile
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from arca import util  # noqa: E402
import release_check as rc  # noqa: E402


def _tmp_root() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(prefix="release-check-", dir=ROOT / "build"))


def make_app(root: pathlib.Path, version: str | None = None) -> pathlib.Path:
    app = root / "release" / "Arca.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    plist = {"CFBundleShortVersionString": version or util.APP_VERSION}
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(plist))
    exe = app / "Contents" / "MacOS" / "Arca"
    exe.write_bytes(b"#!/bin/sh\n")
    exe.chmod(0o755)
    return app


def make_tar(dest: pathlib.Path) -> pathlib.Path:
    payload = dest.parent / "payload" / "Arca.app"
    (payload / "Contents").mkdir(parents=True)
    (payload / "Contents" / "Info.plist").write_bytes(b"x")
    with tarfile.open(dest, "w:xz") as tf:
        tf.add(payload, arcname="Arca.app")
    return dest


def make_dist(root: pathlib.Path, version: str) -> pathlib.Path:
    dist = root / "dist"
    dist.mkdir(parents=True)
    (dist / f"Arca-{version}.dmg").write_bytes(b"dmg")
    (dist / f"Arca-{version}.pkg").write_bytes(b"pkg")
    archive = make_tar(dist / f"Arca-{version}-mac.tar.xz")
    (dist / f"Arca-{version}-release-notes.txt").write_text("notes\n", encoding="utf-8")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (dist / f"Arca-{version}-mac.tar.xz.sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8")
    return dist


def make_summary(root: pathlib.Path, head: str, *, clean=True, result="PASS",
                 head_value=None) -> pathlib.Path:
    log = root / "suite.log"
    log.write_text("ok\n", encoding="utf-8")
    data = {
        "head": head_value or head,
        "worktree_clean": clean,
        "suites": [{"name": "node", "result": result,
                    "log": str(log)}],
        "logs": [str(log)],
    }
    path = root / "all-test-summary-test.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = _tmp_root()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.head = "a" * 40

    def test_app_version_pass_and_mismatch(self):
        app = make_app(self.root)
        self.assertEqual(rc.check_app_version(app, util.APP_VERSION)["status"], "PASS")
        self.assertEqual(rc.check_app_version(app, "9.9.9")["status"], "FAIL")

    def test_assets_and_digest(self):
        dist = make_dist(self.root, "1.1.0")
        results = {r["name"]: r for r in rc.check_assets(dist, "1.1.0")}
        self.assertEqual(results["assets"]["status"], "PASS")
        self.assertEqual(results["digest"]["status"], "PASS")

    def test_missing_asset_fails(self):
        dist = make_dist(self.root, "1.1.0")
        (dist / "Arca-1.1.0.pkg").unlink()
        results = rc.check_assets(dist, "1.1.0")
        self.assertTrue(any(r["name"] == "assets" and r["status"] == "FAIL" for r in results))

    def test_digest_mismatch_fails(self):
        dist = make_dist(self.root, "1.1.0")
        sidecar = dist / "Arca-1.1.0-mac.tar.xz.sha256"
        sidecar.write_text("0" * 64 + "  Arca-1.1.0-mac.tar.xz\n", encoding="utf-8")
        results = {r["name"]: r for r in rc.check_assets(dist, "1.1.0")}
        self.assertEqual(results["digest"]["status"], "FAIL")

    def test_tar_safety_pass(self):
        archive = make_tar(self.root / "ok.tar.xz")
        self.assertEqual(rc.check_tar_safety(archive)["status"], "PASS")

    def test_tar_safety_rejects_traversal_and_escape(self):
        archive = self.root / "bad.tar.xz"
        with tarfile.open(archive, "w:xz") as tf:
            info = tarfile.TarInfo("Arca.app/../../evil.txt")
            data = b"evil"
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        self.assertEqual(rc.check_tar_safety(archive)["status"], "FAIL")

        archive2 = self.root / "bad-link.tar.xz"
        with tarfile.open(archive2, "w:xz") as tf:
            info = tarfile.TarInfo("Arca.app/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "../../etc/passwd"
            tf.addfile(info)
        self.assertEqual(rc.check_tar_safety(archive2)["status"], "FAIL")

    def test_provenance(self):
        summary = make_summary(self.root, self.head)
        self.assertEqual(rc.check_provenance(summary, self.head)["status"], "PASS")
        self.assertEqual(rc.check_provenance(summary, "b" * 40)["status"], "FAIL")
        summary2 = make_summary(self.root, self.head, clean=False)
        self.assertEqual(rc.check_provenance(summary2, self.head)["status"], "FAIL")
        summary3 = make_summary(self.root, self.head, result="FAIL")
        self.assertEqual(rc.check_provenance(summary3, self.head)["status"], "FAIL")

    def test_import_side_effects(self):
        result = rc.check_import_side_effects()
        self.assertEqual(result["status"], "PASS", result["detail"])

    def test_network_wording(self):
        self.assertEqual(rc.check_network_wording(ROOT / "scripts" / "package.py")["status"],
                         "PASS")
        bad = self.root / "package.py"
        bad.write_text("x = '所有处理都在本机完成，不会联网'\n", encoding="utf-8")
        self.assertEqual(rc.check_network_wording(bad)["status"], "FAIL")

    def test_collect_ok(self):
        app = make_app(self.root)
        dist = make_dist(self.root, util.APP_VERSION)
        summary = make_summary(self.root, self.head)
        results = rc.collect(app, dist, summary, self.head)
        failing = [r for r in results if r["status"] == "FAIL"]
        self.assertEqual(failing, [], failing)


if __name__ == "__main__":
    unittest.main(verbosity=2)
