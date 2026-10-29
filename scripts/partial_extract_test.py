"""A1 partial extraction: entry validation, command construction, real extraction.

Covers the /api/extract `entries` contract end to end against the bundled 7-Zip
and a real local HTTP server:

  * absent `entries` keeps the historical whole-archive behaviour;
  * empty / non-string / NUL / absolute / traversal / wildcard / duplicate /
    non-member names are rejected with a clean 400 before a job exists;
  * 7z, ZIP, encrypted ZIP/7z, split ZIP and zipfix (GBK) archives extract only
    the selected entries (and a selected directory's subtree);
  * wrong passwords, failure rollback, existing files and retry-with-entries;
  * engine command construction for 7zz and for WinRAR (fake CLI only -- there
    is no real WinRAR on this machine, so RAR is NOT end-to-end verified here).
"""
from __future__ import annotations

import json
import os
import shutil
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

_LOCAL_TMP = ROOT / ".tmp"
_LOCAL_TMP.mkdir(exist_ok=True)
os.environ["TMPDIR"] = str(_LOCAL_TMP)
tempfile.tempdir = str(_LOCAL_TMP)
# Job history / settings must never land in the real user HOME.  The temporary
# HOME is removed in tearDownModule and the suite asserts no new HOME dirs are
# left behind (the historical leftovers in .tmp are a fixed baseline).
_HOME_BASELINE = {p.name for p in _LOCAL_TMP.glob("arca-partial-home-*")}
_HOME_DIR = tempfile.mkdtemp(prefix="arca-partial-home-", dir=_LOCAL_TMP)
os.environ["HOME"] = _HOME_DIR

from arca import engine as eng            # noqa: E402
from arca import jobs as jobs_mod         # noqa: E402
from arca import server as srv            # noqa: E402
from arca.settings import Settings        # noqa: E402
from acceptance_probe import make_zip     # noqa: E402

ZZ = str(ROOT / "vendor" / "7zz")
TERMINAL = ("done", "error", "cancelled")
SENTINEL = object()


def run7(*args, cwd=None):
    result = subprocess.run([ZZ, *args], cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stdout.decode("utf-8", "replace"))


