"""Deterministic crash / interrupt recovery matrix for the real app.

Unlike the other suites this one drives ``build/Arca-test.app`` itself
(``--headless``), so the process that dies is the same one users run, and the
restart is a real restart of the application, not a new object in the test
process.  Everything is isolated: one temporary directory per scenario holds
``home/`` (=> ``~/Library/Application Support/Arca``), ``tmp/`` (=> TMPDIR) and
all archives/outputs; nothing reads the real user HOME.

What is asserted (docs/ENGINEERING-NOTES.md §"中断恢复"):

1. crash during compression, restart -> visible "interrupted" record;
2. crash during whole-archive extraction, restart -> same;
3. crash during partial extraction, restart -> same, other entries untouched;
4. merge target keeps pre-existing files and other tasks' products;
5. the crashed job is never reported as ``running`` or ``done`` after restart;
6. the record explains that the output may be incomplete and where it is;
7. retry requests are not replayed after restart (no journaled passwords either);
8. the journal is consumed exactly once and app-owned temp state is bounded;
9. only PIDs from this scenario's dedicated process group are signalled, and
   the group / port are gone when the test ends.

Signals are sent only to the recorded app PID or that PID's dedicated process
group (the app is started with ``start_new_session=True``); no pkill/killall
and no process lookup by name is used.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
BUNDLE = BUILD / "Arca-test.app"
LAUNCHER = BUNDLE / "Contents/MacOS/Arca"
ZZ = ROOT / "vendor" / "7zz"
MIB = 1024 * 1024

_RUN_ROOT: Path | None = None
_APP_PGIDS: dict[str, int] = {}


# --------------------------------------------------------------- process tools
def pgid_members(pgid: int) -> list[tuple[int, str]]:
    """PIDs in one dedicated process group, via ps (no name-based matching)."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,pgid=,comm="],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid, group = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if group == pgid:
            rows.append((pid, parts[2].strip() if len(parts) > 2 else ""))
    return rows


def is_zombie(comm: str) -> bool:
    return comm.startswith("[") or "defunct" in comm


def live_members(pgid: int) -> list[tuple[int, str]]:
    return [(pid, comm) for pid, comm in pgid_members(pgid)
            if not is_zombie(comm)]


def reclaim_group(pgid: int, timeout: float = 5.0) -> list[tuple[int, str]]:
    """Kill the leftover live members of *this* process group only.

    Called after the main app PID is gone: anything still in the group is an
    orphaned engine child of this very test.  Returns what was observed/killed
    so the caller can record it instead of silently pretending it never
    existed.
    """
    seen: dict[int, str] = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        members = live_members(pgid)
        if not members:
            break
        for pid, comm in members:
            seen[pid] = comm
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.1)
    return sorted(seen.items())


def untrack(pgid: int) -> None:
    if not live_members(pgid):
        for label, tracked in list(_APP_PGIDS.items()):
            if tracked == pgid:
                _APP_PGIDS.pop(label, None)


def port_closed(port: int) -> bool:
    sock = socket.socket()
    sock.settimeout(0.5)
    try:
        return sock.connect_ex(("127.0.0.1", port)) != 0
    finally:
        sock.close()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_random(path: Path, mib: int) -> None:
    with open(path, "wb") as fh:
        left = mib * MIB
        while left > 0:
            step = min(MIB, left)
            fh.write(os.urandom(step))
            left -= step


