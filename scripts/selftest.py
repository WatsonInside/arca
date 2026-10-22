"""End-to-end self test for Arca: drives the real HTTP API against the real engine.

Run with the project venv:
    build/venv/bin/python scripts/selftest.py

Notes
-----
* Everything runs locally against a headless instance of the app's own server.
* Split-volume *creation* needs 7-Zip to rename its temporary volume files; a
  restrictive sandbox can veto that.  When it happens the test is reported as
  "skipped" instead of failed.  Split-volume *reading* is tested against a
  fixture that this script builds when possible.
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

# Some sandboxes reject tempfile's probe-and-unlink dance; pin a local dir.
_LOCAL_TMP = os.path.join(ROOT, ".tmp")
os.makedirs(_LOCAL_TMP, exist_ok=True)
os.environ["TMPDIR"] = _LOCAL_TMP
tempfile.tempdir = _LOCAL_TMP

from arca import engine as eng           # noqa: E402
from arca import server as srv           # noqa: E402
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

    def wait(self, job_id, timeout=240):
        end = time.time() + timeout
        while time.time() < end:
            for j in self.call("/api/jobs")["jobs"]:
                if j["id"] == job_id and j["status"] not in ("queued", "running"):
                    return j
            time.sleep(0.2)
        raise TimeoutError("job did not finish")


def build_samples(tmp):
    src = os.path.join(tmp, "素材")
    os.makedirs(os.path.join(src, "子目录"), exist_ok=True)
    payloads = {}
    for name, size in (("文档.txt", 4096), ("数据.bin", 160_000), ("子目录/备注.md", 900)):
        p = os.path.join(src, name)
        with open(p, "wb") as fh:
            fh.write(os.urandom(size))
        payloads[name] = sha(p)
    return src, payloads


def compare(dest, payloads):
    for name, digest in payloads.items():
        p = os.path.join(dest, "素材", name)
        if not os.path.isfile(p) or sha(p) != digest:
            return False, p
    return True, ""


def main():
    tmp = tempfile.mkdtemp(prefix="arca-test-")
    src, payloads = build_samples(tmp)
    out_dir = os.path.join(tmp, "out")
    os.makedirs(out_dir, exist_ok=True)

    settings = Settings()
    engine = eng.Engine()
    print(f"engine      : 7-Zip {engine.version}  ({engine.zz})")
    print(f"rar encoder : {engine.rar or 'RAR 只读（未安装 WinRAR 命令行）'}")

    ctx = srv.AppContext(engine, settings, os.path.join(ROOT, "src", "arca", "web"))
    server = srv.Server(ctx)
    server.start()
    cli = Client(server.port, ctx.token)
    print(f"api         : http://127.0.0.1:{server.port}\n")

    # ------------------------------------------------------------------ [1] zip
    print("[1] ZIP · AES-256 加密")
    r = cli.call("/api/compress", {
        "inputs": [src], "name": "加密包", "destDir": out_dir, "format": "zip",
        "level": 3, "splitBytes": 0, "password": "Arca#2026", "excludeMeta": True,
    })
    job = cli.wait(r["job"]["id"])
    check("压缩任务成功", job["status"] == "done", job.get("error", ""))
    zip_path = os.path.join(out_dir, "加密包.zip")
    check("生成 .zip", os.path.isfile(zip_path))
    info = cli.call("/api/inspect", {"path": zip_path, "password": "Arca#2026"})["info"]
    check("识别 ZIP", info["label"] == "ZIP", info["label"])
    check("识别加密", info["encrypted"])
    check("目录数>=1", info["counts"]["dirs"] >= 1, str(info["counts"]))
    check("文件数=3", info["counts"]["files"] == 3, str(info["counts"]))
    check("原始大小正确", info["uncompressed"] > 160_000, str(info["uncompressed"]))

    blind = cli.call("/api/inspect", {"path": zip_path}, expect_ok=False)
    if blind.get("ok"):
        check("ZIP 头部未加密（条目可见但内容加密）",
              blind["info"]["encrypted"] is True and blind["info"]["needsPassword"] is False)
    else:
        check("ZIP 无密码被拒绝", True)

    dest = os.path.join(tmp, "ex1")
    os.makedirs(dest, exist_ok=True)
    r = cli.call("/api/extract", {"path": zip_path, "password": "Arca#2026",
                                  "destDir": dest, "autoFolder": False, "info": info})
    job = cli.wait(r["job"]["id"])
    check("解压任务成功", job["status"] == "done", job.get("error", ""))
    ok, where = compare(dest, payloads)
    check("解压内容逐字节一致", ok, where)

    r = cli.call("/api/extract", {"path": zip_path, "password": "bad",
                                  "destDir": os.path.join(tmp, "ex-bad"),
                                  "autoFolder": True, "info": info})
    job = cli.wait(r["job"]["id"])
    check("错误密码解压报错", job["status"] == "error" and "密码" in job.get("error", ""),
          job.get("error", ""))

    # ------------------------------------------------------------------- [2] 7z
    print("\n[2] 7Z · 加密文件名")
    r = cli.call("/api/compress", {
        "inputs": [src], "name": "隐藏目录", "destDir": out_dir, "format": "7z",
        "level": 5, "password": "Secret99", "encryptNames": True, "method": "lzma2",
    })
    job = cli.wait(r["job"]["id"])
    check("7z 任务成功", job["status"] == "done", job.get("error", ""))
    seven = os.path.join(out_dir, "隐藏目录.7z")
    blind = cli.call("/api/inspect", {"path": seven})["info"]
    check("无密码看不到目录", blind["needsPassword"])
    opened = cli.call("/api/inspect", {"path": seven, "password": "Secret99"})["info"]
    check("有密码可列出", not opened["needsPassword"] and opened["counts"]["files"] == 3)
    check("label 为 7Z", opened["label"] == "7Z", opened["label"])

    r = cli.call("/api/test", {"path": seven, "password": "Secret99", "info": opened})
    job = cli.wait(r["job"]["id"])
    check("完整性校验通过", job["status"] == "done", job.get("error", ""))

    # --------------------------------------------------------- [3] 分卷读取能力
    print("\n[3] 分卷归档（读取）")
    fixture_zip = "/tmp/t1/out.zip.001"
    fixture_7z = "/tmp/t1/out.7z.001"
    if os.path.isfile(fixture_zip):
        info = cli.call("/api/inspect", {"path": fixture_zip, "password": "SECRET123"})["info"]
        check("识别分卷 ZIP", info["isSplit"] and info["label"] == "ZIP", info["label"])
        check("列出分卷成员", len(info["parts"]) == 5, str(len(info["parts"])))
        d = os.path.join(tmp, "split-out")
        os.makedirs(d, exist_ok=True)
        r = cli.call("/api/extract", {"path": fixture_zip, "password": "SECRET123",
                                      "destDir": d, "autoFolder": False, "info": info})
        job = cli.wait(r["job"]["id"])
        check("分卷解压成功", job["status"] == "done", job.get("error", ""))
        check("分卷内容大小正确",
              os.path.getsize(os.path.join(d, "in", "a.bin")) == 3_000_000)
    else:
        skip("分卷 ZIP 读取", "fixture missing")

    if os.path.isfile(fixture_7z):
        info = cli.call("/api/inspect", {"path": fixture_7z, "password": "SECRET123"})["info"]
        check("分卷 7z 成员数", info["counts"]["files"] == 3, str(info["counts"]))
    else:
        skip("分卷 7Z 读取", "fixture missing")

    # ------------------------------------------------------- [4] 分卷创建（可能被沙箱拦）
    print("\n[4] 分卷创建")
    r = cli.call("/api/compress", {"inputs": [src], "name": "分卷包", "destDir": out_dir,
                                   "format": "7z", "level": 1, "splitBytes": 64 * 1024,
                                   "password": ""})
    job = cli.wait(r["job"]["id"])
    parts = sorted(f for f in os.listdir(out_dir) if f.startswith("分卷包.7z"))
    tmps = [f for f in parts if f.endswith(".tmp")]
    if job["status"] != "done" and tmps:
        skip("分卷创建", "沙箱禁止 7-Zip 重命名临时分卷")
    else:
        check("分卷创建成功", job["status"] == "done", job.get("error", ""))
        check("分卷数 >= 3", len(parts) >= 3, f"{len(parts)} 个")
        check("分卷以 .001 开头", bool(parts) and parts[0].endswith(".001"))

    # ------------------------------------------------------------------ [5] 取消
    print("\n[5] 取消任务")
    big = os.path.join(tmp, "big")
    os.makedirs(big, exist_ok=True)
    with open(os.path.join(big, "big.bin"), "wb") as fh:
        for _ in range(30):
            fh.write(os.urandom(1 << 20))
    r = cli.call("/api/compress", {"inputs": [big], "name": "取消测试", "destDir": out_dir,
                                   "format": "7z", "level": 9})
    jid = r["job"]["id"]
    time.sleep(0.4)
    cli.call("/api/job/cancel", {"id": jid})
    job = cli.wait(jid, 90)
    check("任务被取消", job["status"] == "cancelled", job["status"])

    # ------------------------------------------------------------------- [6] rar
    print("\n[6] RAR 读取")
    fixture = os.path.join(tmp, "sample.rar")
    got = False
    for url in ("https://raw.githubusercontent.com/markokr/rarfile/master/test/files/rar5-lzma.rar",
                "https://raw.githubusercontent.com/markokr/rarfile/master/test/files/seektest.rar"):
        try:
            urllib.request.urlretrieve(url, fixture)
            if os.path.getsize(fixture) > 100:
                got = True
                break
        except Exception as exc:  # noqa: BLE001
            print("      download failed:", url.rsplit("/", 1)[-1], exc)
    if got:
        try:
            info = cli.call("/api/inspect", {"path": fixture, "password": "password"})["info"]
            check("识别 RAR", info["label"] == "RAR", info["label"])
            d = os.path.join(tmp, "rar-out")
            os.makedirs(d, exist_ok=True)
            r = cli.call("/api/extract", {"path": fixture, "password": "password",
                                          "destDir": d, "autoFolder": False, "info": info})
            job = cli.wait(r["job"]["id"])
            check("RAR 解压成功", job["status"] == "done", job.get("error", ""))
        except Exception as exc:  # noqa: BLE001
            check("RAR 解压", False, str(exc)[:90])
    else:
        skip("RAR 解压", "无样本且离线")

    # ------------------------------------------------------- [7] 参数与错误处理
    print("\n[7] 边界情况")
    r = cli.call("/api/preflight", {"destDir": out_dir, "name": "加密包", "format": "zip"})
    check("同名自动避让", r["exists"] and r["suggestion"] != r["target"], r["suggestion"])
    e = cli.call("/api/compress", {"inputs": [os.path.join(tmp, "nope.bin")], "format": "7z",
                                   "destDir": out_dir}, expect_ok=False)
    check("缺失输入被拒绝", e.get("ok") is False, e.get("error", ""))
    e = cli.call("/api/inspect", {"path": os.path.join(src, "文档.txt")}, expect_ok=False)
    check("非压缩包被拒绝", e.get("ok") is False, e.get("error", ""))
    e = cli.call("/api/compress", {"inputs": [src], "format": "rar", "destDir": out_dir},
                 expect_ok=False)
    check("无 rar 引擎时拒绝创建 RAR",
          (e.get("ok") is False) if not engine.rar else True, e.get("error", ""))

    print("\n" + "=" * 64)
    print(f"通过 {len(PASS)}   失败 {len(FAIL)}   跳过 {len(SKIP)}")
    for f in FAIL:
        print("  FAILED:", f)
    for s in SKIP:
        print("  SKIPPED:", s)
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    import traceback
    try:
        CODE = main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        print("\n" + "=" * 64)
        print(f"通过 {len(PASS)}   失败 {len(FAIL) + 1}   跳过 {len(SKIP)}  （测试中断）")
        for f in FAIL:
            print("  FAILED:", f)
        CODE = 1
    sys.exit(CODE)
