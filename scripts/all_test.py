"""Run the sequential local gates; real update installation is opt-in.

The suite list below is the single manifest for the default gate.  Each entry
declares how its result is judged (kind), its environment dependencies, and a
minimum expected result count so that an empty or truncated run can never pass
silently.  ``--list`` prints the manifest; ``--only``/``--skip`` accept a suite
name (repeatable or comma-separated).
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
BUILD = Path(os.environ.get("ARCA_BUILD_DIR") or (ROOT / "build"))
PY = [str(BUILD / "venv/bin/python"), "-S", "-E", "-B"]

# kind:
#   node           - JavaScript syntax check, judged by exit status
#   design         - prints "N finding(s)"; 0 required
#   unittest       - prints "Ran N tests"; at least ``min`` required
#   passed_failed  - prints "N passed, M failed"; M==0 and N>=min required
#   custom_cn      - prints "通过 N   失败 M"; M==0 and N>=min required
SUITES = [
    {"name": "node", "kind": "node", "deps": [], "min": 0},
    {"name": "design", "kind": "design", "script": "design_audit", "deps": [], "min": 0},
    {"name": "selftest", "kind": "custom_cn", "script": "selftest", "deps": ["engine"], "min": 30},
    {"name": "integrity", "kind": "custom_cn", "script": "integrity_test", "deps": [], "min": 80},
    {"name": "checksum", "kind": "unittest", "script": "checksum_test", "deps": [], "min": 10},
    {"name": "compress-space", "kind": "unittest", "script": "compress_space_test", "deps": ["engine"], "min": 15},
    {"name": "compress-scan", "kind": "passed_failed", "script": "compress_scan_test", "deps": ["engine"], "min": 8},
    {"name": "inspect-cache", "kind": "unittest", "script": "inspect_cache_test", "deps": [], "min": 10},
    {"name": "zip-inspect", "kind": "unittest", "script": "zip_inspect_test", "deps": [], "min": 15},
    {"name": "extract-list", "kind": "passed_failed", "script": "extract_list_test", "deps": ["engine"], "min": 5},
    {"name": "concurrency", "kind": "passed_failed", "script": "concurrency_test", "deps": ["engine"], "min": 3},
    {"name": "keyring", "kind": "unittest", "script": "keyring_test", "deps": [], "min": 45},
    {"name": "progress", "kind": "passed_failed", "script": "progress_test", "deps": ["engine"], "min": 6},
    {"name": "appupdate", "kind": "unittest", "script": "appupdate_test", "deps": ["fixture"], "min": 24},
    {"name": "zip-password", "kind": "unittest", "script": "zip_password_test", "deps": ["engine"], "min": 6},
    {"name": "stats", "kind": "passed_failed", "script": "stats_test", "deps": [], "min": 18},
    {"name": "jobwait", "kind": "unittest", "script": "jobwait_test", "deps": [], "min": 5},
    {"name": "menu-cleanup", "kind": "unittest", "script": "menu_cleanup_test", "deps": [], "min": 7},
    {"name": "partial-extract", "kind": "unittest", "script": "partial_extract_test", "deps": ["engine"], "min": 30},
    {"name": "fuzz-soak", "kind": "unittest", "script": "fuzz_soak_test", "deps": ["engine"], "min": 5},
    {"name": "interrupt-recovery", "kind": "unittest", "script": "interrupt_recovery_test", "deps": ["app"], "min": 5},
    {"name": "shutdown", "kind": "unittest", "script": "shutdown_test", "deps": [], "min": 10},
    {"name": "menu", "kind": "passed_failed", "script": "menu_test", "deps": ["headless", "chrome"],
     "headless": "short", "min": 90},
    {"name": "compress-draft", "kind": "passed_failed", "script": "compress_draft_test", "deps": ["headless", "chrome"],
     "headless": "short", "min": 30},
    {"name": "acceptance", "kind": "passed_failed", "script": "acceptance_probe", "deps": ["headless", "http"],
     "headless": "short", "min": 18, "acceptance": True},
    {"name": "update", "kind": "custom_cn", "script": "update_test", "deps": ["headless", "fixture"],
     "headless": "short", "fixture": "short", "min": 28},
    {"name": "update-long", "kind": "custom_cn", "script": "update_test", "deps": ["headless", "fixture"],
     "headless": "long", "fixture": "long", "min": 30},
]
MANIFEST_NAMES = [suite["name"] for suite in SUITES]


def clean(text):
    text = re.sub(r'(?i)(token[\s"\x27:=]+)[^\s"\x27,}]+', r'\1[REDACTED]', text)
    return re.sub(r'(http://127\.0\.0\.1:\d+/)\S+', r'\1[REDACTED]', text)


def evaluate(suite, code, text):
    """Judge one suite's output.  Returns (ok, human detail)."""
    kind = suite["kind"]
    minimum = suite.get("min", 0)
    if code == 124:
        return False, "timeout"
    if code != 0:
        return False, f"exit {code}"
    if kind == "node":
        return True, "syntax errors 0"
    if kind == "design":
        found = re.findall(r"(\d+) finding\(s\)", text)
        if not found:
            return False, "missing 'finding(s)' marker"
        actual = int(found[-1])
        return actual == 0, f"{actual} finding(s)"
    if kind == "unittest":
        found = re.findall(r"Ran (\d+) tests?", text)
        if not found:
            return False, "missing 'Ran N tests' marker"
        actual = int(found[-1])
        return actual >= minimum, f"{actual} tests (min {minimum})"
    if kind == "passed_failed":
        found = re.findall(r"(\d+) passed, (\d+) failed", text)
        if not found:
            return False, "missing 'N passed, M failed' marker"
        passed, failed = int(found[-1][0]), int(found[-1][1])
        ok = failed == 0 and passed >= minimum
        return ok, f"{passed} passed, {failed} failed (min {minimum})"
    if kind == "custom_cn":
        found = re.findall(r"通过\s*(\d+)\s*[·]?\s*失败\s*(\d+)", text)
        if not found:
            return False, "missing '通过 N 失败 M' marker"
        passed, failed = int(found[-1][0]), int(found[-1][1])
        ok = failed == 0 and passed >= minimum
        return ok, f"通过 {passed} 失败 {failed} (min {minimum})"
    return False, f"unknown kind {kind}"