def make_7z(archive: Path, sources: list[Path], level: int = 0) -> None:
    subprocess.run([str(ZZ), "a", "-t7z", f"-mx{level}", "-bso0", "-bsp0",
                    str(archive), *[p.name for p in sources]],
                   cwd=str(sources[0].parent), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_until(predicate, timeout: float, poll: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return False


def bundle_sync() -> None:
    """Make the test bundle carry the current source (like all_test does)."""
    if not LAUNCHER.is_file():
        template = ROOT / "release/Arca.app"
        if not (template / "Contents/MacOS/Arca").is_file():
            raise RuntimeError("缺少 build/Arca-test.app，且 release/Arca.app 不存在")
        subprocess.run(["cp", "-R", str(template), str(BUNDLE)], check=True)
    source = ROOT / "src/arca"
    target = BUNDLE / "Contents/Resources/app/arca"
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if not path.is_file() or "__pycache__" in relative.parts \
                or path.name.startswith("._"):
            continue
        if path.suffix != ".py" and "web" not in relative.parts:
            continue
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    for pyc in target.rglob("*.pyc"):
        pyc.unlink()


# ----------------------------------------------------------------- HTTP client
class Client:
    def __init__(self, app: "AppInstance"):
        self.base = f"http://127.0.0.1:{app.port}/api"
        self.token = app.token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, path: str, body=None, timeout: float = 60):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"X-Arca-Token": self.token,
                     "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=timeout) as res:
                return res.status, json.loads(res.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def jobs(self) -> list[dict]:
        _, body = self.call("/jobs")
        return body.get("jobs", [])

    def job(self, job_id: str):
        return next((j for j in self.jobs() if j.get("id") == job_id), None)

    def history(self) -> list[dict]:
        _, body = self.call("/history")
        return body.get("history", [])


# --------------------------------------------------------------- app instance
class AppInstance:
    """One real ``Arca --headless`` process with an isolated HOME/TMPDIR."""

    def __init__(self, scenario: Path, label: str):
        self.scenario = Path(scenario)
        self.label = label
        self.home = self.scenario / "home"
        self.tmp = self.scenario / "tmp"
        self.home.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.port_file = self.scenario / f"port-{label}.json"
        self.log_path = self.scenario / f"app-{label}.log"
        self._log = open(self.log_path, "wb")
        env = dict(os.environ)
        env.update(HOME=str(self.home), TMPDIR=str(self.tmp),
                   PYTHONDONTWRITEBYTECODE="1")
        self.proc = subprocess.Popen(
            [str(LAUNCHER), "--headless", "--port-file", str(self.port_file)],
            cwd=str(ROOT), env=env, stdout=self._log, stderr=subprocess.STDOUT,
            start_new_session=True)
        self.pid = self.proc.pid
        self.pgid = os.getpgid(self.pid)
        self.port = 0
        self.token = ""
        _APP_PGIDS[f"{scenario.name}/{label}"] = self.pgid
        self._await_ready()

    def _await_ready(self, timeout: float = 45.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"headless 实例 {self.label} 提前退出 "
                                   f"rc={self.proc.returncode}")
            try:
                data = json.loads(self.port_file.read_text())
            except (FileNotFoundError, ValueError, OSError):
                data = None
            if isinstance(data, dict) and isinstance(data.get("port"), int) \
                    and data.get("token"):
                self.port = data["port"]
                self.token = data["token"]
                return
            time.sleep(0.05)
        raise RuntimeError(f"headless 实例 {self.label} 启动超时")

    # -- client ---------------------------------------------------------
    def client(self) -> Client:
        return Client(self)

    def state_dir(self) -> Path:
        return self.home / "Library" / "Application Support" / "Arca"

    def pending_files(self) -> list[Path]:
        folder = self.state_dir() / "pending-jobs"
        if not folder.is_dir():
            return []
        return sorted(p for p in folder.iterdir()
                      if p.is_file() and not p.name.startswith(".")
                      and p.suffix == ".json")

    # -- termination ----------------------------------------------------
    def crash_app_only(self) -> list[tuple[int, str]]:
        """SIGKILL the app process; engine children may briefly survive."""
        os.kill(self.pid, signal.SIGKILL)
        self._reap()
        self._close_log()
        reclaimed = reclaim_group(self.pgid)
        if reclaimed:
            print(f"ORPHAN-RECLAIM group={self.pgid} members={reclaimed}",
                  flush=True)
        untrack(self.pgid)
        return reclaimed

    def crash_group(self) -> list[tuple[int, str]]:
        """SIGKILL the whole dedicated process group (app + engine child)."""
        try:
            os.killpg(self.pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self._reap()
        self._close_log()
        reclaimed = reclaim_group(self.pgid)
        if reclaimed:
            print(f"ORPHAN-RECLAIM group={self.pgid} members={reclaimed}",
                  flush=True)
        untrack(self.pgid)
        return reclaimed

    def stop(self) -> list[tuple[int, str]]:
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        self._close_log()
        reclaimed = reclaim_group(self.pgid)
        untrack(self.pgid)
        return reclaimed

    def _reap(self) -> None:
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    def _close_log(self) -> None:
        try:
            if self._log and not self._log.closed:
                self._log.close()
        except Exception:
            pass


# ------------------------------------------------------------------- fixtures
def _read_text_files_under(root: Path) -> dict[str, str]:
    """Small text files under `root` -> content, for secret-leak assertions."""
    out = {}
    if not root.is_dir():
        return out
    for path in root.rglob("*"):
        if not path.is_file() or path.name.startswith("._") \
                or path.stat().st_size > 256 * 1024:
            continue
        try:
            out[str(path.relative_to(root))] = path.read_text(errors="replace")
        except OSError:
            continue
    return out


def _summarize_tree(root: Path) -> dict[str, int]:
    files = {}
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_file() and not path.name.startswith("._"):
                files[str(path.relative_to(root))] = path.stat().st_size
    return files


class InterruptRecoveryTests(unittest.TestCase):
    def setUp(self):
        assert _RUN_ROOT is not None
        self.work = Path(tempfile.mkdtemp(prefix="case-", dir=_RUN_ROOT))

    def tearDown(self):
        # Every app instance is stopped in each test's finally; remove the
        # scenario tree only after none of its jobs can still write into it.
        shutil.rmtree(self.work, ignore_errors=True)

    # ---------------------------------------------------------- assertions
    def _assert_interrupted(self, client: Client, job_id: str, kind: str):
        jobs = client.jobs()
        self.assertEqual([], jobs, f"重启后仍有内存任务：{jobs}")
        history = client.history()
        record = next((h for h in history if h.get("id") == job_id), None)
        self.assertIsNotNone(record, f"历史里没有中断记录：{history}")
        self.assertEqual("interrupted", record["status"], record)
        self.assertNotIn(record["status"], ("running", "done"))
        self.assertEqual(kind, record.get("kind"))
        note = str(record.get("note") or "")
        self.assertIn("中断", note)
        self.assertIn("不完整", note)
        self.assertTrue(record.get("destPath") or record.get("outputs"),
                        f"中断记录没有指出输出位置：{record}")
        return record

    def _assert_retry_not_replayed(self, client: Client, job_id: str) -> None:
        status, body = client.call("/jobs/retry", {"id": job_id})
        self.assertEqual(400, status, body)
        self.assertIn("失效", str(body.get("error") or ""))
        self.assertEqual([], client.jobs(), "重试请求被静默重放")
        history = [h for h in client.history() if h.get("id") == job_id]
        self.assertEqual(1, len(history), "重试请求不得生成新的任务记录")

    # ------------------------------------------------------------- scenario 1
    def test_compression_crash_then_restart_is_visible_and_safe(self):
        src_dir = self.work / "input"
        src_dir.mkdir()
        big = src_dir / "big.bin"
        make_random(big, 128)
        src_digest = sha256(big)
        out = self.work / "out"
        out.mkdir()
        secret = "Interrupt-Secret-9f2c"
        # A finished job first: recovery must append to history, not replace it.
        small_zip = self.work / "done.zip"
        with zipfile.ZipFile(small_zip, "w") as zf:
            zf.writestr("ok.txt", b"ok")

        app = AppInstance(self.work, "run1")
        jid = ""
        done_id = ""
        try:
            client = app.client()
            status, body = client.call("/test", {"path": str(small_zip)})
            self.assertEqual(200, status, body)
            done_id = body["job"]["id"]
            self.assertTrue(
                wait_until(lambda: (client.job(done_id) or {}).get("status")
                           == "done", 60), "预置任务未完成")
            self.assertIn(done_id, [h.get("id") for h in client.history()])

            status, body = client.call("/compress", {
                "inputs": [str(big)], "destDir": str(out), "name": "big",
                "format": "7z", "level": 3, "password": secret,
                "encryptNames": False})
            self.assertEqual(200, status, body)
            jid = body["job"]["id"]
            archive = out / "big.7z"
            self.assertTrue(wait_until(
                lambda: archive.exists() and archive.stat().st_size >= 1 * MIB,
                120), "压缩任务没有开始写归档")
            pending = app.pending_files()
            self.assertEqual(1, len(pending), pending)
            journal = pending[0].read_text()
            self.assertNotIn(secret, journal, "崩溃日志不得包含密码")
            self.assertEqual(jid, json.loads(journal)["id"])
            reclaimed = app.crash_app_only()
        finally:
            app.stop()
        self.assertEqual(src_digest, sha256(big), "用户输入文件被改动")
        self.assertEqual([], live_members(app.pgid), f"子进程未回收：{reclaimed}")
        self.assertTrue(port_closed(app.port), "崩溃后端口未回收")

        # ---- restart with the same isolated HOME --------------------------
        app2 = AppInstance(self.work, "run2")
        try:
            client = app2.client()
            record = self._assert_interrupted(client, jid, "compress")
            self.assertIn("big.7z", json.dumps(record, ensure_ascii=False))
            self.assertEqual([], app2.pending_files())
            before = _summarize_tree(out)
            self.assertIn("big.7z", before)
            self.assertLess(before["big.7z"], 128 * MIB,
                            "崩溃后的归档不应被当成完成")
            self._assert_retry_not_replayed(client, jid)
            self.assertEqual(before, _summarize_tree(out),
                             "重试请求不得改动磁盘输出")
            ids = [h.get("id") for h in client.history()]
            self.assertIn(done_id, ids, "中断恢复覆盖了既有完成历史")
            leaks = _read_text_files_under(app2.state_dir())
            self.assertNotIn(secret, "\n".join(leaks.values()),
                             "密码被持久化到应用状态目录")
        finally:
            app2.stop()
        self.assertTrue(port_closed(app2.port), "重启实例端口未回收")

        # ---- second restart: the journal is consumed exactly once ---------
        app3 = AppInstance(self.work, "run3")
        try:
            client = app3.client()
            hits = [h for h in client.history() if h.get("id") == jid]
            self.assertEqual(1, len(hits), f"中断记录重复：{hits}")
            self.assertEqual(1, len([h for h in client.history()
                                     if h.get("id") == done_id]))
            self.assertEqual([], app3.pending_files())
        finally:
            app3.stop()

    # ------------------------------------------------------------- scenario 2
    def test_whole_extract_group_kill_then_restart(self):
        fixtures = self.work / "fixtures"
        fixtures.mkdir()
        src = fixtures / "payload.bin"
        make_random(src, 256)
        archive = fixtures / "whole.7z"
        make_7z(archive, [src], level=0)
        src.unlink()
        out = self.work / "out"
        app = AppInstance(self.work, "run1")
        jid = ""
        try:
            client = app.client()
            status, body = client.call("/extract", {
                "path": str(archive), "destDir": str(out),
                "autoFolder": True, "onExists": "rename"})
            self.assertEqual(200, status, body)
            jid = body["job"]["id"]
            partial = out / "whole" / "payload.bin"
            self.assertTrue(wait_until(
                lambda: partial.exists() and partial.stat().st_size >= 8 * MIB,
                120), "解压任务没有开始写目标文件")
            reclaimed = app.crash_group()
        finally:
            app.stop()
        self.assertEqual([], live_members(app.pgid), f"进程组未回收：{reclaimed}")
        self.assertTrue(port_closed(app.port), "端口未回收")

        app2 = AppInstance(self.work, "run2")
        try:
            client = app2.client()
            record = self._assert_interrupted(client, jid, "extract")
            self.assertEqual("fresh", (record.get("result") or {}).get("mode"))
            self.assertTrue((out / "whole").is_dir(), "半成品目录被静默删除")
            inside = _summarize_tree(out)
            self.assertIn("whole/payload.bin", inside)
            self.assertLess(inside["whole/payload.bin"], 256 * MIB,
                            "崩溃后的输出不应被当成完整文件")
            self._assert_retry_not_replayed(client, jid)
            self.assertEqual([], app2.pending_files())
        finally:
            app2.stop()

    # ------------------------------------------------------------- scenario 3
    def test_partial_extract_crash_then_restart(self):
        fixtures = self.work / "fixtures"
        fixtures.mkdir()
        first = fixtures / "big-0.bin"
        second = fixtures / "big-1.bin"
        make_random(first, 256)
        make_random(second, 64)
        archive = fixtures / "partial.7z"
        make_7z(archive, [first, second], level=0)
        first.unlink()
        second.unlink()
        out = self.work / "out"
        app = AppInstance(self.work, "run1")
        jid = ""
        try:
            client = app.client()
            status, body = client.call("/extract", {
                "path": str(archive), "destDir": str(out), "autoFolder": True,
                "entries": ["big-0.bin"]})
            self.assertEqual(200, status, body)
            self.assertEqual(1, body.get("partial"))
            jid = body["job"]["id"]
            partial = out / "partial" / "big-0.bin"
            self.assertTrue(wait_until(
                lambda: partial.exists() and partial.stat().st_size >= 8 * MIB,
                120), "部分解压任务没有开始写目标文件")
            reclaimed = app.crash_app_only()
        finally:
            app.stop()
        self.assertEqual([], live_members(app.pgid), f"子进程未回收：{reclaimed}")

        app2 = AppInstance(self.work, "run2")
        try:
            client = app2.client()
            record = self._assert_interrupted(client, jid, "extract")
            self.assertEqual("fresh", (record.get("result") or {}).get("mode"))
            self.assertTrue((out / "partial" / "big-0.bin").exists())
            self.assertFalse((out / "partial" / "big-1.bin").exists(),
                             "未选择条目被解压出来")
            self._assert_retry_not_replayed(client, jid)
        finally:
            app2.stop()

    # ------------------------------------------------------------- scenario 4
    def test_merge_destination_keeps_existing_files_after_crash(self):
        fixtures = self.work / "fixtures"
        fixtures.mkdir()
        src = fixtures / "payload.bin"
        make_random(src, 256)
        archive = fixtures / "merge.7z"
        make_7z(archive, [src], level=0)
        src.unlink()
        dest = self.work / "merge-dest"
        (dest / "other").mkdir(parents=True)
        (dest / "keep.txt").write_text("keep-me-please\n", encoding="utf-8")
        (dest / "other" / "sibling.bin").write_bytes(b"sibling" * 1000)
        sibling_digest = sha256(dest / "other" / "sibling.bin")
        before = _summarize_tree(dest)
        app = AppInstance(self.work, "run1")
        jid = ""
        try:
            client = app.client()
            status, body = client.call("/extract", {
                "path": str(archive), "destDir": str(dest), "autoFolder": False,
                "onExists": "merge", "overwrite": "rename"})
            self.assertEqual(200, status, body)
            jid = body["job"]["id"]
            partial = dest / "payload.bin"
            self.assertTrue(wait_until(
                lambda: partial.exists() and partial.stat().st_size >= 8 * MIB,
                120), "解压任务没有开始写目标文件")
            app.crash_group()
        finally:
            app.stop()

        app2 = AppInstance(self.work, "run2")
        try:
            client = app2.client()
            record = self._assert_interrupted(client, jid, "extract")
            self.assertEqual("merge", (record.get("result") or {}).get("mode"))
            note = str(record.get("note") or "")
            self.assertIn("合并", note)
            self.assertIn("既有文件", note)
            self.assertEqual("keep-me-please\n",
                             (dest / "keep.txt").read_text(encoding="utf-8"))
            self.assertEqual(sibling_digest, sha256(dest / "other" / "sibling.bin"))
            for rel in before:
                self.assertTrue((dest / rel).exists(), f"既有文件被删除：{rel}")
            after = _summarize_tree(dest)
            added = set(after) - set(before)
            self.assertEqual({"payload.bin"}, added,
                             f"出现了不属于本任务的文件：{sorted(added)}")
            self.assertEqual([], app2.pending_files())
            self._assert_retry_not_replayed(client, jid)
        finally:
            app2.stop()

    # ------------------------------------------------------------- scenario 5
    def test_sigterm_cancels_running_job_without_orphans(self):
        """Graceful SIGTERM (not SIGKILL): the app must cancel the running job,
        reap its engine child, clear the journal, and leave no "interrupted"
        record behind on the next start (T01 acceptance)."""
        src_dir = self.work / "input"
        src_dir.mkdir()
        big = src_dir / "big.bin"
        make_random(big, 128)
        src_digest = sha256(big)
        out = self.work / "out"
        out.mkdir()

        app = AppInstance(self.work, "term")
        jid = ""
        reclaimed = []
        try:
            client = app.client()
            status, body = client.call("/compress", {
                "inputs": [str(big)], "destDir": str(out), "name": "big",
                "format": "7z", "level": 3})
            self.assertEqual(200, status, body)
            jid = body["job"]["id"]
            archive = out / "big.7z"
            self.assertTrue(wait_until(
                lambda: archive.exists() and archive.stat().st_size >= 1 * MIB,
                120), "压缩任务没有开始写归档")
            self.assertEqual(1, len(app.pending_files()),
                             "运行中应有 pending journal")
            started = time.monotonic()
            app.proc.terminate()          # SIGTERM: the graceful path
            try:
                app.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.fail("SIGTERM 后应用未在有界时间内退出")
            exit_seconds = time.monotonic() - started
            self.assertLess(exit_seconds, 20)
            app._close_log()
            reclaimed = reclaim_group(app.pgid)
            if reclaimed:
                print(f"ORPHAN-RECLAIM group={app.pgid} members={reclaimed}",
                      flush=True)
        finally:
            app.stop()

        self.assertEqual([], live_members(app.pgid),
                         f"优雅退出后仍有子进程：{reclaimed}")
        self.assertTrue(port_closed(app.port), "退出后端口未回收")
        self.assertEqual(src_digest, sha256(big), "用户输入文件被改动")
        self.assertEqual([], app.pending_files(), "优雅退出后 journal 未清理")
        # Cancelled compress may clean its own half-written archive (T00:
        # identifiable incomplete output); it must never look like a finished
        # product.  Whatever remains is frozen and must survive the restart.
        before = _summarize_tree(out)
        self.assertLess(before.get("big.7z", 0), 128 * MIB,
                        "取消中的归档不应被当成完整产物")

        # ---- restart: no interrupted / done record, retry not replayed ------
        app2 = AppInstance(self.work, "term-restart")
        try:
            client = app2.client()
            hits = [h for h in client.history() if h.get("id") == jid]
            self.assertEqual([], hits,
                             f"优雅退出不应产生 interrupted 记录：{hits}")
            self.assertEqual([], app2.pending_files())
            self.assertEqual([], client.jobs())
            self.assertEqual(before, _summarize_tree(out),
                             "重启不得删除或改写取消中的产物")
        finally:
            app2.stop()
        self.assertTrue(port_closed(app2.port), "重启实例端口未回收")


def setUpModule():
    global _RUN_ROOT
    bundle_sync()
    (BUILD / ".tmp").mkdir(parents=True, exist_ok=True)
    _RUN_ROOT = Path(tempfile.mkdtemp(prefix="interrupt-recovery-",
                                      dir=BUILD / ".tmp"))


def tearDownModule():
    stale = {}
    for label, pgid in list(_APP_PGIDS.items()):
        members = live_members(pgid)
        if members:
            stale[label] = (pgid, members)
    if stale:
        for label, (pgid, members) in stale.items():
            reclaim_group(pgid, timeout=2)
        remaining = {label: live_members(pgid)
                     for label, (pgid, _m) in stale.items()
                     if live_members(pgid)}
        raise AssertionError(f"测试进程组未回收：{stale}；清理后仍残留：{remaining}")
    if _RUN_ROOT is not None:
        shutil.rmtree(_RUN_ROOT, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
