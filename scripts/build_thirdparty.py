"""Build the two helper binaries Arca ships next to 7-Zip.

Neither project publishes a macOS binary:

* zstd          -- releases carry source tarballs and Windows zips only
* par2cmdline   -- Homebrew's bottle needs libomp, so it is not portable

So we compile both from the tagged sources ourselves and put a *universal*
(arm64 + x86_64) executable in vendor/.  The same recipes are reused at
runtime when the user updates a component from the settings panel, which is
why they live here as plain data (`RECIPES`).

    build/venv/bin/python scripts/build_thirdparty.py           # build both
    build/venv/bin/python scripts/build_thirdparty.py zstd      # one of them
    build/venv/bin/python scripts/build_thirdparty.py --check   # report only
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "build", "thirdparty")
VENDOR = os.path.join(ROOT, "vendor")

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# The bundled versions.  `codeload` is used instead of github.com because the
# plain github.com host is unreachable from a lot of networks while the
# archive endpoint is served from a different origin.
RECIPES = {
    "zstd": {
        "version": "1.5.7",
        "source": "https://codeload.github.com/facebook/zstd/tar.gz/refs/tags/v{version}",
        "compiler": "cc",
        "flags": ["-O2", "-DNDEBUG", "-DXXH_NAMESPACE=ZSTD_", "-DZSTD_MULTITHREAD",
                  "-DZSTD_DISABLE_ASM"],
        "sources": [
            "programs/zstdcli.c", "programs/util.c", "programs/timefn.c",
            "programs/fileio.c", "programs/fileio_asyncio.c", "programs/benchzstd.c",
            "programs/benchfn.c", "programs/datagen.c", "programs/lorem.c",
            "programs/dibio.c", "programs/zstdcli_trace.c",
            "lib/common/*.c", "lib/compress/*.c", "lib/decompress/*.c",
            "lib/dictBuilder/*.c",
        ],
        "includes": ["lib", "lib/common", "lib/compress", "lib/decompress", "lib/dictBuilder"],
        "libs": ["-lpthread"],
        "output": "programs/zstd",
        "version_args": ["--version"],
        "version_re": r"v([0-9]+\.[0-9]+\.[0-9]+)",
    },
    "par2": {
        "version": "1.4.0",
        "source": "https://codeload.github.com/Parchive/par2cmdline/tar.gz/refs/tags/v{version}",
        "compiler": "c++",
        "flags": ["-O2", "-w", "-std=c++17", "-DHAVE_CONFIG_H"],
        "sources": ["src/*.cpp"],
        "exclude": ["_test.cpp"],
        "includes": ["src"],
        "libs": [],
        "output": None,              # link every object into one binary
        "version_args": ["-V"],
        "version_re": r"par2cmdline version ([0-9.]+)",
        # par2cmdline expects an autotools-generated config.h; these are the
        # defines a normal macOS configure run produces.
        "config_h": """#define STDC_HEADERS 1
