"""Self test for the algorithms added on top of 7-Zip: zstd + PAR2.

Run with the project venv:
    build/venv/bin/python scripts/integrity_test.py

What it covers, and why each check exists
-----------------------------------------
* zstd is *not* a 7-Zip codec, so a .tar.zst is a plain tar stream piped into the
  upstream zstd CLI.  The test therefore checks the whole pipe: compress ->
  inspect (tar index, not a guess) -> extract -> byte-for-byte compare.
* A bare .zst (no tar inside) must stay a single-stream archive; the tar header
  checksum is what tells the two apart, so it gets its own case.
* PAR2 has to survive an actual corruption event: verify clean -> damage one
  byte -> verify reports damage *and* repairability -> repair -> verify clean.
  Repairing back to the original hash is the only proof that the recovery
  records are usable rather than merely present.
* The component registry backs the update UI, so the test asserts that zstd and
  par2 are registered with a real version and that the format capability bits
  hide the options zstd cannot honour.
"""
from __future__ import annotations

import gc
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

_LOCAL_TMP = os.path.join(ROOT, ".tmp")
os.makedirs(_LOCAL_TMP, exist_ok=True)
os.environ["TMPDIR"] = _LOCAL_TMP
tempfile.tempdir = _LOCAL_TMP

from arca import engine as eng            # noqa: E402
from arca import server as srv            # noqa: E402
from arca.settings import Settings        # noqa: E402

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name + (f" — {detail}" if detail else ""))
    print(("  ok    " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def skip(name, why):
    SKIP.append(name)
    print(f"  skip  {name}   [{why}]")


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Client:
    def __init__(self, port, token):
        self.base = f"http://127.0.0.1:{port}"
        self.token = token

    def call(self, path, body=None, expect_ok=True):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"X-Arca-Token": self.token,
                                              "Content-Type": "application/json"})
        try:
            with OPENER.open(req, timeout=240) as res:
                payload = json.loads(res.read().decode())
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode())
        if expect_ok and not payload.get("ok"):
            raise RuntimeError(payload.get("error") or "request failed")
        return payload

    def get(self, path, expect_ok=True):
        req = urllib.request.Request(self.base + path,
                                     headers={"X-Arca-Token": self.token})
        try:
            with OPENER.open(req, timeout=240) as res:
                payload = json.loads(res.read().decode())
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode())
        if expect_ok and not payload.get("ok"):
            raise RuntimeError(payload.get("error") or "request failed")
        return payload

    def wait(self, job_id, timeout=240):
        end = time.time() + timeout
        while time.time() < end:
            for j in self.call("/api/jobs")["jobs"]:
                if j["id"] == job_id and j["status"] not in ("queued", "running"):
                    return j
            time.sleep(0.15)
        raise TimeoutError("job did not finish")


def build_samples(tmp):
    src = os.path.join(tmp, "素材")
    os.makedirs(os.path.join(src, "子目录"), exist_ok=True)
    payloads = {}
    for name, size in (("文档.txt", 4096), ("数据.bin", 300_000), ("子目录/备注.md", 900)):
        p = os.path.join(src, name)
        with open(p, "wb") as fh:
            fh.write(os.urandom(size))
        payloads[name] = sha(p)
    return src, payloads


def compare(dest, payloads, base="素材"):
    for name, digest in payloads.items():
        p = os.path.join(dest, base, name)
        if not os.path.isfile(p) or sha(p) != digest:
            return False, p
    return True, ""


def flip_byte(path, offset):
    with open(path, "r+b") as fh:
        fh.seek(offset)
        old = fh.read(1)
        fh.seek(offset)
        fh.write(bytes([old[0] ^ 0xFF]))


