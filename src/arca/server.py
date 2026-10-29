"""Local HTTP API + static hosting for the Arca window."""
from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import re
import shutil
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import archtxn
from . import engine as eng
from . import components
from . import integration
from . import par2 as par2_mod
from . import appupdate
from . import stats as stats_mod
from . import updates
from . import jobs as jobs_mod
from . import util
from . import keyring

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
}

FORMATS = [
    {"id": "7z", "label": "7Z", "ext": ".7z", "create": True,
     "hint": "通常体积最小，适合长期保存 · 可加密文件名",
     "encrypt": True, "split": True, "method": True, "solid": True},
    {"id": "zip", "label": "ZIP", "ext": ".zip", "create": True,
     "hint": "兼容性最好，适合发给别人",
     "encrypt": True, "split": True, "method": False, "solid": False},
    {"id": "tar.zst", "label": "ZST", "ext": ".tar.zst", "create": True,
     "hint": "适合大文件与高速压缩 · 不支持密码与分卷",
     "encrypt": False, "split": False, "method": False, "solid": False},
    {"id": "rar", "label": "RAR", "ext": ".rar", "create": False,
     "hint": "可解压 · 创建需配置 WinRAR",
     "encrypt": True, "split": True, "method": False, "solid": False},
]

EXT_BY_FORMAT = {f["id"]: f["ext"] for f in FORMATS}


def _ext_for(fmt: str) -> str:
    return EXT_BY_FORMAT.get(fmt, ".7z")


class AppContext:
    def __init__(self, engine, settings, web_dir):
        self.engine = engine
        self.settings = settings
        self.web_dir = web_dir
        self.manager = jobs_mod.JobManager(engine, settings)
        self.saved_passwords = keyring.SavedPasswords()
        self.token = util.app_support_dir() and _token()
        self.window = None
        self.bridge = None
        self.headless = False
        # T05: the running page reports real readiness (bridge/DOM/meta/engine)
        # once; the server thread waits for it asynchronously before letting the
        # updater delete the old bundle.
        self.ui_ready = threading.Event()
        self.ui_ready_lock = threading.Lock()
        self.ui_ready_checks: dict[str, bool] = {}
        # job id -> {path, body}: server-side only (passwords never go back
        # to the frontend), dropped on restart, powers /api/jobs/retry.
        self.requests = {}
        self.inspect_cache: dict[str, dict] = {}
        self.inspect_lock = threading.RLock()
        self.installer = None          # updates.Installer while one is running
        self.component_jobs: dict[str, object] = {}
        self.app_updater = appupdate.Updater(settings)
        # Requests that arrive from outside the window (Finder service, "open
        # with", double click) wait here until the page is ready to take them.
        self.inbox: list[dict] = []
        self.inbox_lock = threading.Lock()
        # Everything ever handed over, for diagnostics (--probe-service).
        self.delivered: list[dict] = []


def push_inbox(ctx, action: str, paths) -> dict | None:
    """Hand a Finder selection to the UI; returns the queued item."""
    item = integration.make_item(action, paths)
    if item is None:
        return None
    with ctx.inbox_lock:
        # The same selection can arrive twice (Apple event + argv when the app
        # is cold-started); keep it once.
        for old in ctx.inbox:
            if (old["action"] == item["action"] and old["paths"] == item["paths"]
                    and item["at"] - old["at"] < 5):
                return old
        ctx.inbox.append(item)
        ctx.delivered.append(item)
        if len(ctx.delivered) > 50:
            del ctx.delivered[:-50]
    return item


