#!/usr/bin/env python3
"""本地校验 CI 骨架（T08）：只读 YAML 结构 + 本机工具前置。"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _yaml(path: pathlib.Path) -> dict:
    ruby = shutil.which("ruby")
    if not ruby:
        raise RuntimeError("未找到 ruby，无法离线解析 YAML（标记为未验证）")
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
    tools = {
        "python3": shutil.which("python3"),
        "node": shutil.which("node"),
        "chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "7zz(bundle)": str(ROOT / "vendor/7zz"),
        "runtime": str(ROOT / "build" / "python-runtime"),
    }
    results = []
    for name, path in tools.items():
        import os
        ok = bool(path) and os.path.exists(path)
        results.append({"check": f"preflight:{name}", "status": "PASS" if ok else "WARN",
                        "detail": str(path or "未找到")})
    results.append({"check": "preflight:工作流未在远端运行", "status": "WARN",
                    "detail": "本仓库未 push，CI 远端执行标记为未验证"})
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