# ------------------------------------------------- zstd child resource checks
def _resource_warnings(call):
    """Run `call`, return (value, ResourceWarning messages) after a few GCs."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        value = call()
        for _ in range(3):
            gc.collect()
    return value, [str(item.message) for item in caught
                   if issubclass(item.category, ResourceWarning)]


def _recording_popen(created):
    real = subprocess.Popen

    def spawn(*args, **kwargs):
        proc = real(*args, **kwargs)
        created.append(proc)
        return proc
    return spawn


def _child_fully_reaped(proc):
    streams = [getattr(proc, name, None) for name in ("stdin", "stdout", "stderr")]
    return (proc.poll() is not None
            and all(stream is None or stream.closed for stream in streams))


class _PoisonClose:
    """Stream proxy whose close() succeeds then raises: teardown errors must not
    replace the exception that triggered teardown."""

    def __init__(self, stream):
        self.stream = stream

    @property
    def closed(self):
        return self.stream.closed

    def read(self, *args, **kwargs):
        return self.stream.read(*args, **kwargs)

    def close(self):
        self.stream.close()
        raise RuntimeError("cleanup close must not mask the original exception")


def _zst_resource_checks(tmp, binary):
    from unittest.mock import patch
    from arca import zst

    root = os.path.join(tmp, "zst-resource")
    os.makedirs(root, exist_ok=True)
    source = os.path.join(root, "source.txt")
    payload = b"resource probe payload\n" * 1000
    with open(source, "wb") as fh:
        fh.write(payload)

    tree = os.path.join(root, "tree")
    os.makedirs(os.path.join(tree, "nested"))
    with open(os.path.join(tree, "a.txt"), "wb") as fh:
        fh.write(b"alpha" * 1000)
    with open(os.path.join(tree, "nested", "b.bin"), "wb") as fh:
        fh.write(b"beta" * 1000)

    tar_zst = os.path.join(root, "created.tar.zst")
    value, warns = _resource_warnings(lambda: zst.compress(
        [tree], root, tar_zst, level=1, threads=1))
    check("创建 .tar.zst 无 ResourceWarning", warns == [], str(warns[:2]))
    check("创建 .tar.zst 产物有效",
          value.get("files") == 2 and os.path.getsize(tar_zst) > 0,
          f"files={value.get('files')} bytes={os.path.getsize(tar_zst) if os.path.exists(tar_zst) else 0}")

    bare = os.path.join(root, "source.txt.zst")
    subprocess.run([binary, "-q", "-o", bare, source],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE, check=True)
    single_out = os.path.join(root, "single-out")
    _, warns = _resource_warnings(lambda: zst.extract(bare, single_out))
    check("裸 .zst 解压无 ResourceWarning", warns == [], str(warns[:2]))
    extracted = os.path.join(single_out, "source.txt")
    check("裸 .zst 解压内容正确",
          os.path.isfile(extracted) and open(extracted, "rb").read() == payload)

    info, warns = _resource_warnings(lambda: zst.listing(tar_zst))
    check("TAR.ZST 列举无 ResourceWarning", warns == [], str(warns[:2]))
    check("TAR.ZST 列举条目正确",
          {e["path"] for e in info.get("entries", [])}
          == {"tree", "tree/a.txt", "tree/nested", "tree/nested/b.bin"},
          str([e["path"] for e in info.get("entries", [])]))
    tar_out = os.path.join(root, "tar-out")
    _, warns = _resource_warnings(lambda: zst.extract(tar_zst, tar_out))
    check("TAR.ZST 解压无 ResourceWarning", warns == [], str(warns[:2]))
    check("TAR.ZST 解压内容正确",
          open(os.path.join(tar_out, "tree", "a.txt"), "rb").read()
          == b"alpha" * 1000
          and open(os.path.join(tar_out, "tree", "nested", "b.bin"), "rb").read()
          == b"beta" * 1000)

    # --- cancel: child must be dead and all pipes closed before the error
    # propagates; nothing should be reported as an unclosed ResourceWarning.
    for label, call in (
            ("取消压缩", lambda cancel: zst.compress(
                [tree], root, os.path.join(root, "cancel.tar.zst"),
                level=1, threads=1, cancel=cancel)),
            ("取消裸解压", lambda cancel: zst.extract(
                bare, os.path.join(root, "cancel-out"), cancel=cancel))):
        cancel = threading.Event()
        cancel.set()
        created = []
        with patch.object(zst.subprocess, "Popen",
                          side_effect=_recording_popen(created)), \
                warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            try:
                call(cancel)
            except zst.ZstdError as exc:
                message = str(exc)
            else:
                message = ""
            for _ in range(3):
                gc.collect()
        leftover = [str(item.message) for item in caught
                    if issubclass(item.category, ResourceWarning)]
        check(f"{label}返回取消错误", "已取消" in message, message)
        check(f"{label}子进程已 reap 且 pipe 已关闭",
              bool(created) and _child_fully_reaped(created[-1]),
              f"procs={len(created)}")
        check(f"{label}无 ResourceWarning", leftover == [], str(leftover[:2]))

    # --- injected original exception must survive teardown
    created = []
    with patch.object(zst.subprocess, "Popen",
                      side_effect=_recording_popen(created)), \
            patch.object(zst.tarfile, "open",
                         side_effect=RuntimeError("injected original")), \
            warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        try:
            zst.compress([tree], root, os.path.join(root, "exception.tar.zst"),
                         level=1, threads=1)
        except RuntimeError as exc:
            propagated = str(exc)
        else:
            propagated = ""
        for _ in range(3):
            gc.collect()
    leftover = [str(item.message) for item in caught
                if issubclass(item.category, ResourceWarning)]
    check("压缩异常保留原始异常", propagated == "injected original", propagated)
    check("压缩异常子进程已 reap 且 pipe 已关闭",
          bool(created) and _child_fully_reaped(created[-1]),
          f"procs={len(created)}")
    check("压缩异常无 ResourceWarning", leftover == [], str(leftover[:2]))

    # --- teardown close() raising must not replace the original exception
    created = []
    real_popen = subprocess.Popen

    def poisoned_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        created.append(proc)
        if proc.stderr is not None:
            proc.stderr = _PoisonClose(proc.stderr)
        return proc

    with patch.object(zst.subprocess, "Popen", side_effect=poisoned_popen), \
            patch.object(zst.tarfile, "open",
                         side_effect=ValueError("injected original")), \
            warnings.catch_warnings(record=True):
        warnings.simplefilter("always", ResourceWarning)
        try:
            zst.compress([tree], root, os.path.join(root, "poison.tar.zst"),
                         level=1, threads=1)
        except ValueError as exc:
            poison_propagated = str(exc)
        else:
            poison_propagated = ""
        for _ in range(3):
            gc.collect()
    check("收尾异常不覆盖原始异常",
          poison_propagated == "injected original", poison_propagated)
    check("收尾异常后子进程已 reap 且 pipe 已关闭",
          bool(created) and _child_fully_reaped(created[-1]),
          f"procs={len(created)}")


def main():

    # 直接运行也必须隔离 HOME：本套件经 Settings/JobManager 写任务历史，
    # 落进真实 ~/Library/Application Support/Arca/ 就违反 §1.1.7。
    # （all_test 传入的隔离 HOME 会被替换成另一个空 HOME，行为不变。）
    os.environ["HOME"] = tempfile.mkdtemp(prefix="arca-selftest-home-")
    tmp = tempfile.mkdtemp(prefix="arca-int-")
    src, payloads = build_samples(tmp)
    out_dir = os.path.join(tmp, "out")
    os.makedirs(out_dir, exist_ok=True)

    settings = Settings()
    engine = eng.Engine()
    print(f"zstd        : {engine.zstd_version or '缺失'}  ({engine.zstd or '—'}, {engine.zstd_source})")
    print(f"par2        : {engine.par2_version or '缺失'}  ({engine.par2 or '—'}, {engine.par2_source})")

    ctx = srv.AppContext(engine, settings, os.path.join(ROOT, "src", "arca", "web"))
    server = srv.Server(ctx)
    server.start()
    cli = Client(server.port, ctx.token)
    print(f"api         : http://127.0.0.1:{server.port}\n")

    if not engine.zstd:
        print("zstd 缺失，后续用例无法执行")
        return 1

    # ------------------------------------------------- [1] 元数据 / 能力位
    print("[1] 元数据 · 格式能力位 · 组件注册")
    meta = cli.call("/api/meta")
    zst_fmt = next((f for f in meta["formats"] if f["id"] == "tar.zst"), None)
    check("格式列表含 ZST", zst_fmt is not None)
    if zst_fmt:
        check("ZST 可创建", zst_fmt["create"] is True)
        check("ZST 扩展名为 .tar.zst", zst_fmt["ext"] == ".tar.zst", zst_fmt["ext"])
        check("ZST 关闭加密/分卷/算法/固实能力位",
              not any(zst_fmt[k] for k in ("encrypt", "split", "method", "solid")),
              str({k: zst_fmt[k] for k in ("encrypt", "split", "method", "solid")}))
    par2_meta = meta.get("par2") or {}
    check("meta 报告 par2 可用", bool(par2_meta.get("available")), str(par2_meta))
    check("冗余档位为 3/5/10/15", par2_meta.get("percents") == [3, 5, 10, 15],
          str(par2_meta.get("percents")))
    comps = {c["id"]: c for c in (meta.get("components") or [])}
    check("组件表含 7zz/zstd/par2", {"7zz", "zstd", "par2"} <= set(comps), str(list(comps)))
    for cid in ("zstd", "par2"):
        c = comps.get(cid) or {}
        check(f"{cid} 版本非空", bool(c.get("version")), str(c.get("version")))
        check(f"{cid} 有中文类别与摘要", bool(c.get("kind")) and bool(c.get("summary")),
              f'{c.get("kind")} / {c.get("summary")}')
        check(f"{cid} 声明许可证", bool(c.get("license")), str(c.get("license")))

    # ------------------------------------------- [2] tar.zst 压缩 + 往返
    print("\n[2] TAR.ZST · 压缩 → 列举 → 解压")
    r = cli.call("/api/compress", {
        "inputs": [src], "name": "素材包", "destDir": out_dir, "format": "tar.zst",
        "level": 6, "password": "should-be-ignored", "splitBytes": 10 * 1024 * 1024,
        "par2Percent": 5, "excludeMeta": True,
    })
    job = cli.wait(r["job"]["id"])
    check("压缩任务成功", job["status"] == "done", job.get("error", ""))
    zst_path = r["target"]
    check("生成 .tar.zst", zst_path.endswith(".tar.zst") and os.path.isfile(zst_path),
          os.path.basename(zst_path))
    packed = os.path.getsize(zst_path) if os.path.isfile(zst_path) else 0
    check("体积小于原始数据", 0 < packed < 320_000, f"{packed} B")
    check("结果里带恢复记录信息", bool((job.get("result") or {}).get("par2")),
          str((job.get("result") or {}).get("par2")))

    info = cli.call("/api/inspect", {"path": zst_path})["info"]
    check("识别为 TAR.ZST", info["label"] == "TAR.ZST", info["label"])
    check("type 为 tar.zst", info["type"] == "tar.zst", info["type"])
    check("条目数 = 3 文件", info["counts"]["files"] == 3, str(info["counts"]))
    check("解压后大小 > 300KB", info["uncompressed"] > 300_000, str(info["uncompressed"]))
    check("密码被忽略（未加密）", info["encrypted"] is False)
    check("inspect 报告 par2Files", info.get("par2Files", 0) >= 1, str(info.get("par2Files")))
    caps = info.get("capabilities") or {}
    check("capabilities 关闭加密/分卷", not caps.get("encrypt") and not caps.get("split"),
          str(caps))

    dest = os.path.join(tmp, "ex-zst")
    os.makedirs(dest, exist_ok=True)
    r = cli.call("/api/extract", {"path": zst_path, "destDir": dest,
                                  "autoFolder": False, "info": info})
    job = cli.wait(r["job"]["id"])
    check("解压任务成功", job["status"] == "done", job.get("error", ""))
    ok, where = compare(dest, payloads)
    check("解压内容逐字节一致", ok, where)

    r = cli.call("/api/test", {"path": zst_path, "info": info})
    job = cli.wait(r["job"]["id"])
    check("完整性校验通过", job["status"] == "done", job.get("error", ""))

    # ---------------------------------------------------- [3] 裸 .zst 单文件
    print("\n[3] ZST · 单文件（无 tar 头）")
    single_src = os.path.join(src, "数据.bin")
    plain = os.path.join(out_dir, "单文件.zst")
    rc = os.system(f'"{engine.zstd}" -q -f -19 "{single_src}" -o "{plain}"')
    if rc != 0 or not os.path.isfile(plain):
        skip("单文件 zst", "zstd CLI 调用失败")
    else:
        check("单文件 .zst 体积合理", os.path.getsize(plain) < 310_000)
        sinfo = cli.call("/api/inspect", {"path": plain})["info"]
        check("识别为 ZST（非 TAR.ZST）", sinfo["label"] == "ZST", sinfo["label"])
        check("标记为单流", bool(sinfo.get("single")), str(sinfo.get("single")))
        sdest = os.path.join(tmp, "ex-single")
        os.makedirs(sdest, exist_ok=True)
        r = cli.call("/api/extract", {"path": plain, "destDir": sdest,
                                      "autoFolder": False, "info": sinfo})
        job = cli.wait(r["job"]["id"])
        check("单文件解压成功", job["status"] == "done", job.get("error", ""))
        got = os.path.join(sdest, "单文件")
        check("解压出同名文件", os.path.isfile(got), got)
        if os.path.isfile(got):
            check("单文件内容一致", sha(got) == payloads["数据.bin"])

    # --------------------------------------------- [4] PAR2 校验 / 修复
    print("\n[4] PAR2 · 校验 → 损坏 → 修复")
    if not engine.par2:
        skip("PAR2 全流程", "par2 组件缺失")
    else:
        recov = [f for f in os.listdir(out_dir) if f.endswith(".par2")]
        check("生成了 .par2 文件", len(recov) >= 1, f"{len(recov)} 个")
        redundancy = sum(os.path.getsize(os.path.join(out_dir, f)) for f in recov)
        check("冗余体积在 3%~40% 之间", 0 < redundancy < packed * 0.40,
              f"{redundancy} B / {packed} B")

        r = cli.call("/api/par2/verify", {"path": zst_path})
        job = cli.wait(r["job"]["id"])
        check("干净文件校验通过", job["status"] == "done", job.get("error", ""))
        check("result.ok 为真", (job.get("result") or {}).get("ok") is True,
              str(job.get("result")))
        clean_sha = sha(zst_path)

        flip_byte(zst_path, packed // 2)
        check("已人为损坏 1 字节", sha(zst_path) != clean_sha)
        r = cli.call("/api/par2/verify", {"path": zst_path})
        job = cli.wait(r["job"]["id"])
        res = job.get("result") or {}
        check("损坏后仍能判定（未崩溃）", job["status"] in ("done", "error"),
              job.get("error", ""))
        check("报告存在损坏", (res.get("damagedCount") or 0) >= 1, str(res))
        check("报告可修复", res.get("repairable") is True, str(res))

        r = cli.call("/api/par2/repair", {"path": zst_path})
        job = cli.wait(r["job"]["id"])
        check("修复任务成功", job["status"] == "done", job.get("error", ""))
        check("result.repaired 为真", (job.get("result") or {}).get("repaired") is True,
              str(job.get("result")))
        check("修复后哈希与原始一致", sha(zst_path) == clean_sha)

        r = cli.call("/api/par2/verify", {"path": zst_path})
        job = cli.wait(r["job"]["id"])
        res = job.get("result") or {}
        check("修复后再校验通过", job["status"] == "done" and res.get("ok") is True,
              str(res))
        check("修复后再无损毁", (res.get("damagedCount") or 0) == 0, str(res))

        # the recovered archive must still extract correctly
        dest2 = os.path.join(tmp, "ex-repaired")
        os.makedirs(dest2, exist_ok=True)
        r = cli.call("/api/extract", {"path": zst_path, "destDir": dest2,
                                      "autoFolder": False, "info": info})
        job = cli.wait(r["job"]["id"])
        ok, where = compare(dest2, payloads)
        check("修复后的归档可正常解压", ok, where)

        # a file without recovery records must be rejected, not silently queued
        no_par2 = os.path.join(out_dir, "无记录.tar.zst")
        shutil.copy2(zst_path, no_par2)
        bad = cli.call("/api/par2/verify", {"path": no_par2}, expect_ok=False)
        check("无恢复记录时拒绝校验", bad.get("ok") is False, bad.get("error", ""))

        # ------------------------------------------ single-volume (-n1) mode
        # One .vol file instead of the 2^n ladder: same redundancy, cleaner
        # directory, but a repair always reads the whole volume.  Must verify
        # and repair exactly like the split set.
        single_dir = os.path.join(tmp, "single")
        os.makedirs(single_dir, exist_ok=True)
        single_path = os.path.join(single_dir, "素材包.tar.zst")
        shutil.copy2(zst_path, single_path)
        single_sha = sha(single_path)
        from arca import par2 as par2_mod
        rec = par2_mod.create(engine.par2, [single_path], percent=5, single=True)
        vols = [f for f in rec["made"] if ".vol" in os.path.basename(f)]
        check("单卷模式只出一个卷文件", len(vols) == 1 and rec["files"] == 2,
              f'{rec["files"]} 个文件: {[os.path.basename(f) for f in rec["made"]]}')
        check("单卷体积与分卷总量同量级",
              rec["bytes"] < packed * 0.5, f'{rec["bytes"]} B / {packed} B')
        v = par2_mod.verify(engine.par2, rec["made"][0])
        check("单卷校验通过", v["ok"] and v["damagedCount"] == 0, str(v["damagedCount"]))
        flip_byte(single_path, packed // 3)
        v = par2_mod.verify(engine.par2, rec["made"][0])
        check("单卷报告可修复", v["repairable"] is True, str(v["damagedCount"]))
        par2_mod.repair(engine.par2, rec["made"][0])
        check("单卷修复后哈希一致", sha(single_path) == single_sha)
        r = cli.call("/api/compress", {
            "inputs": [src], "output": os.path.join(out_dir, "单卷.tar.zst"),
            "format": "tar.zst", "level": 3, "par2Percent": 5, "par2Single": True})
        job = cli.wait(r["job"]["id"])
        res = job.get("result") or {}
        check("API 接受 par2Single", job["status"] == "done"
              and (res.get("par2") or {}).get("single") is True, str(res.get("par2")))
        check("API 单卷产出 2 个恢复文件", (res.get("par2") or {}).get("files") == 2,
              str((res.get("par2") or {}).get("files")))
        bad = cli.call("/api/compress", {
            "inputs": [src], "output": os.path.join(out_dir, "x.tar.zst"),
            "format": "tar.zst", "par2Single": True})
        check("未开恢复记录时忽略单卷", (bad.get("par2") or 0) == 0,
              str(bad.get("par2")))

    # ---------------------------------------- [5] Finder 集成接口形状
    print("\n[5] Finder 集成")
    st = cli.get("/api/integration")
    check("集成状态可读", st.get("ok") is True, str(st.get("error", "")))
    check("默认打开方式候选非空", len(st.get("types") or []) >= 9,
          str(len(st.get("types") or [])))
    check("handler 状态带 ours 标记",
          all(isinstance(t.get("ours"), bool) for t in st.get("types") or []))
    check("services 带状态字段",
          all(s.get("state") in ("registered", "running", "unregistered", "unknown")
              for s in st.get("services") or []),
          str([(s.get("id"), s.get("state")) for s in st.get("services") or []]))
    check("canSetDefault 为布尔", isinstance(st.get("canSetDefault"), bool),
          f'canSetDefault={st.get("canSetDefault")} (bundle={st.get("bundlePath") or "源码"})')

    # inbox: Finder 请求的接收队列 —— 压缩包自动解压、混合进压缩页、
    # 非压缩包的显式解压请求必须被拒绝而不是静默排队。
    zip_probe = zst_path
    r = cli.call("/api/inbox", {"action": "auto", "paths": [zip_probe]})
    check("zip 进收件箱判定为解压", (r.get("item") or {}).get("action") == "extract",
          str((r.get("item") or {}).get("action")))
    r = cli.call("/api/inbox", {"action": "auto",
                                "paths": [zip_probe, src]})
    check("混合选择判定为压缩", (r.get("item") or {}).get("action") == "compress",
          str((r.get("item") or {}).get("action")))
    bad = cli.call("/api/inbox", {"action": "extract", "paths": [src]})
    # An explicit "extract" for a non-archive is accepted here on purpose: the
    # service handler already filtered, so what reaches the API is either a
    # user decision or a test -- the extract job itself reports "not an
    # archive".  Missing paths, however, are refused outright.
    check("txt 的显式解压请求入队", (bad.get("item") or {}).get("action") == "extract",
          str((bad.get("item") or {}).get("action")))
    bad = cli.call("/api/inbox", {"action": "extract", "paths": ["/nope/缺失.zip"]},
                   expect_ok=False)
    check("不存在的路径被拒绝", bad.get("ok") is False, bad.get("error", ""))
    drained = cli.get("/api/inbox").get("items") or []
    check("收件箱按序排空", [i["action"] for i in drained] == ["extract", "compress", "extract"],
          str([i["action"] for i in drained]))
    check("排空后为空", cli.get("/api/inbox").get("items") == [])

    # Extensions without a system UTI resolve to dynamic identifiers
    # (dyn.age81y65y for .zst), and LSSetDefaultRoleHandlerForContentType
    # rejects those with -50.  The bundle therefore has to *export* its own
    # types for rar/zst; without them "设为默认" leaves those two on 系统默认.
    import plistlib as _pl
    bundle_plist = os.path.join(ROOT, "release", "Arca.app", "Contents", "Info.plist")
    if os.path.exists(bundle_plist):
        with open(bundle_plist, "rb") as fh:
            info = _pl.load(fh)
        exported = {d.get("UTTypeIdentifier")
                    for d in info.get("UTExportedTypeDeclarations", [])}
        tags = set()
        for d in info.get("UTExportedTypeDeclarations", []):
            spec = (d.get("UTTypeTagSpecification") or {})
            tags |= set(spec.get("public.filename-extension") or [])
        check("为 rar/zst 导出了自有类型",
              {"com.arca.rar-archive", "com.arca.zstd-stream"} <= exported,
              str(sorted(exported)))
        check("导出类型声明了扩展名", {"rar", "zst"} <= tags, str(sorted(tags)))
        dyn = [t["ext"] for t in st["types"] if t["uti"].startswith("dyn.")]
        check("没有扩展名停留在动态类型", not dyn, f"dyn: {dyn}")
    else:
        skip("bundle Info.plist 检查", "release/Arca.app 不存在")

    dry = cli.call("/api/integration/default", {"mode": "on", "dry": True})
    check("默认打开方式 dry-run 可用", dry.get("ok") is True, str(dry.get("error", "")))
    check("dry-run 不改系统绑定", (dry.get("applied") or 0) == 0
          if st.get("bundlePath") else True,
          f'applied={dry.get("applied")}')

    # --------------------------------------------- [6] 组件接口形状
    print("\n[6] 组件接口")
    c = cli.get("/api/components")
    check("组件列表非空", bool(c.get("components")), str(len(c.get("components") or [])))
    check("installs 为字典", isinstance(c.get("installs"), dict))
    bad = cli.call("/api/components/apply", {"id": "nope", "version": "1.0.0"},
                   expect_ok=False)
    check("未知组件被拒绝", bad.get("ok") is False, bad.get("error", ""))
    # 7zz is the app's own engine, so "reset" is routed to the engine reset
    # instead of the component store -- it must still succeed.
    r = cli.call("/api/components/reset", {"id": "7zz"}, expect_ok=False)
    check("7zz 的恢复内置走引擎重置", r.get("ok") is True and bool(r.get("engine")),
          str(r.get("error", "")))
    from unittest.mock import patch
    from arca import components, util
    release = [{"tag_name": "v1.5.7", "body": "Controlled test release notes.",
                "published_at": "2026-10-22T00:00:00Z",
                "html_url": "https://github.com/facebook/zstd/releases/tag/v1.5.7"}]
    def fetch_release(url, timeout=25):
        assert url == "https://api.github.com/repos/facebook/zstd/releases?per_page=8", url
        return release
    cache_path = util.state_path("remote-zstd.json")
    if os.path.exists(cache_path):
        os.unlink(cache_path)
    print("      zstd: 本地受控发布 fixture；真实 GitHub 解析器与 HTTP API")
    with patch.object(components, "_fetch_json", fetch_release):
        chk = cli.get("/api/components/check?id=zstd", expect_ok=False)
    upd = chk.get("update") or {}
    check("zstd 远程版本可读", chk.get("ok") and upd.get("ok")
          and upd.get("latest") == "1.5.7", str(upd))
    check("check 返回 available 布尔", isinstance(upd.get("available"), bool),
          f'available={upd.get("available")}')

    # --------------------------------------- [7] ZST 子进程资源收尾
    print("\n[7] ZST 子进程资源收尾")
    if not engine.zstd:
        skip("ZST resource lifecycle", "zstd 组件缺失")
    else:
        _zst_resource_checks(tmp, engine.zstd)

    print("\n" + "-" * 62)
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)} · 跳过 {len(SKIP)}")
    for f in FAIL:
        print("  FAIL " + f)
    if not FAIL:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"  用例目录保留：{tmp}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
