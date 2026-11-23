"""Profile Arca's pure-Python hotspots with large synthetic corpora.

This is intentionally separate from benchmark.py: it adds stress cases without
changing the established quick/full baselines.  Corpus generation stays outside
the profiler.  No user archive, path, password or recorded private call is read.

Usage:
    build/venv/bin/python -S -E -B scripts/hotspot_profile.py
    build/venv/bin/python -S -E -B scripts/hotspot_profile.py --entries 100000
"""
from __future__ import annotations

import argparse
import cProfile
import json
import os
from pathlib import Path
import pstats
import shutil
import struct
import sys
import tempfile
import time
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from _paths import build_dir  # noqa: E402
from arca import engine as eng  # noqa: E402
from arca import server, zipfix  # noqa: E402
from benchmark import machine, make_zip  # noqa: E402


def make_legacy_zip(path: Path, names):
    """Build a stored ZIP whose non-UTF-8 names are raw GBK bytes.

    ZIP64 end records are included when the entry count needs them; individual
    entries remain small enough for ordinary ZIP32 local/central headers.
    """
    names = list(names)
    payload = b"fixed synthetic payload\n"
    crc = zlib.crc32(payload)
    central = []
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        for text in names:
            name = text.encode("gbk")
            offset = fh.tell()
            size = len(payload)
            fh.write(struct.pack(
                "<IHHHHHIIIHH", 0x04034B50, 20, 0, 0, 0, 0,
                crc, size, size, len(name), 0))
            fh.write(name)
            fh.write(payload)
            central.append(struct.pack(
                "<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0, 0, 0, 0,
                crc, size, size, len(name), 0, 0, 0, 0, 0, offset) + name)
        central_offset = fh.tell()
        for record in central:
            fh.write(record)
        central_size = fh.tell() - central_offset
        count = len(names)
        if count >= 0xFFFF:
            zip64_offset = fh.tell()
            fh.write(struct.pack(
                "<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0,
                count, count, central_size, central_offset))
            fh.write(struct.pack(
                "<IIQI", 0x07064B50, 0, zip64_offset, 1))
        fh.write(struct.pack(
            "<IHHHHIIH", 0x06054B50, 0, 0,
            min(count, 0xFFFF), min(count, 0xFFFF),
            central_size, central_offset, 0))


def profile_call(case, fn, output: Path):
    """Profile one already-prepared operation and retain Arca-owned hot rows."""
    output.parent.mkdir(parents=True, exist_ok=True)
    profiler = cProfile.Profile()
    started = time.perf_counter()
    cpu_started = time.process_time()
    profiler.enable()
    try:
        value = fn()
    finally:
        profiler.disable()
    report = {
        "case": case,
        "seconds": time.perf_counter() - started,
        "cpu_seconds": time.process_time() - cpu_started,
        "pstats": str(output),
        "top": [],
    }
    profiler.dump_stats(str(output))
    rows = []
    source_root = str(ROOT / "src" / "arca") + os.sep
    for (filename, line, name), (_cc, calls, own, cumulative, _callers) in \
            pstats.Stats(profiler).stats.items():
        if not filename.startswith(source_root):
            continue
        rows.append((cumulative, own, calls, filename, line, name))
    for cumulative, own, calls, filename, line, name in sorted(rows, reverse=True)[:12]:
        report["top"].append({
            "file": os.path.relpath(filename, source_root), "line": line, "name": name,
            "calls": calls, "own_seconds": own, "cumulative_seconds": cumulative,
        })
    return report, value


def run(work: Path, pstats_dir: Path, *, entries: int, selection_count: int):
    if entries <= 0 or selection_count <= 0:
        raise ValueError("entries and selection_count must be positive")
    work.mkdir(parents=True, exist_ok=True)
    pstats_dir.mkdir(parents=True, exist_ok=True)
    engine = eng.Engine()
    cases = {}

    archive = work / "utf8.zip"
    make_zip(archive, entries)
    report, info = profile_call(
        "zip_inspect", lambda: engine.inspect(str(archive)),
        pstats_dir / "zip-inspect.pstats")
    report["returned_entries"] = len(info["entries"])
    report["source_entries"] = entries
    cases["zip_inspect"] = report
    del info

    legacy = work / "legacy-gbk.zip"
    make_legacy_zip(legacy, (
        f"目录-{index % selection_count:04d}/中文文件-{index:06d}.txt"
        for index in range(entries)))
    report, plan = profile_call(
        "legacy_zip_plan", lambda: zipfix.plan(str(legacy)),
        pstats_dir / "legacy-zip-plan.pstats")
    report["source_entries"] = entries
    report["fixed_entries"] = plan["fixed"] if plan else 0
    cases["legacy_zip_plan"] = report
    del plan

    slt = "Path = synthetic.7z\nType = 7z\n\n" + "\n\n".join(
        f"Path = dir-{index % selection_count:04d}/file-{index:06d}.bin\n"
        f"Size = 64\nPacked Size = 32\nAttributes = A"
        for index in range(entries))
    report, parsed = profile_call(
        "slt_parse", lambda: eng._parse_slt(slt), pstats_dir / "slt-parse.pstats")
    report["parsed_entries"] = len(parsed[1])
    cases["slt_parse"] = report
    del parsed, slt

    directories = [{"path": "root", "size": 0, "isDir": True}] + [
        {"path": f"root/dir-{index:04d}", "size": 0, "isDir": True}
        for index in range(selection_count)]
    files = [{
        "path": f"root/dir-{index % selection_count:04d}/file-{index:06d}.bin",
        "size": 64, "isDir": False,
    } for index in range(entries)]
    full = directories + files
    file_names = [entry["path"] for entry in files]
    report, kept = profile_call(
        "prune_covered", lambda: server._prune_covered_entries(file_names, full),
        pstats_dir / "prune-covered.pstats")
    report["kept_entries"] = len(kept)
    cases["prune_covered"] = report
    del kept

    selected = [entry["path"] for entry in directories[1:]]
    report, selected_bytes = profile_call(
        "selected_bytes", lambda: server._selected_bytes(selected, full),
        pstats_dir / "selected-bytes.pstats")
    report["selected_bytes"] = selected_bytes
    cases["selected_bytes"] = report
    return {
        "benchmark": "arca-hotspot-profile",
        "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": machine(),
        "parameters": {"entries": entries, "selection_count": selection_count},
        "cases": cases,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", type=int, default=50000)
    parser.add_argument("--selection-count", type=int, default=1000)
    parser.add_argument("--json", default="")
    parser.add_argument("--keep", action="store_true", help="keep generated synthetic corpus")
    args = parser.parse_args()
    if args.entries <= 0 or args.selection_count <= 0:
        parser.error("--entries and --selection-count must be positive")
    build = build_dir()
    build.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="hotspot-profile-", dir=build))
    out_path = Path(args.json) if args.json else build / (
        f"hotspot-profile-{time.strftime('%Y%m%d-%H%M%S')}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pstats_dir = Path(str(out_path) + ".pstats")
    try:
        result = run(work, pstats_dir, entries=args.entries,
                     selection_count=args.selection_count)
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    result["cleanup"] = {"kept": args.keep, "root": str(work) if args.keep else "removed"}
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nJSON written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
