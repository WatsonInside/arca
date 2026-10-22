"""Probe which file operations the current policy allows."""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log = []
p_src = os.path.join(ROOT, "vendor", "7zz")
p_dst = os.path.join(ROOT, "build", "y7zz")


def say(*a):
    log.append(" ".join(str(x) for x in a))


try:
    shutil.copyfile(p_src, p_dst)
    say("copyfile     : ok", os.path.getsize(p_dst))
except Exception as exc:
    say("copyfile     : FAIL", exc)

try:
    with open(os.path.join(ROOT, "build", "y.txt"), "w") as fh:
        fh.write("hi")
    say("write        : ok")
except Exception as exc:
    say("write        : FAIL", exc)

r = subprocess.run(["codesign", "--force", "-s", "-", p_dst],
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
say("codesign     : rc=", r.returncode, r.stdout.decode()[:300])

r = subprocess.run(["codesign", "-dv", p_dst], stdout=subprocess.PIPE,
                   stderr=subprocess.STDOUT)
say("codesign -dv : rc=", r.returncode, r.stdout.decode()[:200].replace("\n", " | "))

try:
    os.remove(p_dst)
    say("remove       : ok")
except Exception as exc:
    say("remove       : FAIL", exc)

try:
    os.rename(os.path.join(ROOT, "build", "y.txt"), os.path.join(ROOT, "build", "y2.txt"))
    say("rename       : ok")
except Exception as exc:
    say("rename       : FAIL", exc)

try:
    os.makedirs(os.path.join(ROOT, "build", "probe-dir"), exist_ok=True)
    say("makedirs     : ok")
except Exception as exc:
    say("makedirs     : FAIL", exc)

r = subprocess.run(["xattr", "-c", p_src], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
say("xattr -c     : rc=", r.returncode, r.stdout.decode()[:120])

with open(os.path.join(ROOT, "build", "probe.log"), "w") as fh:
    fh.write("\n".join(log))
print("\n".join(log))
