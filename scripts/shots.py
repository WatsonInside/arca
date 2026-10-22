"""Drive the real UI through Chrome DevTools and capture screenshots."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "src"))

import cdp  # noqa: E402

SHOTS = os.path.join(ROOT, "assets", "shots")
DEMO = os.path.join(ROOT, "build", "demo")
Z = os.path.join(ROOT, "vendor", "7zz")


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(port, token, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api{path}", data=data,
                                 headers={"X-Arca-Token": token,
                                          "Content-Type": "application/json"})
    with OPENER.open(req, timeout=60) as r:
        return json.loads(r.read().decode())


def seed_demo():
    src = os.path.join(DEMO, "素材")
    os.makedirs(os.path.join(src, "设计稿"), exist_ok=True)
    os.makedirs(os.path.join(src, "文档"), exist_ok=True)
    files = {
        "设计稿/首页-Mac.png": 1_240_000,
        "设计稿/图标集.sketch": 860_000,
        "文档/产品说明.md": 18_000,
        "文档/接口约定.pdf": 420_000,
        "交付清单.txt": 3_200,
    }
    for name, size in files.items():
        p = os.path.join(src, name)
        if not os.path.exists(p):
            with open(p, "wb") as fh:
                fh.write(os.urandom(size))
    # a real archive to open in the extract view
    arch = os.path.join(DEMO, "季度归档.7z")
    if not os.path.exists(arch):
        subprocess.run([Z, "a", "-bd", "-t7z", "-pArca#2026", "-mhe=on", arch, "素材"],
                       cwd=DEMO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return src, arch


CLEAR_JS = "document.querySelectorAll('.toast').forEach(e => e.remove()); 'ok'"


def shoot(ws, name, settle=0.45):
    """Drop transient toasts so the capture shows the steady state."""
    ws.call("Runtime.evaluate", {"expression": CLEAR_JS, "returnByValue": True})
    time.sleep(settle)
    cdp.shoot(ws, os.path.join(SHOTS, name))
    print("shot", name.rsplit(".", 1)[0])


DROP_JS = """
window.__drop = (paths) => {
  const dt = new DataTransfer();
  dt.setData('text/uri-list', paths.map(p => 'file://' + encodeURI(p)).join('\\r\\n'));
  const ev = new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: dt});
  window.dispatchEvent(ev);
};
'ok'
"""


def port_file():
    for cand in ("build/port-theme.json", "dist/port4.json"):
        path = os.path.join(ROOT, cand)
        if os.path.exists(path):
            return path
    raise SystemExit("no running instance: start one with --headless --port-file")


def pick_theme(ws, mode):
    """Drive the appearance control the way a user would, then close the sheet."""
    ws.call("Runtime.evaluate", {"expression":
            "document.querySelector('#btn-settings').click(); 'ok'", "returnByValue": True})
    time.sleep(0.5)
    ws.call("Runtime.evaluate", {"expression":
            "[...document.querySelectorAll('#set-theme button')]"
            f".find(b => b.dataset.id === '{mode}').click(); 'ok'", "returnByValue": True})
    time.sleep(0.6)
    ws.call("Runtime.evaluate", {"expression":
            "document.querySelector('.sheet-foot [data-close]').click(); 'ok'", "returnByValue": True})
    time.sleep(0.3)


def right_click(ws, x, y):
    for kind, buttons in (("mousePressed", 2), ("mouseReleased", 0)):
        ws.call("Input.dispatchMouseEvent", {
            "type": kind, "x": x, "y": y, "button": "right",
            "buttons": buttons, "clickCount": 1})
    time.sleep(0.35)


def point_at(ws, selector):
    out = ws.call("Runtime.evaluate", {"expression": f"""
      (() => {{
        const r = document.querySelector({json.dumps(selector)}).getBoundingClientRect();
        return [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)];
      }})()""", "returnByValue": True})
    return out["result"]["value"]


def close_menu(ws):
    ws.call("Runtime.evaluate", {"expression":
            "window.ArcaMenu && window.ArcaMenu.close(); 'ok'", "returnByValue": True})
    time.sleep(0.2)


def main():
    ports = json.load(open(port_file()))
    port, token = ports["port"], ports["token"]
    # No launch-time engine-update dialog in the middle of the screenshots.
    orig_check = api(port, token, "/meta")["settings"].get("checkEngineUpdates", True)
    api(port, token, "/settings", {"patch": {"checkEngineUpdates": False}})
    restore = api(port, token, "/meta")["settings"]["appearance"]
    os.makedirs(SHOTS, exist_ok=True)
    src, arch = seed_demo()

    profile = os.path.join(ROOT, "build", f"chrome-shots-{int(time.time())}")
    proc, ws = cdp.launch(9444, profile)
    try:
        ws.call("Page.enable")
        ws.call("Runtime.enable")
        # --window-size is ignored by headless=new, so pin the viewport to the
        # app's real content size; otherwise every shot is a 540×300 crop.
        ws.call("Emulation.setDeviceMetricsOverride", {
            "width": 1120, "height": 764, "deviceScaleFactor": 2, "mobile": False})
        url = f"http://127.0.0.1:{port}/index.html?t={token}"
        ws.call("Page.navigate", {"url": url})
        time.sleep(2.5)
        ws.call("Runtime.evaluate", {"expression": DROP_JS, "returnByValue": True})
        # Screenshots assume a known starting appearance; the last run may have
        # left the app dark. History is cleared too, or every repeat adds rows.
        api(port, token, "/jobs/clear", {})
        ws.call("Runtime.evaluate", {"expression": "pollJobs(); 'ok'", "returnByValue": True})
        pick_theme(ws, "light")
        time.sleep(0.4)

        # ---- compress, filled in, light theme
        ws.call("Runtime.evaluate", {"expression": f"window.__drop([{json.dumps(src)}])",
                                     "returnByValue": True})
        time.sleep(1.2)
        ws.call("Runtime.evaluate", {"expression": """
          const n = document.querySelector('#c-name');
          n.value = '季度素材备份'; n.dispatchEvent(new Event('input', {bubbles: true}));
          const p = document.querySelector('#c-pw');
          p.value = 'Arca#2026'; p.dispatchEvent(new Event('input', {bubbles: true}));
          const lv = document.querySelector('#c-level');
          lv.value = '4'; lv.dispatchEvent(new Event('input', {bubbles: true}));
          'ok'
        """, "returnByValue": True})
        time.sleep(1.4)
        shoot(ws, "01-compress.png")

        # ---- context menu on a queued file row (light)
        pt = point_at(ws, "#c-list .file-row:nth-child(1) b")
        right_click(ws, pt[0], pt[1])
        shoot(ws, "06-menu-light.png")
        close_menu(ws)

        # ---- ZST selected: capability bits hide the rows zstd cannot honour
        ws.call("Runtime.evaluate", {"expression": """
          const b = document.querySelector('#c-format button[data-id="tar.zst"]');
          if (b) b.click(); 'ok'
        """, "returnByValue": True})
        time.sleep(0.6)
        ws.call("Runtime.evaluate", {"expression": """
          const a = document.querySelector('#c-adv-toggle');
          if (a && !document.querySelector('#c-adv').classList.contains('is-open')) a.click();
          'ok'
        """, "returnByValue": True})
        time.sleep(0.5)
        ws.call("Runtime.evaluate", {"expression": """
          const t = document.querySelector('#c-par2');
          if (t && t.getAttribute('aria-checked') !== 'true') t.click(); 'ok'
        """, "returnByValue": True})
        time.sleep(0.4)
        ws.call("Runtime.evaluate", {"expression": """
          const s = document.querySelector('#c-par2-single');
          if (s && s.getAttribute('aria-checked') !== 'true') s.click(); 'ok'
        """, "returnByValue": True})
        time.sleep(1.0)
        shoot(ws, "10-zst.png")
        # back to 7z with recovery turned off, so the queued run stays a 7z job
        ws.call("Runtime.evaluate", {"expression": """
          const t = document.querySelector('#c-par2');
          if (t && t.getAttribute('aria-checked') === 'true') t.click();
          const a = document.querySelector('#c-adv-toggle');
          if (a && document.querySelector('#c-adv').classList.contains('is-open')) a.click();
          const b = document.querySelector('#c-format button[data-id="7z"]');
          if (b) b.click(); 'ok'
        """, "returnByValue": True})
        time.sleep(0.6)

        # ---- extract view with a real archive
        ws.call("Runtime.evaluate", {"expression":
                "document.querySelector('[data-nav=\"extract\"]').click(); 'ok'",
                "returnByValue": True})
        time.sleep(0.5)
        ws.call("Runtime.evaluate", {"expression": f"window.__drop([{json.dumps(arch)}])",
                                     "returnByValue": True})
        time.sleep(2.0)
        shoot(ws, "02-extract.png")

        # ---- recovery records: the chip plus its two menu entries
        zres = api(port, token, "/compress", {
            "inputs": [src], "name": "季度素材备份", "destDir": DEMO,
            "format": "tar.zst", "level": 5, "par2Percent": 5})
        zpath = zres.get("target") or ""
        for _ in range(120):
            jobs = api(port, token, "/jobs")["jobs"]
            if jobs and all(j["status"] not in ("queued", "running") for j in jobs):
                break
            time.sleep(0.4)
        ws.call("Runtime.evaluate", {"expression":
                f"window.__drop([{json.dumps(zpath)}]); 'ok'", "returnByValue": True})
        time.sleep(2.2)
        pt = point_at(ws, "#x-table tr[data-entry] .name")
        right_click(ws, pt[0], pt[1])
        shoot(ws, "11-par2-menu.png")
        close_menu(ws)

        # ---- run the queued compress job, then show the queue
        ws.call("Runtime.evaluate", {"expression":
                "document.querySelector('[data-nav=\"compress\"]').click(); 'ok'",
                "returnByValue": True})
        time.sleep(0.4)
        ws.call("Runtime.evaluate", {"expression":
                "document.querySelector('#c-start').click(); 'ok'", "returnByValue": True})
        for _ in range(60):
            jobs = api(port, token, "/jobs")["jobs"]
            if jobs and all(j["status"] not in ("queued", "running") for j in jobs):
                break
            time.sleep(0.5)
        time.sleep(1.0)
        shoot(ws, "03-queue.png")

        # ---- dark theme, picked through the appearance control
        pick_theme(ws, "dark")
        time.sleep(0.2)
        ws.call("Runtime.evaluate", {"expression":
                "document.querySelector('[data-nav=\"compress\"]').click(); 'ok'",
                "returnByValue": True})
        time.sleep(0.8)
        shoot(ws, "04-dark.png")

        # ---- context menu in dark theme, opened over the sidebar
        pt = point_at(ws, ".engine-card")
        right_click(ws, pt[0], pt[1])
        shoot(ws, "07-menu-dark.png")
        close_menu(ws)

        # ---- settings sheet with 深色 visibly selected
        ws.call("Runtime.evaluate", {"expression":
                "document.querySelector('#btn-settings').click(); 'ok'",
                "returnByValue": True})
        time.sleep(0.9)
        # The launch check was switched off for this run; turn it back on
        # through the control itself so the shot shows the shipped default.
        if orig_check:
            ws.call("Runtime.evaluate", {"expression": """
              (() => {
                const t = document.querySelector('#set-updcheck');
                if (t && t.getAttribute('aria-checked') !== 'true') t.click();
                return t ? t.getAttribute('aria-checked') : null;
              })()""", "returnByValue": True})
            time.sleep(0.4)
        shoot(ws, "05-settings.png")

        # ---- same sheet scrolled to the component list (zstd / par2 updates)
        ws.call("Runtime.evaluate", {"expression": """
          const s = document.querySelector('#set-components');
          if (s) s.scrollIntoView({block: 'center'});
          'ok'
        """, "returnByValue": True})
        time.sleep(0.7)
        shoot(ws, "12-components.png")

        # ---- Finder 集成：右键服务区，然后默认打开方式区
        ws.call("Runtime.evaluate", {"expression": """
          const body = document.querySelector('.sheet-body');
          const sec = document.querySelector('#set-finder');
          if (body && sec) body.scrollTop = sec.offsetTop - 8;
          'ok'
        """, "returnByValue": True})
        time.sleep(1.0)
        shoot(ws, "13-finder.png")
        ws.call("Runtime.evaluate", {"expression": """
          const body = document.querySelector('.sheet-body');
          const sec = document.querySelector('#set-default');
          if (body && sec) body.scrollTop = sec.offsetTop - 8;
          'ok'
        """, "returnByValue": True})
        time.sleep(0.7)
        shoot(ws, "14-default.png")

        # leave the app the way we found it
        ws.call("Runtime.evaluate", {"expression":
                "document.querySelector('.sheet-foot [data-close]').click(); 'ok'",
                "returnByValue": True})
        time.sleep(0.3)
        pick_theme(ws, restore or "system")
        api(port, token, "/settings", {"patch": {"checkEngineUpdates": orig_check}})
    finally:
        ws.close()
        proc.terminate()


if __name__ == "__main__":
    main()
