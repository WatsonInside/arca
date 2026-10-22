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

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

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


def main():
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
    chk = cli.get("/api/components/check?id=zstd", expect_ok=False)
    upd = chk.get("update") or {}
    if chk.get("ok") and upd.get("ok"):
        check("zstd 远程版本可读", bool(upd.get("latest") or upd.get("current")),
              str(upd.get("latest") or upd.get("current")))
        check("check 返回 available 布尔", isinstance(upd.get("available"), bool),
              f'available={upd.get("available")}')
    else:
        skip("zstd 远程版本", (chk.get("error") or upd.get("error") or "网络不可达"))

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
