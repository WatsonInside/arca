"""Bounded fuzz / soak smoke for archive parsing, extraction containment and cleanup.

Two modes share this file:

* default (no arguments): the deliberately small, deterministic gate that runs
  in a few seconds and is part of ``make test`` / ``make verify``.  It prints
  exactly how many rounds and how long the bounded loop took -- the short gate
  is never presented as a long soak.
* ``--soak-seconds N``: the long monitored run (>= 30 minutes unless
  ``--allow-short`` is passed for debugging).  It repeats the same scenario
  classes, writes heartbeat JSON lines and a final structured JSON with the
  real wall-clock duration, per-scenario counts, failures, RSS/FD trends,
  children and temp-tree bounds.  This mode is explicitly NOT part of
  ``make test``.

Everything is synthetic, generated under an isolated root, and never reads real
user files or history.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import shutil
import struct
import subprocess
import sys
import tempfile
import traceback
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
        code, payload, names = low_disk_probe(self.work)
        self.assertEqual(code, 400, payload)
        self.assertIn("磁盘剩余空间不足", payload["error"])
        self.assertEqual(names, ["dummy.7z"])

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
              f"(short gate only; long soak needs --soak-seconds >= 1800)")




# =============================================================== long soak mode
# The `unittest` run above stays a few-second gate and is the only mode `make
# test` / `make verify` execute.  `--soak-seconds` switches this same file to a
# long, monitored run: every scenario class below is repeated in a bounded
# round, heartbeats are written as JSON lines, and the final JSON records the
# real wall-clock duration, per-scenario counts, failures, process/FD/RSS and
# temp-tree trends.  A long run is never faked: below 30 minutes the mode
# refuses to start unless `--allow-short` is passed explicitly for debugging.

def wait_for(predicate, timeout=120.0, poll=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(poll)
    return None


def rss_bytes(pid):
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout
        return int(out.strip()) * 1024 if out.strip() else None
    except Exception:
        return None


def descendants(root_pid):
    """Every descendant of this process, with RSS and command name."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,comm="],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]),
                         parts[3] if len(parts) > 3 else ""))
        except ValueError:
            continue
    rows = [row for row in rows
            if os.path.basename(row[3] or "") not in ("ps", "python", "")]
    children = {}
    for pid, ppid, rss, comm in rows:
        children.setdefault(ppid, []).append((pid, rss, comm))
    found = []
    stack = [root_pid]
    while stack:
        current = stack.pop()
        for pid, rss, comm in children.get(current, []):
            found.append({"pid": pid, "rss_bytes": rss * 1024, "comm": comm})
            stack.append(pid)
    return found


def fd_count():
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        return None


def tree_stats(root):
    files = total = 0
    root = Path(root)
    if root.is_dir():
        for base, _dirs, names in os.walk(root):
            for name in names:
                if name.startswith("._"):
                    continue
                try:
                    total += os.path.getsize(os.path.join(base, name))
                    files += 1
                except OSError:
                    pass
    return {"files": files, "bytes": total}


def _dir_file_count(path):
    try:
        return sum(1 for p in Path(path).iterdir()
                   if p.is_file() and not p.name.startswith("._"))
    except OSError:
        return 0


def low_disk_probe(work):
    """Controlled low-disk rejection shared by the short gate and the soak."""
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    work = Path(work)
    dummy = work / "dummy.7z"
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
            "path": str(dummy), "destDir": str(work),
            "autoFolder": False, "password": "x"})
    names = sorted(p.name for p in work.iterdir() if not p.name.startswith("."))
    return code, payload, names


