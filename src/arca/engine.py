"""7-Zip engine wrapper: listing, creating and extracting archives.

The app ships a universal 7-Zip build ("7zz") so behaviour is identical on
Intel and Apple silicon and does not depend on Homebrew.  When a RAR encoder
(WinRAR's `rar`) is available on the machine it is used for .rar creation,
otherwise RAR stays read-only -- 7-Zip itself cannot create RAR archives.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from . import util
from . import updates
from . import zipfix
from . import zst
from . import par2

ENV = dict(os.environ)
ENV.update({
    "LC_ALL": "en_US.UTF-8",
    "LANG": "en_US.UTF-8",
    "LANGUAGE": "en_US",
})

CREATE_FORMATS = ("zip", "7z", "rar", "tar.zst")

LEVEL_STEPS = (0, 1, 3, 5, 7, 9)          # UI slider position -> -mx
METHOD_LABELS = {
    "auto": "自动",
    "lzma2": "LZMA2",
    "lzma": "LZMA",
    "ppmd": "PPMd",
    "bzip2": "BZip2",
    "deflate": "Deflate",
    "copy": "仅存储",
}

SPLIT_PRESETS = [
    {"id": "none", "label": "不分卷", "bytes": 0},
    {"id": "5m", "label": "5 MB", "bytes": 5 * 1024 ** 2},
    {"id": "10m", "label": "10 MB", "bytes": 10 * 1024 ** 2},
    {"id": "50m", "label": "50 MB", "bytes": 50 * 1024 ** 2},
    {"id": "100m", "label": "100 MB", "bytes": 100 * 1024 ** 2},
    {"id": "700m", "label": "700 MB · CD", "bytes": 700 * 1024 ** 2},
    {"id": "2g", "label": "2 GB · FAT32", "bytes": 2 * 1024 ** 3},
    {"id": "4g", "label": "4 GB", "bytes": 4 * 1024 ** 3},
    {"id": "custom", "label": "自定义…", "bytes": -1},
]

_META_PATTERNS = [".DS_Store", "__MACOSX", "._*", ".Spotlight-V100", ".Trashes", ".fseventsd"]
_ERROR_HINTS = {
    "Wrong password": "密码不正确",
    "Can't open as archive": "无法识别的压缩包格式",
    "Cannot open the file as archive": "无法识别的压缩包格式",
    "Headers Error": "文件头损坏，压缩包可能不完整",
    "Data Error": "数据校验失败，文件可能已损坏",
    "Unexpected end of archive": "压缩包不完整（缺少分卷？）",
    "is not supported archive": "该格式暂不支持",
    "No space left on device": "磁盘空间不足",
    "Permission denied": "没有访问权限",
    "Cannot create a file when that file already exists": "目标文件已存在",
}



_PROGRESS_SEP = re.compile(r"[\r\n]+|\x08+")


def split_progress(buf: str) -> tuple[list[str], str]:
    """Split a 7-Zip output stream into complete records plus the leftover tail.

    7-Zip 26.x redraws its progress line with backspaces when stdout is a pipe,
    so "\\r\\n" alone never terminates a record: the whole stream stayed in the
    buffer and no percentage was ever reported.
    """
    parts = _PROGRESS_SEP.split(buf)
    tail = parts.pop() if parts else ""
    if len(tail) > 65536:          # no separator in sight: keep it bounded
        tail = tail[-4096:]
    return parts, tail


class EngineError(Exception):
    pass


def _candidate_dirs():
    """Look for the bundled 7zz first, then dev-tree locations."""
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = [
        os.path.join(here, "bin"),                  # src/arca/bin
        os.path.join(here, "..", "vendor"),         # bundle: Resources/app/vendor
        os.path.join(here, "..", "..", "vendor"),   # dev: <project>/vendor
        os.path.join(here, "..", "..", "..", "vendor"),
    ]
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        dirs.insert(0, os.path.join(base, "vendor"))
        exe_dir = os.path.dirname(sys.executable)
        dirs.insert(1, os.path.join(exe_dir, "..", "Resources", "vendor"))
        dirs.insert(2, os.path.join(exe_dir, "..", "Resources", "app", "vendor"))
    return dirs


class Engine:
    def __init__(self, extra_rar_path: str | None = None):
        self.zz, self.source = self._find_zz()
        self.rar = self._find_rar(extra_rar_path)
        self.version = self._version()
        if self.version == "?" and self.source == "user":
            # A downloaded engine that no longer runs must not take the app
            # down with it: drop the pointer and go back to the bundled build.
            updates.forget()
            self.zz, self.source = self._find_zz()
            self.version = self._version()
        # Companion binaries.  They are optional as far as the app is
        # concerned: without them the matching format simply reports why.
        self.zstd, self.zstd_source = zst.find_binary()
        self.zstd_version = zst.version_of(self.zstd)
        self.par2, self.par2_source = par2.find_binary()
        self.par2_version = par2.version_of(self.par2)
        self.lock = threading.Lock()

    def refresh_helpers(self):
        """Re-resolve zstd/par2 after a component install."""
        self.zstd, self.zstd_source = zst.find_binary()
        self.zstd_version = zst.version_of(self.zstd)
        self.par2, self.par2_source = par2.find_binary()
        self.par2_version = par2.version_of(self.par2)

    # ---------------------------------------------------------------- discovery
    def _find_zz(self):
        """Return (path, source).  A user-updated engine wins over the built-in."""
        rec = updates.installed()
        if rec:
            return rec["path"], "user"
        for d in _candidate_dirs():
            for name in ("7zz", "7z"):
                p = os.path.abspath(os.path.join(d, name))
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    return p, "bundled"
        p = shutil.which("7zz") or shutil.which("7z")
        if p:
            return p, "path"
        raise EngineError("未找到 7-Zip 引擎 (7zz)")

    def _find_rar(self, extra: str | None = None) -> str | None:
        """Only trust real WinRAR CLI binaries in well-known locations."""
        cands = [extra, "/usr/local/bin/rar", "/opt/homebrew/bin/rar",
                 "/opt/local/bin/rar", "/usr/local/rar/rar", "/Applications/WinRAR.app/rar"]
        for cand in cands:
            if not cand or not os.path.isfile(cand) or not os.access(cand, os.X_OK):
                continue
            try:
                p = subprocess.run([cand], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=8)
                text = p.stdout.decode("utf-8", "replace")
                if re.search(r"RAR\s+\d", text) or "WinRAR" in text:
                    return cand
            except Exception:
                continue
        return None

    def _version(self) -> str:
        try:
            out = self._run_sync([self.zz], timeout=10)
            m = re.search(r"7-Zip \(z\) ([0-9.]+)", out)
            return m.group(1) if m else "?"
        except Exception:
            return "?"

    # ------------------------------------------------------------------- basics
    @staticmethod
    def _run_sync(cmd, timeout=60) -> str:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, env=ENV, timeout=timeout)
        return p.stdout.decode("utf-8", "replace")

    def info(self) -> dict:
        local = updates.installed() or {}
        return {
            "engine": "7-Zip",
            "engineVersion": self.version,
            "enginePath": self.zz,
            "engineSource": self.source,
            "engineInstalledAt": local.get("installedAt") if self.source == "user" else None,
            "engineUpdatePage": updates.PAGE_URL,
            "rarEncode": bool(self.rar),
            "rarPath": self.rar,
            "zstdVersion": self.zstd_version,
            "zstdPath": self.zstd,
            "zstdSource": self.zstd_source,
            "par2Version": self.par2_version,
            "par2Path": self.par2,
            "par2Source": self.par2_source,
        }

    # ------------------------------------------------------------------ archive
    @staticmethod
    def resolve(path: str) -> str:
        """Given any member/part of a (split) archive, return the entry to open."""
        path = os.path.expanduser(path)
        if os.path.isfile(path):
            low = path.lower()
            # .z01 / .z02 ... -> sibling .zip
            m = re.search(r"\.z(\d{2})$", low)
            if m:
                base = path[: -len(m.group(0))]
                for ext in (".zip",):
                    if os.path.isfile(base + ext):
                        return base + ext
            m = re.search(r"\.r(\d{2})$", low)
            if m:
                base = path[: -len(m.group(0))]
                if os.path.isfile(base + ".rar"):
                    return base + ".rar"
            m = re.search(r"\.part(\d+)\.rar$", low)
            if m and m.group(1) != "1":
                base = re.sub(r"\.part\d+\.rar$", ".part1.rar", path, flags=re.I)
                if os.path.isfile(base):
                    return base
            return path
        if os.path.exists(path + ".001"):
            return path + ".001"
        base = path
        for ext in (".zip", ".7z", ".rar"):
            if os.path.exists(base + ext + ".001"):
                return base + ext + ".001"
        return path

    def parts(self, entry: str) -> list[str]:
        base = entry
        if re.search(r"\.\d{3}$", base):
            base = base[:-4]
        found = sorted(glob.glob(glob.escape(base) + ".[0-9][0-9][0-9]"))
        return found

    def inspect(self, raw_path: str, password: str = "") -> dict:
        entry = self.resolve(raw_path)
        if not os.path.exists(entry):
            raise EngineError("文件不存在：" + raw_path)
        if zst.is_zstd(entry):
            return self._inspect_zstd(entry, raw_path)
        if not password and not self.parts(entry):
            result = self._inspect_zip(entry, raw_path)
            if result is not None:
                return result
        return self._inspect_7zz(entry, raw_path, password)

    def _inspect_zip(self, entry: str, raw_path: str):
        import struct
        import zipfile

        methods = {0: "Store", 8: "Deflate", 12: "BZip2"}
        try:
            with open(entry, "rb") as source:
                end = zipfile._EndRecData(source)
                if (not end or end[1] or end[2] or end[3] != end[4]
                        or end[9] + 22 + len(end[8]) != os.path.getsize(entry)
                        or end[6] + end[5] != end[9]):
                    return None
                source.seek(0)
                if source.read(4) not in (b"PK\x03\x04", b"PK\x05\x06"):
                    return None
            with zipfile.ZipFile(entry) as archive:
                infos = archive.infolist()
                decoded = []
                for zi in infos:
                    archive.fp.seek(zi.header_offset)
                    local = archive.fp.read(30)
                    if len(local) != 30 or local[:4] != b"PK\x03\x04":
                        return None
                    local_name, local_extra = struct.unpack_from("<HH", local, 26)
                    if local_extra:
                        return None
                    if zi.header_offset + 30 + local_name + zi.compress_size > end[6]:
                        return None
                    if (zi.volume or zi.flag_bits & ~0x808
                            or zi.compress_type not in methods
                            or zi.external_attr & 0x10 and not zi.is_dir()):
                        return None
                    offset = 0
                    while offset < len(zi.extra):
                        tag, size = struct.unpack_from("<HH", zi.extra, offset)
                        if tag != 1 or offset + 4 + size > len(zi.extra):
                            return None
                        offset += 4 + size
                    name, encoding = zipfix._decode_with(zi)
                    if name is None or "\\" in name or "\x00" in name:
                        return None
                    decoded.append((name, encoding))
                fixed = any(name != zi.filename for zi, (name, _) in zip(infos, decoded))
                if fixed and not any(not zi.flag_bits & 0x800 and not zi.filename.isascii()
                                     for zi in infos[:400]):
                    return None
                if not fixed and any(not 1 <= zi.date_time[1] <= 12
                                     or not 1 <= zi.date_time[2] <= 31 for zi in infos):
                    return None
                name_encoding = next((enc for zi, (name, enc) in zip(infos, decoded)
                                      if name != zi.filename), "utf-8") if fixed else "utf-8"
                entries = []
                for zi, (name, _) in zip(infos, decoded):
                    is_dir = zi.is_dir()
                    entries.append({
                        "path": name if fixed else name.rstrip("/") if is_dir else name,
                        "size": zi.file_size, "packed": zi.compress_size,
                        "modified": "%04d-%02d-%02d %02d:%02d:%02d" % zi.date_time,
                        "isDir": is_dir, "encrypted": False,
                        "crc": "" if fixed or is_dir and not zi.CRC else "%08X" % zi.CRC,
                        "method": "" if fixed else methods[zi.compress_type],
                    })
            return self._inspect_result(entry, raw_path, {"Type": "zip"}, entries,
                                        False, "", name_encoding)
        except (OSError, ValueError, UnicodeError, struct.error, zipfile.BadZipFile):
            return None

    def _inspect_7zz(self, entry: str, raw_path: str, password: str = "") -> dict:
        # No `-ba`: bare mode also hides the archive header block, which is the
        # only thing a *valid empty* archive prints.  Entry blocks are still
        # parsed the same way, and the header gives us Type/Solid/Method that
        # the bare listing never carried.
        cmd = [self.zz, "l", "-slt", entry]
        if password:
            cmd.insert(1, "-p" + password)
        out = self._run_sync(cmd, timeout=120)
        meta, entries = _parse_slt(out)

        needs_password = bool(re.search(r"(Wrong password|Enter password|encrypted archive)",
                                        out, re.I))
        if not meta and not entries:
            if needs_password:
                return {
                    "path": entry, "rawPath": raw_path,
                    "type": "unknown", "label": _type_label("", entry),
                    "needsPassword": True, "encrypted": True, "entries": [],
                    "counts": {"files": 0, "dirs": 0}, "size": os.path.getsize(entry),
                    "physicalSize": os.path.getsize(entry), "uncompressed": 0,
                    "parts": [], "isSplit": bool(self.parts(entry)),
                    "ratio": 0, "solid": False, "method": "",
                    "entriesTruncated": 0,
                    "error": "目录已加密，需要密码",
                }
            raise EngineError(_hint(out) or "无法读取该压缩包")

        ftype = (meta.get("Type") or "").strip()
        # 7-Zip's macOS build cannot map the OEM code page used by Windows ZIPs,
        # so every high byte lands in the private-use area (U+EFD6 …).  Detect it
        # and re-read the names from the central directory instead.
        garbled = any(0xE000 <= ord(ch) <= 0xF8FF
                      for e in entries[:400] for ch in e["path"])
        name_encoding = "utf-8"
        if garbled and (ftype or "").lower() in ("zip", "zipx"):
            plan = zipfix.plan(entry)
            if plan:
                entries = plan["entries"]
                name_encoding = plan["encoding"]
        return self._inspect_result(entry, raw_path, meta, entries, needs_password,
                                    password, name_encoding)

    def _inspect_result(self, entry, raw_path, meta, entries, needs_password,
                        password, name_encoding):
        ftype = (meta.get("Type") or "").strip()
        label = _type_label(ftype, entry)
        parts = self.parts(entry)
        total_u = sum(e["size"] for e in entries if not e["isDir"])
        files = sum(1 for e in entries if not e["isDir"])
        dirs = sum(1 for e in entries if e["isDir"])
        physical = int(meta.get("Physical Size") or 0) or os.path.getsize(entry)
        if parts:
            physical = sum(os.path.getsize(p) for p in parts)
        header_enc = False
        if entries and all(e["encrypted"] for e in entries if not e["isDir"]):
            header_enc = bool(password) and ftype.lower() == "7z"
        return {
            "path": entry,
            "rawPath": raw_path,
            "type": ftype.lower(),
            "label": label,
            "needsPassword": needs_password,
            "encrypted": any(e["encrypted"] for e in entries),
            "headerEncrypted": header_enc,
            "solid": (meta.get("Solid") or "-") == "+",
            "method": (meta.get("Method") or "").strip(),
            "size": physical,
            "physicalSize": physical,
            "uncompressed": total_u,
            "ratio": round(total_u / physical, 2) if physical else 0,
            "counts": {"files": files, "dirs": dirs},
            "parts": [os.path.basename(p) for p in parts],
            "isSplit": bool(parts) or ftype.lower() == "split",
            "nameEncoding": name_encoding,
            "entries": entries[:800],
            "entriesTruncated": max(0, len(entries) - 800),
            "error": None,
        }

    # ---------------------------------------------------------------- creation
    def _inspect_zstd(self, entry: str, raw_path: str) -> dict:
        """Same shape as the 7-Zip branch so the UI needs no special case."""
        try:
            listing = zst.listing(entry)
        except zst.ZstdError as exc:
            raise EngineError(str(exc))
        entries = listing["entries"]
        files = sum(1 for e in entries if not e["isDir"])
        dirs = sum(1 for e in entries if e["isDir"])
        physical = listing["size"]
        total_u = listing["uncompressed"]
        single = listing.get("single", False)
        return {
            "path": entry,
            "rawPath": raw_path,
            # `tar.zst` keeps the reported type aligned with the format id the
            # API accepts, so a round trip through the UI cannot mis-name it.
            "type": "zst" if single else "tar.zst",
            "label": listing["label"],
            "needsPassword": False,
            "encrypted": False,
            "headerEncrypted": False,
            "solid": False,
            "method": listing["method"],
            "size": physical,
            "physicalSize": physical,
            "uncompressed": total_u,
            "ratio": round(total_u / physical, 2) if physical and total_u else 0,
            "counts": {"files": files, "dirs": dirs},
            "parts": [],
            "isSplit": False,
            "entries": entries[:800],
            "entriesTruncated": listing.get("truncated", 0),
            "single": single,
            # No password support, and no multi-entry table for a bare .zst.
            "capabilities": {"encrypt": False, "split": False, "solid": False},
            "error": None,
        }
    def build_add_cmd(self, opts: dict, base_dir: str) -> list[str]:
        fmt = opts["format"]
        engine = self.rar if fmt == "rar" else self.zz
        if fmt == "rar" and not engine:
            raise EngineError("创建 RAR 需要安装 WinRAR 命令行工具")
        cmd = [engine, "a"]
        level = _level(opts.get("level"))
        if fmt == "rar":
            # WinRAR: -m0..-m5 controls the compression level
            cmd.append("-m%d" % min(5, max(0, round(level * 5 / 9))))
        else:
            # 7-Zip: -mx=0..9 controls the compression level
            cmd.append("-mx=%d" % level)
        if fmt == "zip":
            cmd.append("-tzip")
            if opts.get("password"):
                cmd.append("-mem=AES256")
        elif fmt == "7z":
            cmd.append("-t7z")
            method = (opts.get("method") or "auto").lower()
            spec = {"lzma2": "-m0=lzma2", "lzma": "-m0=lzma", "ppmd": "-m0=ppmd",
                    "bzip2": "-m0=bzip2", "deflate": "-m0=deflate", "copy": "-m0=copy"}
            if method in spec:
                cmd.append(spec[method])
            if opts.get("solid", True):
                cmd.append("-ms=on")
            else:
                cmd.append("-ms=off")
        else:
            cmd.append("-trar")

        pw = opts.get("password") or ""
        if pw:
            if fmt == "rar" and opts.get("encryptNames"):
                cmd.append("-hp" + pw)
            else:
                cmd.append("-p" + pw)
            if fmt == "7z" and opts.get("encryptNames", True):
                cmd.append("-mhe=on")

        split = int(opts.get("splitBytes") or 0)
        if split > 0:
            if split < 64 * 1024:
                raise EngineError("分卷大小不能小于 64 KB")
            cmd.append("-v%db" % split)

        threads = opts.get("threads")
        if threads:
            cmd.append("-mmt=%d" % int(threads))

        if opts.get("excludeMeta", True):
            for pat in _META_PATTERNS:
                cmd.append("-xr!" + pat)

        cmd += ["-bsp1", "-bso0", "-bse1", "-y"]
        cmd.append(opts["output"])
        cmd.append("--")
        cmd += opts["relative"]
        return cmd

    def build_extract_cmd(self, opts: dict) -> list[str]:
        entry = self.resolve(opts["archive"])
        fmt = (opts.get("format") or "").lower()
        engine = self.rar if fmt.startswith("rar") and self.rar else self.zz
        cmd = [engine, "x", entry, "-o" + opts["dest"]]
        pw = opts.get("password") or ""
        if pw:
            cmd.append("-p" + pw)
        mode = opts.get("overwrite") or "rename"
        cmd.append({ "overwrite": "-aoa", "skip": "-aos",
                     "rename": "-aou", "newer": "-aot" }.get(mode, "-aou"))
        cmd += ["-bsp1", "-bso0", "-bse1", "-y"]
        return cmd

    def build_test_cmd(self, archive: str, password: str = "") -> list[str]:
        cmd = [self.zz, "t", self.resolve(archive), "-bsp1", "-bso0", "-bse1"]
        if password:
            cmd.insert(1, "-p" + password)
        return cmd

    # ----------------------------------------------------------------- running
    def run(self, cmd: list[str], cwd: str | None = None, on_progress=None,
            cancel: threading.Event | None = None, total_bytes: int = 0) -> tuple[int, str]:
        """Run a 7z command, streaming progress. Returns (exit_code, text_log)."""
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=ENV, bufsize=0,
        )
        chunks = []
        buf = ""
        last = [0.0, 0.0]  # time, percent
        speed = [0.0]

        def emit(percent, line):
            if not on_progress:
                return
            now = time.time()
            # Only a real percentage feeds the speed estimate: phase lines such
            # as "Scan" arrive with percent=None and must not become a baseline.
            if percent is not None and total_bytes:
                dt = now - last[0]
                if last[0] and dt > 0.25:
                    db = (percent - last[1]) / 100.0 * total_bytes
                    inst = max(0.0, db / dt)
                    speed[0] = inst if speed[0] == 0 else speed[0] * 0.7 + inst * 0.3
                    last[0], last[1] = now, percent
                elif not last[0]:
                    last[0], last[1] = now, percent
            on_progress(percent, line, speed[0])

        while True:
            if cancel is not None and cancel.is_set():
                try:
                    proc.terminate()
                except Exception:
                    pass
                time.sleep(0.4)
                try:
                    proc.kill()
                except Exception:
                    pass
                break
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", "replace")
            chunks.append(text)
            buf += text
            # 7-Zip 26.x rewrites its progress line with backspaces when output
            # is not a terminal, so "\r\n" alone never splits anything: the
            # whole stream stayed in buf and no percent was ever reported.
            parts, buf = split_progress(buf)
            for line in parts:
                line = line.strip()
                if not line:
                    continue
                m = re.findall(r"(\d{1,3})%", line)
                if m:
                    emit(min(100, int(m[-1])), line)
                elif "Scan" in line:
                    # The first phase prints "0M Scan ." with no percent yet;
                    # without this the UI sat on 「准备中」 until 1% arrived.
                    emit(None, line)
        # The last record has no separator after it: flush it, otherwise the
        # final 100% never reaches the UI and the job looks stuck at 99%.
        for line in [*split_progress(buf)[0], split_progress(buf)[1]]:
            line = line.strip()
            if not line:
                continue
            m = re.findall(r"(\d{1,3})%", line)
            if m:
                emit(min(100, int(m[-1])), line)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        code = proc.returncode if proc.returncode is not None else -1
        log = "".join(chunks)
        return code, log


# --------------------------------------------------------------------- parsing
def _parse_slt(text: str) -> tuple[dict, list[dict]]:
    meta: dict = {}
    entries: list[dict] = []
    for block in re.split(r"\n\s*\n", text or ""):
        data = {}
        for line in block.splitlines():
            if " = " in line:
                k, v = line.split(" = ", 1)
                data[k.strip()] = v.strip()
        if not data:
            continue
        if "Attributes" in data or "Folder" in data:
            attrs = data.get("Attributes", "")
            path = data.get("Path", "")
            is_dir = (attrs.strip().startswith("D") or path.endswith("/")
                      or data.get("Folder") == "+")
            try:
                size = int(data.get("Size") or 0)
            except ValueError:
                size = 0
            try:
                packed = int(data.get("Packed Size") or 0)
            except ValueError:
                packed = 0
            entries.append({
                "path": path,
                "size": size,
                "packed": packed,
                "modified": (data.get("Modified") or "")[:19],
                "isDir": is_dir,
                "encrypted": data.get("Encrypted") == "+",
                "crc": data.get("CRC", ""),
                "method": data.get("Method", ""),
            })
        elif "Type" in data and not meta:
            meta = data
    return meta, entries


def _type_label(ftype: str, path: str) -> str:
    t = (ftype or "").lower()
    if t in ("7z",):
        return "7Z"
    if t in ("zip", "zipx"):
        return "ZIP"
    if t.startswith("rar"):
        return "RAR"
    if t in ("tar",):
        return "TAR"
    if t in ("zst", "zstd"):
        return "TAR.ZST" if path.lower().endswith(".tar.zst") else "ZST"
    if t in ("split", "", "unknown"):
        low = path.lower()
        if low.endswith(".tar.zst") or low.endswith(".tzst"):
            return "TAR.ZST"
        if low.endswith(".zst"):
            return "ZST"
        if ".7z" in low:
            return "7Z"
        if ".zip" in low:
            return "ZIP"
        if ".rar" in low:
            return "RAR"
        return "压缩包"
    return t.upper()


def _level(pos) -> int:
    try:
        pos = int(pos)
    except (TypeError, ValueError):
        pos = 3
    pos = min(5, max(0, pos))
    return LEVEL_STEPS[pos]


def _hint(log: str) -> str:
    for needle, msg in _ERROR_HINTS.items():
        if needle.lower() in (log or "").lower():
            return msg
    return ""


ZIP_PASSWORD_HINT = ("ZIP 格式暂不支持含中文等非 ASCII 字符的密码，"
                     "请改用 7z 格式（同时会加密文件名，更安全）")


def describe_error(log: str, code: int, cancelled: bool = False, *,
                   format: str = "", has_password: bool = False) -> str:
    if cancelled:
        return "已取消"
    if code != 0 and format == "zip" and has_password and "E_INVALIDARG" in (log or "").upper():
        return ZIP_PASSWORD_HINT
    hint = _hint(log)
    if hint:
        return hint
    lines = [l.strip() for l in (log or "").splitlines()
             if l.strip() and not l.strip().startswith("7-Zip")]
    errs = [l for l in lines if l.startswith("ERROR") or "Error" in l]
    if errs:
        return errs[-1].replace("ERROR: ", "").replace("System ERROR:", "").strip()[:200]
    if code == 0:
        return ""
    if code == 1:
        return "完成，但存在警告（部分文件被跳过）"
    if code == 2:
        return "操作失败"
    if code == 8:
        return "内存不足"
    if code == 255:
        return "已取消"
    return f"操作失败（代码 {code}）"
