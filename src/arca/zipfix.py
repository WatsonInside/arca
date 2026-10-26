"""Extract ZIP archives whose file names are not UTF-8.

7-Zip's macOS build has no OEM code page table: a ZIP made on a Chinese Windows
box stores "中文.txt" as GBK bytes with the UTF-8 flag cleared, and 7-Zip turns
every byte into a private-use character (U+EFD6 …).  `-mcp=936` is accepted but
not honoured by the zip handler, so there is nothing to pass through -- the only
way to get real names is to decode the central directory ourselves.

This module takes over extraction only for that exact case:

  * type is zip, names carry no UTF-8 flag, and at least one name does not
    decode as UTF-8 but does decode as GBK;
  * the archive is not encrypted (zipfile cannot do AES, 7-Zip must).

Everything else stays on the 7-Zip path.
"""
from __future__ import annotations

import os
import shutil
import time
import zipfile

ENCODINGS = ("utf-8", "gbk", "big5")
CHUNK = 1 << 20


class ZipFixError(Exception):
    pass


def _decode_with(zi: zipfile.ZipInfo) -> tuple[str | None, str]:
    """Best-effort name for an entry stored with the legacy code page."""
    if zi.flag_bits & 0x800:            # explicitly UTF-8: trust zipfile
        return zi.filename, "utf-8"
    try:
        raw = zi.filename.encode("cp437")
    except UnicodeEncodeError:
        return zi.filename, "utf-8"
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return None, ""


def _decode(zi: zipfile.ZipInfo) -> str | None:
    return _decode_with(zi)[0]


def plan(path: str) -> dict | None:
    """Return how to fix this archive, or None when 7-Zip is the better path."""
    if not zipfile.is_zipfile(path):
        return None
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return None
    try:
        infos = zf.infolist()
        if not infos:
            return None
        if any(zi.flag_bits & 0x1 for zi in infos):   # encrypted
            return None
        names = {}
        entries = []
        fixed = 0
        encoding = ""
        for zi in infos:
            dec, enc = _decode_with(zi)
            if dec is None:
                return None                            # unknown encoding: leave it
            if dec != zi.filename:
                fixed += 1
                encoding = encoding or enc
            names[zi.filename] = dec
            entries.append({
                "path": dec,
                "size": zi.file_size,
                "packed": zi.compress_size,
                "modified": "%04d-%02d-%02d %02d:%02d:%02d" % zi.date_time,
                "isDir": zi.is_dir() or dec.endswith("/"),
                "encrypted": False,
                "crc": "",
                "method": "",
            })
        if not fixed:
            return None
        return {"names": names, "fixed": fixed, "total": len(infos),
                "encoding": encoding or "gbk", "entries": entries,
                "bytes": sum(zi.file_size for zi in infos)}
    finally:
        zf.close()


def _target(dest: str, name: str) -> str:
    """Join inside dest, dropping any attempt to escape it."""
    parts = [p for p in name.replace("\\", "/").split("/")
             if p not in ("", ".", "..")]
    return os.path.join(dest, *parts) if parts else dest


def _unique(path: str) -> str:
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 2
    while True:
        cand = f"{root} {i}{ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


def _selected(name: str, only: set[str] | None) -> bool:
    """Partial extraction filter.  A selected directory pulls its subtree,
    matching 7-Zip's own rule; selection names are the decoded (display) names
    because that is what the user picked, while the archive is still read
    through the raw-name → decoded-name map below."""
    if only is None:
        return True
    norm = name.rstrip("/")
    return any(norm == sel or norm.startswith(sel + "/") for sel in only)


def extract(path: str, dest: str, names: dict, overwrite: str = "rename",
            on_progress=None, cancel=None, only=None) -> dict:
    out_files = 0
    out_bytes = 0
    wanted = None if only is None else {
        str(sel).rstrip("/") for sel in only if str(sel).strip()}
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        todo = [zi for zi in infos
                if _selected(names.get(zi.filename) or zi.filename, wanted)]
        total = sum(zi.file_size for zi in todo
                    if not (zi.is_dir() or (names.get(zi.filename) or zi.filename).endswith("/"))) or 1
        for zi in todo:
            if cancel is not None and cancel.is_set():
                raise ZipFixError("已取消")
            name = names.get(zi.filename) or zi.filename
            target = _target(dest, name)
            if zi.is_dir() or name.endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target) or dest, exist_ok=True)
            if os.path.exists(target):
                if overwrite == "skip":
                    continue
                if overwrite in ("rename", "newer"):
                    target = _unique(target)
            with zf.open(zi) as src, open(target, "wb") as dst:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise ZipFixError("已取消")
                    buf = src.read(CHUNK)
                    if not buf:
                        break
                    dst.write(buf)
                    out_bytes += len(buf)
                    if on_progress:
                        on_progress(min(99.9, out_bytes * 100.0 / total), name)
            try:
                ts = time.mktime(zi.date_time + (0, 0, -1))
                os.utime(target, (ts, ts))
            except (OverflowError, ValueError, OSError):
                pass
            out_files += 1
        if on_progress:
            on_progress(100, "")
    return {"files": out_files, "bytes": out_bytes, "total": total}


def cleanup(dest: str, existed: bool) -> None:
    """A cancelled extraction should not leave half a folder behind."""
    if not existed:
        shutil.rmtree(dest, ignore_errors=True)
