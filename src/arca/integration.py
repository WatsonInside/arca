"""Finder integration: 右键服务 + 系统默认打开方式.

macOS gives a normal app (no app extension, no privileged helper) exactly two
hooks, and this module owns both:

* ``NSServices`` -- the Finder context menu entries "用 Arca 压缩" / "用 Arca
  解压".  Declared in Info.plist, delivered as ``compressSelection:userData:
  error:`` / ``extractSelection:userData:error:`` on the app delegate.
* The LaunchServices default handler for archive content types -- i.e. what a
  double click on a .zip opens.  Set per user, no admin rights, and reversible.

What macOS does *not* have is a "default compression app": compressing is a
Finder action, not a document association.  Arca fills that slot with the
service above plus a preferred output format, and the settings sheet says so
instead of shipping a switch that would silently do nothing.

Everything here is best effort.  Reading and writing the association goes
through CoreServices with ctypes (``LSSetDefaultRoleHandlerForContentType`` and
friends) so the bundled interpreter needs no extra pyobjc framework.
"""
from __future__ import annotations

import ctypes
import os
import platform
import plistlib
import re
import subprocess
import time
import urllib.parse
import uuid

from . import util

CORE_SERVICES = "/System/Library/Frameworks/CoreServices.framework/CoreServices"
CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"

# kLSRolesMask values from LaunchServices/LSInfo.h
LS_ROLES_ALL = 0xFFFFFFFF
# kCFStringEncodingUTF8
CF_UTF8 = 0x08000100
# kUTTagClassFilenameExtension
TAG_FILENAME_EXTENSION = "public.filename-extension"

# The extensions Arca can actually read.  "tar.zst" is not a real extension as
# far as LaunchServices is concerned (it matches on the last dot), so only the
# last component is listed; the ext label stays human friendly.
ARCHIVE_EXTS = [
    ("zip", "ZIP"),
    ("7z", "7Z"),
    ("rar", "RAR"),
    ("tar", "TAR"),
    ("gz", "GZ"),
    ("tgz", "TGZ"),
    ("bz2", "BZ2"),
    ("xz", "XZ"),
    ("zst", "ZST"),
]

# What the settings sheet lists as "系统默认打开方式" -- the rest (gz/xz/...)
# are still registered, they would just make the list unreadable.
HEADLINE_EXTS = ("zip", "7z", "rar", "tar", "zst")

# Enough to cover 7z / zip / rar / tar.gz / tar.zst and split volumes.
ARCHIVE_RE = re.compile(
    r"\.(zip|7z|rar|tar|gz|tgz|bz2|xz|zst|z)"
    r"(\.\d{3}|\.part\d+\.rar|\.r\d{2}|\.z\d{2})?$", re.I)

# Extensions the app can *create* -- used by the "右键压缩格式" picker.
CREATABLE = [("", "跟随默认格式"), ("zip", "ZIP"), ("7z", "7Z"), ("tar.zst", "ZST")]


def is_archive(path: str) -> bool:
    return bool(ARCHIVE_RE.search(path or ""))


def ext_label(ext: str) -> str:
    return next((lbl for e, lbl in ARCHIVE_EXTS if e == ext), ext.upper())


