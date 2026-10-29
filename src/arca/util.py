"""Shared helpers for Arca."""
from __future__ import annotations

import atexit
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata

APP_NAME = "Arca"
APP_VERSION = "1.1.0"
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
    # ARCA_HOME 只替换"用户主目录"，便于测试/多配置目录隔离运行；
    # 缺省仍是 ~/Library/Application Support/Arca。
    home = os.environ.get("ARCA_HOME") or os.path.expanduser("~")
    path = os.path.join(home, "Library", "Application Support", APP_NAME)
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


def _fsync_directory(directory: str) -> None:
    """Best-effort fsync of a directory so a rename survives a crash."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_json(path: str, data, *, strict: bool = False) -> bool:
    """Atomically write *data* as UTF-8 JSON.

    The temporary file is created in the destination directory with a unique
    name, flushed and fsynced, then renamed over the destination.  A failure
    leaves the previous file untouched and returns False; callers that need a
    hard error pass ``strict=True``.  Best-effort state writers (task journal,
    history) keep the app running on False, but the degradation is logged to
    stderr instead of being swallowed silently.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        tmp = None
        _fsync_directory(directory)
        return True
    except Exception as exc:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if strict:
            raise
        print(f"[arca] 状态写入失败（保留旧文件）: {path}: {exc}", file=sys.stderr)
        return False


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


# ---------------------------------------------------------------- 写入边界
# T04a: 归档解压的路径与链接契约。字符串层面的检查只负责「明显危险」的
# 名字；目录内既有符号链接/被替换的中间目录由 path_within() 在每级写入前
# 再看一次。外部进程在检查之后替换目录的 TOCTOU 竞争无法仅靠 wrapper 消除。
_SYMLINK_MODE = 0o120000


def name_escape(name: str) -> bool:
    """条目名是否试图逃出解压目录（绝对路径、盘符、``..`` 段、NUL）。"""
    if not isinstance(name, str) or not name:
        return False
    raw = name.replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return True
    if "\x00" in raw:
        return True
    return any(seg == ".." for seg in raw.split("/") if seg)


def safe_join(root: str, name: str) -> str | None:
    """把归档条目名安全地拼到 root 下；不安全的名字返回 None。"""
    if not isinstance(name, str) or not name:
        return None
    raw = name.replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or "\x00" in raw:
        return None
    parts = []
    for seg in raw.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            return None
        parts.append(seg)
    if not parts:
        return None
    return os.path.join(root, *parts)


def path_within(root: str, path: str) -> bool:
    """root 以内已存在的每一段都不得是符号链接；target 必须词法上位于 root 内。

    目标目录本身可以还没创建（任务会先 makedirs）；一旦某一段已存在且是
    symlink，就拒绝继续写入——这是「目录内预先放好的链接把文件引到外面」
    的防线。检查之后被外部进程替换的竞争无法用纯 Python 消除。
    """
    root_abs = os.path.abspath(root)
    target_abs = os.path.abspath(path)
    if target_abs != root_abs and not target_abs.startswith(root_abs + os.sep):
        return False
    rel = os.path.relpath(target_abs, root_abs)
    probe = root_abs
    for part in ([] if rel == "." else rel.split(os.sep)):
        if part in ("", "."):
            continue
        probe = os.path.join(probe, part)
        try:
            st = os.lstat(probe)
        except FileNotFoundError:
            return True          # 更深的部分尚未创建；已检查的父级都是真实目录
        except OSError:
            return False
        if stat.S_ISLNK(st.st_mode):
            return False
    return True


def symlink_mode(mode: int) -> bool:
    """ZIP external_attr 高位里的 Unix 模式是否为符号链接。"""
    return (mode & 0o170000) == _SYMLINK_MODE


_INSTANCE_LOCK_FD = None


def _release_instance_lock() -> None:
    global _INSTANCE_LOCK_FD
    fd, _INSTANCE_LOCK_FD = _INSTANCE_LOCK_FD, None
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def acquire_instance_lock():
    """同一 App Support 目录只允许一个 Arca 实例。

    返回 (True, "") 表示当前进程拿到锁；返回 (False, holder) 表示另一个
    实例正持有（holder 是对方写入的 pid/home 行，可能为空）。锁在进程退出时
    由 atexit 释放；不阻塞、不做请求转交。
    """
    global _INSTANCE_LOCK_FD
    if _INSTANCE_LOCK_FD is not None:
        return True, ""
    path = state_path("instance.lock")
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        # 连锁文件都建不出来时不额外阻塞启动：后续状态写入会各自降级。
        return True, ""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            holder = os.pread(fd, 512, 0).decode("utf-8", "replace").strip()
        except OSError:
            holder = ""
        try:
            os.close(fd)
        except OSError:
            pass
        return False, holder
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, ("pid=%d home=%s\n" % (os.getpid(), app_support_dir())).encode(),
                  0)
    except OSError:
        pass
    _INSTANCE_LOCK_FD = fd
    atexit.register(_release_instance_lock)
    return True, ""