def _token() -> str:
    import secrets
    return secrets.token_urlsafe(24)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    ctx: AppContext = None  # set by App

    def log_message(self, *args):  # silence
        pass

    # ------------------------------------------------------------------ helpers
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        obj = keyring.redact(obj, getattr(self, "_secrets", ()))
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _password(self, body, entry):
        password = body.get("password") or ""
        if not isinstance(password, str):
            raise eng.EngineError("密码格式无效")
        saved = not password
        password = password or keyring.lookup(entry, auth=self.ctx.settings.get("passwordAuth", "off")) or ""
        self._secrets = [password] if password else []
        return password, saved and bool(password)

    def _err(self, message: str, code: int = 400):
        self._json({"ok": False, "error": message}, code)

    def _busy(self, action: str):
        """Refuse an update while a job is writing files.

        Replacing the bundle (or the engines it spawns) under a running job
        kills the worker mid-write and leaves a half-written archive behind.
        """
        manager = getattr(self.ctx, "manager", None)
        active = manager.active_count() if manager is not None else 0
        if active:
            self._err(f"还有 {active} 个任务正在进行，完成后才能{action}", 409)
            return True
        return False

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _authed(self) -> bool:
        token = self.headers.get("X-Arca-Token") or ""
        return token == self.ctx.token

    # --------------------------------------------------------------------- http
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            if not self._authed():
                return self._err("unauthorized", 401)
            return self._api_get(path, urllib.parse.parse_qs(parsed.query))
        return self._static(path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return self._err("not found", 404)
        if not self._authed():
            return self._err("unauthorized", 401)
        return self._api_post(parsed.path, self._body())

    # ------------------------------------------------------------------- static
    def _static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        rel = posixpath.normpath(urllib.parse.unquote(path)).lstrip("/")
        if rel.startswith(".."):
            return self._err("bad path", 403)
        full = os.path.join(self.ctx.web_dir, rel)
        if not os.path.isfile(full):
            full = os.path.join(self.ctx.web_dir, "index.html")
            if not os.path.isfile(full):
                return self._err("missing ui", 500)
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError:
            return self._err("read error", 500)
        ext = os.path.splitext(full)[1].lower()
        ctype = MIME.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        self._send(200, data, ctype)

    # ---------------------------------------------------------------------- api
    def _api_get(self, path: str, query: dict):
        ctx = self.ctx
        if path == "/api/meta":
            home = os.path.expanduser("~")
            engine_info = ctx.engine.info()
            formats = []
            for f in FORMATS:
                item = dict(f)
                if f["id"] == "rar":
                    item["create"] = bool(engine_info["rarEncode"])
                    # 配好 WinRAR 后，静态的「创建需配置」就不再属实了。
                    if item["create"]:
                        item["hint"] = "老牌专有格式 · 老解压工具也能开"
                if f["id"] == "tar.zst" and not engine_info["zstdPath"]:
                    item["create"] = False
                    item["hint"] = "需要 zstd 组件（设置 → 组件）"
                formats.append(item)
            return self._json({
                "ok": True,
                "app": util.APP_NAME,
                "version": util.APP_VERSION,
                "engine": engine_info,
                "formats": formats,
                "splitPresets": eng.SPLIT_PRESETS,
                "levelSteps": list(eng.LEVEL_STEPS),
                "methods": [{"id": k, "label": v} for k, v in eng.METHOD_LABELS.items()],
                "par2": {"available": bool(engine_info["par2Path"]),
                         "version": engine_info["par2Version"],
                         "percents": list(par2_mod.REDUNDANCY)},
                "components": components.summary(ctx.engine),
                "home": home,
                "desktop": os.path.join(home, "Desktop"),
                "downloads": os.path.join(home, "Downloads"),
                "settings": ctx.settings.all(),
                "history": ctx.manager.history_snapshot(20),
            })
        if path == "/api/jobs":
            return self._json({"ok": True, "jobs": ctx.manager.list(),
                               "active": ctx.manager.active_count()})
        if path == "/api/settings":
            return self._json({"ok": True, "settings": ctx.settings.all(),
                                   "savedPasswords": ctx.saved_passwords.summary()})
        if path == "/api/history":
            return self._json({"ok": True, "history": ctx.manager.history_snapshot()})
        if path == "/api/appupdate":
            return self._json({"ok": True, "update": ctx.app_updater.check(force=bool(query.get("refresh")))})
        if path == "/api/engine/update":
            return self._engine_update(query)
        if path == "/api/components":
            return self._json({"ok": True,
                               "components": components.summary(ctx.engine),
                               "installs": self._component_states()})
        if path == "/api/components/check":
            cid = (query.get("id") or [""])[0]
            return self._component_check(cid, bool(query.get("refresh")))
        if path == "/api/integration":
            st = integration.status()
            st["quickActions"] = integration.quick_actions_installed()
            return self._json(st)
        if path == "/api/inbox":
            with ctx.inbox_lock:
                items = list(ctx.inbox)
                ctx.inbox.clear()
            return self._json({"ok": True, "items": items})
        return self._err("unknown endpoint", 404)

    def _api_post(self, path: str, body: dict):
        ctx = self.ctx
        self._secrets = [body["password"]] if isinstance(body.get("password"), str) and body["password"] else []
        try:
            if path == "/api/ui-ready":
                return self._ui_ready(body)
            if path == "/api/inspect":
                return self._inspect(body)
            if path == "/api/compress":
                return self._compress(body)
            if path == "/api/extract":
                return self._extract(body)
            if path == "/api/test":
                return self._test(body)
            if path == "/api/preflight":
                return self._preflight(body)
            if path == "/api/jobs/retry":
                jid = str(body.get("id") or "")
                record = getattr(ctx, "requests", {}).get(jid)
                if not record:
                    return self._err("重试信息已失效（应用重启后仅保留本次会话的任务）")
                job = ctx.manager.jobs.get(jid)
                if job is not None and job.to_dict()["status"] not in ("error", "cancelled"):
                    return self._err("只有失败或已取消的任务可以重试")
                payload = dict(record["body"])
                # 失败重试时目标目录若被用户占用，合并进同名目录继续，
                # 而不是弹“已存在同名文件夹”把重试堵死。
                payload.setdefault("onExists", "merge")
                ctx.requests.pop(jid, None)
                if record["path"] == "/api/compress":
                    return self._compress(payload)
                if record["path"] == "/api/extract":
                    return self._extract(payload)
                return self._test(payload)
            if path == "/api/settings":
                if body.get("clearSavedPasswords") is True:
                    if not ctx.saved_passwords.clear_all():
                        return self._err("部分密码未能清除，请检查钥匙串后重试")
                patch = body.get("patch") or {}
                ctx.settings.patch(patch)
                if "statsPrompted" in patch or "anonymousStats" in patch:
                    stats_mod.maybe_ping(ctx.settings, getattr(ctx, "headless", False))
                if body.get("patch", {}).get("rarPath") is not None or "rarPath" in body:
                    ctx.engine.rar = ctx.engine._find_rar(  # noqa: SLF001
                        ctx.settings.get("rarPath") or None)
                return self._json({"ok": True, "settings": ctx.settings.all(),
                                   "savedPasswords": ctx.saved_passwords.summary()})
            if path == "/api/appupdate/apply":
                if self._busy("更新"):
                    return
                return self._json({"ok": True, "install": ctx.app_updater.apply(body.get("version"))})
            if path == "/api/appupdate/dismiss":
                ctx.app_updater.dismiss(body.get("version"))
                return self._json({"ok": True})
            if path == "/api/appupdate/restart":
                if self._busy("重启"):
                    return
                return self._json(ctx.app_updater.relaunch())
            if path == "/api/engine/update/apply":
                return self._engine_apply(body)
            if path == "/api/engine/update/snooze":
                version = str(body.get("version") or "")
                hours = float(body.get("hours") or 24)
                until = int(time.time() + hours * 3600)
                ctx.settings.patch({"engineSnoozeVersion": version,
                                    "engineSnoozeUntil": until})
                return self._json({"ok": True, "until": until})
            if path == "/api/engine/update/skip":
                version = str(body.get("version") or "")
                ctx.settings.patch({"engineSkipVersion": version})
                return self._json({"ok": True, "skipped": version})
            if path == "/api/engine/reset":
                return self._engine_reset()
            if path == "/api/components/apply":
                return self._component_apply(body)
            if path == "/api/components/reset":
                return self._component_reset(body)
            if path == "/api/par2/verify":
                return self._par2_action(body, "verify")
            if path == "/api/par2/repair":
                return self._par2_action(body, "repair")
            if path == "/api/inbox":
                # Also reachable over HTTP so the queue can be fed in tests and
                # by anything that cannot touch the process directly.
                item = push_inbox(ctx, str(body.get("action") or "auto"),
                                  body.get("paths") or [])
                return self._json({"ok": item is not None, "item": item})
            if path == "/api/integration/default":
                return self._integration_default(body)
            if path == "/api/integration/register":
                return self._json({"ok": integration.register_bundle()})
            if path == "/api/integration/services":
                return self._json({"ok": integration.open_services_prefs()})
            if path == "/api/integration/quickactions":
                return self._json(integration.install_quick_actions())
            if path == "/api/job/cancel":
                ok = ctx.manager.cancel(body.get("id", ""))
                return self._json({"ok": ok})
            if path == "/api/job/remove":
                ctx.manager.remove(body.get("id", ""))
                return self._json({"ok": True})
            if path == "/api/jobs/clear":
                ctx.manager.clear_finished()
                return self._json({"ok": True})
            if path == "/api/reveal":
                util.reveal_in_finder(body.get("path") or "")
                return self._json({"ok": True})
            if path == "/api/stat":
                p = body.get("path") or ""
                if os.path.exists(p):
                    st = os.stat(p)
                    return self._json({"ok": True, "path": p, "size": st.st_size,
                                       "isDir": os.path.isdir(p)})
                return self._json({"ok": False, "error": "not found"})
        except eng.EngineError as exc:
            return self._err(str(exc))
        except appupdate.UpdateError as exc:
            # User-facing wording already: no class prefix, and a client error
            # rather than a 500 (stale check, unverified digest, missing update).
            return self._err(str(exc), 400)
        except ValueError as exc:
            # Repo format / version parsing: the message is the explanation.
            return self._err(str(exc) or "请求参数无效", 400)
        except Exception as exc:  # noqa: BLE001
            return self._err(util.friendly_error(exc)
                             or f"{exc.__class__.__name__}: {exc}", 500)
        return self._err("unknown endpoint", 404)

    # ------------------------------------------------------------------ actions
    # ------------------------------------------------------------- engine update
    def _engine_update(self, query: dict):
        ctx = self.ctx
        force = bool(query.get("refresh"))
        info = updates.check(ctx.engine, force=force)
        skip = ctx.settings.get("engineSkipVersion") or ""
        info["skipped"] = bool(skip) and skip == info.get("latest")
        info["skippedVersion"] = skip
        info["checkOnLaunch"] = bool(ctx.settings.get("checkEngineUpdates", True))
        snooze_ver = ctx.settings.get("engineSnoozeVersion") or ""
        snooze_until = ctx.settings.get("engineSnoozeUntil") or 0
        info["snoozed"] = bool(
            info.get("latest") and snooze_ver == info.get("latest")
            and float(snooze_until) > time.time())
        info["snoozeUntil"] = snooze_until
        inst = ctx.installer
        info["install"] = inst.snapshot() if inst is not None else None
        return self._json({"ok": True, "update": info,
                           "engine": ctx.engine.info()})

    def _engine_apply(self, body: dict):
        """Start the download the user just confirmed.

        The URL is never taken from the request: it is re-read from the cached
        check, so a crafted call cannot point the updater at another host.
        """
        ctx = self.ctx
        if self._busy("更新引擎"):
            return
        wanted = str(body.get("version") or "")
        info = updates.check(ctx.engine, force=False)
        if not info.get("ok"):
            return self._err(info.get("error") or "检查更新失败")
        if not info.get("available"):
            return self._err("当前已是最新版本")
        if wanted and updates.parse_version(wanted) != updates.parse_version(info["latest"]):
            return self._err("版本已在检查后变化，请重新检查更新")
        inst = ctx.installer
        if inst is not None and inst.snapshot()["phase"] in (
                "download", "unpack", "verify", "install"):
            return self._json({"ok": True, "install": inst.snapshot()})
        ctx.installer = updates.make_installer(
            ctx.engine, info.get("urls") or info["url"], info["latest"])
        state = ctx.installer.start()
        return self._json({"ok": True, "install": state, "engine": ctx.engine.info()})

    def _engine_reset(self):
        ctx = self.ctx
        updates.forget()
        try:
            fresh = eng.Engine(ctx.settings.get("rarPath") or None)
        except eng.EngineError as exc:
            return self._err(str(exc))
        ctx.engine.zz = fresh.zz
        ctx.engine.version = fresh.version
        ctx.engine.source = fresh.source
        ctx.installer = None
        return self._json({"ok": True, "engine": ctx.engine.info()})

    # -------------------------------------------------------------- components
    def _component_states(self) -> dict:
        return {cid: job.snapshot() for cid, job in self.ctx.component_jobs.items()}

    def _component_check(self, cid: str, refresh: bool = False):
        ctx = self.ctx
        if not cid:
            return self._json({"ok": True, "components": components.summary(ctx.engine),
                               "installs": self._component_states()})
        info = components.check(cid, ctx.engine, force=refresh)
        job = ctx.component_jobs.get(cid)
        if job is not None:
            snap = job.snapshot()
            info["install"] = snap
            if snap["phase"] == "done":
                # A freshly built binary only becomes visible after the paths
                # are resolved again.
                ctx.engine.refresh_helpers()
        return self._json({"ok": True, "update": info,
                           "components": components.summary(ctx.engine)})

    def _ui_ready(self, body: dict):
        """页面自检上报（T05）：bridge/DOM/meta/引擎自检全部为真才算就绪。"""
        ctx = self.ctx
        names = ("bridge", "dom", "meta", "engine")
        checks = {name: bool(body.get(name)) for name in names}
        ready = all(checks.values())
        with ctx.ui_ready_lock:
            ctx.ui_ready_checks = checks
        if ready:
            ctx.ui_ready.set()
        return self._json({"ok": True, "ready": ready,
                           "headless": bool(getattr(ctx, "headless", False))})

    def _component_apply(self, body: dict):
        """Start the build/download the user confirmed in the dialog."""
        ctx = self.ctx
        if self._busy("替换组件"):
            return
        cid = str(body.get("id") or "")
        wanted = str(body.get("version") or "")
        info = components.check(cid, ctx.engine, force=False)
        if not info.get("ok"):
            return self._err(info.get("error") or "检查更新失败")
        if not info.get("available"):
            return self._err("当前已是最新版本")
        if wanted and updates.parse_version(wanted) != updates.parse_version(info["latest"]):
            return self._err("版本已在检查后变化，请重新检查")
        job = ctx.component_jobs.get(cid)
        if job is not None and job.snapshot()["phase"] in (
                "download", "unpack", "build", "verify", "install"):
            return self._json({"ok": True, "install": job.snapshot()})
        job = components.make_installer(cid, info["latest"], engine=ctx.engine)
        ctx.component_jobs[cid] = job
        state = job.start()
        return self._json({"ok": True, "install": state,
                           "components": components.summary(ctx.engine)})

    def _component_reset(self, body: dict):
        ctx = self.ctx
        cid = str(body.get("id") or "")
        if cid not in components.REGISTRY:
            return self._err("未知组件")
        if cid == "7zz":
            return self._engine_reset()
        components.reset(cid)
        ctx.component_jobs.pop(cid, None)
        ctx.engine.refresh_helpers()
        return self._json({"ok": True, "components": components.summary(ctx.engine),
                           "engine": ctx.engine.info()})

    def _par2_action(self, body: dict, action: str):
        entry = self.ctx.engine.resolve(body.get("path") or "")
        if not entry or not os.path.exists(entry):
            return self._err("文件不存在")
        files = par2_mod.recovery_files(entry)
        if not files:
            return self._err("没有找到这个压缩包的恢复记录（.par2）")
        label = ("校验恢复记录 " if action == "verify" else "修复 ") + os.path.basename(entry)
        job = jobs_mod.Job("par2", label, srcPath=entry, format="PAR2",
                           subtitle=f"{len(files)} 个恢复文件")
        self.ctx.manager.submit(job, jobs_mod.make_par2_task(self.ctx.manager, entry, action))
        return self._json({"ok": True, "job": job.to_dict()})

    def _integration_default(self, body: dict):
        """Hand the archive types to Arca (or back to the system)."""
        mode = str(body.get("mode") or ("on" if body.get("on", True) else "reset"))
        dry = bool(body.get("dry"))
        if mode not in ("on", "reset"):
            return self._err("未知模式")
        res = integration.set_default(mode == "on", dry=dry)
        if not res.get("ok"):
            return self._err(res.get("error") or "无法修改默认打开方式")
        return self._json(res)

    def _inspect(self, body):
        path = (body.get("path") or "").strip()
        if not path:
            return self._err("缺少文件路径")
        password, saved = self._password(body, self.ctx.engine.resolve(path))
        info = self._inspect_archive(path, password)
        info["savedPasswordUsed"] = saved
        # Recovery records live next to the archive, so the UI cannot guess them:
        # a single stat of the directory is cheaper than a separate request.
        info["par2Files"] = len(par2_mod.recovery_files(info["path"]))
        return self._json({"ok": True, "info": info})

    @staticmethod
    def _archive_stat(path):
        try:
            info = os.stat(path)
            return info.st_mtime_ns, info.st_size
        except OSError:
            return None

    def _inspect_archive(self, path, password):
        with self.ctx.inspect_lock:
            return self._inspect_archive_locked(path, password)

    def _inspect_archive_locked(self, path, password):
        entry = self.ctx.engine.resolve(path)
        before = self._archive_stat(entry)
        self.ctx.inspect_cache.pop(entry, None)
        info = self.ctx.engine.inspect(path, password)
        if before is not None and info["path"] == entry and before == self._archive_stat(entry):
            self.ctx.inspect_cache[entry] = {
                "uncompressed": info["uncompressed"], "size": info["size"],
                "type": info["type"], "label": info["label"],
                "needsPassword": info.get("needsPassword", False),
                "stat": before, "ts": time.time(),
            }
            while len(self.ctx.inspect_cache) > 64:
                self.ctx.inspect_cache.pop(next(iter(self.ctx.inspect_cache)))
        return info

    def _preflight(self, body):
        dest = body.get("destDir") or ""
        name = util.sanitize_name(body.get("name") or "归档")
        fmt = body.get("format") or "7z"
        ext = _ext_for(fmt)
        if not name.lower().endswith(ext):
            name += ext
        if not dest or not os.path.isdir(dest):
            return self._err("保存位置无效")
        target = os.path.join(dest, name)
        with self.ctx.manager._cv:
            exists = self._target_exists(target, archive=True)
            suggestion = self._unique_target(target, archive=True)
        return self._json({"ok": True, "target": target, "exists": exists,
                           "suggestion": suggestion})

    def _target_exists(self, path, archive=False):
        return (os.path.realpath(path) in self.ctx.manager._targets.values()
                or os.path.exists(path)
                or bool(archive and self.ctx.engine.parts(self.ctx.engine.resolve(path)))
                # 恢复记录档（.par2 索引与 .vol 数据档）挂在目标名下：归档本体
                # 不在而 sidecar 在时复用该名字，会让新档与旧恢复记录纠缠。
                or bool(archive and par2_mod.recovery_files(path)))

    def _unique_target(self, path, archive=False):
        # ".tar.zst" 是一个整体扩展名：唯一化序号必须插在它前面，
        # 而不是 splitext 的 ".zst" 边界（否则产出 "x.tar 2.zst"）。
        if path.lower().endswith(".tar.zst"):
            root, ext = path[:-8], path[-8:]
        else:
            root, ext = os.path.splitext(path)
        candidate, index = path, 2
        while self._target_exists(candidate, archive):
            candidate = f"{root} {index}{ext}"
            index += 1
        return candidate

    def _record_retry(self, jid, path, body):
        # Best-effort bookkeeping: unit tests build fake contexts without it.
        requests = getattr(self.ctx, "requests", None)
        if requests is None:
            return
        requests[jid] = {"path": path, "body": dict(body)}
        while len(requests) > 60:
            requests.pop(next(iter(requests)), None)

    def _compress(self, body):
        inputs = [os.path.expanduser(p) for p in (body.get("inputs") or []) if p]
        missing = [p for p in inputs if not os.path.exists(p)]
        if missing:
            return self._err("文件不存在：" + os.path.basename(missing[0]))
        if not inputs:
            return self._err("请先添加要压缩的文件")
        fmt = body.get("format") or "7z"
        if fmt not in eng.CREATE_FORMATS:
            return self._err("不支持的格式")
        if fmt == "rar" and not self.ctx.engine.rar:
            return self._err("创建 RAR 需要安装 WinRAR 命令行（rar）")
        if fmt == "tar.zst" and not self.ctx.engine.zstd:
            return self._err("创建 ZST 需要 zstd 组件（设置 → 组件）")
        if fmt == "zip" and not (body.get("password") or "").isascii():
            return self._err(eng.ZIP_PASSWORD_HINT)
        dest_dir = body.get("destDir") or os.path.dirname(os.path.abspath(inputs[0]))
        if not os.path.isdir(dest_dir):
            return self._err("保存位置不存在")
        name = util.sanitize_name(body.get("name") or _default_name(inputs))
        ext = _ext_for(fmt)
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
        target = os.path.join(dest_dir, name + ext)
        par2_percent = int(body.get("par2Percent") or 0)
        if par2_percent and not self.ctx.engine.par2:
            return self._err("生成恢复记录需要 PAR2 组件（设置 → 组件）")
        overwrite = bool(body.get("overwrite"))
        try:
            _, need = util.scan_inputs(inputs)
            free = shutil.disk_usage(dest_dir).free
        except OSError:
            pass
        else:
            # T06：覆盖写不再先删旧集合。临时空间按“新集合 + 新 PAR2 +
            # 余量”检查；旧集合在切换前仍占磁盘（已体现在 free 中）。
            reserve = need + 64 * 1024 * 1024
            if overwrite:
                reserve += int(need * max(0, min(50, par2_percent)) / 100)
            if free < reserve:
                return self._err("磁盘剩余空间不足：还需要约 %s，可用 %s"
                                 % (util.human_size(need), util.human_size(free)))
        opts = {
            "inputs": inputs,
            "output": target,
            "format": fmt,
            "level": body.get("level", 3),
            "method": body.get("method", "auto"),
            "splitBytes": int(body.get("splitBytes") or 0),
            "password": body.get("password") or "",
            "encryptNames": bool(body.get("encryptNames", True)),
            "excludeMeta": bool(body.get("excludeMeta", True)),
            "solid": bool(body.get("solid", True)),
            "threads": int(body.get("threads") or 0),
            "par2Percent": max(0, min(50, par2_percent)),
            "par2Single": bool(body.get("par2Single")) and par2_percent > 0,
        }
        if fmt == "tar.zst":
            # zstd has no password support and cannot be split, so the options
            # simply do not apply -- say so once instead of silently ignoring.
            opts.update({"password": "", "encryptNames": False, "splitBytes": 0,
                         "method": "auto", "solid": False})
        pw = opts["password"]
        if pw and fmt == "zip" and opts["encryptNames"]:
            opts["encryptNames"] = False
        with self.ctx.manager._cv:
            if not overwrite:
                target = self._unique_target(target, archive=True)
            opts["output"] = target
            label = "压缩 " + os.path.basename(target)
            job = jobs_mod.Job("compress", label, subtitle=_join_inputs(inputs),
                               destPath=dest_dir, format=fmt.upper(), inputs=inputs,
                               planned=target, secrets=[pw])
            if overwrite:
                # T06：旧集合不在这里删除。新档先写隐藏 staging，回读校验
                # 通过后同卷 rename 替换；切换前失败/取消/崩溃都不碰旧档。
                txn = archtxn.begin(job.id, target,
                                    par2_percent=opts["par2Percent"],
                                    par2_single=opts["par2Single"])
                opts["output"] = txn.staging
                opts["_txn"] = txn
            task = jobs_mod.make_compress_task(self.ctx.manager, opts)
            def prepared(job):
                task(job)
            self.ctx.manager._targets[job.id] = os.path.realpath(target)
            self.ctx.manager.submit(job, prepared)
            self._record_retry(job.id, "/api/compress", body)
        return self._json({"ok": True, "job": job.to_dict(), "target": target,
                           "encrypted": bool(pw),
                           "par2": opts["par2Percent"],
                           "namesEncrypted": bool(pw and fmt != "zip" and opts["encryptNames"])})

    def _extract(self, body):
        path = body.get("path") or ""
        if not path:
            return self._err("请选择压缩包")
        entry = self.ctx.engine.resolve(path)
        if not os.path.exists(entry):
            return self._err("文件不存在")
        # Partial extraction: validate the selection before a job exists, so
        # bad input is a clean 400 instead of a job that fails after the UI
        # already navigated to the task page.  `entries` absent = whole archive
        # (the old behaviour); an empty list is NOT "whole archive".
        partial = "entries" in body
        entries = None
        if partial:
            entries, err = _validate_entries(body.get("entries"))
            if err:
                return self._err(err)
        password, saved = self._password(body, entry)
        with self.ctx.inspect_lock:
            cached = self.ctx.inspect_cache.pop(entry, None)
            current = self._archive_stat(entry)
            if (cached is not None and current is not None
                    and cached.get("stat") == current and not cached.get("needsPassword")):
                self.ctx.inspect_cache[entry] = cached
                info = cached
            else:
                info = self._inspect_archive_locked(entry, password)
        if partial:
            if (info.get("type") or "").lower() in ("zst", "tar.zst") or info.get("single"):
                return self._err("该格式不支持只解压选中项：zstd 是单流格式，只能整包解压")
            try:
                full = self.ctx.engine.list_entries(entry, password)
            except eng.EngineError as exc:
                return self._err(str(exc))
            if not full:
                # The cached inspect entry can be from a successful run with a
                # different password, so never trust info["needsPassword"] here:
                # an unlistable archive has no verifiable entries either way.
                return self._err("无法读取归档目录，无法校验所选条目（请先输入正确密码，或该压缩包为空）")
            # The API contract is "a selected directory means its whole
            # subtree"; drop descendants that are already covered by a selected
            # ancestor so redundant ancestor+descendant parameters are harmless.
            entries = _prune_covered_entries(entries, full)
            try:
                total = _selected_bytes(entries, full)
            except ValueError as exc:
                return self._err(str(exc))
        else:
            total = int(info.get("uncompressed") or 0)
        dest_root = body.get("destDir") or os.path.dirname(entry)
        if not os.path.isdir(dest_root):
            try:
                os.makedirs(dest_root, exist_ok=True)
            except OSError:
                return self._err("目标位置不存在或不可写")
        need = int(total or 0)
        if need > 0:
            try:
                free = shutil.disk_usage(dest_root).free
            except OSError:
                free = 0
            if free and free < need + 64 * 1024 * 1024:
                return self._err("磁盘剩余空间不足：还需要约 %s，可用 %s"
                                 % (util.human_size(need), util.human_size(free)))
        with self.ctx.manager._cv:
            if body.get("autoFolder", True):
                stem = _archive_stem(entry)
                dest = os.path.join(dest_root, stem)
                if self._target_exists(dest):
                    on_exists = str(body.get("onExists") or "")
                    if on_exists == "rename":
                        dest = self._unique_target(dest)
                    elif on_exists != "merge":
                        return self._json({
                            "ok": False, "code": "exists", "dest": dest,
                            "suggestion": self._unique_target(dest),
                            "error": "已存在同名文件夹",
                        }, 409)
            else:
                dest = dest_root
            opts = {
                "archive": entry,
                "dest": dest,
                "password": password,
                "overwrite": body.get("overwrite") or self.ctx.settings.get("overwrite"),
                "format": info.get("type") or "",
                "totalBytes": total,
            }
            if entries:
                opts["entries"] = list(entries)
            label = "解压 " + os.path.basename(entry)
            job = jobs_mod.Job("extract", label, subtitle=os.path.basename(dest),
                               srcPath=entry, destPath=dest, planned=dest,
                               secrets=[password], bucket=dest_root, total=total,
                               format=(info.get("label") or ""))
            remembered = (self.ctx.saved_passwords.remember(
                entry, password, opt_in=True, auth=self.ctx.settings.get("passwordAuth", "off"))
                          if body.get("remember") is True else False)
            self.ctx.manager._targets[job.id] = os.path.realpath(dest)
            self.ctx.manager.submit(job, jobs_mod.make_extract_task(self.ctx.manager, opts))
            self._record_retry(job.id, "/api/extract", body)
        return self._json({"ok": True, "job": job.to_dict(), "dest": dest,
                           "partial": len(entries) if entries else 0,
                           "savedPasswordUsed": saved, "passwordRemembered": remembered})

    def _test(self, body):
        path = body.get("path") or ""
        if not path:
            return self._err("请选择压缩包")
        entry = self.ctx.engine.resolve(path)
        password, saved = self._password(body, entry)
        job = jobs_mod.Job("test", "校验 " + os.path.basename(entry), secrets=[password],
                           srcPath=entry, planned=entry,
                           format=(body.get("info") or {}).get("label", ""))
        remembered = (self.ctx.saved_passwords.remember(
            entry, password, opt_in=True, auth=self.ctx.settings.get("passwordAuth", "off"))
                      if body.get("remember") is True else False)
        self.ctx.manager.submit(
            job, jobs_mod.make_test_task(self.ctx.manager, entry, password))
        self._record_retry(job.id, "/api/test", body)
        return self._json({"ok": True, "job": job.to_dict(),
                           "savedPasswordUsed": saved, "passwordRemembered": remembered})


# ----------------------------------------------------------------- small helpers
def _default_name(inputs) -> str:
    if not inputs:
        return "归档"
    base = os.path.basename(inputs[0].rstrip("/")) or "归档"
    if len(inputs) == 1 and os.path.isfile(inputs[0]):
        base = os.path.splitext(base)[0]
    return base


def _join_inputs(inputs) -> str:
    if len(inputs) == 1:
        return os.path.basename(inputs[0].rstrip("/"))
    return f"{os.path.basename(inputs[0].rstrip('/'))} 等 {len(inputs)} 项"


def _archive_stem(path: str) -> str:
    name = os.path.basename(path)
    name = re.sub(r"\.(zip|7z|rar|tar|gz|bz2|xz|zst|tzst)\.\d{3}$", "", name, flags=re.I)
    name = re.sub(r"\.tar\.(zst|gz|bz2|xz)$", "", name, flags=re.I)
    name = re.sub(r"\.(zip|7z|rar|tar|gz|bz2|xz|zst|tzst)$", "", name, flags=re.I)
    name = re.sub(r"\.part\d+$", "", name, flags=re.I)
    return util.sanitize_name(name or "解压结果")


def _remove_target(target: str, engine) -> None:
    for f in engine.parts(engine.resolve(target)) or [target]:
        try:
            if os.path.exists(f):
                os.remove(f)
        except OSError:
            pass


# ------------------------------------------------- partial extract selection
_MAX_PARTIAL_ENTRIES = 1000
_ENTRY_WILDCARD = re.compile(r"[*?\[\]]")


def _validate_entries(raw):
    """Syntax-only validation of an /api/extract `entries` value.

    Returns (normalised entries, error).  Rules are deliberately strict and
    documented in docs/ENGINEERING-NOTES.md: archive paths use "/" only, no
    absolute paths, no "."/".."/empty segments, no wildcards (7-Zip would read
    them as patterns), no leading "-"/"@" (7-Zip would read them as switches or
    a list file), no duplicates, and an empty list never means "whole archive".
    """
    if not isinstance(raw, list):
        return None, "entries 必须是字符串列表"
    if not raw:
        return None, "entries 不能为空：整包解压请省略 entries"
    if len(raw) > _MAX_PARTIAL_ENTRIES:
        return None, f"一次最多选择 {_MAX_PARTIAL_ENTRIES} 个条目"
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, str):
            return None, "entries 必须是字符串列表"
        if not item.strip():
            return None, "entries 不能包含空项"
        if "\x00" in item:
            return None, "entries 不能包含 NUL 字符"
        if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in item):
            return None, "entries 不能包含控制字符"
        if "\\" in item:
            return None, "entries 只接受 / 作为路径分隔符"
        if item.startswith("/") or re.match(r"^[A-Za-z]:", item):
            return None, "entries 不能是绝对路径"
        if item[:1] in ("-", "@"):
            return None, "entries 不能以 - 或 @ 开头"
        if _ENTRY_WILDCARD.search(item):
            return None, "entries 不支持通配符"
        if item.endswith("/"):
            return None, "目录条目不要带结尾斜杠"
        if any(seg in ("", ".", "..") for seg in item.split("/")):
            return None, "entries 不能包含空段、. 或 .. 路径穿越"
        if item in seen:
            return None, "entries 含重复条目：" + item
        seen.add(item)
        out.append(item)
    return out, ""


