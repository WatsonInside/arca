"""Offline checksum and component installation regression fixtures."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))
from arca import components, updates

DATA = b"source fixture" * 20000
DIGEST = hashlib.sha256(DATA).hexdigest()
URL = "https://github.com/facebook/zstd/releases/download/v1.5.7/zstd-1.5.7.tar.gz"
API = "https://api.github.com/repos/facebook/zstd/releases/tags/v1.5.7"
NAME = "zstd-1.5.7.tar.gz"


class ChecksumTests(unittest.TestCase):
    def test_uppercase(self):
        self.assertEqual(updates._checksum_from_text(
            f"{DIGEST.upper()} *{NAME.upper()}\n".encode(), NAME), DIGEST)

    def test_windows_path(self):
        self.assertEqual(updates._checksum_from_text(
            f"{DIGEST} *C:\\downloads\\{NAME}\n".encode(), NAME), DIGEST)

    def test_wrong_filename(self):
        self.assertIsNone(updates._checksum_from_text(
            f"{DIGEST} other.tar.gz".encode(), NAME))

    def test_nonhex(self):
        self.assertIsNone(updates._checksum_from_text(
            f"{'g' * 64} {NAME}".encode(), NAME))

    def test_sidecar_priority(self):
        with patch.object(updates, "_get", return_value=f"{DIGEST} {NAME}".encode()) as get:
            self.assertEqual(updates._official_checksum([URL], NAME, release_api=API), DIGEST)
            get.assert_called_once_with(URL + ".sha256", 15.0)

    def test_api_uppercase(self):
        payload = json.dumps({"assets": [{"name": NAME.upper(),
                                          "digest": "SHA256:" + DIGEST.upper()}]}).encode()
        with patch.object(updates, "_get", side_effect=[updates.UpdateError("404"), payload]):
            self.assertEqual(updates._official_checksum([URL], NAME, release_api=API), DIGEST)

    def test_api_rejects_invalid(self):
        for asset in ({"name": "other.tar.gz", "digest": "sha256:" + DIGEST},
                      {"name": NAME, "digest": "sha256:" + "g" * 64},
                      {"name": NAME, "digest": None}):
            with self.subTest(asset=asset), patch.object(updates, "_get", side_effect=[
                    updates.UpdateError("404"), json.dumps({"assets": [asset]}).encode()]):
                with self.assertRaisesRegex(updates.UpdateError, "^无法获取校验值$"):
                    updates._official_checksum([URL], NAME, release_api=API)

    def test_missing(self):
        with patch.object(updates, "_get", side_effect=updates.UpdateError("offline")):
            with self.assertRaisesRegex(updates.UpdateError, "^无法获取校验值$"):
                updates._official_checksum([URL], NAME, release_api=API)

    def test_sevenzip_api_fallback(self):
        name = "7z2605-mac.tar.xz"
        payload = json.dumps({"assets": [{"name": name, "digest": "sha256:" + DIGEST}]}).encode()
        with patch.object(updates, "_get", side_effect=[updates.UpdateError("404"), payload]) as get:
            self.assertEqual(updates._official_checksum(["https://www.7-zip.org/a/" + name], name), DIGEST)
            self.assertEqual(get.call_args.args[0],
                             "https://api.github.com/repos/ip7z/7zip/releases/tags/26.05")

    def test_stream_and_mismatch(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as tmp:
            path = Path(tmp) / NAME
            path.write_bytes(DATA)
            with patch.object(updates, "_official_checksum", return_value=DIGEST.upper()):
                updates.verify_download(str(path), [URL], NAME)
            with patch.object(updates, "_official_checksum", return_value="0" * 64):
                with self.assertRaisesRegex(updates.UpdateError, "^校验不符，已放弃安装$"):
                    updates.verify_download(str(path), [URL], NAME)

    def test_component_failure_before_unpack_compile_record_and_cleanup(self):
        for cid in ("zstd", "par2"):
            for failure in ("missing", "mismatch"):
                with self.subTest(component=cid, failure=failure), tempfile.TemporaryDirectory(dir=ROOT / "build") as tmp:
                    target = Path(tmp) / cid
                    target.write_bytes(b"existing binary")
                    job = components.make_installer(cid, "1.5.7" if cid == "zstd" else "1.4.0")
                    def download(url, dest):
                        Path(dest).write_bytes(DATA)
                    with patch.object(components, "bin_dir", return_value=tmp), \
                         patch.object(job, "_download", side_effect=download), \
                         patch.object(updates, "_get", side_effect=updates.UpdateError("offline")) as get, \
                         patch.object(updates, "_official_checksum", **(
                             {"side_effect": updates.UpdateError("无法获取校验值")} if failure == "missing"
                             else {"return_value": "0" * 64})), \
                         patch.object(components.tarfile, "open") as unpack, \
                         patch.object(job, "_compile") as compile_source, \
                         patch.object(components, "_save_records_merge") as save:
                        job._run()
                    self.assertEqual(job.phase, "error")
                    self.assertEqual(job.error, "无法获取校验值" if failure == "missing" else "校验不符，已放弃安装")
                    unpack.assert_not_called()
                    compile_source.assert_not_called()
                    save.assert_not_called()
                    self.assertEqual(target.read_bytes(), b"existing binary")
                    self.assertFalse(list(Path(tmp).glob(".build-*")))
                    self.assertEqual([p for p in Path(tmp).iterdir()
                                      if not p.name.startswith("._")], [target])
                    self.assertEqual(job.done_path, "")

    def test_components_verified_release_to_install(self):
        for cid, version, output in (("zstd", "1.5.7", "zstd v1.5.7"),
                                     ("par2", "1.4.0", "par2cmdline version 1.4.0")):
            with self.subTest(component=cid), tempfile.TemporaryDirectory(dir=ROOT / "build") as tmp:
                spec = components.SOURCE_RECIPES[cid]
                name = spec["archive"].format(version=version)
                blob = io.BytesIO()
                with tarfile.open(fileobj=blob, mode="w:gz") as tf:
                    member = tarfile.TarInfo("root/src/file.c")
                    member.size = 4
                    tf.addfile(member, io.BytesIO(b"code"))
                content = blob.getvalue()
                digest = hashlib.sha256(content).hexdigest()
                job = components.make_installer(cid, version)
                def download(url, dest):
                    self.assertEqual(url, f"https://github.com/{spec['repo']}/releases/download/v{version}/{name}")
                    Path(dest).write_bytes(content)
                def compile_source(recipe, src, work):
                    self.assertEqual((Path(src) / "src/file.c").read_bytes(), b"code")
                    binary = Path(work) / cid
                    binary.write_bytes(b"new binary")
                    return str(binary)
                payload = json.dumps({"assets": [{"name": name, "digest": "sha256:" + digest}]}).encode()
                replies = [f"{digest} {name}".encode()] if cid == "zstd" else [updates.UpdateError("404"), payload]
                with patch.object(components, "bin_dir", return_value=tmp), \
                     patch.object(job, "_download", side_effect=download), \
                     patch.object(updates, "_get", side_effect=replies) as get, \
                     patch.object(job, "_compile", side_effect=compile_source), \
                     patch.object(components, "_run", return_value=(0, output)), \
                     patch.object(components, "_save_records_merge") as save:
                    job._run()
                self.assertEqual(job.phase, "done", job.error)
                self.assertEqual((Path(tmp) / cid).read_bytes(), b"new binary")
                save.assert_called_once()
                self.assertFalse(list(Path(tmp).glob(".build-*")))
                if cid == "par2":
                    self.assertEqual(get.call_args.args[0], f"https://api.github.com/repos/{spec['repo']}/releases/tags/v{version}")

    def test_engine_installer_rejects_before_unpack(self):
        from types import SimpleNamespace
        for failure in ("无法获取校验值", "校验不符，已放弃安装"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(dir=ROOT / "build") as tmp:
                engine = SimpleNamespace(zz="existing", version="26.04", source="bundled")
                job = updates.Installer(engine, "https://www.7-zip.org/a/7z2605-mac.tar.xz", "26.05")
                with patch.object(updates, "engines_dir", return_value=tmp), \
                     patch.object(job, "_download", side_effect=lambda path: Path(path).write_bytes(DATA)), \
                     patch.object(updates, "verify_download", side_effect=updates.UpdateError(failure)) as verify, \
                     patch.object(job, "_unpack") as unpack, \
                     patch.object(updates.util, "write_json") as save:
                    job._run()
                self.assertEqual(job.snapshot()["phase"], "error")
                self.assertEqual(job.snapshot()["error"], failure)
                verify.assert_called_once()
                self.assertEqual(verify.call_args.args[1:], (job.urls, job.archive_name))
                unpack.assert_not_called()
                save.assert_not_called()
                self.assertEqual(engine.zz, "existing")
                self.assertFalse(list(Path(tmp).glob(".download-*")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
