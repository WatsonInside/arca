"""Bounded fuzz / soak smoke for archive parsing, extraction containment and cleanup.

Deliberately small and deterministic: it runs in a few seconds, generates its
own synthetic corpus under .tmp, and never touches user files.  A long
(30-60 minute) soak is NOT run by this suite -- `SOAK` prints exactly how many
rounds and how long the bounded loop actually took, and docs/BENCHMARK.md
records that the long soak was not executed.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

_LOCAL_TMP = ROOT / ".tmp"
_LOCAL_TMP.mkdir(exist_ok=True)
os.environ["TMPDIR"] = str(_LOCAL_TMP)
tempfile.tempdir = str(_LOCAL_TMP)
_HOME_BASELINE = {p.name for p in _LOCAL_TMP.glob("arca-fuzz-home-*")}
_HOME_DIR = tempfile.mkdtemp(prefix="arca-fuzz-home-", dir=_LOCAL_TMP)
os.environ["HOME"] = _HOME_DIR

from arca import engine as eng            # noqa: E402
from arca import server as srv            # noqa: E402
from arca.settings import Settings        # noqa: E402
from acceptance_probe import make_zip     # noqa: E402

ZZ = str(ROOT / "vendor" / "7zz")
TERMINAL = ("done", "error", "cancelled")


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Client:
    def __init__(self, port, token):
        self.base = f"http://127.0.0.1:{port}/api"
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"X-Arca-Token": self.token,
                                              "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=120) as res:
                return res.status, json.loads(res.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def wait(self, job_id, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            _, body = self.call("/jobs")
            for job in body.get("jobs", []):
                if job["id"] == job_id and job["status"] in TERMINAL:
                    return job
            time.sleep(0.06)
        raise TimeoutError("job did not finish")


class FuzzSoakTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="arca-fuzz-", dir=ROOT / ".tmp"))
        cls.engine = eng.Engine()
        settings = Settings()
        settings.patch({"suppressSound": True})   # isolated HOME: no afplay child
        cls.ctx = srv.AppContext(cls.engine, settings,
                                 str(ROOT / "src" / "arca" / "web"))
        cls.server = srv.Server(cls.ctx)
        cls.server.start()
        cls.client = Client(cls.server.port, cls.ctx.token)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp))

    # ------------------------------------------------ corrupt / truncated
    def test_corrupt_and_truncated_archives_fail_cleanly(self):
        good = self.work / "good.zip"
        make_zip(good, [(b"a.txt", b"alpha"), (b"b.txt", b"beta")])
        data = good.read_bytes()
        variants = {
            "empty": b"",
            "garbage": b"not an archive at all" * 10,
            "truncated-half": data[:len(data) // 2],
            "truncated-tail": data[:-8],
        }
        for name, payload in variants.items():
            with self.subTest(name):
                path = self.work / f"{name}.zip"
                path.write_bytes(payload)
                try:
                    info = self.engine.inspect(str(path))
                except eng.EngineError:
                    info = None
                if info is not None:
                    self.assertIsInstance(info.get("entries"), list)
                status, body = self.client.call("/inspect", {"path": str(path)})
                self.assertIn(status, (200, 400), body)
                self.assertIn("ok", body)
                # Whatever the parser recovers, extraction must terminate as a
                # job (never crash the server) and must not write outside dest.
                dest = self.work / f"out-{name}"
                dest.mkdir()
                status, body = self.client.call("/extract", {
                    "path": str(path), "destDir": str(dest),
                    "autoFolder": False})
                self.assertIn(status, (200, 400), body)
                if status == 200:
                    job = self.client.wait(body["job"]["id"])
                    self.assertIn(job["status"], TERMINAL, job)
                for written in dest.rglob("*"):
                    self.assertIn(self.work, written.parents)

    # ---------------------------------------------------- path containment
    def test_traversal_entry_names_cannot_escape_destination(self):
        archive = self.work / "traversal.zip"
        make_zip(archive, [(b"../escape.txt", b"x"),
                           (b"a/../../escape2.txt", b"x"),
                           (b"/abs-escape.txt", b"x"),
                           (b"normal.txt", b"ok")])
        dest = self.work / "out"
        dest.mkdir()
        status, body = self.client.call("/extract", {
            "path": str(archive), "destDir": str(dest), "autoFolder": False})
        self.assertEqual(status, 200, body)
        job = self.client.wait(body["job"]["id"])
        self.assertIn(job["status"], TERMINAL, job)
        for escaped in (self.work / "escape.txt", self.work / "escape2.txt",
                        self.work / "abs-escape.txt", Path("/abs-escape.txt")):
            self.assertFalse(escaped.exists(), escaped)
        # Normal entries are unaffected by the hostile siblings.
        self.assertTrue((dest / "normal.txt").is_file())

    def test_overlong_entry_names_do_not_escape(self):
        archive = self.work / "longnames.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("x" * 240 + ".txt", b"ok")
            zf.writestr("y" * 300 + ".txt", b"too long for the filesystem")
        dest = self.work / "out"
        dest.mkdir()
        status, body = self.client.call("/extract", {
            "path": str(archive), "destDir": str(dest), "autoFolder": False})
        self.assertEqual(status, 200, body)
        job = self.client.wait(body["job"]["id"])
        self.assertIn(job["status"], TERMINAL, job)
        for path in dest.rglob("*"):
            self.assertEqual(path.parent, dest, f"nested escape: {path}")

    # ------------------------------------------------------- low disk space
    def test_low_disk_space_is_rejected_before_any_write(self):
        from collections import namedtuple
        usage = namedtuple("usage", "total used free")
        dummy = self.work / "dummy.7z"
        dummy.write_bytes(b"x")
        info = {"path": str(dummy), "rawPath": str(dummy), "type": "7z",
                "label": "7Z", "needsPassword": False, "encrypted": False,
                "entries": [], "counts": {"files": 1, "dirs": 0},
                "size": 1, "uncompressed": 10 ** 12, "parts": [], "isSplit": False,
                "ratio": 0, "solid": False, "method": "", "entriesTruncated": 0,
                "error": None}
        fake_engine = SimpleNamespace(
            resolve=lambda p: p,
            inspect=lambda path, password="": info,
            list_entries=lambda path, password="": [],
            parts=lambda p: [])
        handler = object.__new__(srv.Handler)
        handler.ctx = SimpleNamespace(
            engine=fake_engine,
            settings=SimpleNamespace(get=lambda key, default=None: default),
            inspect_lock=threading.RLock(), inspect_cache={},
            manager=SimpleNamespace(_cv=threading.Condition(), _targets={}),
            saved_passwords=SimpleNamespace())
        handler._json = lambda obj, code=200: (code, obj)
        handler._err = lambda message, code=400: (code, {"ok": False,
                                                        "error": message})
        handler._secrets = []
        with mock.patch.object(srv.shutil, "disk_usage",
                               return_value=usage(10 ** 12, 10 ** 12, 1024)):
            code, payload = handler._extract({
                "path": str(dummy), "destDir": str(self.work),
                "autoFolder": False, "password": "x"})
        self.assertEqual(code, 400, payload)
        self.assertIn("磁盘剩余空间不足", payload["error"])
        self.assertEqual([p.name for p in self.work.iterdir()], ["dummy.7z"])

    # ---------------------------------------------------------- killed proc
    def test_killed_engine_process_leaves_no_output_outside_dest(self):
        src = self.work / "big.bin"
        src.write_bytes(os.urandom(128 * 1024 * 1024))
        archive = self.work / "big.7z"
        subprocess.run([ZZ, "a", "-t7z", "-mx0", "-bso0", "-bsp0",
                        str(archive), str(src)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        src.unlink()
        dest = self.work / "out"
        dest.mkdir()
        proc = subprocess.Popen([ZZ, "x", str(archive), "-o" + str(dest),
                                 "-bso0", "-bsp0"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.02)
        proc.kill()
        proc.wait(timeout=10)
        self.assertNotEqual(proc.returncode, 0)
        outside = [p for p in self.work.iterdir()
                   if p.name not in ("big.7z", "out")]
        self.assertEqual(outside, [], [str(p) for p in outside])
        extracted = dest / "big.bin"
        # The file may be partial (process was killed); it must never be
        # reported as a completed 128 MiB output, and it must stay in dest.
        if extracted.exists():
            self.assertLess(extracted.stat().st_size, 128 * 1024 * 1024)

    # ------------------------------------------------------- bounded soak
    def test_bounded_compress_extract_cycles(self):
        src = self.work / "src"
        src.mkdir()
        payloads = {}
        for name, size in (("a.txt", 4096), ("b.bin", 32_000),
                           ("子目录/c.md", 900)):
            path = src / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(os.urandom(size))
            payloads[name] = sha(path)
        rounds = 3
        started = time.monotonic()
        for i in range(rounds):
            archive = self.work / f"cycle-{i}.7z"
            subprocess.run([ZZ, "a", "-t7z", "-mx1", "-bso0", "-bsp0",
                            str(archive), "."], cwd=src, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            dest = self.work / f"out-{i}"
            dest.mkdir()
            subprocess.run([ZZ, "x", str(archive), "-o" + str(dest),
                            "-bso0", "-bsp0"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for name, digest in payloads.items():
                self.assertEqual(sha(dest / name), digest)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 60, "bounded soak must stay short")
        print(f"SOAK: rounds={rounds} elapsed={elapsed:.2f}s "
              f"(long 30-60 min soak NOT executed)")


def tearDownModule():
    shutil.rmtree(_HOME_DIR, ignore_errors=True)
    leftover = {p.name for p in _LOCAL_TMP.glob("arca-fuzz-home-*")} - _HOME_BASELINE
    if leftover:
        raise AssertionError(f"temporary HOME leaked: {sorted(leftover)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