class Runner:
    def __init__(self, env, prefix):
        self.env = env
        self.prefix = prefix
        self.results = []
        self.logs = []

    def log(self, name, text):
        text = clean(text)
        path = BUILD / f"{self.prefix}-{name}.log"
        path.write_text(text)
        self.logs.append(str(path))
        print(text, end="" if text.endswith("\n") else "\n", flush=True)

    def run(self, suite, command, timeout=540):
        name = suite["name"]
        try:
            result = subprocess.run(command, cwd=ROOT, env=self.env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=timeout)
            text, code = result.stdout, result.returncode
        except subprocess.TimeoutExpired as exc:
            text = (exc.stdout or b"")
            if isinstance(text, bytes):
                text = text.decode("utf-8", "replace")
            text += "\nTIMEOUT\n"
            code = 124
        except OSError as exc:
            text, code = str(exc) + "\n", 1
        ok, detail = evaluate(suite, code, text)
        log_name = f"{self.prefix}-{name}.log"
        self.results.append({
            "name": name,
            "kind": suite["kind"],
            "deps": list(suite.get("deps", [])),
            "min": suite.get("min", 0),
            "code": code,
            "result": "PASS" if ok else "FAIL",
            "detail": detail,
            "log": log_name,
        })
        verdict = "PASS" if ok else "FAIL"
        self.log(name, "COMMAND: " + " ".join(command) + "\n" + text +
                 f"EXIT: {code}\nCHECK {suite['kind']}: {verdict} - {detail}\n")
        return ok

    def summary(self):
        print("\nSuite | Result | Kind | Key counts")
        print("--- | --- | --- | ---")
        for item in self.results:
            print(f"{item['name']} | {item['result']} | {item['kind']} | {item['detail']}")
        failed = sum(item["result"] == "FAIL" for item in self.results)
        print(f"{len(self.results) - failed} passed, {failed} failed", flush=True)
        return int(bool(failed))


