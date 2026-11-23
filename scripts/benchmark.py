"""Repeatable performance baseline for Arca.

Everything is generated under build/ from a fixed seed (synthetic corpus), never
from user files, and cleaned up afterwards.  Output is raw JSON (machine,
command, parameters, sample sizes, per-case timings, memory/CPU) so a later run
can be compared on the same machine; nothing here claims a regression threshold
that was not measured first.

Usage:
    build/venv/bin/python -S -E -B scripts/benchmark.py                # quick
    build/venv/bin/python -S -E -B scripts/benchmark.py --mode full    # larger
    ... --json build/benchmark.json --keep

Scopes:
  quick  1000-entry ZIP inspect, 64 MiB compress+extract, 2000 files scan,
         4-way concurrent inspect.  ~2-5 s on the dev machine.
  full   1000-entry ZIP inspect, 512 MiB compress+extract, 20000 files scan,
         8-way concurrent inspect.  Needs ~2.5 GB free and is intentionally
         NOT reported as a "1 GB / 100k files" benchmark.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from _paths import build_dir  # noqa: E402
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from arca import engine as eng            # noqa: E402
from arca import util                     # noqa: E402

ZZ = str(ROOT / "vendor" / "7zz")
MIB = 1024 * 1024
CASES = {
    "quick": {"big_mib": 64, "files": 2000, "dirs": 200, "workers": 4},
    "full": {"big_mib": 512, "files": 20000, "dirs": 1000, "workers": 8},
}


def mem_bytes():
    # macOS reports ru_maxrss in bytes, Linux in KiB.
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def machine():
    out = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }
    try:
        out["memory_bytes"] = int(subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
            timeout=5).stdout.strip())
    except Exception:
        pass
    return out


def timed(fn):
    start = time.perf_counter()
    cpu0 = time.process_time()
    value = fn()
    return {
        "seconds": round(time.perf_counter() - start, 4),
        "cpu_seconds": round(time.process_time() - cpu0, 4),
        "peak_memory_bytes": mem_bytes(),
    }, value


def _outcome(fn, args, kwargs):
    """Capture return semantics or an ordinary exception without hiding exits."""
    try:
        return ("return", fn(*args, **kwargs))
    except Exception as exc:
        return ("raise", f"{type(exc).__name__}: {exc}")


def _shown(outcome):
    kind, value = outcome
    return repr(value) if kind == "return" else value


def replay_calls(reference, candidate, cases):
    """Replay fixed ``(name, args, kwargs)`` cases and compare exact semantics.

    Cases must come from synthetic fixtures, never recorded user archives,
    paths, passwords or other private inputs.
    """
    report = {"total": 0, "passed": 0, "failed": 0, "failures": []}
    for name, args, kwargs in cases:
        report["total"] += 1
        expected = _outcome(reference, args, kwargs)
        actual = _outcome(candidate, args, kwargs)
        if actual == expected:
            report["passed"] += 1
        else:
            report["failed"] += 1
            report["failures"].append({
                "case": name,
                "expected": _shown(expected),
                "actual": _shown(actual),
            })
    return report


def interleaved_median(reference, candidate, *, rounds=7):
    """Time two implementations in alternating order and compare medians."""
    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds <= 0:
        raise ValueError("rounds must be a positive integer")
    samples = {"reference": [], "candidate": []}
    implementations = {"reference": reference, "candidate": candidate}
    for index in range(rounds):
        order = ("reference", "candidate")
        if index % 2:
            order = tuple(reversed(order))
        for name in order:
            stats, _ = timed(implementations[name])
            samples[name].append(stats)

    import statistics

    result = {"rounds": rounds, "order": "alternating"}
    for name in ("reference", "candidate"):
        rows = samples[name]
        result[name] = {
            "seconds": statistics.median(row["seconds"] for row in rows),
            "cpu_seconds": statistics.median(row["cpu_seconds"] for row in rows),
            "peak_memory_bytes": max(row["peak_memory_bytes"] for row in rows),
            "samples_seconds": [row["seconds"] for row in rows],
        }
    candidate_seconds = result["candidate"]["seconds"]
    result["speedup"] = (round(result["reference"]["seconds"] / candidate_seconds, 2)
                         if candidate_seconds else None)
    return result


def check_disk(path: Path, needed: int) -> str:
    free = shutil.disk_usage(path).free
    if free < needed:
        return f"insufficient disk: need {util.human_size(needed)}, free {util.human_size(free)}"
    return ""


def make_zip(path: Path, count: int):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(count):
            zf.writestr(f"entry-{i:04d}.txt", (f"payload {i}\n" * 8).encode())


def _clean_appledouble(root: Path):
    """The build/ volume stores xattrs as AppleDouble sidecars; those would
    double the walk count and make the sample size a lie."""
    for pattern in ("**/._*", "._*"):
        for path in root.glob(pattern):
            if path.is_file():
                path.unlink(missing_ok=True)


def make_tree(root: Path, files: int, dirs: int):
    for d in range(dirs):
        (root / f"dir-{d:03d}").mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (root / f"dir-{i % dirs:03d}" / f"file-{i:05d}.bin").write_bytes(b"x" * 64)
    _clean_appledouble(root)


def compress_extract(work: Path, big_mib: int):
    src = work / "big.bin"
    with open(src, "wb") as fh:
        fh.write(os.urandom(big_mib * MIB))
    archive = work / "big.7z"
    add = [ZZ, "a", "-t7z", "-mx1", "-bso0", "-bsp0", str(archive), str(src)]

    def compress():
        subprocess.run(add, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)

    def extract():
        dest = work / "big-out"
        shutil.rmtree(dest, ignore_errors=True)
        subprocess.run([ZZ, "x", str(archive), "-o" + str(dest), "-bso0", "-bsp0"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    stats, _ = timed(compress)
    comp_result = {**stats, "input_bytes": os.path.getsize(src),
                   "archive_bytes": os.path.getsize(archive) if archive.exists() else 0}
    stats, _ = timed(extract)
    out = work / "big-out" / src.name
    extract_result = {**stats, "output_bytes": os.path.getsize(out) if out.exists() else 0}
    digest_ok = out.is_file() and out.stat().st_size == os.path.getsize(src)
    return comp_result, extract_result, digest_ok


def concurrent_inspect(engine, archives, edge_archives, workers, *, rounds=7):
    """Check sequential/parallel equivalence, then compare interleaved medians."""
    import concurrent.futures

    def parallel_one(path):
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            return pool.submit(engine.inspect, path).result()

    cases = [(Path(path).name, (path,), {}) for path in edge_archives]
    replay = replay_calls(engine.inspect, parallel_one, cases)

    def sequential():
        return [engine.inspect(path) for path in archives]

    def parallel():
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(engine.inspect, archives))

    comparison = interleaved_median(sequential, parallel, rounds=rounds)
    return {
        "workers": workers,
        "rounds": comparison["rounds"],
        "order": comparison["order"],
        "replay": replay,
        "sequential": comparison["reference"],
        "parallel": comparison["candidate"],
        "speedup": comparison["speedup"],
    }


def run(mode: str, work: Path, keep: bool):
    cfg = CASES[mode]
    result = {
        "benchmark": "arca-benchmark",
        "mode": mode,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "command": " ".join(sys.argv),
        "machine": machine(),
        "parameters": {**cfg, "root": str(work)},
        "cases": {},
    }
    engine = eng.Engine()
    result["engine"] = {"version": engine.version, "path": engine.zz}

    # --------------------------------------------- 1000-entry ZIP inspect
    zip_path = work / "inspect-1000.zip"
    make_zip(zip_path, 1000)
    stats, info = timed(lambda: engine.inspect(str(zip_path)))
    result["cases"]["zip_inspect_1000"] = {
        "entries": len(info["entries"]),
        "truncated": info.get("entriesTruncated", 0),
        "json_bytes": len(json.dumps(info, ensure_ascii=False).encode()),
        **stats,
    }

    # ---------------------------------------------- large file round trip
    comp, extract, ok = compress_extract(work, cfg["big_mib"])
    result["cases"]["large_file_7z"] = {
        "input_mib": cfg["big_mib"], "compress": comp, "extract": extract,
        "round_trip_size_ok": ok,
    }

    # ------------------------------------------------ many-file scanning
    tree = work / "tree"
    make_tree(tree, cfg["files"], cfg["dirs"])
    stats, scanned = timed(lambda: util.scan_inputs([str(tree)]))
    result["cases"]["scan_many_files"] = {
        "files": cfg["files"], "dirs": cfg["dirs"],
        "scanned_files": scanned[0], "scanned_bytes": scanned[1], **stats,
    }

    # --------------------------------------------- concurrent inspections
    archives = []
    for i in range(cfg["workers"]):
        path = work / f"conc-{i}.zip"
        make_zip(path, 1000)
        archives.append(str(path))

    edge_empty = work / "edge-empty.zip"
    with zipfile.ZipFile(edge_empty, "w"):
        pass
    edge_unicode = work / "edge-unicode.zip"
    with zipfile.ZipFile(edge_unicode, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("目录/", b"")
        zf.writestr("目录/中文-😀.txt", b"fixed synthetic payload\n")
    edge_broken = work / "edge-broken.zip"
    edge_broken.write_bytes(b"not a ZIP archive\n")
    result["cases"]["concurrent_inspect"] = concurrent_inspect(
        engine, archives,
        [str(edge_empty), str(edge_unicode), str(edge_broken)], cfg["workers"])

    result["cleanup"] = {"kept": keep, "root": str(work) if keep else "removed"}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=sorted(CASES), default="quick")
    parser.add_argument("--json", default="")
    parser.add_argument("--keep", action="store_true", help="keep generated corpus")
    args = parser.parse_args()
    cfg = CASES[args.mode]
    build = build_dir()
    build.mkdir(exist_ok=True)
    # Rough need: big file + archive + extracted copy + tree + fixtures.
    needed = (cfg["big_mib"] * 3 + 32) * MIB
    problem = check_disk(build, needed)
    work = Path(tempfile.mkdtemp(prefix=f"bench-{args.mode}-", dir=build))
    out_path = Path(args.json) if args.json else build / (
        f"benchmark-{args.mode}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    try:
        if problem:
            result = {
                "benchmark": "arca-benchmark", "mode": args.mode,
                "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "command": " ".join(sys.argv), "machine": machine(),
                "parameters": {**cfg, "root": str(work)}, "skipped": problem,
                "cases": {},
            }
        else:
            result = run(args.mode, work, args.keep)
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nJSON written to {out_path}")
    if result.get("skipped"):
        print(f"SKIPPED: {result['skipped']} (no numbers were fabricated)")
    replay = result.get("cases", {}).get("concurrent_inspect", {}).get("replay", {})
    if replay.get("failed"):
        print(f"FAILED: {replay['failed']} semantic replay case(s) did not match")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
