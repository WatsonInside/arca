"""Zstandard support (.zst / .tar.zst).

7-Zip has no zstd codec -- neither the bundled 23.01 nor the current 26.05
advertise one in `7zz i`, and Igor Pavlov has said the 7z container will not
gain one.  So a zstd "archive" here is really a tar stream piped through the
official zstd CLI, which is what every other tool means by `.tar.zst` anyway.

Two consequences are surfaced in the UI instead of being hidden:

* **no encryption** -- zstd has no password support, so the panel drops the
  password and filename-encryption rows for this format;
* **no volumes** -- splitting would have to be done by hand around a stream
  format, and a lost volume would poison everything after it.

Progress is measured on the *input* side: we count bytes fed into the tar
stream, which is the only number that is known up front.
"""
from __future__ import annotations

import os
import re
import subprocess
import tarfile
import threading
import time

from . import procs

from . import util

# UI slider position -> zstd level.  The top two positions both land on 19:
# 20-22 need --ultra, gain almost nothing and double the time.
LEVELS = (1, 3, 7, 12, 19, 19)

_META = [".DS_Store", "__MACOSX", "._", ".Spotlight-V100", ".Trashes", ".fseventsd"]


class ZstdError(Exception):
    pass


# --------------------------------------------------------------------- binary
def candidates():
    here = os.path.dirname(os.path.abspath(__file__))
    return [
        # A user-updated copy wins over the bundled one (components.py writes
        # it here), which is what makes the update actually take effect.
        os.path.join(util.app_support_dir(), "components", "zstd"),
        os.path.join(here, "bin", "zstd"),
        os.path.join(here, "..", "vendor", "zstd"),
        os.path.join(here, "..", "..", "vendor", "zstd"),
        os.path.join(here, "..", "..", "..", "vendor", "zstd"),
    ]


def find_binary(user_path: str | None = None) -> tuple[str | None, str]:
    """Return (path, source) where source is user / bundled / path / none."""
    if user_path and os.path.isfile(user_path) and os.access(user_path, os.X_OK):
        return user_path, "user"
    home = os.path.abspath(util.app_support_dir())
    for p in candidates():
        p = os.path.abspath(p)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            # Installed by the component updater -> "user", not "bundled".
            return p, ("user" if p.startswith(home + os.sep) else "bundled")
    import shutil
    p = shutil.which("zstd")
    return (p, "path") if p else (None, "none")


