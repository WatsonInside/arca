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
    do_install = "--install" in sys.argv
    ports = json.load(open(os.path.join(ROOT, "build", "port-update.json")))
    port, token = ports["port"], ports["token"]

    # a clean slate: no skip flag, and no stale check cache
    api(port, token, "/settings", {"patch": {"engineSkipVersion": "",
                                             "checkEngineUpdates": True,
                                             "engineSnoozeVersion": "",
                                             "engineSnoozeUntil": 0}})
    meta = api(port, token, "/engine/update?refresh=1")["update"]
    # 上游随时会发新版（本次写测试时刚发了 26.05），所以断言一律拿接口回报的
    # latest 做基准，而不是写死某个版本号——否则每次发版都要改测试。
    LATEST = meta.get("latest") or ""
    print(f"  latest  : {meta['current']} -> {LATEST}  ({meta.get('released')})")

    profile = os.path.join(ROOT, "build", f"chrome-upd-{int(time.time())}")
    proc, ws = cdp.launch(9447, profile)
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
        check("显示 旧版 → 新版", st["vers"][0] == "23.01" and st["vers"][1] == LATEST,
              " → ".join(st["vers"]))
        check("写明发布日期与版本跨度",
              "发布于" in st["meta"] and "个版本" in st["meta"], st["meta"])
        check("统计了安全修复数量", "安全修复" in st["meta"], st["meta"])
        # 每条亮点一个类型：小版本可能只有 2 类，所以断言「不重复」而不是「恰好 5 条」。
        check("亮点每种类型各一条", 2 <= len(st["highlights"]) <= 5
              and len({h["tag"] for h in st["highlights"]}) == len(st["highlights"]),
              str([h["tag"] for h in st["highlights"]]))
        check("亮点文字是中文",
              all(any("\u4e00" <= ch <= "\u9fff" for ch in h["text"]) for h in st["highlights"]))
        check("逐版本清单覆盖全部跨越版本", st["rows"] >= 13, f"{st['rows']} 行")
        def _v(x):
            return tuple(int(p) for p in x.split("."))
        check("清单按新到旧排列",
              st["rowVers"][0] == LATEST
              and st["rowVers"] == sorted(st["rowVers"], key=_v, reverse=True),
              f"{st['rowVers'][0]} … {st['rowVers'][-1]}")
        check("英文条目都标注为「官方原文」", st["englishRows"] == st["enNoted"],
              f"英文行 {st['englishRows']} · 已标注 {st['enNoted']}")
        check("底部按钮齐备",
              st["foot"] == ["跳过此版本", "稍后", "下载并更新"], str(st["foot"]))
        check("面板宽度 560 且完整落在窗口内",
              st["width"] == 560 and st["inView"], f"w={st['width']} inView={st['inView']}")
        check("长清单在面板内滚动", st["scrolls"])
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
        ui.js("document.querySelector('#btn-settings').click()")
        time.sleep(0.5)
        ui.click_sel("#set-updnow")
        check("设置里「检查更新…」也能打开", ui.wait_dialog(20))
        if ui.js("!!document.querySelector('.sheet.upd')"):
            ui.click_sel("#upd-skip")
            time.sleep(0.6)
            cfg = api(port, token, "/settings")["settings"]
            check("跳过会记住版本号", cfg.get("engineSkipVersion") == LATEST,
                  str(cfg.get("engineSkipVersion")))
            check("跳过会关闭对话框", not ui.js("!!document.querySelector('.sheet.upd')"))

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
        check("引擎版本已切换", meta["engineVersion"] == "26.04",
              f"{meta['engineVersion']} · {meta['engineSource']}")
        check("标记为应用外安装", meta["engineSource"] == "user", meta["engineSource"])
        cdp.shoot(ws, os.path.join(SHOTS, "09-update-done.png"))

        ui.click_sel("#upd-go")        # now labelled 完成
        time.sleep(1.2)
        check("完成后关闭对话框", not ui.js("!!document.querySelector('.sheet.upd')"))
        check("侧栏显示新版本",
              "26.04" in ui.js("document.querySelector('#engine-ver').textContent"),
              ui.js("document.querySelector('#engine-ver').textContent"))

        # --------------------------------------------- 6. and it still compresses
        demo = os.path.join(ROOT, "build", "update-demo")
        os.makedirs(demo, exist_ok=True)
        src = os.path.join(demo, "更新后测试.txt")
        with open(src, "wb") as fh:
            fh.write(os.urandom(120_000))
        out = os.path.join(demo, "after-update.7z")
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
        check("可恢复内置引擎", r["engine"]["engineVersion"] == "23.01",
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
    except Exception:
        pass
    print("\n通过 %d   失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("  FAILED: " + ", ".join(FAIL))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