# --------------------------------------------------------------------- bridge
class _LaunchServices:
    """Thin ctypes wrapper; ``ok`` is False when anything is missing."""

    def __init__(self):
        self.ok = False
        self.error = ""
        try:
            self.cf = ctypes.CDLL(CORE_FOUNDATION)
            self.cs = ctypes.CDLL(CORE_SERVICES)
        except OSError as exc:                       # pragma: no cover - rare
            self.error = f"无法加载 CoreServices：{exc}"
            return
        try:
            self.cf.CFStringCreateWithCString.restype = ctypes.c_void_p
            self.cf.CFStringCreateWithCString.argtypes = [
                ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
            self.cf.CFStringGetCString.restype = ctypes.c_bool
            self.cf.CFStringGetCString.argtypes = [
                ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
            self.cf.CFRelease.restype = None
            self.cf.CFRelease.argtypes = [ctypes.c_void_p]

            self.cs.UTTypeCreatePreferredIdentifierForTag.restype = ctypes.c_void_p
            self.cs.UTTypeCreatePreferredIdentifierForTag.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]

            self.cs.LSCopyDefaultRoleHandlerForContentType.restype = ctypes.c_void_p
            self.cs.LSCopyDefaultRoleHandlerForContentType.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32]

            self.cs.LSSetDefaultRoleHandlerForContentType.restype = ctypes.c_int32
            self.cs.LSSetDefaultRoleHandlerForContentType.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
        except AttributeError as exc:                # pragma: no cover
            self.error = f"CoreServices 符号缺失：{exc}"
            return
        self.ok = True

    # -- CFString helpers ---------------------------------------------------
    def _cf(self, text):
        return self.cf.CFStringCreateWithCString(None, text.encode("utf-8"), CF_UTF8)

    def _str(self, ref, release=True):
        if not ref:
            return ""
        buf = ctypes.create_string_buffer(2048)
        got = self.cf.CFStringGetCString(ref, buf, len(buf), CF_UTF8)
        out = buf.value.decode("utf-8", "replace") if got else ""
        if release:
            self.cf.CFRelease(ref)
        return out

    # -- queries ------------------------------------------------------------
    def uti_for_ext(self, ext: str) -> str:
        if not self.ok:
            return ""
        tag_class = self._cf(TAG_FILENAME_EXTENSION)
        tag = self._cf(ext)
        try:
            ref = self.cs.UTTypeCreatePreferredIdentifierForTag(tag_class, tag, None)
        finally:
            self.cf.CFRelease(tag_class)
            self.cf.CFRelease(tag)
        return self._str(ref)

    def handler_for_uti(self, uti: str) -> str:
        """Bundle id of the current default handler, '' when never chosen."""
        if not self.ok or not uti:
            return ""
        ref = self._cf(uti)
        try:
            out = self.cs.LSCopyDefaultRoleHandlerForContentType(ref, LS_ROLES_ALL)
        finally:
            self.cf.CFRelease(ref)
        return self._str(out)

    def set_handler(self, uti: str, bundle_id: str | None) -> int:
        """Assign (or reset with None) the default handler. Returns OSStatus."""
        if not self.ok or not uti:
            return -1
        ref = self._cf(uti)
        handler = self._cf(bundle_id) if bundle_id else None
        try:
            return int(self.cs.LSSetDefaultRoleHandlerForContentType(
                ref, LS_ROLES_ALL, handler))
        finally:
            self.cf.CFRelease(ref)
            if handler:
                self.cf.CFRelease(handler)


_LS = _LaunchServices()


# ------------------------------------------------------------------- bundling
def bundle_path() -> str:
    """Path of the running .app, or '' when run straight from the sources."""
    try:
        from Foundation import NSBundle
        path = str(NSBundle.mainBundle().bundlePath() or "")
        return path if path.endswith(".app") else ""
    except Exception:
        return ""


def register_bundle() -> bool:
    """Re-read Info.plist with LaunchServices.

    Needed because Arca can be replaced in place by the installer; without this
    the system keeps the old document types (and no Finder service) until the
    next login.
    """
    app = bundle_path()
    if not app:
        return False
    tool = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
            "LaunchServices.framework/Support/lsregister")
    if not os.path.exists(tool):
        return False
    try:
        subprocess.run([tool, "-f", app], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30, check=False)
        return True
    except Exception:
        return False


def _app_name(bundle_id: str) -> str:
    if not bundle_id:
        return "系统默认"
    if bundle_id == util.BUNDLE_ID:
        return util.APP_NAME
    try:
        from AppKit import NSWorkspace
        url = NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(bundle_id)
        if url:
            return os.path.splitext(os.path.basename(str(url.path())))[0]
    except Exception:
        pass
    known = {
        "com.apple.archiveutility": "归档实用工具",
        "com.apple.finder": "Finder",
        "com.apple.Preview": "预览",
        "com.apple.TextEdit": "文本编辑",
        "com.apple.dt.Xcode": "Xcode",
        "com.google.Chrome": "Chrome",
        "com.apple.Safari": "Safari",
    }
    return known.get(bundle_id, bundle_id)