def version_of(path: str | None) -> str:
    if not path:
        return ""
    try:
        p = subprocess.run([path, "--version"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=10)
        m = re.search(r"v([0-9]+\.[0-9]+\.[0-9]+)", p.stdout.decode("utf-8", "replace"))
        return m.group(1) if m else ""
    except Exception:
        return ""


def level_for(pos) -> int:
    try:
        pos = int(pos)
    except (TypeError, ValueError):
        pos = 3
    return LEVELS[min(len(LEVELS) - 1, max(0, pos))]


def is_zstd(path: str) -> bool:
    return os.path.basename(path).lower().endswith((".zst", ".tzst"))


# -------------------------------------------------------------------- writing
class _Counter:
    """File-like sink that counts bytes on their way into zstd's stdin."""

    def __init__(self, fh, on_bytes):
        self.fh = fh
        self.on_bytes = on_bytes

    def write(self, data):
        n = self.fh.write(data)
        self.on_bytes(n if n is not None else len(data))
        return n

    def flush(self):
        try:
            self.fh.flush()
        except Exception:
            pass


def _wanted(name: str, exclude_meta: bool) -> bool:
    if not exclude_meta:
        return True
    base = os.path.basename(name)
    for pat in _META:
        if base == pat or base.startswith(pat):
            return False
    for part in name.split(os.sep):
        if part in (".Spotlight-V100", ".Trashes", ".fseventsd", "__MACOSX"):
            return False
    return True


def _iter_files(paths: list[str], base: str, exclude_meta: bool):
    """Yield (kind, abs_path, arcname, size) -- directories included."""
    for root_path in paths:
        abs_root = os.path.abspath(root_path)
        if os.path.isdir(abs_root):
            for root, dirs, files in os.walk(abs_root):
                dirs[:] = sorted(d for d in dirs if _wanted(d, exclude_meta))
                yield ("dir", root, os.path.relpath(root, base), 0)
                for f in sorted(files):
                    if not _wanted(f, exclude_meta):
                        continue
                    full = os.path.join(root, f)
                    yield ("file", full, os.path.relpath(full, base), _size(full))
        elif os.path.isfile(abs_root):
            yield ("file", abs_root, os.path.relpath(abs_root, base), _size(abs_root))


def _size(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def _reap_child(proc, timeout: float = 5.0) -> None:
    """Stop and reap a direct zstd child, then close every pipe.

    Cleanup must never replace the exception that caused it: only OSError-ish
    cleanup is ignored, and close/wait/timeout handling is bounded.  This is
    separate from _Stream.close (already validated) and engine.run().
    """
    if proc is None:
        return
    if proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
        except Exception:
            pass
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(proc, name, None)
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


def compress(paths: list[str], base: str, out_path: str, level: int = 3,
             exclude_meta: bool = True, threads: int = 0,
             on_progress=None, cancel: threading.Event | None = None) -> dict:
    """Write a .tar.zst.  Returns {"bytes", "files", "log"}."""
    binary, _src = find_binary()
    if not binary:
        raise ZstdError("未找到 zstd 程序，请在设置 → 组件里安装")

    total = 0
    plan = []
    for kind, full, arc, size in _iter_files(paths, base, exclude_meta):
        plan.append((kind, full, arc, size))
        total += size

    started = time.time()
    written = [0]
    last = [0.0, 0.0, 0.0]          # ts, bytes, speed

    def bump(n):
        written[0] += n
        if not on_progress:
            return
        now = time.time()
        if last[0] and now - last[0] > 0.25:
            inst = (written[0] - last[1]) / (now - last[0])
            last[2] = inst if not last[2] else last[2] * 0.7 + inst * 0.3
            last[0], last[1] = now, written[0]
        elif not last[0]:
            last[0], last[1] = now, written[0]
        pct = min(99.0, written[0] * 100.0 / total) if total else 0.0
        on_progress(pct, "", last[2] or 0.0)

    level = level_for(level) if level <= 5 else max(1, min(19, int(level)))
    # No --stream-size: it has to match the byte count *exactly*, and a tar
    # stream is never exactly the sum of its members (headers, padding).  The
    # unpacked size is recovered from the tar index instead, which the
    # inspector already walked before the job started.
    cmd = [binary, "-q", "-%d" % level, "--check", "-T%d" % (threads or 0)]
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "wb") as out:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=out,
                                stderr=subprocess.PIPE, bufsize=0)
        procs.register(proc)
        try:
            sink = _Counter(proc.stdin, bump)
            files = 0
            try:
                # mode "w|" = streaming tar, no seeking required: that is what
                # lets the archive go straight into zstd instead of a temp file
                # first.
                with tarfile.open(fileobj=sink, mode="w|",
                                  format=tarfile.PAX_FORMAT) as tf:
                    for kind, full, arc, size in plan:
                        if cancel is not None and cancel.is_set():
                            raise ZstdError("已取消")
                        if kind == "dir":
                            if arc in (".", ""):
                                continue
                            info = tarfile.TarInfo(arc.replace(os.sep, "/"))
                            info.type = tarfile.DIRTYPE
                            info.mode = 0o755
                            # TarInfo defaults to mtime 0, which the preview
                            # would show as 1970 -- carry the real directory
                            # time over.
                            try:
                                info.mtime = int(os.stat(full).st_mtime)
                            except OSError:
                                pass
                            tf.addfile(info)
                            continue
                        try:
                            st = os.stat(full)
                        except OSError:
                            continue
                        info = tarfile.TarInfo(arc)
                        info.size = st.st_size
                        info.mtime = int(st.st_mtime)
                        info.mode = st.st_mode & 0o7777
                        with open(full, "rb") as fh:
                            # Feed in chunks so progress moves and cancel is
                            # noticed even inside a single huge file.
                            class _Chunk:
                                def __init__(self, f):
                                    self.f = f

                                def read(self, n=-1):
                                    if cancel is not None and cancel.is_set():
                                        raise ZstdError("已取消")
                                    return self.f.read(n)

                            tf.addfile(info, _Chunk(fh))
                        files += 1
            except ZstdError:
                try:
                    os.remove(out_path)
                except OSError:
                    pass
                raise
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            err = (proc.stderr.read().decode("utf-8", "replace")
                   if proc.stderr else "")
            try:
                code = proc.wait(timeout=5)
                if code != 0:
                    raise ZstdError(_hint(err)
                                    or f"zstd 压缩失败（退出码 {code}）")
            except subprocess.TimeoutExpired:
                raise ZstdError("zstd 压缩进程未退出")
        finally:
            procs.unregister(proc)
            _reap_child(proc)

    if on_progress:
        on_progress(100.0, "", last[2] or 0.0)
    return {"bytes": os.path.getsize(out_path), "files": files,
            "seconds": time.time() - started, "total": total}


