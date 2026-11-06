#!/usr/bin/env python3
"""本地校验 CI 骨架（T08）：只读 YAML 结构 + 本机工具前置。

本工具完全离线：不查询、也不断言 GitHub Actions 的远端运行状态；远端证据以
文档记录的 run ID 为准（full 42284502991、quick 42287171456，见 AGENTS.md 与
docs/ENGINEERING-NOTES.md 的 T08 段）。local_preflight() 只检查本机能否执行
workflow 中的命令（python3/node/Chrome/7zz/runtime 是否存在），不代表远端跑过。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
from _paths import build_dir

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _yaml(path: pathlib.Path) -> dict:
    ruby = shutil.which("ruby")
    if not ruby:
        raise RuntimeError("未找到 ruby，无法离线解析 YAML")
    code = ('require "yaml"; require "json"; '
            'puts JSON.generate(YAML.load_file(ARGV[0]))')
    out = subprocess.run([ruby, "-ryaml", "-rjson", "-e", code, str(path)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         check=False)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode("utf-8", "replace")[-300:])
    return json.loads(out.stdout)


def check_workflow(path: pathlib.Path) -> list[dict]:
    results = []
    try:
        data = _yaml(path)
    except Exception as exc:  # noqa: BLE001
        return [{"check": f"yaml:{path.name}", "status": "FAIL", "detail": str(exc)}]
    triggers = data.get("on") or data.get("true") or data.get(True) or {}
    keys = set(triggers) if isinstance(triggers, dict) else {triggers}
    text = path.read_text(encoding="utf-8")
    jobs = data.get("jobs") or {}
    checks = [
        ("workflow_dispatch 默认存在", "workflow_dispatch" in keys),
        ("jobs 非空", bool(jobs)),
        ("ARCA_BUILD_DIR 覆盖", "ARCA_BUILD_DIR" in text),
        ("permissions contents: read", (data.get("permissions") or {}).get("contents") == "read"),
        ("无 secrets 引用", "secrets." not in text),
        ("每个 job 有 runs-on macos", all("runs-on" in job and "macos" in str(job["runs-on"])
                                          for job in jobs.values())),
        ("每个 job 有 timeout-minutes", all("timeout-minutes" in job for job in jobs.values())),
        ("nightly 未预启用", "\n  schedule:" not in text),
    ]
    for name, ok in checks:
        results.append({"check": f"{path.name}:{name}", "status": "PASS" if ok else "FAIL",
                        "detail": ""})
    return results


def local_preflight() -> list[dict]:
    """本机前置检查；刻意不包含任何远端 CI 状态探测或断言。"""
    tools = {
        "python3": shutil.which("python3"),
        "node": shutil.which("node"),
        "chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "7zz(bundle)": str(ROOT / "vendor/7zz"),
        "runtime": str(build_dir() / "python-runtime"),
    }
    results = []
    for name, path in tools.items():
        import os
        ok = bool(path) and os.path.exists(path)
        results.append({"check": f"preflight:{name}", "status": "PASS" if ok else "WARN",
                        "detail": str(path or "未找到")})
    return results


def main() -> int:
    results = []
    if not WORKFLOWS.is_dir():
        results.append({"check": "workflows", "status": "FAIL", "detail": "缺少 .github/workflows"})
    else:
        for path in sorted(WORKFLOWS.glob("*.yml")):
            results.extend(check_workflow(path))
    results.extend(local_preflight())
    for item in results:
        print(f"{item['status']:4}  {item['check']:48} {item['detail']}")
    failed = [r for r in results if r["status"] == "FAIL"]
    print(f"\n{len(results) - len(failed)}/{len(results)} checked; FAIL={len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
