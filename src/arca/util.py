"""Shared helpers for Arca."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata

APP_NAME = "Arca"
APP_VERSION = "1.0.1"
BUNDLE_ID = "com.arca.archive"

UNITS = ("B", "KB", "MB", "GB", "TB")


def friendly_error(exc) -> str:
    """Turn an OS-level failure into something a person can act on.

    Returns "" for anything unrecognised: callers keep their own message then,
    so we never replace a precise engine error with a vague one.
    """
    import errno
    if isinstance(exc, (PermissionError,)):
        return "没有写入权限：换个保存位置，或在「显示简介」里放开这个文件夹的权限"
    if isinstance(exc, (FileNotFoundError,)):
        return "找不到文件：可能已被移动、重命名或删除"
    if isinstance(exc, OSError):
        code = exc.errno
        table = {
            errno.ENOSPC: "磁盘空间不足：清理一些空间，或换一个磁盘",
            errno.EROFS: "目标磁盘是只读的，不能写入",
            errno.ENAMETOOLONG: "文件名或路径太长，超出了系统限制",
            errno.EDQUOT: "磁盘配额已满，无法继续写入",
            errno.EMFILE: "打开的文件太多，请稍后再试",
            errno.ENOTDIR: "目标路径里有一段不是文件夹",
            errno.EISDIR: "目标是一个文件夹，不能当作文件写入",
        }
        if code in table:
            return table[code]
    return ""


def human_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    neg = n < 0
    n = abs(n)
    i = 0
    while n >= 1024 and i < len(UNITS) - 1:
        n /= 1024.0
        i += 1
    if i == 0:
        out = f"{int(n)} {UNITS[i]}"
    elif n >= 100:
        out = f"{n:.0f} {UNITS[i]}"
    else:
        out = f"{n:.1f} {UNITS[i]}"
    return ("-" if neg else "") + out


def scan_inputs(inputs, *, on_progress=None, cancel=None):
    import stat

    seen = set()
    count = total = 0

    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise RuntimeError("已取消")

    def count_file(path):
        nonlocal count, total
        check_cancel()
        info = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("Unknown input size")
        key = (info.st_dev, info.st_ino)
        if key not in seen:
            seen.add(key)
            count += 1
            total += info.st_size
            if on_progress is not None:
                on_progress(count, total)
        check_cancel()

    def walk_error(error):
        raise error

    for path in inputs:
        check_cancel()
        info = os.stat(path, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            for root, dirs, files in os.walk(path, onerror=walk_error):
                check_cancel()
                for directory in dirs:
                    check_cancel()
                    if os.path.islink(os.path.join(root, directory)):
                        raise OSError("Unknown input size")
                for filename in files:
                    count_file(os.path.join(root, filename))
        else:
            count_file(path)
    check_cancel()
    return count, total


def parse_size(value, default=None):
    """Accept 10485760, '10m', '700 MB', '1.5g'. Returns bytes or default."""
    if value is None or value == "" or value is False:
        return default
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().lower().replace(",", "")
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*([kmgt]?)b?", s)
    if not m:
        return default
    num = float(m.group(1))
    mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}[m.group(2)]
    return int(num * mult)


def app_support_dir() -> str:
    path = os.path.join(os.path.expanduser("~"), "Library", "Application Support", APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def state_path(name: str) -> str:
    return os.path.join(app_support_dir(), name)


def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def sanitize_name(name: str) -> str:
    name = unicodedata.normalize("NFC", (name or "").strip())
    name = name.replace("/", "-").replace(":", "-").replace("\0", "")
    name = name.strip(". ")
    return name or "归档"


def unique_path(path: str) -> str:
    """Return a non-existing sibling path ('name 2.ext')."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    if root.lower().endswith((".tar",)):
        pass
    i = 2
    while True:
        cand = f"{root} {i}{ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


def reveal_in_finder(paths) -> None:
    if isinstance(paths, str):
        paths = [paths]
    paths = [p for p in paths if p]
    if not paths:
        return
    try:
        from AppKit import NSWorkspace, NSURL
        urls = [NSURL.fileURLWithPath_(p) for p in paths if os.path.exists(p)]
        if urls:
            NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_(urls)
    except Exception:
        pass


def open_with_default(path: str) -> None:
    if path and os.path.exists(path):
        subprocess.Popen(["/usr/bin/open", path])


def open_url(url: str) -> bool:
    """Open an http(s) link in the user's browser.  Anything else is refused."""
    if not re.match(r"^https?://[\w.-]+", url or ""):
        return False
    try:
        from AppKit import NSWorkspace, NSURL
        return bool(NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url)))
    except Exception:
        try:
            subprocess.Popen(["/usr/bin/open", url])
            return True
        except Exception:
            return False


def beep() -> None:
    try:
        subprocess.Popen(["/usr/bin/afplay", "/System/Library/Sounds/Glass.aiff"])
    except Exception:
        pass


def tail(text: str, limit: int = 4000) -> str:
    text = (text or "").strip()
    return text[-limit:]


def which_all(names):
    found = []
    for n in names:
        p = shutil.which(n)
        if p:
            found.append(p)
    return found