# -------------------------------------------------------------------- reading
def listing(path: str) -> dict:
    """Describe a .zst / .tar.zst the way engine.inspect() describes a 7z."""
    binary, _src = find_binary()
    if not binary:
        raise ZstdError("未找到 zstd 程序，请在设置 → 组件里安装")
    phys = os.path.getsize(path)
    size_text = _frame_size(binary, path)

    if not _looks_like_tar_zst(binary, path):
        name = os.path.basename(path)
        name = re.sub(r"\.(zst|tzst)$", "", name, flags=re.I) or "文件"
        entries = [{
            "path": name, "size": size_text, "packed": phys,
            "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))),
            "isDir": False, "encrypted": False, "crc": "", "method": "Zstandard",
        }]
        return {
            "entries": entries, "size": phys, "uncompressed": size_text,
            "method": "Zstandard", "solid": False, "truncated": 0,
            "type": "zst", "label": "ZST", "single": True,
        }

    entries = []
    total = 0
    truncated = 0
    with _decompress_stream(binary, path) as stream:
        with tarfile.open(fileobj=stream, mode="r|") as tf:
            for m in tf:
                if m.isdir():
                    entries.append(_entry(m.name.rstrip("/"), 0, True, m))
                    continue
                if not m.isfile():
                    continue
                total += m.size
                if len(entries) < 800:
                    entries.append(_entry(m.name, m.size, False, m))
                else:
                    truncated += 1
    return {
        "entries": entries, "size": phys, "uncompressed": total,
        "method": "Zstandard · TAR", "solid": False, "truncated": truncated,
        "type": "tar.zst", "label": "TAR.ZST", "single": False,
    }


def _entry(name, size, is_dir, member) -> dict:
    return {
        "path": name, "size": size, "packed": 0,
        "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(member.mtime or 0)),
        "isDir": is_dir, "encrypted": False, "crc": "", "method": "Zstandard",
    }


class _Stream:
    """Subprocess stdout wrapper that can be closed from another thread."""

    def __init__(self, proc, on_bytes=None):
        self.proc = proc
        self.raw = proc.stdout
        self.on_bytes = on_bytes

    def read(self, n=-1):
        data = self.raw.read(n)
        if data and self.on_bytes:
            self.on_bytes(len(data))
        return data

    def close(self):
        """Close both pipes and reap the child with bounded waits.

        Closing stdout can make zstd exit via SIGPIPE; stderr must still be
        closed, and a child that ignores the close must be terminated/killed
        rather than left running after the stream object is gone.
        """
        proc = self.proc
        stream = self.raw
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        err = getattr(proc, "stderr", None)
        if err is not None:
            try:
                err.close()
            except OSError:
                pass
        procs.unregister(proc)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _decompress_stream(binary: str, path: str, on_bytes=None) -> _Stream:
    proc = subprocess.Popen([binary, "-d", "-q", "-c", path],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    procs.register(proc)
    return _Stream(proc, on_bytes)


_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}


def _frame_size(binary: str, path: str) -> int:
    """Decompressed size from the frame header (`zstd -l`), 0 when unknown.

    `zstd -l` prints one row per file:
        Frames Skips Compressed [Uncompressed] Ratio Check Filename
    The uncompressed pair is omitted entirely when the frame did not record a
    content size, so the columns are read from the right (ratio is always
    numeric and always present).
    """
    try:
        p = subprocess.run([binary, "-l", path], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=20)
        for line in p.stdout.decode("utf-8", "replace").splitlines():
            toks = line.split()
            if len(toks) < 6 or not toks[0].isdigit():
                continue
            ratio = next((i for i, t in enumerate(toks)
                          if i > 2 and re.fullmatch(r"\d+\.\d+", t)), None)
            if ratio is None:
                continue
            mid = toks[2:ratio]
            if len(mid) < 4:
                return 0
            try:
                return int(float(mid[2]) * _UNITS.get(mid[3], 1))
            except (ValueError, IndexError):
                return 0
    except Exception:
        pass
    return 0