def git_provenance():
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.strip()
    except OSError:
        return "", ""
    return head, dirty


def expand(values):
    names = []
    for value in values or []:
        for part in str(value).split(","):
            part = part.strip()
            if part:
                names.append(part)
    return names


def choose_suites(args):
    only = expand(args.only)
    skip = expand(args.skip)
    unknown = [name for name in only + skip if name not in MANIFEST_NAMES]
    if unknown:
        raise ValueError("unknown suite(s): " + ", ".join(unknown))
    if len(set(only)) != len(only) or len(set(skip)) != len(skip):
        raise ValueError("duplicate suite name in --only/--skip")
    if only and "acceptance" in only and args.skip_acceptance:
        raise ValueError("acceptance is excluded by --skip-acceptance")
    if only and "update-long" in only and args.full_update:
        raise ValueError("update-long is not compatible with --full-update")

    selected = [suite for suite in SUITES if (not only or suite["name"] in only)]
    selected = [suite for suite in selected if suite["name"] not in skip]
    if args.skip_acceptance:
        selected = [suite for suite in selected if not suite.get("acceptance")]
    if not selected:
        raise ValueError("no suites selected")

    skipped = []
    chosen = {suite["name"] for suite in selected}
    for suite in SUITES:
        if suite["name"] in chosen:
            continue
        if args.skip_acceptance and suite.get("acceptance"):
            reason = "--skip-acceptance"
        elif suite["name"] in skip:
            reason = "--skip"
        elif args.full_update and suite["name"] == "update-long":
            reason = "--full-update"
        elif only:
            reason = "not in --only"
        else:
            reason = "not selected"
        skipped.append({"name": suite["name"], "reason": reason})
    return selected, skipped


def print_manifest():
    print("Suite | Kind | Headless | Deps | Min | Fixture")
    print("--- | --- | --- | --- | --- | ---")
    for suite in SUITES:
        print("{} | {} | {} | {} | {} | {}".format(
            suite["name"], suite["kind"], suite.get("headless", "-"),
            ",".join(suite.get("deps", [])) or "-", suite.get("min", 0),
            suite.get("fixture", "-")))


def sync_bundle(runner):
    bundle = BUILD / "Arca-test.app"
    launcher = bundle / "Contents/MacOS/Arca"
    if not launcher.is_file():
        template = ROOT / "release/Arca.app"
        if not (template / "Contents/MacOS/Arca").is_file():
            raise RuntimeError("No reusable test launcher; prepare Arca-test.app using handoff §4.1")
        subprocess.run(["cp", "-R", str(template), str(bundle)], check=True)
    runtime = bundle / "Contents/Resources/python"
    if not runtime.is_dir():
        source = Path(os.environ.get("ARCA_PYTHON", str(BUILD / "python-runtime")))
        if not source.is_dir():
            source = ROOT / "release/Arca.app/Contents/Resources/python"
        if not source.is_dir():
            raise RuntimeError("No reusable runtime; set ARCA_PYTHON per handoff §4.1")
        subprocess.run(["cp", "-R", str(source), str(runtime)], check=True)
    source = ROOT / "src/arca"
    target = bundle / "Contents/Resources/app/arca"
    lines = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if not path.is_file() or "__pycache__" in relative.parts or path.name.startswith("._"):
            continue
        if path.suffix != ".py" and "web" not in relative.parts:
            continue
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != hashlib.sha256(dest.read_bytes()).hexdigest():
            raise RuntimeError(f"Source synchronization mismatch: {relative}")
        lines.append(f"{relative} SHA256 {digest} identical")
    for path in target.rglob("*.pyc"):
        path.unlink()
    runner.log("sync", "\n".join(lines) + f"\nTest bundle synchronized: {len(lines)} source/web files, SHA256 identical; bundled pyc removed\nEXIT: 0\n")
    return launcher, target.parent