# --------------------------------------------------------------------- types
def types() -> list[dict]:
    """One row per archive extension: which app owns it right now."""
    out = []
    for ext, label in ARCHIVE_EXTS:
        uti = _LS.uti_for_ext(ext)
        bundle = _LS.handler_for_uti(uti) if uti else ""
        out.append({
            "ext": ext,
            "label": label,
            "uti": uti,
            "handler": bundle,
            "handlerName": _app_name(bundle),
            "ours": bundle == util.BUNDLE_ID,
            "headline": ext in HEADLINE_EXTS,
        })
    return out


def set_default(on: bool = True, dry: bool = False) -> dict:
    """Make Arca (or the system default) the handler for every archive type."""
    if not _LS.ok:
        return {"ok": False, "error": _LS.error or "无法访问 LaunchServices"}
    if not bundle_path() and not dry:
        return {"ok": False,
                "error": "只有以 .app 方式安装后才能设为默认（当前是从源码运行）"}
    if not dry:
        register_bundle()
    results = []
    for row in types():
        uti = row["uti"]
        target = util.BUNDLE_ID if on else None
        code = 0 if dry else _LS.set_handler(uti, target)
        results.append({"ext": row["ext"], "uti": uti, "code": code,
                        "label": row["label"]})
    if dry:
        # Nothing was written, so report what *would* be true afterwards --
        # including the app name, otherwise the preview mixes old and new.
        target = util.BUNDLE_ID if on else ""
        after = [dict(r, handler=target, handlerName=_app_name(target), ours=on)
                 for r in types()]
    else:
        after = types()
    failed = [r for r in results if r["code"] != 0]
    return {
        "ok": True,
        "dry": dry,
        "applied": len(results) - len(failed),
        # -50 paramErr means LaunchServices refused the *identifier*, not the
        # request: the extension only maps to a dynamic `dyn.*` type, which
        # cannot be assigned.  Say so instead of a bare error code.
        "failed": [{"ext": r["ext"], "code": r["code"],
                    "reason": ("该扩展名还没有稳定的类型标识（dyn.*），"
                               "重新安装应用以注册类型后可设置"
                               if r["code"] == -50 else "")}
                   for r in failed],
        "types": after,
        "ours": sum(1 for r in after if r.get("ours")),
    }


# ------------------------------------------------------------------ services
SERVICES = [
    {"id": "compress", "message": "compressSelection:",
     "title": "用 Arca 压缩",
     "note": "在 Finder 里选中文件或文件夹 → 右键 → 服务"},
    {"id": "extract", "message": "extractSelection:",
     "title": "用 Arca 解压",
     "note": "选中压缩包 → 右键 → 服务，或「打开方式 → Arca」"},
]


def _pbs_prefs() -> dict | None:
    """pbs.plist keeps the Services tick boxes; None means "cannot tell"."""
    try:
        from Foundation import NSUserDefaults
        store = NSUserDefaults.alloc().initWithSuiteName_("pbs")
        disabled = set(str(x) for x in (store.arrayForKey_("NSDisabledServices") or []))
        enabled = set(str(x) for x in (store.arrayForKey_("NSEnabledServices") or []))
    except Exception:
        return None
    if not disabled and not enabled:
        return None
    return {"disabled": disabled, "enabled": enabled}


def services() -> list[dict]:
    prefs = _pbs_prefs()
    out = []
    for svc in SERVICES:
        state = "unknown"
        if prefs:
            key = f"{util.BUNDLE_ID}.{svc['message'].rstrip(':')}"
            if any(k.endswith(svc["message"].rstrip(":")) for k in prefs["disabled"]) \
                    or key in prefs["disabled"]:
                state = "off"
            elif any(k.endswith(svc["message"].rstrip(":")) for k in prefs["enabled"]) \
                    or key in prefs["enabled"]:
                state = "on"
        out.append(dict(svc, state=state))
    return out


def services_prefs_url() -> str:
    """Deep link to the pane that holds the Services tick boxes."""
    try:
        major = int((platform.mac_ver()[0] or "13").split(".")[0])
    except ValueError:
        major = 13
    if major >= 13:
        return "x-apple.systempreferences:com.apple.Keyboard-Settings.extension"
    return "x-apple.systempreferences:com.apple.preference.keyboard?Shortcuts"


def open_services_prefs() -> bool:
    url = services_prefs_url()
    try:
        from AppKit import NSWorkspace, NSURL
        if NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url)):
            return True
    except Exception:
        pass
    try:
        subprocess.Popen(["/usr/bin/open", url])
        return True
    except Exception:
        return False


