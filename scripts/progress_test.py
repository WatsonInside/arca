"""Progress parsing: 7-Zip 26.x redraws its percent line with backspaces.

Regression for the job that looked stuck at 0 bytes: the stream carried no
"\\r\\n" at all, so nothing was split and no percentage reached the UI.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from arca import engine  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok    " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def percents_of(lines):
    out = []
    for line in lines:
        found = re.findall(r"(\d{1,3})%", line)
        if found:
            out.append(min(100, int(found[-1])))
    return out


# The exact byte shape captured from 7zz 26.05 with -bsp1 -bso0 -bse1:
#   "  0M Scan ./" + BS*13 + spaces + BS*10 + "  0%" + BS...
SYNTHETIC = (
    "  0M Scan ./" + "\b" * 13 + " " * 14 + "\b" * 10 + "  0%"
    + "\b" * 12 + " " * 12 + "\b" * 12 + "  5% 1 + DSC_0006.tif"
    + "\b" * 16 + " " * 16 + "\b" * 16 + " 42% 12 + DSC_0021.tif"
    + "\b" * 18 + " " * 18 + "\b" * 18 + "100% 24 + DSC_0026.tif\n"
)

parts, tail = engine.split_progress(SYNTHETIC)
check("反斜线进度流被切成多条记录", len(parts) >= 4, f"{len(parts)} 段")
check("最后一个百分比可解析", percents_of(parts) == [0, 5, 42, 100], str(percents_of(parts)))
check("整段不含换行也不丢记录", "\r" not in SYNTHETIC and not tail.strip(), repr(tail[:20]))

# 残缺尾段必须留在 tail 里，等下一块数据补齐
partial = "  7% 3 + a.bin"
_, tail2 = engine.split_progress(partial)
check("未终止的记录留在缓冲区", tail2 == partial, repr(tail2))

# 真实命令：小任务至少也要报出 0%，并且产物非空
eng = engine.Engine()
work = tempfile.mkdtemp(prefix="arca-progress-")
try:
    src = os.path.join(work, "src")
    os.makedirs(src)
    blob = b"\0" * (1024 * 1024)
    for i in range(12):
        with open(os.path.join(src, f"f{i:02d}.bin"), "wb") as fh:
            fh.write(blob)
    out = os.path.join(work, "out.7z")
    seen = []
    cmd = [eng.zz, "a", "-t7z", "-mx=1", "-ms=on", "-bsp1", "-bso0", "-bse1",
           "-y", out, "--"] + sorted(os.listdir(src))
    code, log = eng.run(cmd, cwd=src,
                        on_progress=lambda pct, line, speed: seen.append((pct, line)),
                        total_bytes=12 * 1024 * 1024)
    pcts = [p for p, _ in seen if p is not None]
    check("真实压缩成功", code == 0, f"exit={code}")
    check("真实命令有百分比回调", bool(pcts), f"{pcts}")
    check("扫描阶段有提示", any(p is None and "Scan" in line for p, line in seen))
    check("产物非空", os.path.getsize(out) > 0, f"{os.path.getsize(out)} B")
finally:
    shutil.rmtree(work, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
raise SystemExit(0 if not FAIL else 1)