@contextmanager
def headless(runner, full_update=False, fixture="short", port_file=None):
    launcher, app_source = sync_bundle(runner)
    port = Path(port_file) if port_file is not None else BUILD / "port-all.json"
    port.unlink(missing_ok=True)
    command = [str(launcher), "--headless", "--port-file", str(port)]
    if full_update:
        old = BUILD / "improve-c4-old-engine"
        if not (old / "7zz").is_file():
            raise RuntimeError("--full-update requires the isolated 23.01 fixture build/improve-c4-old-engine/7zz")
        bootstrap = (
            "import sys,json,signal;sys.path.insert(0,sys.argv[1]);"
            "from arca import engine,server,util;from arca.settings import Settings;"
            "engine._candidate_dirs=lambda:[sys.argv[2]];e=engine.Engine();"
            "assert e.version=='23.01',e.version;"
            "assert util.app_support_dir().startswith(__import__('os').environ['HOME']);"
            "ctx=server.AppContext(e,Settings(),sys.argv[1]+'/arca/web');"
            "service=server.Server(ctx);service.start();"
            "open(sys.argv[3],'w').write(json.dumps({'port':service.port,'token':ctx.token}));"
            "print('Isolated 23.01 headless server ready',flush=True);signal.pause()"
        )
        command = PY + ["-c", bootstrap, str(app_source), str(old), str(port)]
    else:
        bootstrap = (
            "import sys,json,signal;sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[2]);"
            "from update_fixtures import upstream;fixture=upstream(sys.argv[4]);fixture.__enter__();"
            "from arca import engine,server;from arca.settings import Settings;"
            "e=engine.Engine();assert e.version=='26.05',e.version;"
            "ctx=server.AppContext(e,Settings(),sys.argv[1]+'/arca/web');"
            "service=server.Server(ctx);service.start();"
            "open(sys.argv[3],'w').write(json.dumps({'port':service.port,'token':ctx.token}));"
            "print('Bundled 26.05 server with controlled '+sys.argv[4]+' upstream fixture',flush=True);signal.pause()"
        )
        command = PY + ["-c", bootstrap, str(app_source), str(ROOT / "scripts"), str(port), fixture]
    with tempfile.TemporaryFile(mode="w+") as output:
        proc = subprocess.Popen(command, cwd=ROOT, env=runner.env, stdout=output,
                                stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.monotonic() + 30
            while True:
                if proc.poll() is not None:
                    raise RuntimeError("Headless process exited before readiness")
                try:
                    data = json.loads(port.read_text())
                    if isinstance(data.get("port"), int) and data.get("token"):
                        break
                except (FileNotFoundError, ValueError):
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError("Headless readiness timed out")
                time.sleep(.1)
            yield port
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            output.seek(0)
            runner.log("headless", output.read() + f"HEADLESS STOPPED: {proc.returncode}\n")
            port.unlink(missing_ok=True)


def update_command(port, install=False):
    shots = BUILD / "all-test-shots"
    shots.mkdir(exist_ok=True)
    bootstrap = "import sys;sys.path.insert(0,sys.argv.pop(1));import update_test;update_test.SHOTS=sys.argv.pop(1);update_test.main()"
    return PY + ["-c", bootstrap, str(ROOT / "scripts"), str(shots), "--port-file", str(port)] + (["--install"] if install else [])


def headless_command(suite, port):
    name = suite["name"]
    if name == "menu":
        return PY + ["scripts/menu_test.py", "--port-file", str(port)]
    if name == "compress-draft":
        return PY + ["scripts/compress_draft_test.py", "--port-file", str(port)]
    if name == "acceptance":
        return PY + ["scripts/acceptance_probe.py", "--port-file", str(port)]
    if name in ("update", "update-long"):
        return update_command(port) + ["--fixture", suite.get("fixture", "short")]
    raise RuntimeError(f"no headless command mapping for {name}")


def write_summary(path, payload):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list the suite manifest and exit")
    parser.add_argument("--only", action="append", default=[], metavar="SUITE",
                        help="run only these suites (repeatable or comma-separated)")
    parser.add_argument("--skip", action="append", default=[], metavar="SUITE",
                        help="skip these suites (repeatable or comma-separated)")
    parser.add_argument("--full-update", action="store_true", help="download/install with isolated 23.01 fixture")
    parser.add_argument("--skip-acceptance", action="store_true", help="explicitly omit HTTP acceptance probes")
    args = parser.parse_args()

    if args.list:
        print_manifest()
        return 0

    try:
        selected, skipped = choose_suites(args)
    except ValueError as exc:
        print(f"suite selection error: {exc}", file=sys.stderr)
        return 2

    BUILD.mkdir(parents=True, exist_ok=True)
    started = time.time()
    started_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    head, dirty = git_provenance()
    if dirty:
        print(f"[warn] worktree is not clean ({len(dirty.splitlines())} entries); provenance recorded as dirty", flush=True)
    prefix = f"all-test-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    stamp = prefix[len("all-test-"):]
    summary_path = BUILD / f"all-test-summary-{stamp}.json"
    with tempfile.TemporaryDirectory(prefix="arca-all-test-home-") as home:
        env = dict(os.environ, HOME=home, PYTHONDONTWRITEBYTECODE="1")
        runner = Runner(env, prefix)
        for suite in selected:
            if suite.get("headless"):
                continue
            if suite["kind"] == "node":
                runner.run(suite, ["node", "--check", "src/arca/web/app.js"])
            else:
                runner.run(suite, PY + [f"scripts/{suite['script']}.py"])
        short = [suite for suite in selected
                 if suite.get("headless") == "short" and not (args.full_update and suite["name"] == "update")]
        long_group = [suite for suite in selected if suite.get("headless") == "long"]
        install_update = args.full_update and any(suite["name"] == "update" for suite in selected)
        try:
            if short:
                with headless(runner) as port:
                    for suite in short:
                        runner.run(suite, headless_command(suite, port))
            if long_group:
                with headless(runner, fixture="long") as port:
                    for suite in long_group:
                        runner.run(suite, headless_command(suite, port))
            if install_update:
                suite = next(item for item in SUITES if item["name"] == "update")
                with headless(runner, full_update=True) as port:
                    runner.run(suite, update_command(port, install=True), timeout=900)
        except Exception as exc:
            runner.results.append({
                "name": "headless", "kind": "runtime", "deps": ["headless"], "min": 0,
                "code": 1, "result": "FAIL", "detail": str(exc), "log": f"{prefix}-headless-error.log",
            })
            runner.log("headless-error", f"{type(exc).__name__}: {exc}\nEXIT: 1\n")
    exit_code = runner.summary()
    finished = time.time()
    payload = {
        "schema": 1,
        "generator": "scripts/all_test.py",
        "argv": sys.argv[1:],
        "prefix": prefix,
        "started": started_iso,
        "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_seconds": round(finished - started, 3),
        "head": head,
        "worktree_clean": not dirty,
        "dirty_entries": len(dirty.splitlines()) if dirty else 0,
        "selected": [suite["name"] for suite in selected],
        "skipped": skipped,
        "suites": runner.results,
        "logs": runner.logs,
        "summary_path": str(summary_path),
    }
    write_summary(summary_path, payload)
    print(f"summary JSON: {summary_path}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