# --------------------------------------------------------- Quick Actions
# Finder's right-click entries for third-party apps go through NSServices,
# and on macOS 27 pbs only delivers (and only *shows*) services provided by
# Apple-platform or Developer-ID signed apps -- an ad-hoc or self-signed
# bundle is silently filtered out of the menu entirely.  A Quick Action
# (.workflow in ~/Library/Services) runs through the system's own
# WorkflowServiceRunner instead, so the provider signature does not matter.
# The workflow writes an intent flag and hands the selection to the app with
# `open -a Arca`; app-side `_pending_action()` consumes the flag.

_QUACTIONS = [
    ("用 Arca 压缩", "com.arca.quickaction.compress", "compress"),
    ("用 Arca 解压", "com.arca.quickaction.extract", "extract"),
]


def _quick_action_doc(action: str) -> bytes:
    """document.wflow for a Run Shell Script Quick Action.

    The action fields (AMAccepts / AMProvides / BundleIdentifier / ...) are
    required by Automator's loader; a minimal dict is rejected with -10811 at
    registration time.
    """
    support = util.app_support_dir()
    script = (
        f'printf "{action} $(date +%%s)" > "{support}/next-action"\n'
        'open -a "Arca" "$@"\n'
    )
    doc = {
        "AMApplicationBuild": "528",
        "AMDocumentVersion": "2",
        "actions": [{
            "action": {
                "ActionBundlePath":
                    "/System/Library/Automator/Run Shell Script.action",
                "ActionName": "Run Shell Script",
                "ActionParameters": {
                    "CheckedForUserDefaultShell": True,
                    "COMMAND_STRING": script,
                    "inputMethod": 1,
                    "shell": "/bin/zsh",
                    "source": "",
                },
                "AMAccepts": {"Container": "List", "Optional": True,
                              "Types": ["com.apple.cocoa.path"]},
                "AMActionVersion": "2.0.3",
                "AMApplication": ["Automator"],
                "AMParameterProperties": {
                    k: {} for k in ("CheckedForUserDefaultShell",
                                    "COMMAND_STRING", "inputMethod",
                                    "shell", "source")},
                "AMProvides": {"Container": "List",
                               "Types": ["com.apple.cocoa.string"]},
                "BundleIdentifier": "com.apple.RunShellScript",
                "CanShowSelectedItemsWhenRun": False,
                "CanShowWhenRun": True,
                "Category": ["AMCategoryUtilities"],
                "Class": "RunShellScriptAction",
                "Input": {"Container": "List", "Optional": True,
                          "Types": ["com.apple.cocoa.path"]},
                "Keywords": ["Shell"],
                "Output": {"Container": "List",
                           "Types": ["com.apple.cocoa.string"]},
                "Rating": 0,
                "ShowsUI": False,
                "SystemParameters": {},
                "Title": "Run Shell Script",
                "Type": "Configuration",
                "TypeDescription": "Configuration data for an action.",
            },
        }],
        "connectors": {},
        "workflowMetaData": {
            "workflowTypeIdentifier":
                "com.apple.Automator.servicesMenu",
            "serviceInputTypeIdentifier":
                "com.apple.Automator.fileSystemObject",
            "serviceOutputTypeIdentifier":
                "com.apple.Automator.nothing",
            "serviceProcessesInput": 0,
            "useAutomaticInputType": False,
        },
    }
    return plistlib.dumps(doc)


def quick_actions_installed() -> bool:
    missing = [name for name, _bid, _act in _QUACTIONS
               if not os.path.isdir(os.path.expanduser(
                   f"~/Library/Services/{name}.workflow"))]
    return not missing