class SoakSession:
    """One isolated in-process server, repeated scenario rounds, heartbeats."""

    def __init__(self, root, seconds, heartbeat, json_path, keep):
        self.root = Path(root)
        self.seconds = float(seconds)
        self.heartbeat = max(1.0, float(heartbeat))
        self.json_path = Path(json_path)
        self.keep = keep
        self.started_wall = time.time()
        self.started = time.monotonic()
        self.last_heartbeat = self.started
        self.rounds = 0
        self.counts = {}
        self.failures = []
        self.heartbeats = []
        self.max_temp = {"files": 0, "bytes": 0}
        self.max_children = 0
        self.max_fd = 0
        self.rounds_dir = self.root / "rounds"
        self.rounds_dir.mkdir(parents=True, exist_ok=True)
        self.engine = eng.Engine()
        self.settings = Settings()
        self.settings.patch({"suppressSound": True})
        self.ctx = srv.AppContext(self.engine, self.settings,
                                  str(ROOT / "src" / "arca" / "web"))
        self.server = srv.Server(self.ctx)
        self.server.start()
        self.client = Client(self.server.port, self.ctx.token)
        self.home = Path(os.environ["HOME"])
        self.code = {"head": _git("rev-parse", "HEAD"),
                     "dirty_files": _git_status_count()}

    # ------------------------------------------------------------- scenarios
    def _scenario_roundtrip(self, work):
        src = work / "src"
        src.mkdir()
        files = []
        for name, size in (("a.bin", 2 * 1024 ** 2), ("b.bin", 6 * 1024 ** 2),
                           ("sub/c.md", 512 * 1024)):
            path = src / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(os.urandom(size))
            files.append(path)
        payloads = {str(p.relative_to(src)): sha(p) for p in files}
        (work / "out").mkdir()
        status, body = self.client.call("/compress", {
            "inputs": [str(p) for p in files], "destDir": str(work / "out"),
            "name": "round", "format": "7z", "level": 1})
        assert status == 200, body
        job = self.client.wait(body["job"]["id"], timeout=180)
        assert job["status"] == "done", job
        archive = work / "out" / "round.7z"
        assert archive.is_file(), archive
        status, body = self.client.call("/extract", {
            "path": str(archive), "destDir": str(work / "xout"),
            "autoFolder": True})
        assert status == 200, body
        job = self.client.wait(body["job"]["id"], timeout=180)
        assert job["status"] == "done", job
        dest = work / "xout" / "round"
        for rel, digest in payloads.items():
            assert sha(dest / rel) == digest, rel
        return {"compress": 1, "extract": 1,
                "roundtrip_files": len(payloads)}

    def _scenario_partial(self, work):
        src = work / "src"
        src.mkdir()
        payloads = {}
        for name, size in (("a.bin", 3 * 1024 ** 2), ("b.bin", 1024 ** 2),
                           ("c.bin", 1024 ** 2)):
            path = src / name
            path.write_bytes(os.urandom(size))
            payloads[name] = sha(path)
        archive = work / "part.7z"
        subprocess.run([ZZ, "a", "-t7z", "-mx1", "-bso0", "-bsp0",
                        str(archive), *payloads], cwd=src, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        status, body = self.client.call("/extract", {
            "path": str(archive), "destDir": str(work / "out"),
            "autoFolder": True, "entries": ["a.bin"]})
        assert status == 200, body
        job = self.client.wait(body["job"]["id"], timeout=180)
        assert job["status"] == "done", job
        dest = work / "out" / "part"
        assert sha(dest / "a.bin") == payloads["a.bin"]
        assert not (dest / "b.bin").exists(), "未选中条目被解压"
        assert not (dest / "c.bin").exists(), "未选中条目被解压"
        return {"partial_extract": 1}

    def _scenario_cancel_retry(self, work):
        src = work / "src"
        src.mkdir()
        payloads = {}
        for i in range(160):
            path = src / f"f{i:03d}.bin"
            path.write_bytes(os.urandom(32 * 1024))
            payloads[path.name] = sha(path)
        archive = work / "many.7z"
        subprocess.run([ZZ, "a", "-t7z", "-mx0", "-bso0", "-bsp0",
                        str(archive), "."], cwd=src, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        status, body = self.client.call("/extract", {
            "path": str(archive), "destDir": str(work / "out"),
            "autoFolder": True})
        assert status == 200, body
        job_id = body["job"]["id"]
        dest = work / "out" / "many"
        assert wait_for(lambda: _dir_file_count(dest) >= 8, timeout=90), \
            "取消窗口内没有写出文件"
        status, body = self.client.call("/job/cancel", {"id": job_id})
        assert status == 200, body
        job = self.client.wait(job_id, timeout=120)
        assert job["status"] == "cancelled", job
        assert not dest.exists(), "fresh 目标取消后应整体回滚"
        status, body = self.client.call("/jobs/retry", {"id": job_id})
        assert status == 200, body
        job = self.client.wait(body["job"]["id"], timeout=180)
        assert job["status"] == "done", job
        files = [p for p in dest.iterdir()
                 if p.is_file() and not p.name.startswith("._")]
        assert len(files) == len(payloads), (len(files), len(payloads))
        for name, digest in payloads.items():
            assert sha(dest / name) == digest, name
        return {"cancel": 1, "retry": 1, "cancel_retry_files": len(payloads)}

    def _scenario_bad_archives(self, work):
        good = work / "good.zip"
        make_zip(good, [(b"a.txt", b"alpha"), (b"b.txt", b"beta")])
        data = good.read_bytes()
        variants = {
            "empty": b"",
            "garbage": b"not an archive at all" * 10,
            "truncated-half": data[:len(data) // 2],
            "truncated-tail": data[:-8],
        }
        for name, payload in variants.items():
            path = work / f"{name}.zip"
            path.write_bytes(payload)
            status, body = self.client.call("/inspect", {"path": str(path)})
            assert status in (200, 400), body
            dest = work / f"out-{name}"
            dest.mkdir()
            status, body = self.client.call("/extract", {
                "path": str(path), "destDir": str(dest), "autoFolder": False})
            assert status in (200, 400), body
            if status == 200:
                job = self.client.wait(body["job"]["id"], timeout=120)
                assert job["status"] in ("done", "error", "cancelled"), job
            for written in dest.rglob("*"):
                assert str(written).startswith(str(work)), written
        traversal = work / "traversal.zip"
        make_zip(traversal, [(b"../escape.txt", b"x"),
                             (b"a/../../escape2.txt", b"x"),
                             (b"/abs-escape.txt", b"x"),
                             (b"normal.txt", b"ok")])
        dest = work / "out-traversal"
        dest.mkdir()
        status, body = self.client.call("/extract", {
            "path": str(traversal), "destDir": str(dest), "autoFolder": False})
        assert status == 200, body
        job = self.client.wait(body["job"]["id"], timeout=120)
        assert job["status"] in ("done", "error", "cancelled"), job
        for escaped in (work / "escape.txt", work / "escape2.txt",
                        Path("/escape.txt")):
            assert not escaped.exists(), escaped
        assert (dest / "normal.txt").is_file()
        return {"bad_archive_extract": len(variants) + 1}

    def _scenario_wrong_password(self, work):
        src = work / "plain.bin"
        src.write_bytes(os.urandom(64 * 1024))
        digest = sha(src)
        archive = work / "secret.zip"
        subprocess.run([ZZ, "a", "-tzip", "-pcorrect-horse", "-mem=AES256",
                        "-bso0", "-bsp0", str(archive), src.name],
                       cwd=work, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wrong = work / "out-wrong"
        wrong.mkdir()
        status, body = self.client.call("/extract", {
            "path": str(archive), "destDir": str(wrong), "autoFolder": False,
            "password": "wrong-password"})
        assert status == 200, body
        job = self.client.wait(body["job"]["id"], timeout=120)
        assert job["status"] != "done", job
        right = work / "out-right"
        right.mkdir()
        status, body = self.client.call("/extract", {
            "path": str(archive), "destDir": str(right), "autoFolder": False,
            "password": "correct-horse"})
        assert status == 200, body
        job = self.client.wait(body["job"]["id"], timeout=180)
        assert job["status"] == "done", job
        assert sha(right / "plain.bin") == digest
        return {"wrong_password": 1, "password_ok": 1}

    def _scenario_low_disk(self, work):
        code, payload, names = low_disk_probe(work)
        assert code == 400, payload
        assert "磁盘剩余空间不足" in payload["error"], payload
        assert names == ["dummy.7z"], names
        return {"low_disk_rejected": 1}

    # --------------------------------------------------------------- driving
    def _run_scenario(self, name, fn):
        work = self.round_dir / name
        work.mkdir(parents=True, exist_ok=True)
        try:
            counts = fn(work) or {}
        except Exception as exc:      # noqa: BLE001 -- one bad round must not stop the soak
            self.failures.append({
                "round": self.rounds, "scenario": name,
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc()[-1500:]})
            return
        for key, value in counts.items():
            self.counts[key] = self.counts.get(key, 0) + value

    def run_round(self):
        self.round_dir = self.rounds_dir / f"{self.rounds:06d}"
        self.round_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._run_scenario("roundtrip", self._scenario_roundtrip)
            self._run_scenario("partial", self._scenario_partial)
            self._run_scenario("cancel_retry", self._scenario_cancel_retry)
            self._run_scenario("bad_archives", self._scenario_bad_archives)
            self._run_scenario("wrong_password", self._scenario_wrong_password)
            self._run_scenario("low_disk", self._scenario_low_disk)
        finally:
            try:
                shutil.rmtree(self.round_dir, ignore_errors=True)
            except OSError as exc:
                self.failures.append({"round": self.rounds,
                                      "scenario": "cleanup",
                                      "error": str(exc)})
        # Invariants that must hold after every round: no journal leftovers,
        # no un-finished jobs, no unbounded app temp state.
        try:
            assert self.ctx.manager.active_count() == 0, "round left active jobs"
            assert self.ctx.manager.pending_count() == 0, "pending journal leaked"
            leftover = [p for p in self.rounds_dir.iterdir()
                        if not p.name.startswith("._")]
            assert not leftover, f"round directory leaked: {leftover}"
            temp = tree_stats(self.root)
            self.max_temp = {"files": max(self.max_temp["files"], temp["files"]),
                             "bytes": max(self.max_temp["bytes"], temp["bytes"])}
            assert temp["bytes"] < 256 * 1024 ** 2, \
                f"soak temp tree grew past 256 MiB: {temp}"
            assert temp["files"] < 20000, f"soak temp file count: {temp}"
        except AssertionError as exc:
            self.failures.append({"round": self.rounds, "scenario": "invariants",
                                  "error": str(exc)})

    def snapshot(self):
        temp = tree_stats(self.root)
        home = tree_stats(self.home)
        children = descendants(os.getpid())
        fds = fd_count()
        self.max_children = max(self.max_children, len(children))
        self.max_fd = max(self.max_fd, fds or 0)
        return {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "elapsed_s": round(time.monotonic() - self.started, 1),
            "rounds": self.rounds,
            "scenario_counts": dict(self.counts),
            "failures": len(self.failures),
            "jobs_in_memory": len(self.ctx.manager.list()),
            "history_entries": len(self.ctx.manager.history),
            "pending_journal": self.ctx.manager.pending_count(),
            "self_rss_bytes": rss_bytes(os.getpid()),
            "self_rss_peak_bytes": resource.getrusage(
                resource.RUSAGE_SELF).ru_maxrss,
            "fd_count": fds,
            "temp_root": temp,
            "home_root": home,
            "children": children,
        }

    def emit_heartbeat(self):
        snap = self.snapshot()
        self.heartbeats.append(snap)
        self.last_heartbeat = time.monotonic()
        line = json.dumps(snap, ensure_ascii=False)
        print("HEARTBEAT " + line, flush=True)
        with open(str(self.json_path) + ".heartbeats.jsonl", "a",
                  encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _wait_children_gone(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not descendants(os.getpid()):
                return []
            time.sleep(0.1)
        return descendants(os.getpid())

    def run(self):
        wall_start = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            while time.monotonic() - self.started < self.seconds:
                self.rounds += 1
                self.run_round()
                if time.monotonic() - self.last_heartbeat >= self.heartbeat:
                    self.emit_heartbeat()
                if self.rounds % 20 == 0:
                    self.ctx.manager.clear_finished()
                    try:
                        assert self.ctx.manager.list() == [], "clear_finished left jobs"
                        assert len(self.ctx.manager.history) <= 40
                    except AssertionError as exc:
                        self.failures.append({"round": self.rounds,
                                              "scenario": "job-record-cleanup",
                                              "error": str(exc)})
            # Drain: no scenario may leave a running job or a child process.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline \
                    and self.ctx.manager.active_count() > 0:
                time.sleep(0.1)
            leftover_jobs = [job for job in self.ctx.manager.list()
                             if job["status"] in ("queued", "running")]
            for job in leftover_jobs:
                self.ctx.manager.cancel(job["id"])
            stuck = self._wait_children_gone()
            if leftover_jobs:
                self.failures.append({"round": self.rounds,
                                      "scenario": "unfinished-jobs",
                                      "error": f"{len(leftover_jobs)} jobs not terminal"})
            if stuck:
                for child in stuck:
                    try:
                        os.kill(child["pid"], 9)
                    except OSError:
                        pass
                self.failures.append({"round": self.rounds,
                                      "scenario": "child-process-leak",
                                      "error": json.dumps(stuck,
                                                          ensure_ascii=False)})
            final = self.snapshot()
            if not self.keep:
                shutil.rmtree(self.root, ignore_errors=True)
            duration = time.monotonic() - self.started
            result = {
                "soak": "arca-soak",
                "command": " ".join(sys.argv),
                "code": self.code,
                "machine": _machine(),
                "started": wall_start,
                "ended": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "requested_seconds": self.seconds,
                "duration_seconds": round(duration, 1),
                "rounds": self.rounds,
                "scenario_counts": dict(self.counts),
                "failures": self.failures,
                "heartbeat_seconds": self.heartbeat,
                "heartbeats": self.heartbeats,
                "max_temp_tree": self.max_temp,
                "max_children": self.max_children,
                "max_fd_count": self.max_fd,
                "final": final,
                "temp_kept": self.keep,
            }
            self.json_path.parent.mkdir(parents=True, exist_ok=True)
            self.json_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            print("SOAK-RESULT " + json.dumps({
                "duration_seconds": result["duration_seconds"],
                "rounds": result["rounds"],
                "scenario_counts": result["scenario_counts"],
                "failures": len(result["failures"]),
                "json": str(self.json_path)}, ensure_ascii=False), flush=True)
            return result
        finally:
            self.server.stop()


def _git(*args):
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def _git_status_count():
    out = _git("status", "--short")
    return len([line for line in out.splitlines() if line.strip()])


def _machine():
    import platform
    out = {"platform": platform.platform(), "machine": platform.machine(),
           "python": platform.python_version(), "cpu_count": os.cpu_count()}
    try:
        out["memory_bytes"] = int(subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
            timeout=5).stdout.strip())
    except Exception:
        pass
    return out


def run_soak(argv):
    parser = argparse.ArgumentParser(
        description="Arca long monitored soak (same scenarios as the short gate)")
    parser.add_argument("--soak-seconds", type=int, default=0,
                        help="duration of the long run; 0 = run the short unittest gate")
    parser.add_argument("--heartbeat", type=float, default=30.0,
                        help="seconds between structured heartbeats (default 30)")
    parser.add_argument("--json", default="",
                        help="structured result path (default build/soak-<time>.json)")
    parser.add_argument("--allow-short", action="store_true",
                        help="allow durations below 30 minutes (debug only)")
    parser.add_argument("--keep", action="store_true",
                        help="keep the isolated soak root for inspection")
    args, rest = parser.parse_known_args(argv)

    if args.soak_seconds <= 0:
        unittest.main(argv=[sys.argv[0], *rest], verbosity=2)
        return 0
    if args.soak_seconds < 1800 and not args.allow_short:
        print("拒绝启动：--soak-seconds < 1800 属于短测，不允许冒充长时 soak；"
              "调试请显式加 --allow-short", file=sys.stderr)
        return 2

    (ROOT / "build" / ".tmp").mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="arca-soak-", dir=ROOT / "build" / ".tmp"))
    home = root / "home"
    tmpdir = root / "tmp"
    home.mkdir()
    tmpdir.mkdir()
    shutil.rmtree(_HOME_DIR, ignore_errors=True)   # superseded by root/home
    os.environ["HOME"] = str(home)
    os.environ["TMPDIR"] = str(tmpdir)
    tempfile.tempdir = str(tmpdir)
    json_path = Path(args.json) if args.json else ROOT / "build" / (
        f"soak-{time.strftime('%Y%m%d-%H%M%S')}.json")
    if not json_path.is_absolute():
        json_path = ROOT / json_path
    print(f"SOAK-START requested={args.soak_seconds}s heartbeat={args.heartbeat}s "
          f"root={root} json={json_path}", flush=True)
    try:
        session = SoakSession(root, args.soak_seconds, args.heartbeat,
                              json_path, args.keep)
        result = session.run()
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
    failed = bool(result["failures"])
    return 0 if not failed else 1


def tearDownModule():
    shutil.rmtree(_HOME_DIR, ignore_errors=True)
    leftover = {p.name for p in _LOCAL_TMP.glob("arca-fuzz-home-*")} - _HOME_BASELINE
    if leftover:
        raise AssertionError(f"temporary HOME leaked: {sorted(leftover)}")


if __name__ == "__main__":
    raise SystemExit(run_soak(sys.argv[1:]))