class Client:
    def __init__(self, port, token):
        self.base = f"http://127.0.0.1:{port}/api"
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"X-Arca-Token": self.token, "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=240) as res:
                return res.status, json.loads(res.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def wait(self, job_id, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            status, body = self.call("/jobs")
            for job in body.get("jobs", []):
                if job["id"] == job_id and job["status"] in TERMINAL:
                    return job
            time.sleep(0.08)
        raise TimeoutError(f"job {job_id} did not finish")


def build_fixtures(root: Path):
    src = root / "src"
    (src / "子目录" / "嵌套").mkdir(parents=True)
    (src / "a.txt").write_bytes(b"alpha")
    (src / "b.txt").write_bytes(b"beta")
    (src / "子目录" / "c.txt").write_bytes(b"child")
    (src / "子目录" / "嵌套" / "d.txt").write_bytes(b"deep")
    (src / "空目录").mkdir()
    fixtures = {}

    plain7z = root / "plain.7z"
    run7("a", "-t7z", str(plain7z), "a.txt", "b.txt", "子目录", "空目录", cwd=src)
    fixtures["7z"] = plain7z

    plainzip = root / "plain.zip"
    run7("a", "-tzip", str(plainzip), "a.txt", "b.txt", "子目录", "空目录", cwd=src)
    fixtures["zip"] = plainzip

    enczip = root / "enc.zip"
    run7("a", "-tzip", "-psecret", "-mem=AES256", str(enczip),
         "a.txt", "b.txt", "子目录", "空目录", cwd=src)
    fixtures["enc-zip"] = enczip

    enc7z = root / "enc.7z"
    run7("a", "-t7z", "-psecret", "-mhe=on", str(enc7z),
         "a.txt", "b.txt", "子目录", "空目录", cwd=src)
    fixtures["enc-7z"] = enc7z

    splitdir = root / "split"
    splitdir.mkdir()
    (splitdir / "large.bin").write_bytes(os.urandom(200_000))
    (splitdir / "small.txt").write_bytes(b"small")
    run7("a", "-tzip", "-v64k", "split.zip", "large.bin", "small.txt",
         cwd=splitdir)
    first = sorted(splitdir.glob("split.zip.0*"))[0]
    fixtures["split-zip"] = first

    gbk = root / "gbk.zip"
    make_zip(gbk, [("中文文件.txt".encode("gbk"), b"hello"),
                   ("其他.txt".encode("gbk"), b"other")])
    fixtures["gbk"] = gbk

    gbk_nested = root / "gbk-nested.zip"
    make_zip(gbk_nested, [
        ("根.txt".encode("gbk"), b"root"),
        ("中文目录/".encode("gbk"), b""),
        ("中文目录/保留.txt".encode("gbk"), b"keep"),
        ("中文目录/取消.txt".encode("gbk"), b"drop"),
        ("中文目录/嵌套/".encode("gbk"), b""),
        ("中文目录/嵌套/深.txt".encode("gbk"), b"deep"),
    ])
    fixtures["gbk-nested"] = gbk_nested

    many = root / "many.zip"
    with zipfile.ZipFile(many, "w", zipfile.ZIP_STORED) as zf:
        for i in range(810):
            zf.writestr(f"f{i:04d}.txt", b"x")
    fixtures["many"] = many
    return fixtures


class EngineCommandTests(unittest.TestCase):
    def engine(self, rar="/fake/rar"):
        obj = object.__new__(eng.Engine)
        obj.zz = "/fake/7zz"
        obj.rar = rar
        obj.lock = threading.Lock()
        return obj

    def test_7zz_partial_cmd_puts_entries_after_switch_guard(self):
        cmd = self.engine(rar=None).build_extract_cmd({
            "archive": "/tmp/x.7z", "format": "7z", "dest": "/tmp/out",
            "entries": ["a.txt", "子目录"]})
        self.assertEqual(cmd[:3], ["/fake/7zz", "x", "/tmp/x.7z"])
        self.assertEqual(cmd[-3:], ["--", "a.txt", "子目录"])
        self.assertIn("-aou", cmd)

    def test_7zz_whole_archive_cmd_unchanged(self):
        cmd = self.engine(rar=None).build_extract_cmd({
            "archive": "/tmp/x.7z", "format": "7z", "dest": "/tmp/out"})
        self.assertNotIn("--", cmd)
        self.assertEqual(cmd[-1], "-y")          # no positional extras

    def test_rar_partial_cmd_puts_entries_before_dest(self):
        cmd = self.engine().build_extract_cmd({
            "archive": "/tmp/x.rar", "format": "rar", "dest": "/tmp/out",
            "entries": ["a.txt", "子目录"]})
        self.assertEqual(cmd[0], "/fake/rar")
        self.assertEqual(cmd[-3:], ["a.txt", "子目录", "/tmp/out" + os.sep])
        self.assertIn("-or", cmd)

    def test_rar_whole_archive_cmd_keeps_dest_last(self):
        cmd = self.engine().build_extract_cmd({
            "archive": "/tmp/x.rar", "format": "rar", "dest": "/tmp/out"})
        self.assertEqual(cmd[-1], "/tmp/out" + os.sep)
        self.assertEqual(cmd[-2], "-y")


class EntryValidationTests(unittest.TestCase):
    def test_valid_list_is_kept_in_order(self):
        entries, err = srv._validate_entries(["b.txt", "a.txt", "子目录/c.txt"])
        self.assertEqual(err, "")
        self.assertEqual(entries, ["b.txt", "a.txt", "子目录/c.txt"])

    def test_rejections_are_clean(self):
        cases = {
            "not a list": "a.txt",
            "empty list": [],
            "non string": ["a.txt", 3],
            "empty item": ["a.txt", ""],
            "blank item": ["a.txt", "   "],
            "nul": ["a\x00b"],
            "absolute": ["/etc/passwd"],
            "drive": ["C:/Windows/win.ini"],
            "traversal": ["../../etc/passwd"],
            "dotdot segment": ["子目录/../a.txt"],
            "backslash": ["子目录\\c.txt"],
            "wildcard": ["*.txt"],
            "question mark": ["a?.txt"],
            "bracket": ["[ab].txt"],
            "leading dash": ["-a.txt"],
            "leading at": ["@list.txt"],
            "trailing slash": ["子目录/"],
            "duplicate": ["a.txt", "a.txt"],
            "control char": ["a\tb"],
        }
        for label, raw in cases.items():
            with self.subTest(label):
                entries, err = srv._validate_entries(raw)
                self.assertIsNone(entries)
                self.assertTrue(err, label)

    def test_selected_bytes_counts_directory_subtree(self):
        full = [{"path": "a.txt", "size": 10, "isDir": False},
                {"path": "d", "size": 0, "isDir": True},
                {"path": "d/x.bin", "size": 7, "isDir": False},
                {"path": "d/sub/y.bin", "size": 5, "isDir": False},
                {"path": "z.bin", "size": 99, "isDir": False}]
        self.assertEqual(srv._selected_bytes(["a.txt", "d"], full), 22)
        with self.assertRaises(ValueError):
            srv._selected_bytes(["nope.txt"], full)


class PartialExtractHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="arca-partial-", dir=ROOT / ".tmp"))
        cls.fixtures = build_fixtures(cls.tmp)
        cls.engine = eng.Engine()
        settings = Settings()
        # Isolated HOME: no afplay child per finished job (normal app behaviour
        # is untouched; only the test settings suppress the sound).
        settings.patch({"suppressSound": True})
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
        self.dest = self.work / "out"
        self.dest.mkdir()

    def extract(self, fixture, entries=SENTINEL, **extra):
        body = {"path": str(self.fixtures[fixture]), "destDir": str(self.dest),
                "autoFolder": False, **extra}
        if entries is not SENTINEL:
            body["entries"] = entries
        return self.client.call("/extract", body)

    def start(self, fixture, entries=SENTINEL, **extra):
        status, body = self.extract(fixture, entries, **extra)
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("ok"), body)
        return body["job"]["id"]

    def run_job(self, fixture, entries=SENTINEL, **extra):
        status, body = self.extract(fixture, entries, **extra)
        self.assertEqual(status, 200, body)
        return self.client.wait(body["job"]["id"])

    def names(self):
        found = []
        for root, _dirs, files in os.walk(self.dest):
            for name in files:
                found.append(os.path.relpath(os.path.join(root, name), self.dest))
        return sorted(found)

    # ------------------------------------------------------------ happy paths
    def test_absent_entries_keeps_whole_archive(self):
        job = self.run_job("7z")
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(self.names(), ["a.txt", "b.txt", "子目录/c.txt",
                                       "子目录/嵌套/d.txt"])

    def test_7z_single_file(self):
        job = self.run_job("7z", ["a.txt"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(self.names(), ["a.txt"])
        self.assertEqual((self.dest / "a.txt").read_bytes(), b"alpha")
        self.assertFalse((self.dest / "b.txt").exists())

    def test_zip_multiple_files_and_directory_subtree(self):
        job = self.run_job("zip", ["a.txt", "子目录"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(self.names(), ["a.txt", "子目录/c.txt",
                                       "子目录/嵌套/d.txt"])
        self.assertFalse((self.dest / "b.txt").exists())

    def test_zip_nested_directory_only(self):
        job = self.run_job("zip", ["子目录/嵌套"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(self.names(), ["子目录/嵌套/d.txt"])

    def test_split_zip_selects_one_entry(self):
        job = self.run_job("split-zip", ["large.bin"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(self.names(), ["large.bin"])
        self.assertFalse((self.dest / "small.txt").exists())

    def test_zipfix_decoded_name_is_the_selector(self):
        job = self.run_job("gbk", ["中文文件.txt"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(self.names(), ["中文文件.txt"])
        self.assertEqual((self.dest / "中文文件.txt").read_bytes(), b"hello")

    def test_zip_nested_deselect_omits_unchecked_child(self):
        # This is the payload the fixed UI sends when the parent directory is
        # partially selected: root files + the fully selected nested dir.  The
        # unchecked 子目录/c.txt must not come back through a parent subtree.
        job = self.run_job("zip", ["a.txt", "b.txt", "子目录/嵌套"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(self.names(), ["a.txt", "b.txt", "子目录/嵌套/d.txt"])
        self.assertFalse((self.dest / "子目录" / "c.txt").exists())
        self.assertEqual((self.dest / "子目录" / "嵌套" / "d.txt").read_bytes(), b"deep")

    def test_zipfix_nested_deselect_omits_unchecked_child(self):
        job = self.run_job("gbk-nested",
                           ["根.txt", "中文目录/保留.txt", "中文目录/嵌套"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(self.names(),
                         ["中文目录/保留.txt", "中文目录/嵌套/深.txt", "根.txt"])
        self.assertFalse((self.dest / "中文目录" / "取消.txt").exists())

    def test_selected_empty_directory_is_created(self):
        job = self.run_job("7z", ["空目录"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertTrue((self.dest / "空目录").is_dir())
        self.assertEqual(self.names(), [])

    def test_redundant_ancestor_and_descendant_entries_are_equivalent(self):
        # API contract: a selected directory means its subtree, so passing a
        # descendant as well is redundant but valid (not a duplicate error) and
        # must not extract anything twice or outside the subtree.
        job = self.run_job("zip", ["子目录", "子目录/c.txt"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(self.names(), ["子目录/c.txt", "子目录/嵌套/d.txt"])
        self.assertFalse((self.dest / "a.txt").exists())

    def test_full_listing_is_not_capped_at_800(self):
        # The UI list is capped, but the API must validate against the full
        # archive; entry #805 is real and must be extractable by name.
        job = self.run_job("many", ["f0805.txt"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(self.names(), ["f0805.txt"])
        info = self.client.call("/inspect", {"path": str(self.fixtures["many"])})[1]["info"]
        self.assertEqual(len(info["entries"]), 800)
        self.assertGreater(info["entriesTruncated"], 0)

    # -------------------------------------------------------------- passwords
    def test_encrypted_zip_correct_password(self):
        job = self.run_job("enc-zip", ["a.txt"], password="secret")
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(self.names(), ["a.txt"])
        self.assertEqual((self.dest / "a.txt").read_bytes(), b"alpha")

    def test_encrypted_zip_wrong_password_rolls_back_fresh_destination(self):
        # autoFolder creates a destination this job owns: a failed partial
        # extraction must remove it rather than leave a half-decrypted folder.
        job = self.run_job("enc-zip", ["a.txt"], password="wrong", autoFolder=True)
        self.assertEqual(job["status"], "error", job)
        self.assertFalse((self.work / "enc").exists())

    def test_encrypted_7z_wrong_password_is_rejected_before_job(self):
        status, body = self.extract("enc-7z", ["a.txt"], password="wrong")
        self.assertEqual(status, 400, body)
        self.assertFalse(body.get("ok"))
        self.assertIn("无法读取归档目录", body.get("error", ""))
        self.assertFalse(self.dest.exists() and any(self.dest.iterdir()))

    def test_encrypted_7z_correct_password(self):
        job = self.run_job("enc-7z", ["子目录"], password="secret")
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(self.names(), ["子目录/c.txt", "子目录/嵌套/d.txt"])

    # ---------------------------------------------------------------- invalid
    def test_empty_entries_is_400_not_whole_archive(self):
        status, body = self.extract("7z", [])
        self.assertEqual(status, 400, body)
        self.assertIn("不能为空", body.get("error", ""))
        self.assertEqual(self.names(), [])

    def test_invalid_entries_are_clean_400(self):
        cases = [["/etc/passwd"], ["../a.txt"], ["a\x00b"], [""], [""],
                 ["nope.txt"], ["a.txt", "a.txt"], ["a.txt", 5], "a.txt"]
        for raw in cases:
            with self.subTest(raw=raw):
                self.setUp()
                status, body = self.extract("7z", raw)
                self.assertEqual(status, 400, body)
                self.assertFalse(body.get("ok"))
                self.assertTrue(body.get("error"))
                self.assertFalse(self.dest.exists() and any(self.dest.iterdir()))

    def test_directory_traversal_name_that_exists_as_prefix_is_still_rejected(self):
        status, body = self.extract("zip", ["子目录/../a.txt"])
        self.assertEqual(status, 400, body)

    def test_zstd_partial_is_explicitly_unsupported(self):
        # A tar.zst has a single stream; the capability is disabled rather than
        # silently extracting the whole archive.
        source = self.work / "only.txt"
        source.write_text("x")
        target = self.work / "one.tar.zst"
        result = subprocess.run([self.engine.zstd or str(ROOT / "vendor" / "zstd"),
                                 "-q", "-o", str(target), str(source)],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(result.returncode, 0)
        status, body = self.client.call("/extract", {
            "path": str(target), "destDir": str(self.dest),
            "autoFolder": False, "entries": ["only.txt"]})
        self.assertEqual(status, 400, body)
        self.assertIn("不支持", body.get("error", ""))
        self.assertEqual(self.names(), [])

    # ------------------------------------------------------- retry / rollback
    def test_retry_keeps_entries_semantics(self):
        # First attempt fails because the auto-folder name is occupied by a
        # regular file; after removing the blocker, /jobs/retry must re-run the
        # same partial request (entries included) and extract only a.txt.
        blocker = self.dest / "plain"
        blocker.write_bytes(b"not a directory")
        status, body = self.extract(
            "7z", ["a.txt"], autoFolder=True, onExists="merge")
        self.assertEqual(status, 200, body)
        job = self.client.wait(body["job"]["id"])
        self.assertEqual(job["status"], "error", job)
        blocker.unlink()
        status, body = self.client.call("/jobs/retry", {"id": job["id"]})
        self.assertEqual(status, 200, body)
        retried = self.client.wait(body["job"]["id"])
        self.assertEqual(retried["status"], "done", retried.get("error"))
        self.assertEqual(self.names(), ["plain/a.txt"])

    def _cancelled_job_with_request(self, dest, payload):
        """A task that was cancelled in this session leaves its request in
        ctx.requests; build exactly that state without racing a real cancel."""
        job = jobs_mod.Job("extract", "cancelled-retry", destPath=str(dest))
        job.status = "cancelled"
        self.ctx.manager.jobs[job.id] = job
        self.ctx.requests[job.id] = {"path": "/api/extract", "body": dict(payload)}
        return job

    def test_cancelled_extract_retry_merges_and_is_single_shot(self):
        # Cancelled tasks expose retry too.  The stored request carries the
        # partial-extraction selection, and the retry must merge into the
        # existing target folder (keeping files written by earlier attempts)
        # while the one-shot request is consumed (no silent re-replay).
        target = self.dest / "plain"
        target.mkdir()
        (target / "keep.txt").write_bytes(b"keep")
        payload = {"path": str(self.fixtures["7z"]), "destDir": str(self.dest),
                   "autoFolder": True, "entries": ["a.txt"]}
        job = self._cancelled_job_with_request(target, payload)
        try:
            status, body = self.client.call("/jobs/retry", {"id": job.id})
        finally:
            self.ctx.manager.jobs.pop(job.id, None)
        self.assertEqual(status, 200, body)
        retried = self.client.wait(body["job"]["id"])
        self.assertEqual(retried["status"], "done", retried.get("error"))
        self.assertEqual(retried["mode"], "merge", retried)
        self.assertEqual((target / "keep.txt").read_bytes(), b"keep")
        self.assertFalse((target / "b.txt").exists())
        self.assertEqual((target / "a.txt").read_bytes(), b"alpha")
        status, body = self.client.call("/jobs/retry", {"id": job.id})
        self.assertEqual(status, 400, body)
        self.assertIn("重试信息已失效", body.get("error", ""))

    def test_cancelled_extract_retry_reuses_password_without_persisting_it(self):
        payload = {"path": str(self.fixtures["enc-zip"]), "destDir": str(self.dest),
                   "autoFolder": False, "entries": ["a.txt"], "password": "secret"}
        job = self._cancelled_job_with_request(self.dest, payload)
        try:
            status, body = self.client.call("/jobs/retry", {"id": job.id})
        finally:
            self.ctx.manager.jobs.pop(job.id, None)
        self.assertEqual(status, 200, body)
        retried = self.client.wait(body["job"]["id"])
        self.assertEqual(retried["status"], "done", retried.get("error"))
        self.assertEqual((self.dest / "a.txt").read_bytes(), b"alpha")
        self.assertFalse((self.dest / "b.txt").exists())
        leaks = []
        for root, _dirs, files in os.walk(_HOME_DIR):
            for name in files:
                path = Path(root) / name
                try:
                    if b"secret" in path.read_bytes():
                        leaks.append(str(path))
                except OSError:
                    pass
        self.assertEqual(leaks, [], f"password leaked into state files: {leaks}")

    def test_failed_partial_keeps_existing_destination_contents(self):
        # Wrong password on an archive whose names are still readable: the job
        # fails and must remove the partial file it created in a merge
        # destination while preserving everything that existed before.
        (self.dest / "keep.txt").write_bytes(b"keep")
        job = self.run_job("enc-zip", ["a.txt"], password="wrong")
        self.assertEqual(job["status"], "error", job)

    def test_correct_password_retry_after_failed_merge_is_clean(self):
        (self.dest / "keep.txt").write_bytes(b"keep")
        failed = self.run_job("enc-zip", ["a.txt"], password="wrong")
        self.assertEqual(failed["status"], "error", failed)
        self.assertFalse((self.dest / "a.txt").exists())
        ok = self.run_job("enc-zip", ["a.txt"], password="secret")
        self.assertEqual(ok["status"], "done", ok.get("error"))
        self.assertEqual(self.names(), ["a.txt", "keep.txt"])
        self.assertEqual((self.dest / "a.txt").read_bytes(), b"alpha")
        self.assertEqual((self.dest / "keep.txt").read_bytes(), b"keep")

    def _controlled_extract(self, dest: Path, action: str):
        """Write a real partial file, then cancel or fail inside the fake
        engine.  This exercises the task's rollback, not just an exception."""
        run_started = threading.Event()

        class SlowEngine:
            def resolve(self, path):
                return path

            def precheck_extract(self, path, password=""):
                # T04a: fake engine accepts any archive; the path/link contract
                # itself is covered by scripts/extract_safety_test.py.
                return None

            def build_extract_cmd(self, opts):
                return ["slow", opts["dest"]]

            def run(self, cmd, cwd=None, on_progress=None, cancel=None,
                    total_bytes=0):
                target = Path(cmd[1])
                target.mkdir(parents=True, exist_ok=True)
                (target / "partial.txt").write_bytes(b"half-written")
                (target / "partial-dir").mkdir(exist_ok=True)
                (target / "partial-dir" / "nested.txt").write_bytes(b"half")
                run_started.set()
                if action == "cancel":
                    if cancel is not None:
                        cancel.wait(10)
                    return 1, "cancelled"
                if action == "raise":
                    raise OSError("controlled engine read failure after partial output")
                return 2, "boom"

        settings = Settings()
        settings.patch({"suppressSound": True})
        manager = jobs_mod.JobManager(SlowEngine(), settings)
        job = jobs_mod.Job("extract", "controlled", destPath=str(dest))
        opts = {"archive": "/tmp/fake.7z", "dest": str(dest), "format": "7z",
                "entries": ["a.txt"], "totalBytes": 1}
        manager.submit(job, jobs_mod.make_extract_task(manager, opts))
        self.assertTrue(run_started.wait(5))
        if action == "cancel":
            manager.cancel(job.id)
        deadline = time.time() + 10
        while time.time() < deadline:
            if job.to_dict()["status"] in TERMINAL:
                break
            time.sleep(0.05)
        return job.to_dict()

    def test_cancel_fresh_destination_removes_everything(self):
        fresh = self.dest / "fresh-cancel"
        status = self._controlled_extract(fresh, "cancel")
        self.assertEqual(status["status"], "cancelled", status)
        # The UI words its cancel hint from this field: a fresh destination
        # tells the user "retry merges, existing files are kept".
        self.assertEqual(status["mode"], "fresh", status)
        self.assertFalse(fresh.exists())

    def test_failure_fresh_destination_removes_everything(self):
        fresh = self.dest / "fresh-fail"
        status = self._controlled_extract(fresh, "fail")
        self.assertEqual(status["status"], "error", status)
        self.assertFalse(fresh.exists())

    def test_cancel_merge_removes_only_this_jobs_files(self):
        merge = self.dest / "merge-cancel"
        merge.mkdir()
        (merge / "keep.txt").write_bytes(b"keep")
        other = merge / "other-task"
        other.mkdir()
        (other / "output.bin").write_bytes(b"other")
        status = self._controlled_extract(merge, "cancel")
        self.assertEqual(status["status"], "cancelled", status)
        # Merge wording must disclose that a retry may overwrite same-name
        # files and that this cannot be undone.
        self.assertEqual(status["mode"], "merge", status)
        self.assertEqual((merge / "keep.txt").read_bytes(), b"keep")
        self.assertEqual((other / "output.bin").read_bytes(), b"other")
        self.assertFalse((merge / "partial.txt").exists())
        self.assertFalse((merge / "partial-dir").exists())

    def test_engine_exception_fresh_destination_removes_everything(self):
        fresh = self.dest / "fresh-exception"
        status = self._controlled_extract(fresh, "raise")
        self.assertEqual(status["status"], "error", status)
        self.assertIn("controlled engine read failure", status.get("error", ""))
        self.assertFalse(fresh.exists())

    def test_engine_exception_merge_removes_only_this_jobs_files(self):
        merge = self.dest / "merge-exception"
        merge.mkdir()
        (merge / "keep.txt").write_bytes(b"keep")
        other = merge / "other-task"
        other.mkdir()
        (other / "output.bin").write_bytes(b"other")
        status = self._controlled_extract(merge, "raise")
        self.assertEqual(status["status"], "error", status)
        self.assertIn("controlled engine read failure", status.get("error", ""))
        self.assertEqual((merge / "keep.txt").read_bytes(), b"keep")
        self.assertEqual((other / "output.bin").read_bytes(), b"other")
        self.assertFalse((merge / "partial.txt").exists())
        self.assertFalse((merge / "partial-dir").exists())

    def test_failure_merge_removes_only_this_jobs_files(self):
        merge = self.dest / "merge-fail"
        merge.mkdir()
        (merge / "keep.txt").write_bytes(b"keep")
        status = self._controlled_extract(merge, "fail")
        self.assertEqual(status["status"], "error", status)
        self.assertEqual((merge / "keep.txt").read_bytes(), b"keep")
        self.assertFalse((merge / "partial.txt").exists())
        self.assertFalse((merge / "partial-dir").exists())


def tearDownModule():
    shutil.rmtree(_HOME_DIR, ignore_errors=True)
    leftover = {p.name for p in _LOCAL_TMP.glob("arca-partial-home-*")} - _HOME_BASELINE
    if leftover:
        raise AssertionError(f"temporary HOME leaked: {sorted(leftover)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
