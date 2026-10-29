"""One registry for everything Arca runs, and how each piece gets updated.

The 7-Zip engine update grew a whole download/verify/install pipeline.  Adding
zstd and par2 would have meant copying it twice, so the pipeline is now driven
by this table instead: every component knows how to describe itself, where its
upstream version lives, and how to turn a download into a working binary.

Two install strategies exist because the projects publish different things:

``archive``
    an official pre-built macOS binary inside a tarball -- 7-Zip only.
``source``
    a tagged source tarball compiled with the local toolchain -- zstd and
    par2cmdline, because neither ships a portable macOS binary.  The recipes
    are the same ones `scripts/build_thirdparty.py` uses to vendor them, so a
    user-side update produces a binary built exactly like the bundled one.

Everything is opt-in: a check only reads public metadata, and an install only
happens after the user presses the button in the dialog.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.request

from . import par2 as par2_mod
from . import updates
from . import util
from . import zst

UA = {"User-Agent": "Arca/1.0"}


# --------------------------------------------------------------- Chinese notes
# Hand-written, newest first.  Same rule as the 7-Zip notes: only what was
# actually in the upstream changelog, and no translation of release prose we
# cannot verify.
NOTES_ZH = {
    "zstd": {
        "1.5.7": [
            ("speed", "大幅提速：解压快 10–20%，小数据块压缩快 10–30%。"),
            ("fix", "修复 32 位平台上的压缩结果错误。"),
            ("feature", "新增 --exclude-compressed 与更好的字典训练参数。"),
        ],
        "1.5.6": [
            ("security", "修复若干边界情况下的越界读取（模糊测试发现）。"),
            ("speed", "改善多线程压缩的负载分配。"),
            ("fix", "修复流式解压时偶发的校验失败。"),
        ],
        "1.5.5": [
            ("feature", "新增 --train 的覆盖模式，字典训练结果更稳定。"),
            ("speed", "解压性能提升约 5%。"),
        ],
    },
    "par2": {
        "0.8.1": [
            ("fix", "修复大文件（>4 GB）在 32 位构建下的处理问题。"),
            ("fix", "修复部分平台上的编译与 OpenMP 兼容问题。"),
            ("feature", "校验阶段输出更清晰的损坏位置提示。"),
        ],
        "0.8.0": [
            ("feature", "重写文件 I/O：校验与修复速度显著提升。"),
            ("feature", "支持多线程计算恢复块（-t 参数）。"),
        ],
    },
}

TAG_LABELS = {
    "security": "安全", "speed": "速度", "format": "格式",
    "feature": "功能", "fix": "修复", "official": "官方原文",
}


def _official_notes(body: str) -> list[dict]:
    """First few bullets of the upstream release notes, untranslated.

    Same rule as the 7-Zip side: when there is no hand-written Chinese entry we
    show the official text and say so, instead of machine-translating it.
    """
    out = []
    for line in (body or "").splitlines():
        line = line.strip()
        if len(line) > 2 and line[0] in "-*" and line[1] == " ":
            text = line[2:].strip().replace("`", "")
            if not text or text.startswith("[") and "]" in text[:20]:
                continue
            out.append({"tag": "official", "label": TAG_LABELS["official"],
                        "text": text[:170]})
        if len(out) >= 4:
            break
    return out


class ComponentError(Exception):
    pass


# --------------------------------------------------------------------- helpers
def bin_dir() -> str:
    d = os.path.join(util.app_support_dir(), "components")
    os.makedirs(d, exist_ok=True)
    return d


def _record_path() -> str:
    return os.path.join(util.app_support_dir(), "components.json")


def records() -> dict:
    data = util.read_json(_record_path(), {})
    return data if isinstance(data, dict) else {}


def _save_records(data: dict) -> None:
    util.write_json(_record_path(), data)


def installed_path(comp_id: str) -> str | None:
    rec = (records() or {}).get(comp_id) or {}
    p = rec.get("path")
    return p if p and os.path.isfile(p) else None


def forget(comp_id: str) -> None:
    data = records()
    if comp_id in data:
        data.pop(comp_id, None)
        _save_records(data)


def _fetch_json(url: str, timeout: int = 25):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        raw = r.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ComponentError("更新信息响应过大，已中止")
    return json.loads(raw.decode("utf-8", "replace"))


def _run(cmd, cwd=None, timeout=900):
    p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace")


MAX_JSON_BYTES = 5 * 1024 * 1024      # T15：API 响应上限
MAX_SOURCE_BYTES = 64 * 1024 * 1024   # T15：源码包下载上限


def _install_binary(binary: str, target: str) -> None:
    """临时文件 → fsync → 原子替换；任何一步失败旧文件保持完整。"""
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = target + ".new"
    try:
        shutil.copyfile(binary, tmp)
        os.chmod(tmp, 0o755)
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ components
class Component:
    id = ""
    label = ""
    kind = ""
    summary = ""
    page = ""
    license_name = ""

    def current(self) -> dict:
        raise NotImplementedError

    def remote(self, force: bool = False) -> dict:
        raise NotImplementedError

    def install(self, version: str) -> "InstallJob":
        raise NotImplementedError


class SevenZip(Component):
    id = "7zz"
    label = "7-Zip 引擎"
    kind = "压缩引擎"
    summary = "7z / zip / rar 的读写核心"
    page = updates.PAGE_URL
    license_name = "LGPL-2.1 + unRAR restriction"

    def current(self) -> dict:
        return {"version": "", "path": "", "source": ""}

    def remote(self, force: bool = False) -> dict:
        # The 7-Zip pipeline already exists (page + history.txt + Chinese notes
        # written by hand); this wrapper only renames its fields.
        info = updates.check(None, force=force)
        info.setdefault("id", self.id)
        return info

    def install(self, version: str, engine=None, force: bool = True) -> "InstallJob":
        info = updates.check(engine, force=force)
        return InstallJob(self, version, strategy="sevenzip", meta=info, engine=engine)


class Zstd(Component):
    id = "zstd"
    label = "Zstandard"
    kind = "压缩算法"
    summary = ".tar.zst 压缩与解压"
    page = "https://github.com/facebook/zstd/releases"
    # zstd ships a BSD-3 library behind a GPL-2.0 command line front end.
    license_name = "BSD-3-Clause / GPL-2.0"

    def current(self) -> dict:
        path, source = zst.find_binary()
        return {"version": zst.version_of(path), "path": path or "", "source": source}

    def remote(self, force: bool = False) -> dict:
        return _github_remote(self, "facebook/zstd", force=force)


class Par2(Component):
    id = "par2"
    label = "PAR2 恢复记录"
    kind = "数据恢复"
    summary = "为压缩包生成可修复的冗余数据"
    page = "https://github.com/Parchive/par2cmdline/releases"
    license_name = "GPL-2.0"

    def current(self) -> dict:
        path, source = par2_mod.find_binary()
        return {"version": par2_mod.version_of(path), "path": path or "", "source": source}

    def remote(self, force: bool = False) -> dict:
        return _github_remote(self, "Parchive/par2cmdline", force=force)


REGISTRY = {c.id: c for c in (SevenZip(), Zstd(), Par2())}


def _github_remote(comp: Component, repo: str, force: bool = False) -> dict:
    """Version + notes from the GitHub API (api.github.com, not github.com).

    T15：force 绕过本地 TTL；draft/prerelease 一律排除；响应带上限；
    网络失败时返回明确标记的 stale 缓存（只用于展示）。
    """
    cache_key = f"remote-{comp.id}.json"
    cache = util.read_json(util.state_path(cache_key), {})
    fresh = (isinstance(cache, dict) and cache.get("ts")
             and time.time() - cache["ts"] < 6 * 3600)
    if fresh and not force:
        return cache["data"]
    try:
        releases = _fetch_json(f"https://api.github.com/repos/{repo}/releases?per_page=8")
    except Exception as exc:  # noqa: BLE001
        cached = cache.get("data") if isinstance(cache, dict) else None
        if cached:
            cached = dict(cached)
            cached["stale"] = True
            cached["error"] = f"无法连接更新服务器（{exc}），展示的是缓存"
            return cached
        return {"ok": False, "error": f"无法连接更新服务器（{exc}）",
                "component": comp.id}
    if not isinstance(releases, list) or not releases:
        return {"ok": False, "error": "没有读到发布信息", "component": comp.id}

    versions = []
    for rel in releases:
        if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
            continue
        tag = (rel.get("tag_name") or "").lstrip("vV")
        if not re.match(r"^\d+(\.\d+)+$", tag):
            continue
        notes = [{"tag": t, "label": TAG_LABELS.get(t, t), "text": text}
                 for t, text in NOTES_ZH.get(comp.id, {}).get(tag, [])]
        if not notes:
            # No hand-written Chinese entry: show upstream's own words, tagged
            # so the dialog can say they are untranslated.
            notes = _official_notes(rel.get("body") or "")
        versions.append({
            "version": tag,
            "date": (rel.get("published_at") or "")[:10],
            "notes": notes,
            "official": bool((rel.get("body") or "").strip()),
            "url": rel.get("html_url") or comp.page,
        })
    if not versions:
        return {"ok": False, "error": "发布信息里没有稳定版本号", "component": comp.id}

    versions.sort(key=lambda v: updates.parse_version(v["version"]), reverse=True)
    latest = versions[0]
    local = comp.current()
    available = bool(local["version"]) and updates.is_newer(latest["version"], local["version"])
    data = {
        "ok": True, "component": comp.id, "label": comp.label, "kind": comp.kind,
        "latest": latest["version"], "released": latest["date"],
        "versions": versions, "available": available,
        "highlights": latest["notes"][:5],
        "securityFixes": len([n for v in versions for n in v["notes"] if n["tag"] == "security"]),
        "page": comp.page, "license": comp.license_name,
        "built": True, "urls": [], "ts": int(time.time()), "source": "github",
    }
    util.write_json(util.state_path(cache_key), {"ts": time.time(), "data": data})
    return data


# ------------------------------------------------------------------- installing
class InstallJob:
    """Download -> unpack -> (compile) -> verify -> install, one step per phase."""

    def __init__(self, comp: Component, version: str, strategy: str = "source",
                 meta: dict | None = None, engine=None):
        self.comp = comp
        self.version = version or ""
        self.strategy = strategy
        self.meta = meta or {}
        self.engine = engine
        self.phase = "idle"
        self.percent = 0.0
        self.message = ""
        self.error = ""
        self.done_path = ""
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._started = 0.0

    # -- state ---------------------------------------------------------------
    def _set(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snapshot(self) -> dict:
        with self._lock:
            return {"component": self.comp.id, "version": self.version,
                    "phase": self.phase, "percent": round(self.percent, 1),
                    "message": self.message, "error": self.error,
                    "path": self.done_path, "strategy": self.strategy}

    def start(self) -> dict:
        if self._thread and self._thread.is_alive():
            return self.snapshot()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self.snapshot()

    # -- work ----------------------------------------------------------------
    def _run(self):
        try:
            if self.strategy == "sevenzip":
                return self._run_sevenzip()
            return self._run_source()
        except Exception as exc:  # noqa: BLE001
            self._set(phase="error", error=str(exc) or exc.__class__.__name__,
                      message=str(exc) or "更新失败")
        return None

    def _run_sevenzip(self):
        info = self.meta
        if not self.engine:
            raise ComponentError("内部错误：缺少引擎引用")
        urls = info.get("urls") or [info.get("url") or ""]
        inst = updates.make_installer(self.engine, urls, self.version)
        state = inst.start()
        while True:
            snap = inst.snapshot()
            self._set(phase=snap["phase"], percent=snap.get("percent", 0.0),
                      message=snap.get("message", ""), error=snap.get("error", ""),
                      done_path=getattr(self.engine, "zz", "") or "")
            if snap["phase"] in ("done", "error"):
                if snap["phase"] == "done":
                    self._set(percent=100.0)
                return
            time.sleep(0.25)

    def _run_source(self):
        spec = SOURCE_RECIPES[self.comp.id].copy()
        spec["version"] = self.version
        work = os.path.join(bin_dir(), ".build-%s-%s" % (self.comp.id, self.version))
        os.makedirs(work, exist_ok=True)
        tag = spec["tag"].format(version=self.version)
        filename = spec["archive"].format(version=self.version)
        url = f"https://github.com/{spec['repo']}/releases/download/{tag}/{filename}"
        archive = os.path.join(work, filename)
        try:
            self._set(phase="download", percent=0.0, message="正在下载源码…")
            self._download(url, archive)
            self._set(phase="verify", percent=0.0, message="正在校验下载文件…")
            updates.verify_download(
                archive, [url], filename, timeout=25,
                release_api=f"https://api.github.com/repos/{spec['repo']}/releases/tags/{tag}")
            self._build_source(spec, work, archive)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _build_source(self, spec: dict, work: str, archive: str):
        self._set(phase="unpack", percent=0.0, message="正在解包…")
        src = os.path.join(work, "src")
        shutil.rmtree(src, ignore_errors=True)
        os.makedirs(src, exist_ok=True)
        with tarfile.open(archive, "r:gz") as tf:
            members = []
            for m in tf.getmembers():
                parts = m.name.split("/", 1)
                if len(parts) < 2 or not parts[1]:
                    continue
                m.name = parts[1]
                if m.name.startswith("..") or m.name.startswith("/"):
                    continue
                members.append(m)
            tf.extractall(src, members=members, filter="data")

        if spec.get("config_h"):
            with open(os.path.join(src, "src", "config.h"), "w") as fh:
                fh.write(spec["config_h"].format(version=self.version))

        self._set(phase="build", percent=0.0, message="正在编译（需要命令行工具，约一分钟）…")
        binary = self._compile(spec, src, work)

        self._set(phase="verify", percent=0.0, message="正在校验…")
        code, text = _run([binary] + spec["version_args"], timeout=30)
        m = re.search(spec["version_re"], text)
        if code not in (0, 1) or not m:
            raise ComponentError("编译出的程序无法运行")
        got = m.group(1)
        if updates.is_newer(self.version, got):
            raise ComponentError(f"版本不符：期望 {self.version}，实际 {got}")

        self._set(phase="install", percent=0.0, message="正在安装…")
        target = os.path.join(bin_dir(), self.comp.id)
        _install_binary(binary, target)
        _save_records_merge(self.comp.id, {"path": target, "version": got,
                                           "installedAt": time.time()})
        shutil.rmtree(work, ignore_errors=True)
        self._set(phase="done", percent=100.0, done_path=target,
                  message=f"已安装 {self.comp.label} {got}")

    def _download(self, url: str, dest: str):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        tmp = dest + ".part"
        try:
            with opener.open(urllib.request.Request(url, headers=UA), timeout=180) as r:
                total = int(r.headers.get("Content-Length") or 0)
                if total > MAX_SOURCE_BYTES:
                    raise ComponentError("源码包异常大（%s），已中止" % util.human_size(total))
                read = 0
                with open(tmp, "wb") as fh:
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        read += len(chunk)
                        if read > MAX_SOURCE_BYTES:
                            raise ComponentError("源码包超过大小上限，已中止")
                        fh.write(chunk)
                        if total:
                            self._set(percent=read * 100.0 / total,
                                      message="正在下载源码… %s/%s"
                                              % (util.human_size(read), util.human_size(total)))
            if total and read < total:
                raise ComponentError("下载不完整")
            os.replace(tmp, dest)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def _compile(self, spec: dict, src: str, work: str) -> str:
        import glob as _glob
        sources = []
        for pat in spec["sources"]:
            for m in sorted(_glob.glob(os.path.join(src, pat))):
                if any(x in os.path.basename(m) for x in spec.get("exclude", [])):
                    continue
                sources.append(os.path.relpath(m, src) if spec.get("output") else m)
        if not sources:
            raise ComponentError("源码包里没有找到源文件")
        incs = []
        for d in spec["includes"]:
            incs += ["-I", os.path.join(src, d)]
        out = os.path.join(work, self.comp.id)
        cmd = ([spec["compiler"]] + spec["flags"] + incs + ["-o", out] + sources
               + spec["libs"])
        code, text = _run(cmd, cwd=src if spec.get("output") else None, timeout=1800)
        if code != 0:
            raise ComponentError("编译失败：" + (text.strip().splitlines() or ["未知错误"])[-1][:160])
        return out


def _save_records_merge(comp_id: str, data: dict) -> None:
    rec = records()
    rec[comp_id] = data
    _save_records(rec)


# Recipes mirror scripts/build_thirdparty.py; kept in one place per component so
# a runtime update and a fresh bundle produce the same binary.
PAR2_CONFIG_H = """#define STDC_HEADERS 1
#define HAVE_STDLIB_H 1
#define HAVE_STDIO_H 1
#define HAVE_STRING_H 1
#define HAVE_STRINGS_H 1
#define HAVE_MEMCPY 1
#define HAVE_MEMMOVE 1
#define HAVE_STRCHR 1
#define HAVE_STRRCHR 1
#define HAVE_MEMORY_H 1
#define HAVE_SYS_STAT_H 1
#define HAVE_SYS_TYPES_H 1
#define HAVE_LIMITS_H 1
#define HAVE_UNISTD_H 1
#define HAVE_FCNTL_H 1
#define HAVE_DIRENT_H 1
#define HAVE_INTTYPES_H 1
#define HAVE_STDINT_H 1
#define HAVE_STRCASECMP 1
#define PACKAGE "par2cmdline"
#define PACKAGE_NAME "par2cmdline"
#define PACKAGE_STRING "par2cmdline {version}"
#define PACKAGE_VERSION "{version}"
#define VERSION "{version}"
"""

SOURCE_RECIPES = {
    "zstd": {
        "repo": "facebook/zstd",
        "tag": "v{version}",
        "archive": "zstd-{version}.tar.gz",
        "compiler": "cc",
        "flags": ["-O2", "-DNDEBUG", "-DXXH_NAMESPACE=ZSTD_", "-DZSTD_MULTITHREAD",
                  "-DZSTD_DISABLE_ASM"],
        "sources": ["programs/zstdcli.c", "programs/util.c", "programs/timefn.c",
                    "programs/fileio.c", "programs/fileio_asyncio.c",
                    "programs/benchzstd.c", "programs/benchfn.c", "programs/datagen.c",
                    "programs/lorem.c", "programs/dibio.c", "programs/zstdcli_trace.c",
                    "lib/common/*.c", "lib/compress/*.c", "lib/decompress/*.c",
                    "lib/dictBuilder/*.c"],
        "includes": ["lib", "lib/common", "lib/compress", "lib/decompress",
                     "lib/dictBuilder"],
        "libs": ["-lpthread"],
        "output": "programs/zstd",
        "version_args": ["--version"],
        "version_re": r"v([0-9]+\.[0-9]+\.[0-9]+)",
    },
    "par2": {
        "repo": "Parchive/par2cmdline",
        "tag": "v{version}",
        "archive": "par2cmdline-{version}.tar.gz",
        "compiler": "c++",
        "flags": ["-O2", "-w", "-std=c++17", "-DHAVE_CONFIG_H"],
        "sources": ["src/*.cpp"],
        "exclude": ["_test.cpp"],
        "includes": ["src"],
        "libs": [],
        "output": None,
        "version_args": ["-V"],
        "version_re": r"par2cmdline version ([0-9.]+)",
        "config_h": PAR2_CONFIG_H,
    },
}


# ---------------------------------------------------------------- public surface
def summary(engine=None) -> list[dict]:
    """Cheap view for /api/meta: current version + source, no network."""
    out = []
    for cid, comp in REGISTRY.items():
        cur = comp.current()
        if cid == "7zz" and engine is not None:
            cur = {"version": engine.version, "path": engine.zz, "source": engine.source}
        out.append({"id": cid, "label": comp.label, "kind": comp.kind,
                    "summary": comp.summary, "page": comp.page,
                    "license": comp.license_name, **cur})
    return out


def check(comp_id: str, engine=None, force: bool = False) -> dict:
    comp = REGISTRY.get(comp_id)
    if not comp:
        return {"ok": False, "error": "未知组件"}
    info = comp.remote(force=force)
    cur = comp.current()
    if comp_id == "7zz" and engine is not None:
        cur = {"version": engine.version, "path": engine.zz, "source": engine.source}
    info["current"] = cur
    info["id"] = comp_id
    info["label"] = comp.label
    info["kind"] = comp.kind
    info["summary"] = comp.summary
    info["license"] = comp.license_name
    info["page"] = comp.page
    if info.get("ok"):
        info["available"] = bool(cur.get("version")) and updates.is_newer(
            info["latest"], cur["version"])
        info["currentVersion"] = cur.get("version")
        info["currentSource"] = cur.get("source")
    return info


def make_installer(comp_id: str, version: str, engine=None) -> InstallJob:
    comp = REGISTRY[comp_id]
    if comp_id == "7zz":
        return comp.install(version, engine=engine)
    return InstallJob(comp, version, strategy="source")


def reset(comp_id: str) -> dict:
    """Drop a user-installed copy so the bundled one is used again."""
    if comp_id == "7zz":
        updates.forget()
        return {"ok": True, "component": comp_id}
    rec = records().get(comp_id) or {}
    path = rec.get("path")
    forget(comp_id)
    if path:
        try:
            os.replace(path, path + ".removed")
        except OSError:
            pass
    return {"ok": True, "component": comp_id}
