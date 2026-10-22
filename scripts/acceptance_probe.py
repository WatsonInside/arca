"""Exercise acceptance boundaries through a running local HTTP service."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import tempfile
import time
import urllib.error
import urllib.request
import zlib

ROOT = Path(__file__).resolve().parents[1]


def probe_size(value):
    match = re.fullmatch(r"(\d+)(B|KB|MB|GB|TB)?", value.upper())
    if not match or int(match[1]) <= 0:
        raise argparse.ArgumentTypeError("use a positive integer size, e.g. 50GB")
    return int(match[1]) * 1024 ** ["B", "KB", "MB", "GB", "TB"].index(match[2] or "B")


def make_zip(path, entries):
    out, central = bytearray(), bytearray()
    for name, data in entries:
        crc, offset = zlib.crc32(data) & 0xFFFFFFFF, len(out)
        out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, 0, 0, 0,
                           crc, len(data), len(data), len(name), 0) + name + data
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0, 0, 0, 0,
                               crc, len(data), len(data), len(name), 0, 0, 0, 0, 0, offset) + name
    offset = len(out)
    out += central
    out += struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries),
                       len(central), offset, 0)
    path.write_bytes(out)


class Probe:
    def __init__(self, port_file):
        ports = json.loads(port_file.read_text())
        self.base = f"http://127.0.0.1:{int(ports['port'])}/api"
        self.token = ports["token"]
        self.results = []
        self.active_jobs = set()

    def check(self, name, condition, detail=""):
        self.results.append(bool(condition))
        detail = str(detail).replace(self.token, "[REDACTED]").replace("\n", " ")
        print(("PASS " if condition else "FAIL ") + name + (f" [{detail}]" if detail else ""), flush=True)

    def api(self, path, body=None):
        headers = {"X-Arca-Token": self.token}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, data, headers)
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.code, json.loads(response.read().decode("utf-8"))

    def extract(self, archive, destination):
        status, body = self.api("/extract", {"path": str(archive), "destDir": str(destination)})
        job_id = body.get("job", {}).get("id")
        if status != 200 or not job_id:
            return {"status": "rejected", "error": body.get("error"), "http": status}
        self.active_jobs.add(job_id)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            _, body = self.api("/jobs")
            for job in body.get("jobs", []):
                if job["id"] == job_id and job["status"] in ("done", "ok", "error", "failed", "cancelled"):
                    self.active_jobs.discard(job_id)
                    return job
            time.sleep(.1)
        raise RuntimeError("extract job timed out")

    def run(self, tmp, size):
        archive = tmp / "gbk测试.zip"
        entries = [("中文文件.txt", "中文内容测试\n".encode()), ("子目录/数据.csv", b"a,b,c\n1,2,3\n")]
        make_zip(archive, [(name.encode("gbk"), data) for name, data in entries])
        status, body = self.api("/inspect", {"path": str(archive)})
        info = body.get("info", {})
        names = [entry["path"] for entry in info.get("entries", [])]
        self.check("GBK inspect HTTP 200", status == 200, status)
        self.check("GBK inspect 显示正确中文名", all(name in names for name, _ in entries), names)
        self.check("GBK inspect 标记 nameEncoding=gbk", info.get("nameEncoding") == "gbk", info.get("nameEncoding"))
        job = self.extract(archive, tmp)
        self.check("GBK 解压任务完成", job.get("status") in ("done", "ok"), job.get("error") or job.get("status"))
        dest = tmp / archive.stem
        self.check("GBK 解压落地中文名正确", all((dest / name).is_file() for name, _ in entries))
        self.check("GBK 解压内容一致", all((dest / name).is_file() and (dest / name).read_bytes() == data for name, data in entries))

        empty = tmp / "empty.zip"
        make_zip(empty, [])
        status, body = self.api("/inspect", {"path": str(empty)})
        self.check("空 zip inspect 正常", status == 200 and body.get("ok") is True, f"{status} counts={body.get('info', {}).get('counts')}")
        job = self.extract(empty, tmp)
        self.check("空 zip 解压任务完成", job.get("status") in ("done", "ok"), job.get("error") or job.get("status"))
        self.check("空 zip 解压建空目录", (tmp / "empty").is_dir() and not any((tmp / "empty").iterdir()))
        status, body = self.api("/extract", {"path": str(empty), "destDir": str(tmp)})
        self.check("重复解压返回 409 code=exists", status == 409 and body.get("code") == "exists", f"{status} {body.get('code')}")

        readonly = tmp / "readonly"
        readonly.mkdir()
        mode = stat.S_IMODE(readonly.stat().st_mode)
        archive = tmp / "ro.zip"
        make_zip(archive, [(b"file.txt", b"hello\n")])
        try:
            readonly.chmod(0o555)
            job = self.extract(archive, readonly)
            error = str(job.get("error") or "")
            self.check("只读目录解压任务失败或同步拒绝", job.get("status") in ("error", "failed", "rejected"), job.get("status"))
            self.check("只读目录返回中文权限提示", "权限" in error and not any(word in error for word in ("EACCES", "Errno", "OSError")), error[:120])
        finally:
            readonly.chmod(mode)

        self.space(tmp, size)
        archive = tmp / "swap.zip"
        for index, (name, data) in enumerate((("a.txt", b"AAAA\n" * 100), ("b.txt", b"B" * 7)), 1):
            make_zip(archive, [(name.encode(), data)])
            status, body = self.api("/inspect", {"path": str(archive)})
            names = [entry["path"] for entry in body.get("info", {}).get("entries", [])]
            self.check(f"同路径归档第 {index} 次 inspect 刷新", status == 200 and names == [name], names)
            destination = tmp / f"out{index}"
            job = self.extract(archive, destination)
            self.check(f"同路径归档第 {index} 次解压完成", job.get("status") in ("done", "ok"), job.get("status"))
            landed = destination / "swap" / name
            self.check(f"同路径归档第 {index} 次落地内容正确", landed.is_file() and landed.read_bytes() == data)
        archive = tmp / "gbk.zip"
        make_zip(archive, [("中文文件.txt".encode("gbk"), "内容\n".encode())])
        status, body = self.api("/inspect", {"path": str(archive)})
        info = body.get("info", {})
        names = [entry["path"] for entry in info.get("entries", [])]
        self.check("GBK inspect 中文名未回退", status == 200 and "中文文件.txt" in names and info.get("nameEncoding") == "gbk", names)

    def space(self, tmp, size):
        free = shutil.disk_usage(tmp).free
        detail = f"probe-size={size} bytes; free={free} bytes; margin=67108864 bytes"
        sufficient = size > free + 64 * 1024 * 1024
        self.check("空间探针大小足以安全触发预检", sufficient, detail + ("" if sufficient else "; 未发请求，请显式增大 --probe-size；默认仍为 50GB"))
        if not sufficient:
            return
        big = tmp / "big.bin"
        try:
            with big.open("wb") as stream:
                stream.truncate(size)
            big.chmod(0)
            try:
                with big.open("rb"):
                    pass
            except PermissionError:
                unreadable = True
            else:
                unreadable = False
            self.check("稀疏输入安全保护（不可读取且不分配大文件块）", unreadable and big.stat().st_blocks * 512 < 1024 * 1024,
                       f"logical={big.stat().st_size}; allocated={big.stat().st_blocks * 512}; unreadable={unreadable}")
            if not unreadable or big.stat().st_blocks * 512 >= 1024 * 1024:
                return
            status, body = self.api("/compress", {"inputs": [str(big)], "destDir": str(tmp), "format": "zip"})
            job_id = body.get("job", {}).get("id")
            if job_id:
                self.active_jobs.add(job_id)
            self.check("压缩大文件被空间预检同步拒绝且未提交任务", status != 200 and "磁盘剩余空间不足" in str(body.get("error")) and not job_id,
                       f"{status} {body.get('error', '')}")
        finally:
            if big.exists():
                big.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port-file", type=Path, default=ROOT / "build/port-menu.json")
    parser.add_argument("--probe-size", type=probe_size, default=probe_size("50GB"))
    args = parser.parse_args()
    probe = None
    try:
        probe = Probe(args.port_file)
        with tempfile.TemporaryDirectory(prefix="arca-accept-") as directory:
            try:
                probe.run(Path(directory), args.probe_size)
            finally:
                for job_id in probe.active_jobs:
                    probe.api("/job/cancel", {"id": job_id})
    except Exception as exc:
        if probe is None:
            print(f"FAIL acceptance setup [{type(exc).__name__}]", flush=True)
            print("0 passed, 1 failed")
            return 1
        probe.check("acceptance execution", False, f"{type(exc).__name__}: {exc}")
    passed = sum(probe.results)
    failed = len(probe.results) - passed
    print(f"{passed} passed, {failed} failed")
    return int(bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
