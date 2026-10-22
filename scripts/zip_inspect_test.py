from __future__ import annotations

from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))
from arca import engine, server
from acceptance_probe import make_zip


class ZipInspectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "build")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.engine = engine.Engine()

    def archive(self, names=("目录/", "目录/文本.txt", "empty"), method=8, extra=b""):
        path = self.root / "fixture.zip"
        with zipfile.ZipFile(path, "w", compression=method) as archive:
            for name in names:
                zi = zipfile.ZipInfo(name, (2024, 2, 3, 4, 5, 6))
                zi.compress_type = method
                zi.extra = extra
                archive.writestr(zi, b"" if name.endswith("/") or name == "empty" else b"abc")
        return path

    def compare(self, path, fast=True, password=""):
        entry = str(path)
        expected = self.engine._inspect_7zz(entry, entry, password)
        if fast:
            with patch.object(self.engine, "_run_sync", side_effect=AssertionError("7zz invoked")), \
                    patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess invoked")):
                actual = self.engine.inspect(entry, password)
        else:
            with patch.object(self.engine, "_inspect_7zz", wraps=self.engine._inspect_7zz) as slow:
                actual = self.engine.inspect(entry, password)
            slow.assert_called_once()
        self.assertEqual(actual, expected)
        for item in actual["entries"]:
            self.assertEqual(set(item), {"path", "size", "packed", "modified", "isDir", "encrypted", "crc", "method"})
        return actual

    def test_utf8_directory_and_empty_file(self):
        self.compare(self.archive())

    def test_store_deflate_bzip2(self):
        for method in (0, 8, 12):
            with self.subTest(method=method):
                self.compare(self.archive(method=method))

    def test_empty_archive(self):
        self.compare(self.archive(names=()))

    def test_gbk_mixed_directory(self):
        path = self.root / "gbk.zip"
        make_zip(path, [(name.encode("gbk"), data) for name, data in
                       [("中文目录/", b""), ("中文目录/中文.txt", b"abc"), ("ascii", b"x")]])
        result = self.compare(path)
        self.assertEqual(result["nameEncoding"], "gbk")
        self.assertTrue(result["entries"][0]["path"].endswith("/"))
        self.assertTrue(all(item["crc"] == item["method"] == "" for item in result["entries"]))

    def test_truncated_listing(self):
        info = self.compare(self.archive(names=tuple(f"f{i}" for i in range(805))))
        self.assertEqual(info["entriesTruncated"], 5)
        self.assertEqual(info["counts"]["files"], 805)

    def test_unsupported_method_falls_back(self):
        self.compare(self.archive(method=14), fast=False)

    def test_timestamp_extra_falls_back(self):
        extra = struct.pack("<HHBI", 0x5455, 5, 1, 1700000001)
        self.compare(self.archive(extra=extra), fast=False)

    def test_corrupt_falls_back(self):
        path = self.archive()
        path.write_bytes(path.read_bytes()[:-10])
        with patch.object(self.engine, "_inspect_7zz", return_value={"fallback": True}) as slow:
            self.assertEqual(self.engine.inspect(str(path)), {"fallback": True})
        slow.assert_called_once()

    def test_multidisk_falls_back(self):
        path = self.archive()
        data = bytearray(path.read_bytes())
        end = data.rfind(b"PK\x05\x06")
        struct.pack_into("<H", data, end + 4, 1)
        path.write_bytes(data)
        with patch.object(self.engine, "_inspect_7zz", return_value={}) as slow:
            self.engine.inspect(str(path))
        slow.assert_called_once()

    def test_numbered_split_falls_back(self):
        path = self.archive()
        (self.root / "fixture.zip.001").write_bytes(b"part")
        with patch.object(self.engine, "_inspect_7zz", return_value={}) as slow:
            self.engine.inspect(str(path))
        slow.assert_called_once()

    def test_aes_and_verify_password_preserve_7zz(self):
        source = self.root / "data.txt"
        source.write_text("AES fixture")
        path = self.root / "aes.zip"
        result = subprocess.run([self.engine.zz, "a", "-tzip", "-mem=AES256", "-pAscii42!",
                                 str(path), str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.compare(path, fast=False)["encrypted"])
        for password in ("Ascii42!", "wrong"):
            with self.subTest(password=password):
                self.compare(path, fast=False, password=password)
        handler = object.__new__(server.Handler)
        handler.ctx = SimpleNamespace(engine=self.engine, inspect_cache={},
                                      inspect_lock=server.threading.RLock())
        handler._json = lambda payload: payload
        handler._err = lambda error: {"error": error}
        with patch.object(self.engine, "_inspect_7zz", wraps=self.engine._inspect_7zz) as slow:
            response = handler._inspect({"path": str(path), "password": "Ascii42!"})
        slow.assert_called_once()
        self.assertEqual(response["info"]["counts"]["files"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