def _prune_covered_entries(entries, full):
    """Remove descendants already covered by a selected ancestor directory.

    The public contract keeps "directory entry = whole subtree"; redundant
    ancestor+descendant parameters therefore mean the same thing and must not
    produce duplicate work or a rejection.
    """
    selected = set(entries)
    dirs = {(e.get("path") or "").rstrip("/") for e in full if e.get("isDir")}
    kept = []
    for name in entries:
        parts = name.split("/")
        covered = any(
            "/".join(parts[:i]) in selected and "/".join(parts[:i]) in dirs
            for i in range(1, len(parts)))
        if not covered:
            kept.append(name)
    return kept


def _selected_bytes(entries, full):
    """Membership + selected-size check. A selected directory covers its whole
    subtree (the engine's own rule for a directory name)."""
    known = {(e.get("path") or "").rstrip("/"): e for e in full}
    missing = [name for name in entries if name not in known]
    if missing:
        raise ValueError("条目不属于该压缩包：" + missing[0])

    def selected(path):
        norm = (path or "").rstrip("/")
        return any(norm == sel or norm.startswith(sel + "/") for sel in entries)

    return sum(int(e.get("size") or 0) for e in full
               if not e.get("isDir") and selected(e.get("path") or ""))


class _ArcaHTTPServer(ThreadingHTTPServer):
    """A client that disconnects mid-request (e.g. a UI/probe closing its
    socket while the app shuts down) is not a server error.  Only these two
    connection-teardown exceptions are kept out of the log; every other
    uncaught exception still goes through socketserver's normal traceback."""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def confirm_app_update(ctx, timeout: float = 20.0) -> None:
    """确认待处理的 App 更新（T05）。

    由服务线程之外的后台线程调用：页面真正可用（/api/ui-ready 四项自检通过）
    才让 Updater 删除旧包；超时就保留旧包与恢复标记。headless 进程没有网页，
    永远不会收到 ui-ready，因此直接按"未就绪"处理，旧包留给下次启动或人工恢复。
    """
    updater = getattr(ctx, "app_updater", None)
    if updater is None or not updater.marker.exists():
        return
    if getattr(ctx, "headless", False):
        updater.startup(ready=False)
        return
    ready = ctx.ui_ready.wait(timeout)
    checks = dict(getattr(ctx, "ui_ready_checks", {}) or {})
    if not ready:
        print("[arca] 页面就绪确认超时（%.0fs），保留旧版本与恢复标记: %s"
              % (timeout, json.dumps(checks, ensure_ascii=False)), file=sys.stderr)
    updater.startup(ready=ready)


class Server:
    def __init__(self, ctx: AppContext):
        cls = type("BoundHandler", (Handler,), {"ctx": ctx})
        self.httpd = _ArcaHTTPServer(("127.0.0.1", 0), cls)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self):
        self.thread.start()
        ctx = self.httpd.RequestHandlerClass.ctx
        updater = getattr(ctx, "app_updater", None)
        if updater is not None and updater.marker.exists():
            # 不阻塞窗口/网页创建：确认在后台等页面的 /api/ui-ready。
            threading.Thread(target=confirm_app_update, args=(ctx,),
                             daemon=True, name="arca-update-confirm").start()

    def stop(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass
        try:
            self.thread.join(timeout=5)
        except Exception:
            pass