def install_quick_actions() -> dict:
    """Write the two Quick Actions into ~/Library/Services and register them."""
    results = []
    failed = []
    for name, bundle_id, action in _QUACTIONS:
        root = os.path.expanduser(f"~/Library/Services/{name}.workflow")
        try:
            os.makedirs(os.path.join(root, "Contents", "Resources"),
                        exist_ok=True)
            doc = _quick_action_doc(action)
            for rel in ("Contents/document.wflow",
                        "Contents/Resources/document.wflow"):
                with open(os.path.join(root, rel), "wb") as fh:
                    fh.write(doc)
            info = {
                "CFBundleDevelopmentRegion": "English",
                "CFBundleExecutable": "",
                "CFBundleIdentifier": bundle_id,
                "CFBundleInfoDictionaryVersion": "6.0",
                "CFBundleName": name,
                "CFBundlePackageType": "BNDL",
                "CFBundleShortVersionString": "1.0",
                "CFBundleVersion": "1",
                "NSServices": [{
                    "NSMenuItem": {"default": name},
                    "NSMessage": "runWorkflowAsService",
                    "NSSendFileTypes": ["public.item"],
                }],
            }
            with open(os.path.join(root, "Contents", "Info.plist"), "wb") as fh:
                plistlib.dump(info, fh)
            r = subprocess.run(
                ["/System/Library/Frameworks/CoreServices.framework/"
                 "Frameworks/LaunchServices.framework/Support/lsregister",
                 "-f", root],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
            # lsregister exits non-zero on a Spotlight scan warning for
            # .workflow bundles even when registration itself succeeds; the
            # pbs daemon rescans ~/Library/Services on flush anyway.
            results.append({"name": name, "ok": True, "path": root,
                            "registerRc": r.returncode})
        except Exception as exc:  # noqa: BLE001
            results.append({"name": name, "ok": False, "error": repr(exc)})
            failed.append(name)
    if not failed:
        try:
            subprocess.run(["/System/Library/CoreServices/pbs", "-flush"],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": not failed, "installed": results, "failed": failed}


# -------------------------------------------------------------------- status
def status() -> dict:
    rows = types()
    ours = sum(1 for r in rows if r["ours"])
    return {
        "ok": True,
        "canSetDefault": _LS.ok and bool(bundle_path()),
        "lsError": _LS.error,
        "bundleId": util.BUNDLE_ID,
        "bundlePath": bundle_path(),
        "registered": bool(bundle_path()),
        "types": rows,
        "ours": ours,
        "total": len(rows),
        "services": services(),
        "servicesPrefsUrl": services_prefs_url(),
        "creatable": [{"id": i, "label": lbl} for i, lbl in CREATABLE],
        "compressNote": ("macOS 没有「默认压缩软件」这个设置：压缩属于文件操作，"
                         "不是文件关联。Arca 用上面的 Finder 服务承担这个角色，"
                         "并可选一个默认输出格式。"),
    }


# ---------------------------------------------------------------- pasteboard
def _url_to_path(text: str) -> str:
    if not text:
        return ""
    if text.startswith("file://"):
        parsed = urllib.parse.urlparse(text)
        return urllib.parse.unquote(parsed.path)
    return text


def pasteboard_paths(pboard) -> list[str]:
    """File paths from a Finder drag / service pasteboard."""
    raw = []
    try:
        value = pboard.propertyListForType_("NSFilenamesPboardType")
        if isinstance(value, (list, tuple)):
            raw = [str(v) for v in value]
    except Exception:
        raw = []
    if not raw:
        try:
            for item in (pboard.pasteboardItems() or []):
                text = item.stringForType_("public.file-url")
                if text:
                    raw.append(_url_to_path(str(text)))
        except Exception:
            pass
    if not raw:
        for kind in ("public.file-url", "NSFilenamesPboardType"):
            try:
                text = pboard.stringForType_(kind)
            except Exception:
                text = None
            if text:
                raw.append(_url_to_path(str(text)))
                break
    seen, out = set(), []
    for p in raw:
        p = (p or "").strip()
        if not p or p in seen:
            continue
        seen.add(p)
        if os.path.exists(p):
            out.append(os.path.abspath(p))
    return out


# --------------------------------------------------------------------- inbox
def make_item(action: str, paths) -> dict | None:
    """Normalise a Finder request into a queue entry for the web app."""
    clean = []
    for p in (paths or []):
        p = (p or "").strip()
        if p and os.path.exists(p):
            clean.append(os.path.abspath(p))
    # Drop duplicates but keep the Finder order (it drives the archive name).
    seen, ordered = set(), []
    for p in clean:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    if not ordered:
        return None
    if action not in ("compress", "extract"):
        action = "extract" if all(is_archive(p) for p in ordered) else "compress"
    return {
        "id": uuid.uuid4().hex[:12],
        "action": action,
        "paths": ordered,
        "source": "finder",
        "at": time.time(),
    }
