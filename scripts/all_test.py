"""Run the sequential local gates; real update installation is opt-in."""
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
BUILD = ROOT / "build"
PY = [str(BUILD / "venv/bin/python"), "-S", "-E", "-B"]


def clean(text):
    text = re.sub(r'(?i)(token[\s"\x27:=]+)[^\s"\x27,}]+', r'\1[REDACTED]', text)
    return re.sub(r'(http://127\.0\.0\.1:\d+/)\S+', r'\1[REDACTED]', text)


class Runner:
    def __init__(self, env, prefix):
        self.env = env
        self.prefix = prefix
        self.results = []

    def log(self, name, text):
        text = clean(text)
        (BUILD / f"{self.prefix}-{name}.log").write_text(text)
        print(text, end="" if text.endswith("\n") else "\n", flush=True)

    def run(self, name, command, timeout=540):
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
        counts = re.findall(r"\d+ passed, \d+ failed|通过\s*\d+\s*[·]?\s*失败\s*\d+|Ran \d+ tests?[^\n]*|\d+ finding\(s\)", text)
        detail = counts[-1] if counts else ("syntax errors 0" if name == "node" and code == 0 else f"exit {code}")
        self.results.append((name, code, detail))
        self.log(name, "COMMAND: " + " ".join(command) + "\n" + text + f"EXIT: {code}\n")
        return code

    def summary(self):
        print("\nSuite | Result | Key counts")
        print("--- | --- | ---")
        for name, code, detail in self.results:
            print(f"{name} | {'PASS' if code == 0 else 'FAIL'} | {detail}")
        failed = sum(code != 0 for _, code, _ in self.results)
        print(f"{len(self.results) - failed} passed, {failed} failed", flush=True)
        return int(bool(failed))


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-update", action="store_true", help="download/install with isolated 23.01 fixture")
    parser.add_argument("--skip-acceptance", action="store_true", help="explicitly omit HTTP acceptance probes")
    args = parser.parse_args()
    BUILD.mkdir(exist_ok=True)
    prefix = f"all-test-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    with tempfile.TemporaryDirectory(prefix="arca-all-test-home-") as home:
        env = dict(os.environ, HOME=home, PYTHONDONTWRITEBYTECODE="1")
        runner = Runner(env, prefix)
        runner.run("node", ["node", "--check", "src/arca/web/app.js"])
        for name, script in [("design", "design_audit"), ("selftest", "selftest"),
                             ("integrity", "integrity_test"), ("checksum", "checksum_test"),
                             ("compress-space", "compress_space_test"), ("compress-scan", "compress_scan_test"),
                             ("inspect-cache", "inspect_cache_test"), ("zip-inspect", "zip_inspect_test"),
                             ("extract-list", "extract_list_test"), ("concurrency", "concurrency_test"),
                             ("keyring", "keyring_test"), ("progress", "progress_test"), ("appupdate", "appupdate_test"),
                             ("zip-password", "zip_password_test"), ("stats", "stats_test")]:
            runner.run(name, PY + [f"scripts/{script}.py"])
        try:
            with headless(runner) as port:
                runner.run("menu", PY + ["scripts/menu_test.py", "--port-file", str(port)])
                if not args.skip_acceptance:
                    runner.run("acceptance", PY + ["scripts/acceptance_probe.py", "--port-file", str(port)])
                if not args.full_update:
                    runner.run("update", update_command(port) + ["--fixture", "short"])
            if not args.full_update:
                with headless(runner, fixture="long") as port:
                    runner.run("update-long", update_command(port) + ["--fixture", "long"])
            if args.full_update:
                with headless(runner, full_update=True) as port:
                    runner.run("update", update_command(port, install=True), timeout=900)
        except Exception as exc:
            runner.results.append(("headless", 1, str(exc)))
            runner.log("headless-error", f"{type(exc).__name__}: {exc}\nEXIT: 1\n")
        return runner.summary()


if __name__ == "__main__":
    raise SystemExit(main())
