"""Engine-update checks: the real dialog, the real download, the real install.

Run a headless instance first, then:
    build/venv/bin/python scripts/update_test.py            # dialog + skip flow
    build/venv/bin/python scripts/update_test.py --install   # ... and download it

The instance must run with a throwaway HOME so the install lands in a temp
Application Support directory instead of the user's:
    HOME=$(mktemp -d) PYTHONPATH=src build/venv/bin/python -c \
      'from arca.app import run; run(["arca","--headless","--port-file","build/port-update.json"])'
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "src"))

import cdp  # noqa: E402

PASS, FAIL = [], []
SHOTS = os.path.join(ROOT, "assets", "shots")

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok    " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def api(port, token, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api{path}", data=data,
                                 headers={"X-Arca-Token": token,
                                          "Content-Type": "application/json"})
    with OPENER.open(req, timeout=180) as r:
        return json.loads(r.read().decode())


HELPERS = r"""
window.__upd = () => {
  const el = document.querySelector('.sheet.upd');
  if (!el) return null;
  const rows = [...el.querySelectorAll('.upd-row')];
  return {
    title: el.querySelector('h2').textContent.trim(),
    vers: [...el.querySelectorAll('.upd-vers b')].map(b => b.textContent.trim()),
    meta: el.querySelector('.upd-meta').textContent.trim(),
    highlights: [...el.querySelectorAll('.upd-hl li')].map(li => ({
      tag: li.querySelector('.upd-tag').textContent.trim(),
      text: li.querySelector('span:not(.upd-tag)').textContent.trim(),
    })),
    rows: rows.length,
    rowVers: rows.map(r => r.querySelector('.upd-row-head b').textContent.trim()),
    englishRows: el.querySelectorAll('.upd-notes.en').length,
    enNoted: rows.filter(r => r.querySelector('.upd-notes.en')
                            && r.querySelector('.upd-hint')).length,
    foot: [...el.querySelectorAll('.sheet-foot button')].map(b => b.textContent.trim()),
    width: Math.round(el.getBoundingClientRect().width),
    inView: el.getBoundingClientRect().bottom <= innerHeight + 1
            && el.getBoundingClientRect().top >= -1,
    scrolls: (() => {
      const b = el.querySelector('.sheet-body');
      return b.scrollHeight > b.clientHeight + 2;
    })(),
    // 逐版本条目里也会出现 .upd-hint（「暂无中文摘要」），页脚那条才是安装说明。
    hint: (el.querySelector('.sheet-body > .upd-hint') || {}).textContent || '',
    bg: getComputedStyle(el).backgroundColor,
    fg: getComputedStyle(el.querySelector('.upd-meta')).color,
    bodyBg: getComputedStyle(document.body).backgroundColor,
  };
};
window.__updProg = () => {
  const el = document.querySelector('.sheet.upd');
  if (!el) return null;
  const box = el.querySelector('#upd-prog');
  return {
    visible: !!box && !box.classList.contains('hidden'),
    msg: (el.querySelector('#upd-msg') || {}).textContent || '',
    pct: (el.querySelector('#upd-pct') || {}).textContent || '',
    width: box ? el.querySelector('.bar i').style.width : '',
    go: (el.querySelector('#upd-go') || {}).textContent.trim() || '',
    indet: box ? el.querySelector('.bar').classList.contains('indet') : null,
  };
};
'ok'
"""


class UI:
    def __init__(self, ws):
        self.ws = ws

    def js(self, expr):
        out = self.ws.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True})
        if out.get("exceptionDetails"):
            raise RuntimeError(out["exceptionDetails"].get("text")
                               or json.dumps(out["exceptionDetails"])[:300])
        return out.get("result", {}).get("value")

    def state(self):
        return self.js("window.__upd()")

    def prog(self):
        return self.js("window.__updProg()")

    def click_sel(self, sel):
        box = self.js(f"""(() => {{
          const e = document.querySelector({json.dumps(sel)});
          if (!e) return null;
          const r = e.getBoundingClientRect();
          return [r.x + r.width / 2, r.y + r.height / 2];
        }})()""")
        if not box:
            return False
        for kind in ("mousePressed", "mouseReleased"):
            self.ws.call("Input.dispatchMouseEvent", {
                "type": kind, "x": box[0], "y": box[1], "button": "left",
                "buttons": 1 if kind == "mousePressed" else 0, "clickCount": 1})
        time.sleep(0.35)
        return True

    def key(self, key, code, vk):
        for kind in ("keyDown", "keyUp"):
            self.ws.call("Input.dispatchKeyEvent", {
                "type": kind, "key": key, "code": code,
                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})
        time.sleep(0.2)

    def wait_dialog(self, seconds=25.0):
        end = time.time() + seconds
        while time.time() < end:
            st = self.js("!!document.querySelector('.sheet.upd')")
            if st:
                time.sleep(0.35)      # let the entry animation finish
                return True
            time.sleep(0.25)
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--fixture", choices=("short", "long"))
    parser.add_argument("--port-file", default=os.path.join(ROOT, "build", "port-update.json"))
    args = parser.parse_args()
    do_install = args.install
    with open(args.port_file) as handle:
        ports = json.load(handle)
    port, token = ports["port"], ports["token"]

    # a clean slate: no skip flag, and no stale check cache
    # Engine check on, app check off: this suite drives the engine dialog and a
    # second launch-time sheet would sit on top of it.
    api(port, token, "/settings", {"patch": {"engineSkipVersion": "",
                                             "checkEngineUpdates": True,
                                             "checkAppUpdates": False,
                                             "engineSnoozeVersion": "",
                                             "engineSnoozeUntil": 0}})
    meta = api(port, token, "/engine/update?refresh=1")["update"]
    # 上游随时会发新版（本次写测试时刚发了 26.05），所以断言一律拿接口回报的
    # latest 做基准，而不是写死某个版本号——否则每次发版都要改测试。
    LATEST = meta.get("latest") or ""
    CURRENT = meta["current"]
    from arca import updates
    from contextlib import nullcontext
    from update_fixtures import upstream
    with upstream(args.fixture) if args.fixture else nullcontext():
        history = updates.fetch_history()
    expected = sorted((v for v in history
                       if updates.parse_version(CURRENT) < updates.parse_version(v)
                       <= updates.parse_version(LATEST)),
                      key=updates.parse_version, reverse=True)
    bundled = api(port, token, "/meta")["engine"]
    check("更新测试从内置引擎开始", bundled["engineSource"] == "bundled")
    print(f"  latest  : {meta['current']} -> {LATEST}  ({meta.get('released')})")

    import socket
    import tempfile
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        debug_port = probe.getsockname()[1]
    profile = tempfile.mkdtemp(prefix="chrome-upd-", dir=os.path.join(ROOT, "build"))
    proc, ws = cdp.launch(debug_port, profile)
    ui = UI(ws)
    try:
        ws.call("Page.enable")
        ws.call("Runtime.enable")
        ws.call("Emulation.setDeviceMetricsOverride", {
            "width": 1120, "height": 764, "deviceScaleFactor": 2, "mobile": False})
        ws.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/index.html?t={token}"})
        time.sleep(2.0)
        ui.js(HELPERS)

        # ---------------------------------------------------- 1. launch-time popup
        appeared = ui.wait_dialog(30)
        check("启动后自动弹出更新提示", appeared)
        if not appeared:
            return finish(proc, ws)

        st = ui.state()
        check("标题为「引擎更新」", st["title"] == "引擎更新", st["title"])
        check("显示 旧版 → 新版", st["vers"][0] == CURRENT and st["vers"][1] == LATEST,
              " → ".join(st["vers"]))
        check("写明发布日期与版本跨度",
              "发布于" in st["meta"] and (len(expected) == 1 or
              f"跨越 {len(expected)} 个版本" in st["meta"]), st["meta"])
        security = updates._security_count(history, CURRENT, LATEST)
        check("统计了安全修复数量", (f"{security} 项安全修复" in st["meta"])
              if security else "安全修复" not in st["meta"], st["meta"])
        # A release without hand-written Chinese notes shows the official list
        # only, so an empty highlight block is legitimate; otherwise the tags
        # must be distinct (one line per kind).
        curated = bool((updates.NOTES_ZH or {}).get(LATEST))
        check("亮点每种类型各一条",
              (not curated and not st["highlights"])
              or (2 <= len(st["highlights"]) <= 5
                  and len({h["tag"] for h in st["highlights"]}) == len(st["highlights"])),
              str([h["tag"] for h in st["highlights"]]))
        check("亮点文字是中文",
              all(any("\u4e00" <= ch <= "\u9fff" for ch in h["text"]) for h in st["highlights"]))
        check("逐版本清单覆盖全部跨越版本", st["rows"] == len(expected),
              f"实际 {st['rows']} 行 · fetch_history 预期 {len(expected)} 行")
        check("清单按新到旧排列", st["rowVers"] == expected, str(st["rowVers"]))
        check("英文条目都标注为「官方原文」", st["englishRows"] == st["enNoted"],
              f"英文行 {st['englishRows']} · 已标注 {st['enNoted']}")
        check("底部按钮齐备",
              st["foot"] == ["跳过此版本", "稍后", "下载并更新"], str(st["foot"]))
        check("面板宽度 560 且完整落在窗口内",
              st["width"] == 560 and st["inView"], f"w={st['width']} inView={st['inView']}")
        if len(expected) >= 13:
            check("长清单在面板内滚动", st["scrolls"])
            before = ui.js("document.querySelector('.sheet.upd .sheet-body').scrollTop")
            box = ui.js("(() => {const r=document.querySelector('.sheet.upd .sheet-body').getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]})()")
            ws.call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": box[0],
                    "y": box[1], "deltaX": 0, "deltaY": 500})
            time.sleep(.4)
            check("真实滚轮移动长清单", ui.js("document.querySelector('.sheet.upd .sheet-body').scrollTop") > before)
        else:
            print(f"  skip  长清单滚动 skip（跨度 {len(expected)} 行）")
        check("说明里写明安装位置不影响应用",
              "Application Support" in st["hint"] and "不会修改应用本身" in st["hint"])

        os.makedirs(SHOTS, exist_ok=True)
        cdp.shoot(ws, os.path.join(SHOTS, "08-update.png"))

        # ---------------------------------------------------------- 2. Esc closes
        ui.key("Escape", "Escape", 27)
        time.sleep(0.7)
        check("Esc 关闭对话框", not ui.js("!!document.querySelector('.sheet.upd')"))
        cfg = api(port, token, "/settings")["settings"]
        check("「稍后」记为 24 小时提醒（不是永久跳过）",
              cfg.get("engineSnoozeVersion") == LATEST
              and cfg.get("engineSnoozeUntil", 0) > time.time() + 20 * 3600,
              f"{cfg.get('engineSnoozeVersion')} · +{int((cfg.get('engineSnoozeUntil',0)-time.time())/3600)}h")
        check("稍后不算跳过", cfg.get("engineSkipVersion") == "",
              cfg.get("engineSkipVersion"))
        check("接口回报已暂缓", api(port, token, "/engine/update")["update"]["snoozed"])

        # --------------------------------------------- 3. re-open from the sidebar
        ok = ui.js("""(() => {
          const card = document.querySelector('.engine-card');
          const r = card.getBoundingClientRect();
          card.dispatchEvent(new MouseEvent('contextmenu', {
            bubbles: true, cancelable: true, button: 2,
            clientX: r.x + r.width / 2, clientY: r.y + r.height / 2}));
          return true;
        })()""")
        time.sleep(0.4)
        picked = ui.js("""(() => {
          const item = [...document.querySelectorAll('.menu .menu-item')]
            .find(b => b.querySelector('.menu-label').textContent.includes('引擎更新'));
          if (!item) return null;
          const label = item.querySelector('.menu-label').textContent;
          item.click();
          return label;
        })()""")
        check("侧栏右键有「检查引擎更新…」", picked is not None, str(picked))
        check("手动检查后重新弹出对话框", ui.wait_dialog(20))
        if ui.js("!!document.querySelector('.sheet.upd')"):
            ui.click_sel(".sheet.upd .sheet-foot [data-close]")   # 稍后
            time.sleep(0.4)

        # ------------------------------------------------------- 4. skip a version
        # A real mouse press focuses the button before opening; do the same so
        # the focus-restore assertions exercise the real opener.
        ui.js("(() => { const b = document.querySelector('#btn-settings'); b.focus(); b.click(); return true; })()")
        time.sleep(0.5)
        SETTINGS = "!!document.querySelector('.sheet[aria-label=\"设置\"]')"
        ui.js("(() => { const b = document.querySelector('#set-updnow'); b.focus(); b.click(); return true; })()")
        check("设置里「检查更新…」也能打开", ui.wait_dialog(20))
        check("检查更新不会关掉设置页", ui.js(SETTINGS))
        check("更新对话框叠在设置页之上", ui.js("""(() => {
          const sheets = [...document.querySelectorAll('.sheet-back')];
          const top = sheets[sheets.length - 1];
          return sheets.length === 2 && top
            && top.querySelector('.sheet').getAttribute('aria-label') === '引擎更新'
            && sheets[0].querySelector('.sheet').getAttribute('aria-label') === '设置';
        })()"""))
        # T11: only the top layer may be exposed as a modal dialog; the sheet
        # underneath is inert + aria-hidden so the screen reader stays on top.
        check("叠层只暴露最上层 aria-modal，下层 inert/aria-hidden", ui.js("""(() => {
          const sheets = [...document.querySelectorAll('.sheet-back')];
          const top = sheets[sheets.length - 1];
          const lower = sheets[0];
          const modals = [...document.querySelectorAll('[aria-modal="true"]')];
          return modals.length === 1 && top.contains(modals[0])
            && lower.getAttribute('aria-hidden') === 'true' && lower.inert === true
            && document.querySelector('.shell').inert === true;
        })()"""))
        for _ in range(10):
            ui.key("Tab", "Tab", 9)
            time.sleep(0.03)
        check("Tab 焦点不逃出最上层 sheet", ui.js("""(() => {
          const sheet = document.querySelector('.sheet.upd');
          return !!(sheet && sheet.contains(document.activeElement));
        })()"""))
        ui.key("Escape", "Escape", 27)
        time.sleep(0.7)
        check("Esc 只关闭最上层的更新对话框", not ui.js("!!document.querySelector('.sheet.upd')"))
        check("Esc 之后设置页仍然打开", ui.js(SETTINGS))
        check("关闭叠层后下层恢复 aria-modal/inert", ui.js("""(() => {
          const sheet = document.querySelector('.sheet[aria-label="设置"]')
            ?.closest('.sheet-back');
          const modals = [...document.querySelectorAll('[aria-modal="true"]')];
          return !!sheet && sheet.inert === false && !sheet.hasAttribute('aria-hidden')
            && modals.length === 1 && sheet.contains(modals[0]);
        })()"""))
        check("Esc 关闭更新对话框后焦点回到设置页内", ui.js("""(() => {
          const sheet = document.querySelector('.sheet[aria-label="设置"]');
          return !!(sheet && sheet.contains(document.activeElement));
        })()"""))
        ui.js("(() => { const b = document.querySelector('#set-updnow'); b.focus(); b.click(); return true; })()")
        check("设置页内可再次打开更新对话框", ui.wait_dialog(20))
        check("再次打开后设置页仍在其下", ui.js(SETTINGS))
        if ui.js("!!document.querySelector('.sheet.upd')"):
            ui.click_sel("#upd-skip")
            time.sleep(0.6)
            cfg = api(port, token, "/settings")["settings"]
            check("跳过会记住版本号", cfg.get("engineSkipVersion") == LATEST,
                  str(cfg.get("engineSkipVersion")))
            check("跳过会关闭对话框", not ui.js("!!document.querySelector('.sheet.upd')"))
            check("跳过不关闭设置页", ui.js(SETTINGS))
            ui.key("Escape", "Escape", 27)
            time.sleep(0.7)
            check("Esc 关闭设置页后 shell 恢复可交互",
                  ui.js("!document.querySelector('.shell').inert"))
            check("关闭设置页后焦点回到设置按钮",
                  ui.js("document.activeElement && document.activeElement.id === 'btn-settings'"))

            ws.call("Page.reload")
            time.sleep(2.4)
            ui.js(HELPERS)
            time.sleep(4.0)          # auto check runs 900ms after init
            check("跳过之后启动不再弹窗", not ui.js("!!document.querySelector('.sheet.upd')"))
            check("侧栏仍标注有新版本",
                  ui.js("document.querySelector('#engine-card').classList.contains('has-update')"))

        if not do_install:
            return finish(proc, ws)

        # ------------------------------------------------ 5. the actual download
        api(port, token, "/settings", {"patch": {"engineSkipVersion": "",
                                                 "engineSnoozeVersion": "",
                                                 "engineSnoozeUntil": 0}})
        ws.call("Page.reload")
        time.sleep(2.4)
        ui.js(HELPERS)
        check("清除跳过后再次提示", ui.wait_dialog(30))
        if not ui.js("!!document.querySelector('.sheet.upd')"):
            return finish(proc, ws)

        ui.click_sel("#upd-go")
        time.sleep(0.8)
        p = ui.prog()
        check("点击后出现进度条", bool(p and p["visible"]), str(p))
        check("按钮进入更新中状态", bool(p and "更新中" in p["go"]), p and p["go"])

        deadline = time.time() + 420
        last = None
        while time.time() < deadline:
            p = ui.prog() or {}
            if p.get("msg") and p["msg"] != last:
                last = p["msg"]
                print("    ·", last[:100])
            if "已更新到" in (p.get("msg") or "") or "失败" in (p.get("msg") or ""):
                break
            time.sleep(1.0)

        p = ui.prog() or {}
        check("下载完成后提示已更新", "已更新到" in (p.get("msg") or ""), p.get("msg"))
        check("进度条到 100%", p.get("width", "").startswith("100"), p.get("width"))
        meta = api(port, token, "/meta")["engine"]
        check("引擎版本已切换", meta["engineVersion"] == LATEST,
              f"{meta['engineVersion']} · {meta['engineSource']}")
        check("标记为应用外安装", meta["engineSource"] == "user", meta["engineSource"])
        cdp.shoot(ws, os.path.join(SHOTS, "09-update-done.png"))

        ui.click_sel("#upd-go")        # now labelled 完成
        time.sleep(1.2)
        check("完成后关闭对话框", not ui.js("!!document.querySelector('.sheet.upd')"))
        check("侧栏显示新版本",
              LATEST in ui.js("document.querySelector('#engine-ver').textContent"),
              ui.js("document.querySelector('#engine-ver').textContent"))

        # --------------------------------------------- 6. and it still compresses
        demo = os.path.join(ROOT, "build", "update-demo")
        os.makedirs(demo, exist_ok=True)
        src = os.path.join(demo, "更新后测试.txt")
        with open(src, "wb") as fh:
            fh.write(os.urandom(120_000))
        out = os.path.join(demo, "after-update.7z")
        if os.path.exists(out):
            os.unlink(out)
        r = api(port, token, "/compress", {
            "inputs": [src], "destDir": demo, "name": "after-update",
            "format": "7z", "level": 3, "method": "auto",
            "splitId": "none", "password": "", "encryptNames": True,
            "excludeMeta": True, "solid": True})
        jid = r.get("job", {}).get("id")
        for _ in range(120):
            jobs = api(port, token, "/jobs")["jobs"]
            job = next((j for j in jobs if j["id"] == jid), None)
            if job and job.get("status") in ("done", "error", "cancelled"):
                break
            time.sleep(0.5)
        check("用新引擎压缩成功", bool(job and job.get("status") == "done"),
              job and job.get("status"))
        check("生成的归档存在", os.path.exists(out), os.path.basename(out))

        # -------------------------------------------------- 7. back to built-in
        r = api(port, token, "/engine/reset", {})
        check("可恢复内置引擎", r["engine"]["engineVersion"] == CURRENT,
              r["engine"]["engineVersion"])
    finally:
        finish(proc, ws)


def finish(proc, ws):
    try:
        ws.close()
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        pass
    print("\n通过 %d   失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("  FAILED: " + ", ".join(FAIL))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