#define HAVE_STDLIB_H 1
#define HAVE_STDIO_H 1
#define HAVE_STRING_H 1
#define HAVE_STRINGS_H 1
#define HAVE_MEMCPY 1
#define HAVE_MEMMOVE 1
#define HAVE_STRCHR 1
#define HAVE_STRRCHR 1
#define HAVE_MEMORY_H 1
#define HAVE_SYS_STAT_H 1
#define HAVE_SYS_TYPES_H 1
#define HAVE_LIMITS_H 1
#define HAVE_UNISTD_H 1
#define HAVE_FCNTL_H 1
#define HAVE_DIRENT_H 1
#define HAVE_INTTYPES_H 1
#define HAVE_STDINT_H 1
#define HAVE_STRCASECMP 1
#define PACKAGE "par2cmdline"
#define PACKAGE_NAME "par2cmdline"
#define PACKAGE_STRING "par2cmdline {version}"
#define PACKAGE_VERSION "{version}"
#define VERSION "{version}"
""",
    },
}

UNIVERSAL = ["-arch", "arm64", "-arch", "x86_64"]


def run(cmd, cwd=None, timeout=600):
    p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def fetch(url: str, dest: str) -> str:
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    with OPENER.open(urllib.request.Request(url, headers={"User-Agent": "Arca"}), timeout=180) as r:
        with open(tmp, "wb") as fh:
            shutil.copyfileobj(r, fh)
    os.replace(tmp, dest)
    return dest


def unpack(archive: str, dest: str) -> str:
    if os.path.isdir(dest) and os.listdir(dest):
        return dest
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tf:
        members = []
        for m in tf.getmembers():
            name = m.name.split("/", 1)
            if len(name) < 2 or not name[1]:
                continue
            m.name = name[1]                      # drop the "<repo>-<tag>/" prefix
            if m.name.startswith("..") or m.name.startswith("/"):
                continue
            members.append(m)
        tf.extractall(dest, members=members, filter="data")
    return dest


def build(name: str, force: bool = False) -> str:
    spec = RECIPES[name]
    version = spec["version"]
    root = os.path.join(WORK, f"{name}-{version}")
    src = os.path.join(root, "src")
    url = spec["source"].format(version=version)
    archive = os.path.join(root, os.path.basename(url) + ".tar.gz")

    out_name = "zstd" if name == "zstd" else "par2"
    final = os.path.join(VENDOR, out_name)
    if os.path.isfile(final) and not force:
        code, text = run([final] + spec["version_args"], timeout=20)
        if code == 0 and version in text:
            print(f"· {out_name} {version} already in vendor/ (use --force to rebuild)")
            return final

    print(f"· {out_name}: fetching {url}")
    fetch(url, archive)
    unpack(archive, src)

    if spec.get("config_h"):
        with open(os.path.join(src, "src", "config.h"), "w") as fh:
            fh.write(spec["config_h"].format(version=version))

    sources = []
    for pattern in spec["sources"]:
        import glob
        matches = sorted(glob.glob(os.path.join(src, pattern)))
        for m in matches:
            if any(x in os.path.basename(m) for x in spec.get("exclude", [])):
                continue
            sources.append(os.path.relpath(m, src) if spec["output"] else m)
    if not sources:
        raise SystemExit(f"{name}: no sources matched")

    incs = []
    for d in spec["includes"]:
        incs += ["-I", os.path.join(src, d)]
    binary = os.path.join(root, "out", out_name)
    os.makedirs(os.path.dirname(binary), exist_ok=True)
    base = [spec["compiler"]] + spec["flags"] + incs
    tail = ["-o", binary] + sources + spec["libs"]
    print(f"  compiling {len(sources)} files (universal)")
    code, text = run(base + UNIVERSAL + tail, cwd=src if spec["output"] else None, timeout=900)
    if code != 0:
        # Fall back to the host architecture only: an x86_64 slice can be
        # missing from a stripped-down SDK.
        print("  universal build failed, retrying for the host architecture")
        code, text = run(base + tail, cwd=src if spec["output"] else None, timeout=900)
    if code != 0:
        print(text[-3000:])
        raise SystemExit(f"{name}: build failed")

    code, text = run([binary] + spec["version_args"], timeout=20)
    import re
    m = re.search(spec["version_re"], text)
    if code not in (0, 1) or not m:
        print(text[:500])
        raise SystemExit(f"{name}: built binary does not report a version")

    os.makedirs(VENDOR, exist_ok=True)
    if os.path.exists(final):
        os.replace(final, final + ".old")
    shutil.copyfile(binary, final)
    os.chmod(final, 0o755)
    size = os.path.getsize(final)
    print(f"  ok  vendor/{out_name}  v{m.group(1)}  {size/1024:.0f} KB  ({time.strftime('%H:%M:%S')})")
    return final


def main(argv):
    names = [a for a in argv if not a.startswith("-")]
    force = "--force" in argv
    if "--check" in argv:
        for name in ("zstd", "par2"):
            p = os.path.join(VENDOR, name)
            if os.path.isfile(p):
                code, text = run([p] + RECIPES[name]["version_args"], timeout=20)
                print(f"{name}: {text.strip().splitlines()[0][:60]}")
            else:
                print(f"{name}: missing")
        return 0
    for name in (names or ["zstd", "par2"]):
        if name not in RECIPES:
            raise SystemExit(f"unknown component: {name}")
        build(name, force=force)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
