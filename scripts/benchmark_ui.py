"""Measure the real list rendering path (B4) in headless Chrome.

Why this exists: B4 ("800-row innerHTML render") must be driven by data before
any pagination / incremental rendering / virtual-scroll rewrite is justified.
This script loads the real UI from the current source tree (via all_test's
bundle sync), opens a 1000-entry ZIP (the backend cap returns 800 rows), and
measures:

  * time from loadArchive() start until the initial 200 rows are in the DOM;
  * time from clicking "显示全部" until all 800 rows are in the DOM + painted;
  * row / node counts.

Run:
    build/venv/bin/python -S -E -B scripts/benchmark_ui.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import all_test          # noqa: E402
import cdp               # noqa: E402

ZZ = ROOT / "vendor" / "7zz"


def make_archive(path: Path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(1000):
            zf.writestr(f"entry-{i:04d}.txt", (f"payload {i}\n" * 8).encode())


BENCH_JS = r"""
window.__dropPaths = (paths) => {
  const dt = new DataTransfer();
  dt.setData('text/uri-list', paths.map(p => 'file://' + encodeURI(p)).join('\r\n'));
  window.dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true,
                                             dataTransfer: dt}));
  return true;
};
window.__bench = {
  load: (path) => new Promise((resolve, reject) => {
    document.querySelector('[data-nav="extract"]').click();
    const t0 = performance.now();
    window.__dropPaths([path]);
    setTimeout(() => reject(new Error('render timeout')), 60000);
    const check = () => {
      const rows = document.querySelectorAll('#x-table tr[data-entry]').length;
      if (rows > 0) {
        resolve({ms: performance.now() - t0, rows});
      } else { setTimeout(check, 10); }
    };
    setTimeout(check, 10);
  }),
  expand: () => new Promise((resolve) => {
    const btn = document.querySelector('#x-show-all');
    if (!btn || btn.classList.contains('hidden')) {
      resolve({error: 'no show-all button'});
      return;
    }
    const t0 = performance.now();
    btn.click();
    const check = () => {
      const rows = document.querySelectorAll('#x-table tr[data-entry]').length;
      if (rows >= 800) {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve({
          layout_ms: performance.now() - t0,
          painted_ms: performance.now() - t0,
          rows,
          nodes: document.querySelectorAll('#x-table *').length,
        })));
      } else { setTimeout(check, 5); }
    };
    check();
  }),
};
'ok'
"""


class _NullRunner:
    """all_test.headless needs .env and .log(); avoid writing bench-ui-*.log
    files into build/ on every benchmark run."""
    def __init__(self, env):
        self.env = env

    def log(self, name, text):
        pass


def main():
    build = ROOT / "build"
    profile_baseline = {p.name for p in build.glob("chrome-bench-*")}
    work = Path(tempfile.mkdtemp(prefix="bench-ui-", dir=build))
    archive = work / "entries-1000.zip"
    make_archive(archive)
    out_path = build / f"benchmark-ui-{time.strftime('%Y%m%d-%H%M%S')}.json"
    result = {
        "benchmark": "arca-ui-render",
        "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "command": " ".join(sys.argv),
        "sample": {"archive_entries": 1000, "backend_cap": 800,
                   "initial_rows": 200},
        "cases": {},
    }
    profile = build / f"chrome-bench-{int(time.time())}"
    with tempfile.TemporaryDirectory(prefix="arca-bench-ui-home-") as home:
        env = dict(os.environ, HOME=home, PYTHONDONTWRITEBYTECODE="1")
        runner = _NullRunner(env)
        try:
            with all_test.headless(runner) as port:
                ports = json.loads(Path(port).read_text())
                proc = ws = None
                try:
                    proc, ws = cdp.launch(9453, str(profile))
                    ws.call("Page.enable")
                    ws.call("Runtime.enable")
                    ws.call("Emulation.setDeviceMetricsOverride", {
                        "width": 1120, "height": 764, "deviceScaleFactor": 2,
                        "mobile": False})
                    ws.call("Page.navigate", {
                        "url": f"http://127.0.0.1:{ports['port']}/index.html"
                               f"?t={ports['token']}"})
                    time.sleep(2.6)
                    ws.call("Runtime.evaluate", {
                        "expression": BENCH_JS, "returnByValue": True})
                    load = ws.call("Runtime.evaluate", {
                        "expression": f"window.__bench.load({json.dumps(str(archive))})",
                        "awaitPromise": True, "returnByValue": True})["result"]["value"]
                    expand = ws.call("Runtime.evaluate", {
                        "expression": "window.__bench.expand()",
                        "awaitPromise": True, "returnByValue": True})["result"]["value"]
                    result["cases"]["initial_200"] = load
                    result["cases"]["expand_to_800"] = expand
                finally:
                    if ws is not None:
                        ws.close()
                    if proc is not None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait(timeout=10)
                    shutil.rmtree(profile, ignore_errors=True)
                    if profile.exists():
                        raise RuntimeError(f"Chrome profile not removed: {profile}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
            leftover = {p.name for p in build.glob("chrome-bench-*")} - profile_baseline
            if leftover:
                raise RuntimeError(f"Chrome profile leaked: {sorted(leftover)}")
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print(f"JSON written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
