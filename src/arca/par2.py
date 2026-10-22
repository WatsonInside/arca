"""PAR2 recovery records.

7-Zip has no parity records and RAR's ones need WinRAR, so the only realistic
way to make an archive repairable is PAR2 -- still the format every recovery
tool understands (MultiPar, QuickPar, SABnzbd).  We ship `par2cmdline`, built
from source by `scripts/build_thirdparty.py` because neither the project nor
Homebrew offers a portable macOS binary.

Design choices worth knowing:

* **opt-in, never default.** Recovery data costs 3-15% of the archive size and
  is generated *after* compression, so it doubles the wait on big jobs.  The
  panel only shows the switch when the tool is actually installed.
* **generated next to the archive**, one recovery set per archive (including
  split volumes, where it is genuinely useful: a lost volume otherwise makes
  everything after it unusable).
* the tool is only ever run through this module, so a missing binary is a
  normal state the UI can describe rather than a crash.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import threading

from . import util

REDUNDANCY = (3, 5, 10, 15)          # percent presets offered in the UI


class Par2Error(Exception):
    pass


def candidates():
    here = os.path.dirname(os.path.abspath(__file__))
    return [
        os.path.join(util.app_support_dir(), "components", "par2"),
        os.path.join(here, "bin", "par2"),
        os.path.join(here, "..", "vendor", "par2"),
        os.path.join(here, "..", "..", "vendor", "par2"),
        os.path.join(here, "..", "..", "..", "vendor", "par2"),
    ]


def find_binary(user_path: str | None = None) -> tuple[str | None, str]:
    if user_path and os.path.isfile(user_path) and os.access(user_path, os.X_OK):
        return user_path, "user"
    home = os.path.abspath(util.app_support_dir())
    for p in candidates():
        p = os.path.abspath(p)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            # A copy installed by the component updater lives in app support and
            # must not be reported as "built in".
            return p, ("user" if p.startswith(home + os.sep) else "bundled")
    import shutil
    p = shutil.which("par2")
    return (p, "path") if p else (None, "none")


def version_of(path: str | None) -> str:
    if not path:
        return ""
    try:
        p = subprocess.run([path, "-V"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=10)
        m = re.search(r"version ([0-9.]+)", p.stdout.decode("utf-8", "replace"))
        return m.group(1) if m else ""
    except Exception:
        return ""


def recovery_files(archive: str) -> list[str]:
    """Every .par2 belonging to this archive: index file first, then volumes.

    For a split set the recovery files are named after the first volume
    (`name.7z.001.par2`), so both stems are considered.
    """
    folder = os.path.dirname(os.path.abspath(archive))
    base = os.path.basename(archive)
    stems = [base]
    if re.search(r"\.\d{3}$", base):
        stems.append(base[:-4])
    if not os.path.isdir(folder):
        return []
    found = []
    for pat in ("*.par2", "*.PAR2"):
        found += glob.glob(os.path.join(glob.escape(folder), pat))
    picked = []
    for p in found:
        name = os.path.basename(p)
        for stem in stems:
            if name == stem + ".par2" or name.startswith(stem + ".vol"):
                picked.append(p)
                break
    picked = sorted(set(picked), key=lambda p: (1 if ".vol" in os.path.basename(p) else 0,
                                                os.path.basename(p)))
    return picked


def create(binary: str, sources: list[str], percent: int = 5,
           single: bool = False, on_progress=None,
           cancel: threading.Event | None = None) -> dict:
    """Write <first source>.par2 next to the archive.

    `sources` is the archive plus every volume of a split set: one recovery
    record has to cover the whole set, otherwise repairing a lost volume puts
    the rest of the archive out of reach anyway.

    `single=True` uses `-n1`: all recovery data lands in one `.vol` file
    instead of the default 2^n ladder.  Same total redundancy, one file less
    of per-volume metadata, but a repair always reads the whole volume --
    good for local archives, worse when only a slice can be fetched.
    """
    percent = max(1, min(50, int(percent or 5)))
    sources = [os.path.abspath(p) for p in sources if os.path.exists(p)]
    if not sources:
        raise Par2Error("找不到要保护的文件")
    folder = os.path.dirname(sources[0]) or "."
    target = os.path.basename(sources[0]) + ".par2"
    total = sum(os.path.getsize(p) for p in sources)

    # Block count drives both granularity and overhead: par2 stores a hash per
    # block, so the default 2000-block ceiling makes a small archive mostly
    # metadata (400 KB ended up with 87% overhead).  ~1 block per MiB, floored
    # at 200, keeps the index in the kilobytes and the real overhead at the
    # requested percentage.
    blocks = max(200, min(2000, (total + 1024 * 1024 - 1) // (1024 * 1024)))
    # All options must precede the file arguments: par2cmdline stops parsing
    # switches at the first positional argument, so an appended `-n1` is
    # silently ignored (it took a test to catch the 4-volume output).
    cmd = [binary, "create", "-q", "-r%d" % percent, "-b%d" % blocks, "-f0"]
    if single:
        cmd.append("-n1")
    cmd += [target] + [os.path.basename(p) for p in sources]

    code, log, made = _run(cmd, cwd=folder, on_progress=on_progress, cancel=cancel)
    if code != 0:
        raise Par2Error(_hint(log) or "生成恢复记录失败")
    produced = recovery_files(sources[0])
    if on_progress:
        on_progress(100.0, "")
    size = sum(os.path.getsize(f) for f in produced if os.path.exists(f))
    return {"files": len(produced), "bytes": size, "made": produced,
            "log": "\n".join(made[-40:]), "blocks": blocks, "percent": percent}


def verify(binary: str, par2_file: str, on_progress=None,
           cancel: threading.Event | None = None) -> dict:
    folder = os.path.dirname(os.path.abspath(par2_file)) or "."
    cmd = [binary, "verify", "-q", os.path.basename(par2_file)]
    code, log, _out = _run(cmd, cwd=folder, on_progress=on_progress, cancel=cancel,
                           expect=r"Verifying|Repair is")
    ok = "All files are correct" in log or code == 0
    # 0.8.1 prints `Target: "x" - damaged.`; older builds used `"x" is damaged`.
    damaged = re.findall(r'Target: "(.+?)" - damaged', log) \
        or re.findall(r'"(.+?)" is damaged', log)
    missing = re.findall(r'Target: "(.+?)" - (?:missing|not found)', log) \
        or re.findall(r'"(.+?)" is missing', log)
    # `-q` keeps the summary only, so the counts are the reliable source.
    summary = re.search(r"(\d+)\s+file\(s\) exist but are damaged", log)
    summary_missing = re.search(r"(\d+)\s+file\(s\) are missing", log)
    damaged_count = max(len(damaged), int(summary.group(1)) if summary else 0)
    missing_count = max(len(missing), int(summary_missing.group(1)) if summary_missing else 0)
    repairable = "Repair is possible" in log
    return {"ok": ok, "damaged": damaged, "missing": missing,
            "damagedCount": damaged_count, "missingCount": missing_count,
            "repairable": repairable, "log": log[-4000:], "code": code}


def repair(binary: str, par2_file: str, on_progress=None,
           cancel: threading.Event | None = None) -> dict:
    folder = os.path.dirname(os.path.abspath(par2_file)) or "."
    cmd = [binary, "repair", "-q", os.path.basename(par2_file)]
    code, log, _out = _run(cmd, cwd=folder, on_progress=on_progress, cancel=cancel,
                           expect=r"Repairing|Repair complete|Repair failed")
    done = "Repair complete" in log or code == 0
    return {"ok": done, "log": log[-4000:], "code": code}


def _run(cmd, cwd=None, on_progress=None, cancel=None, expect=""):
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    chunks, made = [], []
    buf = ""
    last = 0.0
    while True:
        if cancel is not None and cancel.is_set():
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
            raise Par2Error("已取消")
        chunk = proc.stdout.read(4096)
        if not chunk:
            break
        text = chunk.decode("utf-8", "replace")
        chunks.append(text)
        buf += text
        lines = re.split(r"[\r\n]+", buf)
        buf = lines.pop() if lines else ""
        for line in lines:
            line = line.strip()
            if not line:
                continue
            made.append(line)
            m = re.search(r"(\d{1,3})[.,](\d)\s*%", line) or re.search(r"(\d{1,3})\s*%", line)
            if m and on_progress:
                try:
                    val = float(m.group(1) + ("." + m.group(2) if m.lastindex == 2 else ""))
                except ValueError:
                    continue
                last = max(last, val)
                on_progress(min(99.0, last), line[:80])
    proc.wait()
    return proc.returncode, "".join(chunks), made


def _hint(log: str) -> str:
    low = (log or "").lower()
    if "no space" in low:
        return "磁盘空间不足"
    if "permission denied" in low:
        return "没有写入权限"
    if "could not" in low and "open" in low:
        return "无法读取源文件"
    return ""
