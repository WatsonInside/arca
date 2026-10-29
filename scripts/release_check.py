#!/usr/bin/env python3
"""只读发布预检（v3 任务书 T09）。

不构建、不联网、不改 README/资产；复用 package/publish 的版本与资产约定，
但 import 它们不应产生任何副作用（本脚本会验证这一点）。

用法：
    build/venv/bin/python -S -E -B scripts/release_check.py
    build/venv/bin/python -S -E -B scripts/release_check.py --json
    build/venv/bin/python -S -E -B scripts/release_check.py --require-signed   # 签名完成后再用
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from arca import util  # noqa: E402
from _paths import build_dir  # noqa: E402


def _run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(ROOT), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout, check=False)


def _check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_app_version(app: Path, expected: str) -> dict:
    plist = app / "Contents" / "Info.plist"
    if not plist.is_file():
        return _check("app-version", "FAIL", f"缺少 {plist}")
    try:
        data = plistlib.loads(plist.read_bytes())
    except Exception as exc:  # noqa: BLE001
        return _check("app-version", "FAIL", f"Info.plist 解析失败：{exc!r}")
    actual = str(data.get("CFBundleShortVersionString") or "")
    if actual != expected:
        return _check("app-version", "FAIL",
                      f"应用版本 {actual!r} != 期望 {expected!r}")
    return _check("app-version", "PASS", f"CFBundleShortVersionString={actual}")


def _sidecar_digest(sidecar: Path) -> tuple[str, str]:
    text = sidecar.read_text(encoding="utf-8").strip()
    parts = text.split()
    if len(parts) != 2:
        raise ValueError("sha256 文件格式不是 '<hex>  <name>'")
    return parts[0].lower(), parts[1]


def check_assets(dist: Path, version: str) -> list[dict]:
    results = []
    names = [f"Arca-{version}.dmg", f"Arca-{version}.pkg",
             f"Arca-{version}-mac.tar.xz", f"Arca-{version}-release-notes.txt"]
    missing = [name for name in names if not (dist / name).is_file()]
    if missing:
        results.append(_check("assets", "FAIL", "缺少资产：" + ", ".join(missing)))
    else:
        results.append(_check("assets", "PASS", ", ".join(names)))
    archive = dist / names[2]
    sidecar = archive.with_name(archive.name + ".sha256")
    if not sidecar.is_file():
        results.append(_check("digest", "FAIL", f"缺少 {sidecar.name}"))
    elif archive.is_file():
        try:
            expected, listed = _sidecar_digest(sidecar)
        except ValueError as exc:
            results.append(_check("digest", "FAIL", str(exc)))
        else:
            actual = _sha256(archive)
            if listed != archive.name or expected != actual:
                results.append(_check("digest", "FAIL",
                                      f"sha256 不匹配：sidecar={expected[:12]}… 实际={actual[:12]}…"))
            else:
                results.append(_check("digest", "PASS", f"sha256={actual}"))
    for name in names[:2]:
        path = dist / name
        if path.is_file():
            results.append(_check(f"size:{name}", "PASS",
                                  f"{path.stat().st_size} bytes, sha256={_sha256(path)[:16]}…"))
    return results


def check_tar_safety(archive: Path) -> dict:
    try:
        with tarfile.open(archive, "r:xz") as tf:
            members = tf.getmembers()
    except Exception as exc:  # noqa: BLE001
        return _check("tar-safety", "FAIL", f"tar.xz 无法读取：{exc!r}")
    if not members:
        return _check("tar-safety", "FAIL", "归档为空")
    bad = []
    for member in members:
        name = member.name
        if name.startswith("/") or name.startswith(".."):
            bad.append(f"{name}（绝对路径或 ..）")
            continue
        if any(part == ".." for part in Path(name).parts):
            bad.append(f"{name}（路径包含 ..）")
            continue
        if member.isdev():
            bad.append(f"{name}（设备文件）")
            continue
        if member.issym() or member.islnk():
            target = member.linkname
            if target.startswith("/"):
                bad.append(f"{name} -> {target}（绝对链接）")
                continue
            resolved = (Path(name).parent / target).as_posix()
            if any(part == ".." for part in Path(resolved).parts):
                bad.append(f"{name} -> {target}（链接逃逸）")
    top = {Path(m.name).parts[0] for m in members if Path(m.name).parts}
    if top != {"Arca.app"}:
        bad.append(f"顶层目录不是单个 Arca.app：{sorted(top)}")
    if bad:
        return _check("tar-safety", "FAIL", "；".join(bad[:5]))
    return _check("tar-safety", "PASS", f"{len(members)} 个成员，路径与链接均安全")


def check_permissions(app: Path) -> dict:
    exe = app / "Contents" / "MacOS" / "Arca"
    if not exe.is_file():
        return _check("permissions", "FAIL", f"缺少可执行文件 {exe}")
    if not os.access(exe, os.X_OK):
        return _check("permissions", "FAIL", f"{exe} 无执行权限")
    return _check("permissions", "PASS", "Contents/MacOS/Arca 可执行")


def check_signature(app: Path, require_signed: bool = False) -> list[dict]:
    results = []
    codesign = _run(["codesign", "-dv", "--verbose=2", str(app)])
    verify = _run(["codesign", "--verify", "--deep", "--strict", str(app)])
    signed = verify.returncode == 0
    results.append(_check("codesign-verify", "PASS" if signed else "WARN",
                          "签名校验通过" if signed else
                          "codesign 校验未通过（未签名/未公证属于已知状态，不能作为已签名证据）"))
    if require_signed and not signed:
        results[-1]["status"] = "FAIL"
    return results


def check_provenance(summary: Path, head: str) -> dict:
    if not summary.is_file():
        return _check("provenance", "FAIL", f"找不到测试 summary：{summary}")
    try:
        data = json.loads(summary.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return _check("provenance", "FAIL", f"summary 解析失败：{exc!r}")
    problems = []
    if data.get("head") != head:
        problems.append(f"HEAD {str(data.get('head'))[:12]} != 当前 {head[:12]}")
    if not data.get("worktree_clean"):
        problems.append("测试时工作树不干净")
    suites = data.get("suites") or []
    if not suites:
        problems.append("summary 没有套件结果")
    failing = [s.get("name") for s in suites if s.get("result") != "PASS"]
    if failing:
        problems.append("非 PASS 套件：" + ", ".join(map(str, failing)))
    logs = data.get("logs") or []
    if isinstance(logs, dict):
        logs = list(logs.values())
    missing = [str(p) for p in logs if not Path(str(p)).is_file()]
    if missing:
        problems.append("日志缺失：" + ", ".join(missing[:3]))
    if problems:
        return _check("provenance", "FAIL", "；".join(problems))
    return _check("provenance", "PASS",
                  f"HEAD={head[:12]}，{len(suites)} 套全 PASS，summary={summary.name}")


def check_import_side_effects() -> dict:
    with tempfile.TemporaryDirectory(prefix="release-check-") as tmp:
        env = dict(os.environ, ARCA_BUILD_DIR=tmp, PYTHONDONTWRITEBYTECODE="1")
        code = ("import sys; sys.path.insert(0, 'scripts'); "
                "import package, publish; print('IMPORT_OK')")
        proc = subprocess.run([sys.executable, "-S", "-E", "-B", "-c", code],
                              cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=60, check=False)
        leftovers = list(Path(tmp).iterdir())
    if proc.returncode != 0 or b"IMPORT_OK" not in proc.stdout:
        return _check("import-side-effects", "FAIL",
                      "import package/publish 失败：" + proc.stdout.decode("utf-8", "replace")[-300:])
    if leftovers:
        return _check("import-side-effects", "FAIL",
                      "import 产生了构建目录内容：" + ", ".join(p.name for p in leftovers[:5]))
    return _check("import-side-effects", "PASS", "package/publish import 无构建副作用")


def check_network_wording(package_py: Path) -> dict:
    text = package_py.read_text(encoding="utf-8")
    if "不会联网" in text:
        return _check("wording", "FAIL", "package.py 仍有“不会联网”绝对表述")
    if "主动触发的" not in text:
        return _check("wording", "FAIL", "package.py 缺少显式联网范围说明")
    return _check("wording", "PASS", "安装器文案已限定联网范围")


def collect(app: Path, dist: Path, summary: Path, head: str,
            require_signed: bool = False) -> list[dict]:
    results = [check_app_version(app, util.APP_VERSION)]
    results.extend(check_assets(dist, util.APP_VERSION))
    archive = dist / f"Arca-{util.APP_VERSION}-mac.tar.xz"
    if archive.is_file():
        results.append(check_tar_safety(archive))
    results.append(check_permissions(app))
    results.extend(check_signature(app, require_signed=require_signed))
    results.append(check_provenance(summary, head))
    results.append(check_import_side_effects())
    results.append(check_network_wording(ROOT / "scripts" / "package.py"))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=ROOT / "release/Arca.app")
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--summary", type=Path, default=None)
    parser.add_argument("--head", default=None)
    parser.add_argument("--require-signed", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    head = args.head or _run(["git", "rev-parse", "HEAD"]).stdout.decode().strip()
    summary = args.summary
    if summary is None:
        candidates = sorted(build_dir().glob("all-test-summary-*.json"))
        if candidates:
            summary = candidates[-1]
        else:
            summary = build_dir() / "all-test-summary-missing.json"
    results = collect(args.app, args.dist, summary, head,
                      require_signed=args.require_signed)
    payload = {"ok": all(r["status"] != "FAIL" for r in results),
               "head": head, "summary": str(summary), "checks": results}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for item in results:
            print(f"{item['status']:4}  {item['name']:22} {item['detail']}")
        failed = [r for r in results if r["status"] == "FAIL"]
        print(f"\n{len(results) - len(failed)}/{len(results)} checked; "
              f"FAIL={len(failed)}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
