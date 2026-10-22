"""Debug the exact 7zz invocation Arca builds."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from arca import engine as eng

OUT = os.path.join(ROOT, "build", "dbg.log")
lines = []


def log(*a):
    lines.append(" ".join(str(x) for x in a))


work = os.path.join(ROOT, "build", "dbgwork")
os.makedirs(os.path.join(work, "素材", "子目录"), exist_ok=True)
for n, sz in (("a.txt", 2048), ("b.bin", 70000), ("子目录/c.md", 512)):
    with open(os.path.join(work, "素材", n), "wb") as fh:
        fh.write(os.urandom(sz))

e = eng.Engine()
log("engine:", e.zz, e.version, "rar:", e.rar)
log("candidates rar detect:", e._find_rar())

out = os.path.join(work, "out.zip")
opts = {
    "output": out, "format": "zip", "level": 3, "method": "auto",
    "splitBytes": 65536, "password": "Arca#2026", "encryptNames": True,
    "excludeMeta": True, "solid": True, "threads": 0,
    "relative": ["素材"],
}
cmd = e.build_add_cmd(opts, work)
log("CMD:", " ".join(cmd))
p = subprocess.run(cmd, cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                   stderr=subprocess.STDOUT, env=eng.ENV)
log("exit:", p.returncode)
log("--- output ---")
log(p.stdout.decode("utf-8", "replace")[:3000])
log("--- files ---")
log(sorted(os.listdir(work)))

# try without "--"
opts2 = dict(opts)
out2 = os.path.join(work, "out2.zip")
opts2["output"] = out2
cmd2 = [c for c in cmd if c != "--"]
cmd2[cmd2.index(out) if out in cmd2 else -1] = out2
p2 = subprocess.run(cmd2, cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=eng.ENV)
log("exit(no --):", p2.returncode)
log(p2.stdout.decode("utf-8", "replace")[:2000])

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("written", OUT)
