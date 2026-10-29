"""T04a: extraction write boundary regression tests.

Covers the three extraction backends:

* ``zipfix`` (legacy-ZIP names decoded by us) -- unsafe names and symlink
  entries are skipped, pre-existing symlinked directories cannot be written
  through;
* ``zst`` (tar.zst) -- tar links and traversal names are skipped;
* ``engine.precheck_extract`` -- archives that 7zz would extract are rejected
  before the job touches the target directory when they contain traversal
  names or symlink entries.

All fixtures are generated in a private temporary directory; nothing outside
it is read or written.
"""
from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))
from arca import engine, util, zipfix, zst


def _write_zip(path: Path, entries) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)


class ZipFixBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "build")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_traversal_and_absolute_names_are_skipped(self):
        archive = self.root / "bad.zip"
        _write_zip(archive, [("../evil.txt", "pwn"),
                             ("/abs.txt", "pwn"),
                             ("ok/中文.txt", "ok")])
        dest = self.root / "out"
        result = zipfix.extract(str(archive), str(dest), {})
        self.assertEqual(result["skipped"], 2)
        self.assertFalse((self.root / "evil.txt").exists())
        self.assertFalse((self.root / "abs.txt").exists())
        self.assertEqual((dest / "ok" / "中文.txt").read_bytes(), b"ok")

    def test_symlink_mode_entry_is_skipped(self):
        archive = self.root / "link.zip"
        info = zipfile.ZipInfo("link")
        info.external_attr = (0o120777 << 16)
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(info, "/etc/passwd")
            zf.writestr("ok.txt", "ok")
        dest = self.root / "out"
        result = zipfix.extract(str(archive), str(dest), {})
        self.assertGreaterEqual(result["skipped"], 1)
        self.assertFalse((dest / "link").exists())
        self.assertEqual((dest / "ok.txt").read_bytes(), b"ok")

    def test_preexisting_symlink_parent_is_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        dest = self.root / "out"
        dest.mkdir()
        (dest / "sub").symlink_to(outside)
        archive = self.root / "escape.zip"
        _write_zip(archive, [("sub/escape.txt", "pwn")])
        result = zipfix.extract(str(archive), str(dest), {})
        self.assertGreaterEqual(result["skipped"], 1)
        self.assertFalse((outside / "escape.txt").exists())

    def test_legit_multilevel_names_still_extract(self):
        archive = self.root / "good.zip"
        _write_zip(archive, [("目录/", ""), ("目录/文本.txt", "x")])
        dest = self.root / "out"
        result = zipfix.extract(str(archive), str(dest), {})
        self.assertEqual(result["skipped"], 0)
        self.assertEqual((dest / "目录" / "文本.txt").read_bytes(), b"x")


class ZstBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "build")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.binary, _source = zst.find_binary()
        if not self.binary:
            self.skipTest("zstd 程序不可用")

    def _tar_zst(self, members) -> Path:
        tar_path = self.root / "fixture.tar"
        with tarfile.open(tar_path, "w") as tf:
            for name, kind, payload in members:
                info = tarfile.TarInfo(name)
                if kind == "file":
                    data = payload.encode()
                    info.size = len(data)
                    import io
                    tf.addfile(info, io.BytesIO(data))
                elif kind == "sym":
                    info.type = tarfile.SYMTYPE
                    info.linkname = payload
                    tf.addfile(info)
                else:
                    info.type = tarfile.DIRTYPE
                    tf.addfile(info)
        out = self.root / "fixture.tar.zst"
        subprocess.run([self.binary, "-q", "-f", str(tar_path), "-o", str(out)],
                       stdin=subprocess.DEVNULL, check=True)
        return out

    def test_tar_links_and_traversal_are_skipped(self):
        archive = self._tar_zst([
            ("../evil.txt", "file", "pwn"),
            ("evil-link", "sym", "/etc/passwd"),
            ("ok.txt", "file", "ok"),
        ])
        dest = self.root / "out"
        result = zst.extract(str(archive), str(dest))
        self.assertGreaterEqual(result["skipped"], 2)
        self.assertFalse((self.root / "evil.txt").exists())
        self.assertFalse((dest / "evil-link").exists())
        self.assertEqual((dest / "ok.txt").read_bytes(), b"ok")

    def test_preexisting_symlink_parent_is_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        dest = self.root / "out"
        dest.mkdir()
        (dest / "sub").symlink_to(outside)
        archive = self._tar_zst([("sub/escape.txt", "file", "pwn")])
        result = zst.extract(str(archive), str(dest))
        self.assertGreaterEqual(result["skipped"], 1)
        self.assertFalse((outside / "escape.txt").exists())


class EnginePrecheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "build")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.engine = engine.Engine()
        if not self.engine.zz:
            self.skipTest("7zz 不可用")

    def _zip_with_7zz(self, archive: Path, source: Path, extra=()):
        subprocess.run([self.engine.zz, "a", "-snl", "-tzip", str(archive),
                        str(source)] + list(extra),
                       stdin=subprocess.DEVNULL, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_traversal_archive_is_rejected(self):
        archive = self.root / "bad.zip"
        _write_zip(archive, [("../evil.txt", "pwn"), ("ok.txt", "ok")])
        with self.assertRaises(engine.EngineError) as ctx:
            self.engine.precheck_extract(str(archive))
        self.assertIn("不安全路径", str(ctx.exception))

    def test_symlink_archive_is_rejected(self):
        source = self.root / "src"
        source.mkdir()
        (source / "ok.txt").write_text("ok")
        (source / "link").symlink_to("/etc/passwd")
        archive = self.root / "link.zip"
        subprocess.run([self.engine.zz, "a", "-snl", "-tzip", str(archive),
                        str(source / "link"), str(source / "ok.txt")],
                       stdin=subprocess.DEVNULL, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with self.assertRaises(engine.EngineError) as ctx:
            self.engine.precheck_extract(str(archive))
        self.assertIn("符号链接", str(ctx.exception))

    def test_clean_archive_passes(self):
        source = self.root / "src"
        source.mkdir()
        (source / "ok.txt").write_text("ok")
        archive = self.root / "good.7z"
        subprocess.run([self.engine.zz, "a", str(archive), str(source)],
                       stdin=subprocess.DEVNULL, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.engine.precheck_extract(str(archive))   # 不抛异常即可

    def test_name_helpers_cover_drive_and_backslash(self):
        self.assertTrue(util.name_escape("C:/windows/x"))
        self.assertTrue(util.name_escape("a\\..\\b"))
        self.assertFalse(util.name_escape("目录/文本.txt"))
        self.assertIsNone(util.safe_join("/tmp", "../x"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
