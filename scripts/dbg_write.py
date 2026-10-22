"""Check whether the engine can write at all in this environment."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
Z = os.path.join(ROOT, "vendor", "7zz")
work = os.path.join(ROOT, "build", "w2")
os.makedirs(work, exist_ok=True)
with open(os.path.join(work, "a.txt"), "w") as fh:
    fh.write("hello world " * 5000)
with open(os.path.join(work, "b.txt"), "w") as fh:
    fh.write("second file " * 5000)

log = []


def run(label, args, cwd=work):
    p = subprocess.run([Z] + args, cwd=cwd, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log.append(f"[{label}] exit={p.returncode}")
    tail = p.stdout.decode("utf-8", "replace").strip()[-400:]
    if tail:
        log.append("   " + tail.replace("\n", "\n   "))
    log.append("   files=" + str(sorted(os.listdir(work))))


run("plain-7z", ["a", "-bd", "plain.7z", "a.txt", "b.txt"])
run("plain-zip", ["a", "-bd", "plain.zip", "a.txt", "b.txt"])
run("split-zip", ["a", "-bd", "-v20k", "s.zip", "a.txt", "b.txt"])
run("pw-7z", ["a", "-bd", "-pXYZ", "-mhe=on", "p.7z", "a.txt"])
run("extract", ["x", "-bd", "-oout", "plain.zip", "-y"])
run("list", ["l", "-slt", "-ba", "plain.zip"])

with open(os.path.join(ROOT, "build", "w2.log"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(log))
print("done")