def _looks_like_tar_zst(binary: str, path: str) -> bool:
    """A tar stream starts with a header whose checksum field is at 148..156."""
    with _decompress_stream(binary, path) as stream:
        head = stream.read(512)
    if len(head) < 512:
        return False
    try:
        chksum = int(head[148:156].split(b"\0")[0].strip() or b"0", 8)
    except ValueError:
        return False
    body = head[:148] + b" " * 8 + head[156:512]
    return sum(body) == chksum


def extract(path: str, dest: str, overwrite: str = "rename", on_progress=None,
            cancel: threading.Event | None = None) -> dict:
    """Unpack a .zst or .tar.zst into dest.  Returns {"files", "bytes"}."""
    binary, _src = find_binary()
    if not binary:
        raise ZstdError("未找到 zstd 程序，请在设置 → 组件里安装")
    os.makedirs(dest, exist_ok=True)
    if not _looks_like_tar_zst(binary, path):
        return _extract_single(binary, path, dest, overwrite, on_progress, cancel)

    total = max(1, _frame_size(binary, path) or 0)
    seen = [0]
    files = 0
    skipped = 0
    with _decompress_stream(binary, path) as stream:
        with tarfile.open(fileobj=stream, mode="r|") as tf:
            for m in tf:
                if cancel is not None and cancel.is_set():
                    raise ZstdError("已取消")
                target = _safe_target(dest, m.name)
                if target is None or not util.path_within(dest, target):
                    skipped += 1
                    continue
                if m.isdir():
                    os.makedirs(target, exist_ok=True)
                    continue
                if not m.isfile():
                    # tar 的 symlink/hardlink 成员不跟随：目标可能指向解压目录
                    # 之外，解压后写入又可能穿过它（T04a）。
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(target) or dest, exist_ok=True)
                if not util.path_within(dest, target):
                    skipped += 1
                    continue
                if os.path.exists(target):
                    if overwrite == "skip":
                        continue
                    if overwrite in ("rename", "newer"):
                        target = _unique(target)
                src = tf.extractfile(m)
                if src is None:
                    continue
                with open(target, "wb") as out:
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise ZstdError("已取消")
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                        seen[0] += len(chunk)
                        if on_progress:
                            on_progress(min(99.0, seen[0] * 100.0 / total), m.name, 0.0)
                try:
                    os.utime(target, (m.mtime, m.mtime))
                except OSError:
                    pass
                files += 1
    if on_progress:
        on_progress(100.0, "", 0.0)
    return {"files": files, "bytes": seen[0], "skipped": skipped}


def _extract_single(binary, path, dest, overwrite, on_progress, cancel) -> dict:
    name = re.sub(r"\.(zst|tzst)$", "", os.path.basename(path), flags=re.I) or "输出"
    target = os.path.join(dest, name)
    if not util.path_within(dest, target):
        raise ZstdError("目标路径是符号链接或越出解压目录，已拒绝写入")
    if os.path.islink(target):
        raise ZstdError("目标路径是符号链接，已拒绝写入")
    if os.path.exists(target) and overwrite != "overwrite":
        target = _unique(target) if overwrite != "skip" else None
    if target is None:
        return {"files": 0, "bytes": 0}
    total = max(1, _frame_size(binary, path) or 0)
    with open(target, "wb") as out:
        proc = subprocess.Popen([binary, "-d", "-q", "-c", path],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        procs.register(proc)
        try:
            seen = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise ZstdError("已取消")
                chunk = proc.stdout.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                seen += len(chunk)
                if on_progress:
                    on_progress(min(99.0, seen * 100.0 / total), "", 0.0)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise ZstdError("zstd 解压进程未退出")
            if cancel is not None and cancel.is_set():
                raise ZstdError("已取消")
        finally:
            procs.unregister(proc)
            _reap_child(proc)
    if on_progress:
        on_progress(100.0, "", 0.0)
    return {"files": 1, "bytes": seen}


def _safe_target(dest: str, name: str) -> str | None:
    """Shared write-boundary rule (T04a); kept as a thin wrapper for tests."""
    return util.safe_join(dest, name)


def _unique(path: str) -> str:
    stem, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{stem} {i}{ext}"):
        i += 1
    return f"{stem} {i}{ext}"


def _hint(log: str) -> str:
    low = (log or "").lower()
    if "no space" in low:
        return "磁盘空间不足"
    if "permission denied" in low:
        return "没有写入权限"
    if "unsupported" in low or "corrupted" in low:
        return "文件已损坏或不是 zstd 格式"
    return ""
