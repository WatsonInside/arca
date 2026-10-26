"""Right-click menu checks, driven through the real UI in headless Chrome.

Run with the project venv against a headless instance:
    build/venv/bin/python scripts/menu_test.py
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "src"))

import cdp  # noqa: E402
import jobwait  # noqa: E402

PASS, FAIL = [], []
Z = os.path.join(ROOT, "vendor", "7zz")
DEMO = os.path.join(ROOT, "build", "menu-demo")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok    " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))


# A session-wide HTTP proxy would swallow loopback calls; talk to the app direct.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(port, token, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api{path}", data=data,
                                 headers={"X-Arca-Token": token,
                                          "Content-Type": "application/json"})
    with OPENER.open(req, timeout=120) as r:
        return json.loads(r.read().decode())


TERMINAL = ("done", "error", "cancelled")


def job_by_id(port, token, jid):
    for j in api(port, token, "/jobs")["jobs"]:
        if j["id"] == jid:
            return j
    return None


def await_job(port, token, jid, timeout, label):
    """等待单个任务到真实终态。时序（详见 jobwait.await_job）：完整
    timeout 内不干预——慢任务不许半程取消；到点才取消一次，并给独立的
    回收宽限期。返回 (job, reclaimed)，reclaimed=False 表示取消后仍未
    终态，调用方必须输出诊断。"""
    return jobwait.await_job(
        lambda: job_by_id(port, token, jid),
        lambda: api(port, token, "/job/cancel", {"id": jid}),
        timeout)


def wait_new_job(port, token, before, kind="extract", timeout=180):
    """Wait for a job created after `before` (ids) to reach a terminal state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for job in api(port, token, "/jobs")["jobs"]:
            if job["id"] not in before and job["kind"] == kind \
                    and job["status"] in TERMINAL:
                return job
        time.sleep(0.15)
    raise RuntimeError(f"new {kind} job did not finish within {timeout}s")


def reclaim_round(state, timeout=60.0):
    """轮次收尾：取消并等待本轮仍活动的任务，之后才允许删除 DEMO。"""
    port, token = state.get("port"), state.get("token")
    if not port or not token:
        return None  # 尚未连上实例：本轮不可能提交过任务
    left, jobs = jobwait.reclaim_active(
        lambda: api(port, token, "/jobs")["jobs"],
        lambda jid: api(port, token, "/job/cancel", {"id": jid}),
        timeout)
    if left:
        stuck = [(j["id"], j["status"]) for j in jobs if j["status"] not in TERMINAL]
        print(f"  WARN  清理时仍有 {left} 个任务未回收：{stuck}")
    return left


HELPERS = r"""
window.__menuState = () => {
  const el = document.querySelector('.menu');
  if (!el) return null;
  const r = el.getBoundingClientRect();
  const items = [...el.querySelectorAll('.menu-item')];
  const txt = (b) => b.querySelector('.menu-label').textContent;
  return {
    rect: {x: r.x, y: r.y, w: r.width, h: r.height, right: r.right, bottom: r.bottom},
    labels: items.map(txt),
    disabled: items.filter(b => b.classList.contains('is-disabled')).map(txt),
    checked: items.filter(b => b.getAttribute('aria-checked') === 'true').map(txt),
    kbd: [...el.querySelectorAll('.menu-kbd')].map(b => b.textContent),
    icons: items.filter(b => b.querySelector('.menu-ic svg')).length,
    seps: el.querySelectorAll('.menu-sep').length,
    active: (el.querySelector('.menu-item.is-active .menu-label') || {}).textContent || null,
    count: document.querySelectorAll('.menu').length,
    vw: innerWidth, vh: innerHeight,
    radius: parseFloat(getComputedStyle(el).borderTopLeftRadius),
    role: el.getAttribute('role'),
    itemRole: items[0] ? items[0].getAttribute('role') : null,
    bg: getComputedStyle(el).backgroundColor,
    fg: items[0] ? getComputedStyle(items[0]).color : '',
  };
};
window.__rectOf = (sel) => {
  const e = document.querySelector(sel);
  if (!e) return null;
  const r = e.getBoundingClientRect();
  return {x: r.x, y: r.y, w: r.width, h: r.height, cx: r.x + r.width / 2, cy: r.y + r.height / 2};
};
window.__dropPaths = (paths) => {
  const dt = new DataTransfer();
  dt.setData('text/uri-list', paths.map(p => 'file://' + encodeURI(p)).join('\r\n'));
  window.dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: dt}));
  return true;
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

    def menu(self):
        return self.js("window.__menuState()")

    def rect(self, sel):
        return self.js(f"window.__rectOf({json.dumps(sel)})")

    def right_click(self, x, y):
        target = self.js(f"""(() => {{
          const el = document.elementFromPoint({x}, {y});
          return el ? (el.id || el.className || el.tagName) : null;
        }})()""")
        self.ws.call("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y, "button": "right",
            "buttons": 2, "clickCount": 1})
        self.ws.call("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y, "button": "right",
            "buttons": 0, "clickCount": 1})
        time.sleep(0.22)
        return target

    def click(self, x, y):
        for kind in ("mousePressed", "mouseReleased"):
            self.ws.call("Input.dispatchMouseEvent", {
                "type": kind, "x": x, "y": y, "button": "left",
                "buttons": 1 if kind == "mousePressed" else 0, "clickCount": 1})
        time.sleep(0.3)

    def key(self, key, code, vk):
        for kind in ("keyDown", "keyUp"):
            self.ws.call("Input.dispatchKeyEvent", {
                "type": kind, "key": key, "code": code,
                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})
        time.sleep(0.16)

    def pick(self, label):
        """Activate an item by its visible label, as a user would."""
        ok = self.js(f"""(() => {{
          const el = [...document.querySelectorAll('.menu .menu-item')]
            .find(b => b.querySelector('.menu-label').textContent === {json.dumps(label)});
          if (!el) return false;
          el.click();
          return true;
        }})()""")
        time.sleep(0.45)
        return ok


def seed():
    os.makedirs(DEMO, exist_ok=True)
    names = ["设计稿.psd", "预算表.xlsx", "说明.md"]
    for i, n in enumerate(names):
        p = os.path.join(DEMO, n)
        if not os.path.exists(p):
            with open(p, "wb") as fh:
                fh.write(os.urandom(20_000 + i * 4000))
    arch = os.path.join(DEMO, "样例归档.7z")
    if not os.path.exists(arch):
        subprocess.run([Z, "a", "-bd", "-t7z", arch, names[0], names[1], names[2]],
                       cwd=DEMO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Nested fixture for the parent/child selection regression.  The source tree
    # lives on .tmp (APFS) so AppleDouble sidecars cannot leak into the archive;
    # only the archive itself is written under DEMO.
    nested = os.path.join(DEMO, "嵌套归档.7z")
    if not os.path.exists(nested):
        source = tempfile.mkdtemp(prefix="menu-nested-", dir=os.path.join(ROOT, ".tmp"))
        try:
            os.makedirs(os.path.join(source, "子目录", "嵌套"))
            for rel, data in (("a.txt", b"alpha"), ("b.txt", b"beta"),
                              ("子目录/c.txt", b"child"),
                              ("子目录/嵌套/d.txt", b"deep")):
                with open(os.path.join(source, rel), "wb") as fh:
                    fh.write(data)
            subprocess.run([Z, "a", "-bd", "-t7z", nested, "a.txt", "b.txt", "子目录"],
                           cwd=source, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        finally:
            shutil.rmtree(source, ignore_errors=True)
    return [os.path.join(DEMO, n) for n in names], arch, nested


def parse_color(text: str):
    """Return (rgb 0..1, alpha) for rgb()/rgba()/color(srgb ...) strings."""
    t = text.strip()
    if t.startswith("color("):
        body = t[t.index("(") + 1:t.rindex(")")]
        parts = body.replace("/", " ").split()
        nums = [float(x) for x in parts[1:4]]
        alpha = float(parts[4]) if len(parts) > 4 else 1.0
        return nums, alpha
    if t.startswith("rgb"):
        body = t[t.index("(") + 1:t.rindex(")")].replace("/", ",")
        parts = [p.strip() for p in body.split(",") if p.strip()]
        nums = [float(p) / 255.0 for p in parts[:3]]
        alpha = float(parts[3]) if len(parts) > 3 else 1.0
        return nums, alpha
    raise ValueError(f"unsupported color: {text!r}")


def luminance(text: str) -> float:
    (r, g, b), _a = parse_color(text)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _teardown(state, profile):
    """轮次收尾：确认本轮任务全部回收后才删除现场。

    返回 True 表示现场已删除、收尾通过；False 表示现场必须保留（任务仍
    在写 DEMO，或回收检查本身失败），调用方应让测试以非零退出。
    """
    try:
        left = reclaim_round(state)
    except Exception as exc:
        print(f"  FAIL  收尾任务回收检查失败：{type(exc).__name__}: {exc}")
        print(f"  FAIL  保留现场：DEMO={DEMO}  Chrome profile={profile}")
        return False
    if left is None:
        print("  ok    收尾：尚未连接实例，本轮不可能提交任务")
    elif left == 0:
        print("  ok    收尾：本轮活动任务已全部回收")
    else:
        print(f"  FAIL  收尾：仍有 {left} 个本轮任务未回收")
        print(f"  FAIL  保留现场：DEMO={DEMO}  Chrome profile={profile}")
        return False
    shutil.rmtree(DEMO, ignore_errors=True)
    shutil.rmtree(profile, ignore_errors=True)
    return True


def main():
    global DEMO
    # 每轮独立临时目录：共享的 build/menu-demo 会把上一轮产物带进下一轮
    # （名字冲突触发唯一化、6GB 无限累积）。收尾只在确认回收后进行；
    # 未回收或回收检查异常时保留现场，并让测试以非零退出。
    build_dir = os.path.join(ROOT, "build")
    os.makedirs(build_dir, exist_ok=True)
    DEMO = tempfile.mkdtemp(prefix="menu-demo-", dir=build_dir)
    profile = os.path.join(build_dir, f"chrome-menu-{int(time.time())}")
    state = {}
    try:
        code = _run(profile, state)
    except BaseException as exc:
        # 原异常必须继续传播：收尾失败只追加诊断（print + add_note），
        # 既不能在 finally 里 return 非零把它吞掉，也不能覆盖它。
        try:
            clean = _teardown(state, profile)
            detail = "现场已清理" if clean else "现场已保留"
        except BaseException as teardown_exc:
            detail = (f"收尾自身异常：{type(teardown_exc).__name__}: "
                      f"{teardown_exc}；现场已保留")
            print(f"  FAIL  {detail}")
        add_note = getattr(exc, "add_note", None)
        if add_note:
            add_note(f"menu_test 收尾：{detail}")
        raise
    if not _teardown(state, profile):
        print("  FAIL  收尾未通过：测试按失败退出（现场保留供诊断）")
        return 1
    return code


def _run(profile, state):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port-file", default=os.path.join(ROOT, "build", "port-menu.json"))
    args = parser.parse_args()
    with open(args.port_file) as handle:
        ports = json.load(handle)
    port, token = ports["port"], ports["token"]
    state["port"], state["token"] = port, token
    orig_theme = api(port, token, "/meta")["settings"].get("appearance") or "system"
    # The engine-update dialog opens by itself on launch when a newer 7-Zip
    # exists; it would sit on top of everything this script clicks.
    orig_check = api(port, token, "/meta")["settings"].get("checkEngineUpdates", True)
    orig_appcheck = api(port, token, "/meta")["settings"].get("checkAppUpdates", True)
    # The app-update dialog is the other launch-time sheet: once a newer
    # release exists on GitHub it would cover the same clicks.
    api(port, token, "/settings", {"patch": {"checkEngineUpdates": False,
                                            "checkAppUpdates": False}})
    files, arch, nested = seed()
    api(port, token, "/jobs/clear", {})

    proc, ws = cdp.launch(9446, profile)
    ui = UI(ws)
    try:
        ws.call("Page.enable")
        ws.call("Runtime.enable")
        ws.call("Emulation.setDeviceMetricsOverride", {
            "width": 1120, "height": 764, "deviceScaleFactor": 2, "mobile": False})
        ws.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/index.html?t={token}"})
        # 密码拷贝段落要读回剪贴板；headless Chrome 需显式授权（应用内
        # WKWebView 走 native 桥，不受此影响）。
        ws.call("Browser.grantPermissions", {
            "permissions": ["clipboardReadWrite", "clipboardSanitizedWrite"],
            "origin": f"http://127.0.0.1:{port}"})
        ws.call("Page.bringToFront", {})
        time.sleep(2.6)
        ui.js(HELPERS)
        # A known starting point: light theme, compress view, empty list.
        ui.js("document.documentElement.dataset.theme = 'light'")
        ui.js("document.querySelector('[data-nav=\"compress\"]').click()")

        # ---------------------------------------------------- compress: blank area
        area = ui.rect("#drop-c")
        bx, by = area["cx"] + 60, area["y"] + 26
        hit = ui.right_click(bx, by)
        state = ui.menu()
        check("空白处右键弹出菜单", bool(state), f"命中 {hit}")
        if state:
            labels = state["labels"]
            check("空白菜单包含添加项", all(k in labels for k in
                                        ["添加文件…", "添加文件夹…", "从剪贴板添加"]), str(labels))
            check("空白菜单包含设置", "设置…" in labels, str(labels))
            check("菜单在光标处", abs(state["rect"]["x"] - bx) <= 2
                  and abs(state["rect"]["y"] - by) <= 2,
                  f"{state['rect']['x']},{state['rect']['y']} vs {bx},{by}")
            check("菜单圆角/层级样式生效", state["radius"] >= 8, f"r={state['radius']}")
            check("语义角色正确", state["role"] == "menu" and state["itemRole"] == "menuitem",
                  f"{state['role']}/{state['itemRole']}")
            check("分组分隔线存在", state["seps"] >= 1, f"seps={state['seps']}")
            check("菜单项带图标", state["icons"] >= 5, f"icons={state['icons']}")
            lbg, lfg = luminance(state["bg"]), luminance(state["fg"])
            check("浅色主题菜单为浅底深字", lbg > 0.6 and lfg < 0.4,
                  f"bg={lbg:.2f} fg={lfg:.2f}")
            lbg, lfg = luminance(state["bg"]), luminance(state["fg"])
            check("浅色主题菜单为浅底深字", lbg > 0.6 and lfg < 0.4,
                  f"bg={lbg:.2f} fg={lfg:.2f}")

        # ------------------------------------------------------------- keyboard
        ui.key("ArrowDown", "ArrowDown", 40)
        s1 = ui.menu()
        check("↓ 高亮第一项", s1 and s1["active"] == "添加文件…",
              s1["active"] if s1 else "菜单已关")
        ui.key("ArrowDown", "ArrowDown", 40)
        s2 = ui.menu()
        check("↓↓ 高亮第二项", s2 and s2["active"] == "添加文件夹…",
              s2["active"] if s2 else "菜单已关")
        ui.key("ArrowUp", "ArrowUp", 38)
        s3 = ui.menu()
        check("↑ 返回上一项", s3 and s3["active"] == "添加文件…",
              s3["active"] if s3 else "菜单已关")
        ui.key("End", "End", 35)
        s4 = ui.menu()
        check("End 跳到末项", s4 and s4["active"] == s4["labels"][-1],
              f"{s4['active']} / {s4['labels'][-1]}" if s4 else "菜单已关")
        ui.key("Escape", "Escape", 27)
        check("Esc 关闭菜单", ui.menu() is None and not ui.js("!!document.querySelector('.menu')"))

        # ---------------------------------------------------------- dismiss paths
        ui.right_click(bx, by)
        ui.click(6, 740)
        check("点击外部关闭菜单", ui.menu() is None)
        ui.right_click(bx, by)
        ui.js("window.dispatchEvent(new Event('resize'))")
        time.sleep(0.15)
        check("窗口尺寸变化关闭菜单", ui.menu() is None)

        # ----------------------------------------------------- only one at a time
        ui.right_click(bx, by)
        ui.right_click(700, 726)
        solo = ui.menu()
        check("重复右键只保留一个菜单", solo and solo["count"] == 1,
              f"count={solo['count']}" if solo else "无菜单")
        ui.js("window.ArcaMenu.close()")

        # ------------------------------------------------------- viewport clamping
        ui.right_click(1112, 754)
        edge = ui.menu()
        check("右下角右键时菜单自动翻转", bool(edge)
              and edge["rect"]["right"] <= edge["vw"] - 7
              and edge["rect"]["bottom"] <= edge["vh"] - 7,
              f"{edge['rect']} vp={edge['vw']}x{edge['vh']}" if edge else "无菜单")
        ui.js("window.ArcaMenu.close()")

        # ------------------------------------------------------- text fields pass
        pw = ui.rect("#c-pw")
        ui.right_click(pw["cx"], pw["cy"])
        check("输入框内保留系统菜单", ui.menu() is None)

        # ------------------------------------------------------ password copy
        def clip_text():
            return ui.js("navigator.clipboard.readText().catch(()=>'')")

        def last_toast():
            return ui.js("(()=>{const t=document.querySelectorAll('#toasts .toast:not(.out)');"
                         "return t.length?t[t.length-1].textContent:''})()")

        ws.call("Page.bringToFront", {})
        ui.js("document.getElementById('c-pw-gen').click()")
        time.sleep(0.5)
        genpw = ui.js("document.getElementById('c-pw').value")
        gtype = ui.js("document.getElementById('c-pw').type")
        check("生成强密码并明文展示", len(genpw) == 16 and gtype == "text",
              f"len={len(genpw)} type={gtype}")
        t1 = last_toast()
        check("生成即自动拷贝并提示", "拷贝" in (t1 or ""), t1)
        check("剪贴板内容即生成的密码", clip_text() == genpw,
              f"{clip_text()[:6]}… vs {genpw[:6]}…")
        ui.js("document.getElementById('c-pw-copy').click()")
        time.sleep(0.5)
        t2 = last_toast()
        check("拷贝按钮可再次拷贝", clip_text() == genpw and "已拷贝密码" in (t2 or ""),
              f"{t2}")
        # 还原字段（清空 + 点眼睛复位明文），后续段落从干净状态开始。
        ui.js("(()=>{const el=document.getElementById('c-pw');"
              "el.value='';el.dispatchEvent(new Event('input',{bubbles:true}));"
              "document.getElementById('c-pw-eye').click();})()")

        # ------------------------------------------------------------ file rows
        ui.js(f"window.__dropPaths({json.dumps(files)})")
        time.sleep(1.0)
        rows = ui.js("document.querySelectorAll('#c-list .file-row').length")
        check("准备三行待压缩文件", rows == 3, f"rows={rows}")

        row2 = ui.rect("#c-list .file-row:nth-child(2) b")
        ui.right_click(row2["cx"], row2["cy"])
        rm = ui.menu()
        check("文件行右键弹出专属菜单", bool(rm), "")
        if rm:
            check("文件行菜单项完整", all(k in rm["labels"] for k in
                                     ["开始压缩", "仅压缩此项", "在 Finder 中显示",
                                      "拷贝路径", "从列表移除", "清空列表"]), str(rm["labels"]))
            check("菜单显示真实快捷键", "⌘↵" in rm["kbd"], str(rm["kbd"]))
            check("删除项标红", ui.js("""(() => {
              const b = [...document.querySelectorAll('.menu .menu-item')]
                .find(x => x.textContent.includes('从列表移除'));
              return b ? getComputedStyle(b).color : '';
            })()""").startswith("rgb(220"), "危险色")

        # danger action
        before = ui.js("document.querySelectorAll('#c-list .file-row').length")
        ui.pick("从列表移除")
        after = ui.js("document.querySelectorAll('#c-list .file-row').length")
        check("从列表移除生效", after == before - 1, f"{before}→{after}")

        # keep only
        row1 = ui.rect("#c-list .file-row:nth-child(1) b")
        name1 = ui.js("document.querySelector('#c-list .file-row b').textContent")
        ui.right_click(row1["cx"], row1["cy"])
        ui.pick("仅压缩此项")
        left = ui.js("document.querySelectorAll('#c-list .file-row').length")
        name_l = ui.js("document.querySelector('#c-list .file-row b').textContent")
        check("仅压缩此项生效", left == 1 and name_l == name1, f"{left} 项 {name_l}")

        # clear
        row1 = ui.rect("#c-list .file-row:nth-child(1) b")
        ui.right_click(row1["cx"], row1["cy"])
        ui.pick("清空列表")
        rows_after = ui.js("document.querySelectorAll('#c-list .file-row').length")
        check("清空列表生效", rows_after == 0, f"rows={rows_after}")
        check("空列表回到拖放区", ui.js(
            "!document.querySelector('#drop-c').classList.contains('hidden')"))

        # --------------------------------------------------------- extract view
        ui.js("document.querySelector('[data-nav=\"extract\"]').click()")
        time.sleep(0.4)
        ui.right_click(700, 300)
        ex_empty = ui.menu()
        check("解压空态菜单", bool(ex_empty) and "选择压缩包…" in ex_empty["labels"],
              str(ex_empty["labels"]) if ex_empty else "无菜单")
        ui.js("window.ArcaMenu.close()")

        ui.js(f"window.__dropPaths({json.dumps([arch])})")
        time.sleep(2.0)
        cell = ui.rect("#x-table tr[data-entry] .name")
        if cell:
            ui.right_click(cell["cx"], cell["cy"])
            er = ui.menu()
            check("归档条目右键菜单", bool(er) and all(k in er["labels"] for k in
                                                ["拷贝条目名称", "解压到…", "仅校验"]),
                  str(er["labels"]) if er else "无菜单")
            ui.js("window.ArcaMenu.close()")
        else:
            check("归档条目右键菜单", False, "归档没有条目")

        # --------------------------------------------- A1 选择性解压（真实 UI）
        sel_rows = ui.js("document.querySelectorAll('#x-table tr[data-entry]').length")
        check("归档条目有选择列", sel_rows > 0 and ui.js("""(() => {
          const t = document.querySelector('#x-table').closest('table');
          return !!t && !t.classList.contains('no-sel')
            && document.querySelectorAll('#x-table .row-sel').length ===
               document.querySelectorAll('#x-table tr[data-entry]').length;
        })()"""), f"rows={sel_rows}")
        check("默认全选且主操作为「解压选中 N 项」", ui.js("""(() => {
          const txt = document.querySelector('#x-start').textContent;
          return document.querySelectorAll('#x-table .row-sel:checked').length === %d
            && txt.includes('解压选中 %d 项')
            && !document.querySelector('#x-start').disabled
            && document.querySelector('#x-sel-all').checked;
        })()""" % (sel_rows, sel_rows)), f"rows={sel_rows}")
        # 取消一项：计数、按钮与读屏状态同步；不重建表格（焦点仍可用）
        ui.js("document.querySelector('#x-table .row-sel').click()")
        time.sleep(0.2)
        check("取消一项后计数与主操作同步", ui.js("""(() => {
          const n = document.querySelectorAll('#x-table .row-sel:checked').length;
          return n === %d
            && document.querySelector('#x-start').textContent.includes('解压选中 %d 项')
            && document.querySelector('#x-sel-count').textContent.includes('已选 %d');
        })()""" % (sel_rows - 1, sel_rows - 1, sel_rows - 1)),
              f"rows={sel_rows}")
        check("选择状态带读屏语义", ui.js("""(() => {
          const tr = document.querySelector('#x-table tr[data-entry]');
          const box = tr.querySelector('.row-sel');
          return tr.getAttribute('aria-selected') === 'false'
            && /^选择 /.test(box.getAttribute('aria-label') || '')
            && !!(document.querySelector('#x-sel-all').getAttribute('aria-label'));
        })()"""))
        # 键盘：聚焦行 + Space 切换；焦点必须留在同一行（选择不重建 DOM）
        ui.js("""(() => { document.querySelector('#x-table tr[data-entry]').focus();
                  return 'ok'; })()""")
        ui.key(" ", "Space", 32)
        time.sleep(0.2)
        check("键盘空格切换选中且焦点不丢", ui.js("""(() => {
          const tr = document.querySelector('#x-table tr[data-entry]');
          return document.activeElement === tr
            && tr.classList.contains('is-selected')
            && tr.getAttribute('aria-selected') === 'true';
        })()"""))
        ui.js("document.querySelector('#x-sel-clear').click()")
        time.sleep(0.2)
        check("清空选择后主按钮禁用", ui.js("""(() => {
          return document.querySelector('#x-start').disabled
            && document.querySelectorAll('#x-table .row-sel:checked').length === 0
            && document.querySelector('#x-sel-count').textContent.includes('已选 0');
        })()"""))
        ui.js("document.querySelector('#x-sel-all-btn').click()")
        time.sleep(0.2)
        check("全选恢复并重新启用主按钮", ui.js("""(() => {
          return !document.querySelector('#x-start').disabled
            && document.querySelectorAll('#x-table .row-sel:checked').length === %d
            && document.querySelector('#x-start').textContent.includes('解压选中 %d 项');
        })()""" % (sel_rows, sel_rows)))

        # --------------------------- A1 嵌套选择：父目录半选/整树/真实落盘
        # Parent rows are tri-state: a directory row means its whole subtree,
        # and a partially selected parent must never be sent to the engine (it
        # would re-include the child the user just unchecked).
        ui.js(f"window.__dropPaths({json.dumps([nested])})")
        time.sleep(2.2)
        nested_rows = ui.js("document.querySelectorAll('#x-table tr[data-entry]').length")
        check("嵌套归档列表就绪", nested_rows == 6, f"rows={nested_rows}")
        def row_click(path):
            ui.js("""(() => {
              const tr = [...document.querySelectorAll('#x-table tr[data-entry]')]
                .find(r => r.dataset.entry === %s);
              if (!tr) return false;
              tr.querySelector('.row-sel').click();
              return true;
            })()""" % json.dumps(path))
            time.sleep(0.2)
        # whole directory row = whole subtree
        row_click("子目录")
        check("取消整目录=取消整棵子树", ui.js("""(() => {
          const tr = [...document.querySelectorAll('#x-table tr[data-entry]')]
            .find(r => r.dataset.entry === '子目录');
          return !tr.querySelector('.row-sel').checked
            && document.querySelector('#x-start').textContent.includes('解压选中 2 项');
        })()"""))
        row_click("子目录")
        check("再次点击目录恢复整棵子树", ui.js("""(() => {
          const tr = [...document.querySelectorAll('#x-table tr[data-entry]')]
            .find(r => r.dataset.entry === '子目录');
          return tr.querySelector('.row-sel').checked
            && document.querySelector('#x-start').textContent.includes('解压选中 6 项');
        })()"""))
        # uncheck one nested file: parent becomes indeterminate and is not sent
        row_click("子目录/c.txt")
        check("取消嵌套文件后父目录为半选", ui.js("""(() => {
          const row = (p) => [...document.querySelectorAll('#x-table tr[data-entry]')]
            .find(r => r.dataset.entry === p);
          const parent = row('子目录').querySelector('.row-sel');
          const child = row('子目录/c.txt').querySelector('.row-sel');
          return !child.checked && !parent.checked && parent.indeterminate === true
            && document.querySelector('#x-start').textContent.includes('解压选中 4 项');
        })()"""), str(ui.js("""(() => {
          const row = (p) => [...document.querySelectorAll('#x-table tr[data-entry]')]
            .find(r => r.dataset.entry === p);
          const parent = row('子目录').querySelector('.row-sel');
          const child = row('子目录/c.txt').querySelector('.row-sel');
          return {parentChecked: parent.checked, parentIndeterminate: parent.indeterminate,
                  childChecked: child.checked,
                  label: document.querySelector('#x-start').textContent,
                  count: document.querySelector('#x-sel-count').textContent};
        })()""")))
        before_jobs = {j["id"] for j in api(port, token, "/jobs")["jobs"]}
        ui.js("document.querySelector('#x-start').click()")
        nested_job = wait_new_job(port, token, before_jobs)
        nested_dest = nested_job.get("destPath") or ""
        files_ok = (nested_job["status"] == "done"
                    and os.path.isfile(os.path.join(nested_dest, "a.txt"))
                    and os.path.isfile(os.path.join(nested_dest, "b.txt"))
                    and os.path.isfile(os.path.join(nested_dest, "子目录", "嵌套", "d.txt"))
                    and not os.path.exists(os.path.join(nested_dest, "子目录", "c.txt")))
        check("真实 UI→HTTP→磁盘：嵌套取消后 c.txt 未落盘", files_ok,
              f"status={nested_job['status']} dest={nested_dest} "
              f"error={nested_job.get('error')!r}")
        if files_ok:
            payload_ok = (open(os.path.join(nested_dest, "a.txt"), "rb").read() == b"alpha"
                          and open(os.path.join(nested_dest, "b.txt"), "rb").read() == b"beta"
                          and open(os.path.join(nested_dest, "子目录", "嵌套", "d.txt"),
                                   "rb").read() == b"deep")
            check("嵌套取消后其余应选项内容逐字正确", payload_ok, nested_dest)

        # ------------------------------------ A1 取消后重新全选：回到整包请求
        # 半选父目录被移出 sel 后，重新勾选最后一个子项必须能恢复父目录；
        # 所有条目恢复选中时 request 不应再携带 entries。
        ui.js("document.querySelector('[data-nav=\"extract\"]').click()")
        time.sleep(0.4)
        row_click("子目录/c.txt")
        check("重新勾选后父目录与全选框恢复", ui.js("""(() => {
          const row = (p) => [...document.querySelectorAll('#x-table tr[data-entry]')]
            .find(r => r.dataset.entry === p);
          const parent = row('子目录').querySelector('.row-sel');
          const child = row('子目录/c.txt').querySelector('.row-sel');
          return child.checked && parent.checked && !parent.indeterminate
            && document.querySelector('#x-sel-all').checked
            && !document.querySelector('#x-sel-all').indeterminate
            && document.querySelector('#x-start').textContent.includes('解压选中 6 项')
            && document.querySelector('#x-sel-count').textContent.includes('已选 6 / 6');
        })()"""))
        # Reuse the same auto-folder destination: the first job's output is a
        # test artifact under DEMO, so removing it keeps the second extraction
        # on the normal UI path instead of triggering the exists dialog.
        shutil.rmtree(nested_dest, ignore_errors=True)
        ui.js("""(() => {
          window.__extractRequests = [];
          window.__origReviewFetch = window.fetch;
          window.fetch = function(input, init) {
            try {
              const url = typeof input === 'string' ? input : input.url;
              if (url.includes('/api/extract')) {
                window.__extractRequests.push(JSON.parse(init.body));
              }
            } catch (_) {}
            return window.__origReviewFetch.apply(this, arguments);
          };
          return 'ok';
        })()""")
        before_jobs = {j["id"] for j in api(port, token, "/jobs")["jobs"]}
        ui.js("document.querySelector('#x-start').click()")
        reselect_job = wait_new_job(port, token, before_jobs)
        reqs = ui.js("window.__extractRequests") or []
        req_body = reqs[-1] if reqs else {}
        reselect_dest_path = reselect_job.get("destPath") or ""
        reselect_files = set()
        if reselect_dest_path:
            for base, _dirs, names in os.walk(reselect_dest_path):
                for name in names:
                    if name.startswith("._"):
                        continue
                    reselect_files.add(os.path.relpath(os.path.join(base, name),
                                                       reselect_dest_path))
        reselect_ok = (reselect_job["status"] == "done"
                       and "entries" not in req_body
                       and (reselect_job.get("result") or {}).get("partial") is None
                       and reselect_files == {"a.txt", "b.txt", "子目录/c.txt",
                                              "子目录/嵌套/d.txt"})
        check("重新全选后真实请求省略 entries 并解压整包", reselect_ok,
              f"bodyKeys={sorted(req_body)} status={reselect_job['status']} "
              f"files={sorted(reselect_files)}")

        # ------------------------------------------- 轮询焦点稳定（专项回归锁）
        # patchJobEl 必须幂等补丁：轮询重画时如果整卡（或取消按钮）被重建，
        # 焦点立刻丢失、方向键断链。锁法：聚焦运行中任务卡的取消按钮，跨过
        # 至少 2 个轮询周期（1.6s×2）后确认它仍是同一节点且保持焦点。
        probe = os.path.join(DEMO, "focus-probe.bin")
        os.makedirs(DEMO, exist_ok=True)
        with open(probe, "wb") as fh:
            fh.write(os.urandom(320 * 1024 * 1024))   # 随机数据 + 高级别，保证任务跑得过采样窗口
        api(port, token, "/jobs/clear", {})
        pr = api(port, token, "/compress", {"inputs": [probe], "name": "焦点探针",
                                            "destDir": DEMO, "format": "7z", "level": 7,
                                            "par2Percent": 5})
        probe_id = pr["job"]["id"]
        # 任务页未激活时 renderQueue 不渲染，#q-list 恒空——用 ⌘3 切过去。
        ui.js("document.dispatchEvent(new KeyboardEvent('keydown', "
              "{key: '3', metaKey: true, bubbles: true, cancelable: true})); 'ok'")
        time.sleep(1.0)
        ui.js("delete window.__focusBtn")
        samples = 0
        gone = 0
        broken = ""
        finished = False
        deadline = time.time() + 60
        while time.time() < deadline and samples < 14 and not broken:
            snap = ui.js("""(() => {
              const card = document.querySelector('#q-list .job.running, #q-list .job.queued');
              if (!card) return {gone: true};
              const btn = card.querySelector('[data-cancel]');
              if (!btn) return {gone: true};
              if (!window.__focusBtn) window.__focusBtn = btn;
              if (document.activeElement !== window.__focusBtn) window.__focusBtn.focus();
              return {same: window.__focusBtn === btn,
                      connected: window.__focusBtn.isConnected,
                      focused: document.activeElement === window.__focusBtn};
            })()""")
            if snap is None:
                time.sleep(0.3)
                continue
            if snap.get("gone"):
                # 卡片还没渲染出来，或任务已结束（结束会重建 actions，属正常）
                if samples > 0 and ui.js(
                        "!!document.querySelector('#q-list .job.done, #q-list .job.error')"):
                    finished = True
                    break
                gone += 1
                if gone > 40:
                    break
            else:
                gone = 0
                samples += 1
                if not (snap["same"] and snap["connected"] and snap["focused"]):
                    broken = f"sample#{samples} {snap}"
            time.sleep(0.45)
        try:
            os.remove(probe)
        except OSError:
            pass
        # /jobs/clear 只清终态任务（产品语义）。探针本来就是有意长跑的：
        # 采样结束后显式取消让它让出队列，再等真实终态（cancelled）。
        # 未回收则终止——后续每个用例都依赖空队列。
        try:
            api(port, token, "/job/cancel", {"id": probe_id})
        except Exception:
            pass
        probe_done, probe_reclaimed = await_job(port, token, probe_id, 90, "焦点探针")
        if probe_done is None or probe_done["status"] not in TERMINAL:
            raise RuntimeError(
                "焦点探针未能在 /jobs/clear 前回收："
                f"status={probe_done and probe_done['status']} "
                f"reclaimed={probe_reclaimed} "
                f"error={probe_done and probe_done.get('error')!r} "
                f"log={(probe_done and (probe_done.get('log') or ''))[-200:]!r}"
                "——后续用例依赖空队列，终止")
        api(port, token, "/jobs/clear", {})
        check("轮询周期内任务卡焦点不丢（幂等补丁）",
              (samples >= 8 or finished) and not broken,
              f"samples={samples} finished={finished} {broken}")
        # 回到解压页：后续用例依赖“drop 落在解压页”的隐式前提。
        ui.js("document.dispatchEvent(new KeyboardEvent('keydown', "
              "{key: '2', metaKey: true, bubbles: true, cancelable: true})); 'ok'")
        time.sleep(0.8)

        # ------------------------------------------------------- zstd + PAR2
        # A .tar.zst plus recovery records is the only archive that can offer
        # the recovery entries, so it needs its own fixture.
        api(port, token, "/jobs/clear", {})
        z = api(port, token, "/compress", {
            "inputs": [files[0]], "name": "zst测试", "destDir": DEMO,
            "format": "tar.zst", "level": 5, "par2Percent": 5})
        zpath = z.get("target") or ""
        zj, zreclaimed = await_job(port, token, z["job"]["id"], 120, "zst+par2")
        zst_ok = bool(zj) and zreclaimed and zj["status"] == "done" and os.path.isfile(zpath)
        check("生成 .tar.zst 与恢复记录", zst_ok
              and any(f.endswith(".par2") for f in os.listdir(DEMO)),
              f"{os.path.basename(zpath)} status={zj and zj['status']} "
              f"reclaimed={zreclaimed} error={zj and zj.get('error')!r} "
              f"log={(zj and (zj.get('log') or ''))[-160:]!r}")

        if not zst_ok:
            # 产物缺失时依赖它的检查只能显式失败，不能拿空选择器掩盖。
            check("归档页显示恢复记录标记", False, "前置 .tar.zst 任务未完成")
            check("恢复记录菜单项出现", False, "前置 .tar.zst 任务未完成")
        else:
            ui.js(f"window.__dropPaths({json.dumps([zpath])})")
            time.sleep(2.4)
            chip = ui.js("""(() => {
              const c = document.querySelector('#x-chip-par2');
              return c && !c.classList.contains('hidden') ? c.textContent.trim() : '';
            })()""")
            check("归档页显示恢复记录标记", chip.startswith("恢复记录"), chip or "未显示")

            cell = ui.rect("#x-table tr[data-entry] .name")
            if cell:
                ui.right_click(cell["cx"], cell["cy"])
                zm = ui.menu()
                check("恢复记录菜单项出现", bool(zm) and all(k in zm["labels"] for k in
                                                       ["校验恢复记录", "修复损坏的文件"]),
                      str(zm["labels"]) if zm else "无菜单")
                ui.js("window.ArcaMenu.close()")
            else:
                check("恢复记录菜单项出现", False, "tar.zst 没有条目")

        # zstd cannot encrypt or split, so those rows must disappear entirely
        ui.js("document.querySelector('[data-nav=\"compress\"]').click()")
        time.sleep(0.4)
        ui.js("""(() => {
          const b = document.querySelector('#c-format button[data-id="tar.zst"]');
          if (b) b.click();
        })()""")
        time.sleep(0.45)
        rows = ui.js("""(() => {
          const v = (id) => { const e = document.querySelector(id);
            return e ? !e.classList.contains('hidden') : null; };
          return {pw: v('#c-pw-row'), split: v('#c-split-row'),
                  method: v('#c-method-row'), solid: v('#c-solid-row'),
                  par2: v('#c-par2-row')};
        })()""")
        check("ZST 隐藏加密/分卷/算法/固实行",
              rows and not any(rows[k] for k in ("pw", "split", "method", "solid")),
              str(rows))
        check("ZST 仍提供恢复记录开关", bool(rows and rows["par2"]), str(rows))

        ui.js("document.querySelector('#c-par2').click()")
        time.sleep(0.3)
        hint = ui.js("document.querySelector('#c-par2-hint').textContent")
        check("打开恢复记录给出冗余预估", "冗余" in (hint or ""), hint or "无提示")
        check("恢复记录档位可选", ui.js(
            "document.querySelector('#c-par2-pct').options.length") >= 3,
            str(ui.js("document.querySelector('#c-par2-pct').options.length")))
        # Single-volume advice is a disclosure now: hover-only text was
        # unreachable by keyboard and reflowed the card on every pointer pass.
        check("单卷开关随恢复记录显示",
              ui.js("!document.querySelector('#c-par2-single-row').classList.contains('hidden')"))
        check("单卷说明默认收起",
              ui.js("document.querySelector('#c-par2-single-note').classList.contains('hidden')"))
        ui.js("document.querySelector('#c-par2-single-help').click()")
        time.sleep(0.25)
        note = ui.js("document.querySelector('#c-par2-single-note').textContent")
        check("说明可展开且内容完整",
              "合成一个" in (note or "") and "网盘" in (note or ""), (note or "")[:36])
        check("说明按钮标记展开态",
              ui.js("document.querySelector('#c-par2-single-help').getAttribute('aria-expanded')") == "true")
        check("状态行对读屏可见",
              ui.js("document.querySelector('#c-par2-hint').getAttribute('aria-live')") == "polite")
        ui.js("document.querySelector('#c-par2-single').click()")
        time.sleep(0.2)
        check("单卷开关可切换",
              ui.js("document.querySelector('#c-par2-single').getAttribute('aria-checked')") == "true")
        # hovering must leave the status line alone (it is state, not advice)
        ui.js("""(() => {
          const row = document.querySelector('#c-par2-single-row');
          row.dispatchEvent(new MouseEvent('mouseenter', {bubbles: false}));
          return 'ok';
        })()""")
        hint2 = ui.js("document.querySelector('#c-par2-hint').textContent")
        check("悬停不改写状态行", "冗余" in (hint2 or ""), (hint2 or "")[:36])

        # ----------------------------------------------------- seg / pill
        # An unrelated toggle re-rendered the row; the rebuilt pill then slid
        # from the first option to the real one (dx ~ -43px) even though the
        # selection had not moved.
        ui.js("""document.querySelector('#c-format button[data-id="zip"]').click()""")
        time.sleep(0.5)
        ui.js("document.querySelector('#c-par2').click()")
        time.sleep(0.06)
        drift = ui.js("""(() => {
          const seg = document.querySelector('#c-format');
          const a = seg.querySelector('[aria-checked="true"]').getBoundingClientRect();
          const p = seg.querySelector('.seg-pill').getBoundingClientRect();
          return +(p.x - a.x).toFixed(1);
        })()""")
        check("开关不再让 pill 从首项滑入", drift is not None and abs(drift) <= 1, f"dx={drift}")

        # Rebuilding also killed focus, so arrow keys died after one press.
        same_node = ui.js("""(() => {
          const before = document.querySelector('#c-format button[data-id="zip"]');
          document.querySelector('#c-par2').click();
          const after = document.querySelector('#c-format button[data-id="zip"]');
          return before === after;
        })()""")
        check("开关不再重建格式按钮", same_node is True)

        ui.js("""(() => { const b = document.querySelector('#c-format button[data-id="7z"]');
          b.click(); b.focus(); return b.dataset.id; })()""")
        time.sleep(0.25)
        ui.key("ArrowRight", "ArrowRight", 39)
        one = ui.js("""document.querySelector('#c-format [aria-checked="true"]').dataset.id""")
        ui.key("ArrowRight", "ArrowRight", 39)
        two = ui.js("""document.querySelector('#c-format [aria-checked="true"]').dataset.id""")
        check("键盘可连续切换格式", one != two and two is not None, f"{one} → {two}")

        # RAR without WinRAR is not "disabled": it becomes available by pointing
        # at WinRAR, so the tab stays focusable and clicking it lands on exactly
        # that setting row. A link parked after the selected format's
        # description read as part of that description (see DESIGN.md).
        rar_state = ui.js("""(() => {
          const b = document.querySelector('#c-format button[data-id="rar"]');
          if (!b) return null;
          const sel = document.querySelector('#c-format [aria-checked="true"]');
          return {aria: b.getAttribute('aria-disabled'), disabled: b.disabled,
                  tag: (b.querySelector('.ext') || {}).textContent || '',
                  focusable: b.tabIndex >= 0, selectedBefore: sel.dataset.id};
        })()""")
        check("RAR 呈现为「需配置」而非纯禁用",
              bool(rar_state) and rar_state["aria"] == "true"
              and not rar_state["disabled"] and rar_state["tag"] == "需配置",
              str(rar_state))
        ui.js("""(() => { const b = document.querySelector('#c-format button[data-id="rar"]');
          b.focus(); return 'ok'; })()""")
        time.sleep(0.25)
        rar_hint = ui.js("document.querySelector('#c-format-hint').textContent")
        check("聚焦 RAR 显示它自己的说明", "WinRAR" in (rar_hint or ""), (rar_hint or "")[:32])
        ui.js("document.querySelector('#c-format button[data-id=\\'rar\\']').click()")
        time.sleep(1.1)
        landed = ui.js("""(() => {
          const sheet = document.querySelector('.sheet');
          if (!sheet) return null;
          const fold = sheet.querySelector('details.sheet-fold');
          const row = sheet.querySelector('#set-rar')?.closest('.sheet-row');
          const r = row ? row.getBoundingClientRect() : null;
          return {open: !!fold && fold.open,
                  visible: !!r && r.height > 0 && r.top > 0 && r.bottom < innerHeight,
                  target: !!(row && row.classList.contains('is-target')),
                  focused: document.activeElement.id};
        })()""")
        check("点击 RAR 落到 WinRAR 设置行",
              bool(landed) and landed["open"] and landed["visible"] and landed["target"],
              str(landed))
        check("跳转把焦点交给 WinRAR 按钮",
              (landed or {}).get("focused") == "set-rar", str((landed or {}).get("focused")))
        ui.js("document.querySelector('.sheet [data-close]').click()")
        time.sleep(0.5)
        check("跳转后设置可正常关闭", ui.js("!document.querySelector('.sheet')"))
        check("点击 RAR 未改变当前格式",
              ui.js("""document.querySelector('#c-format [aria-checked="true"]').dataset.id""")
              == (rar_state or {}).get("selectedBefore"),
              str(ui.js("""document.querySelector('#c-format [aria-checked="true"]').dataset.id""")))

        ui.js("document.querySelector('#c-par2-single').click()")
        ui.js("document.querySelector('#c-par2').click()")
        time.sleep(0.2)

        # ------------------------------------------------------------- job card
        api(port, token, "/jobs/clear", {})
        rj = api(port, token, "/compress", {
            "inputs": [files[0]], "name": "右键测试", "destDir": DEMO, "format": "zip"})
        rjob, rreclaimed = await_job(port, token, rj["job"]["id"], 60, "右键测试")
        rj_ok = bool(rjob) and rreclaimed and rjob["status"] == "done"
        check("右键测试任务完成", rj_ok,
              f"status={rjob and rjob['status']} reclaimed={rreclaimed} "
              f"error={rjob and rjob.get('error')!r} "
              f"log={(rjob and (rjob.get('log') or ''))[-160:]!r}")
        time.sleep(0.6)
        ui.js("document.querySelector('[data-nav=\"queue\"]').click()")
        time.sleep(0.9)
        card = ui.rect(".job[data-job] b")
        if card and rj_ok:
            ui.right_click(card["cx"], card["cy"])
            jm = ui.menu()
            check("任务卡右键菜单", bool(jm) and all(k in jm["labels"] for k in
                                              ["在 Finder 中显示", "拷贝路径", "再次压缩这些文件"]),
                  str(jm["labels"]) if jm else "无菜单")
            check("任务菜单含重做项图标", bool(jm) and jm["icons"] >= 3,
                  f"icons={jm['icons']}" if jm else "")
            ui.js("window.ArcaMenu.close()")
        elif not rj_ok:
            check("任务卡右键菜单", False, "前置「右键测试」任务未完成")
            check("任务菜单含重做项图标", False, "前置「右键测试」任务未完成")
        else:
            check("任务卡右键菜单", False, "没有任务卡")

        # Sidebar "recent": the rail's lower half used to be a blank column
        # (53% empty at 764px, 64% at 982px). It now carries the last three
        # finished jobs, and it must update from any view (polling), not only
        # while the task page happens to be open.
        recent = ui.js("""(() => {
          const box = document.querySelector('#rail-recent');
          if (!box) return null;
          const items = [...document.querySelectorAll('#rail-recent-list .recent-item')];
          return {visible: !box.classList.contains('hidden'), n: items.length,
                  first: items[0] ? items[0].dataset.recent : null};
        })()""")
        check("侧栏显示最近任务且不超过 3 条",
              bool(recent) and recent["visible"] and 1 <= recent["n"] <= 3, str(recent))
        ui.js("""document.querySelector('[data-nav="compress"]').click()""")
        time.sleep(0.4)
        if ui.js("!!document.querySelector('#rail-recent-list .recent-item')"):
            ui.js("""document.querySelector('#rail-recent-list .recent-item').click()""")
            time.sleep(0.7)
            check("点击最近任务跳到任务页",
                  ui.js("document.querySelector('.view.is-active').id") == "view-queue",
                  ui.js("document.querySelector('.view.is-active').id"))
        else:
            check("点击最近任务跳到任务页", False,
                  "侧栏没有最近任务条目可点（上一条「侧栏显示最近任务」已失败）")

        # ------------------------------------------------------- action via keys
        rail = ui.rect(".rail-foot")
        ui.right_click(rail["cx"], rail["y"] + 4)
        gm = ui.menu()
        check("侧栏右键弹通用菜单", bool(gm) and "外观：深色" in gm["labels"],
              str(gm["labels"]) if gm else "无菜单")
        if gm:
            check("外观项恰好一个勾选", len(gm["checked"]) == 1, str(gm["checked"]))
            for _ in range(6):
                ui.key("ArrowDown", "ArrowDown", 40)
                cur = ui.menu()
                if cur and cur["active"] == "外观：深色":
                    break
            ui.key("Enter", "Enter", 13)
            time.sleep(0.5)
            check("键盘回车执行菜单项", ui.js("document.documentElement.dataset.theme") == "dark"
                  and ui.menu() is None, ui.js("document.documentElement.dataset.theme"))

        # ------------------------------------------------------------ dark theme
        ui.js("document.querySelector('[data-nav=\"compress\"]').click()")
        time.sleep(0.4)
        ui.right_click(bx, by)
        st = ui.menu()
        if st:
            bg = luminance(st["bg"])
            fg = luminance(st["fg"])
            check("深色主题菜单为暗底亮字", bg < 0.2 and fg > 0.6, f"bg={bg:.2f} fg={fg:.2f}")
            ui.js("window.ArcaMenu.close()")
        else:
            check("深色主题菜单为暗底亮字", False, "无菜单（当前在任务页）")

        # restore appearance
        ui.js("document.querySelector('#btn-theme').click()")
        time.sleep(0.3)

        # -------------------------------------------------- layout sanity re-run
        ui.js("document.querySelector('[data-nav=\"compress\"]').click()")
        time.sleep(0.4)
        ui.right_click(bx, by)
        w = ui.menu()
        check("菜单宽度受控", bool(w) and w["rect"]["w"] <= 322 and w["rect"]["w"] >= 200,
              f"w={w['rect']['w']}" if w else "无菜单")
        ui.js("window.ArcaMenu.close()")

        # ------------------------------------------- settings · Finder 集成区
        # The sheet is the only surface for "设为默认解压软件" and the service
        # preferences, so its rows must render from /api/integration even in a
        # source-tree run (where every button degrades to disabled, never to a
        # silent no-op).
        ui.js("document.querySelector('#btn-settings').click()")
        time.sleep(0.6)
        check("设置面板打开", ui.js("!!document.querySelector('.sheet')"))
        check("Finder 服务区存在", ui.js("!!document.querySelector('#set-finder')"))
        check("快捷键设置按钮存在",
              ui.js("(document.querySelector('#set-svc-prefs')||{}).textContent") == "打开快捷键设置")
        check("快速操作安装按钮存在",
              ui.js("!!document.querySelector('#set-qa-install')"))
        check("右键后直接开始默认关闭（先到压缩设置页）",
              ui.js("document.querySelector('#set-svc-auto').getAttribute('aria-checked')") == "false")
        check("右键压缩格式可选",
              ui.js("document.querySelectorAll('#set-svc-format option').length") >= 2,
              str(ui.js("document.querySelectorAll('#set-svc-format option').length")))
        check("默认打开方式区存在", ui.js("!!document.querySelector('#set-default')"))
        check("关联类型行齐全",
              ui.js("document.querySelectorAll('#set-default [data-def]').length") >= 5,
              str(ui.js("document.querySelectorAll('#set-default [data-def]').length")))
        check("状态芯片带计数",
              "/9" in str(ui.js("document.querySelector('#set-default .chip').textContent")),
              str(ui.js("document.querySelector('#set-default .chip').textContent")))
        in_bundle = ui.js("!!document.querySelector('#set-finder .sheet-row small')")
        check("「设为默认」在源码运行时禁用",
              ui.js("document.querySelector('#set-def-on').disabled") is True
              or ui.js("!!document.querySelector('#set-def-on')"),
              f"bundlePath={bool(in_bundle)}")
        check("「恢复系统默认」按钮存在", ui.js("!!document.querySelector('#set-def-off')"))
        # The sheet must fit without scrolling in its default state; a settings
        # panel that needs a scroll to reach "设为默认" hides its own main action.
        fits = ui.js("""(() => {
          const b = document.querySelector('.sheet-body');
          return b ? b.scrollHeight <= b.clientHeight + 1 : false;
        })()""")
        check("设置面板一屏可见", fits is True,
              ui.js("""(() => {
                const b = document.querySelector('.sheet-body');
                return b ? `${b.scrollHeight} vs ${b.clientHeight}` : 'no body';
              })()"""))
        check("低频项收进折叠组",
              ui.js("document.querySelectorAll('.sheet-fold').length") >= 2,
              str(ui.js("document.querySelectorAll('.sheet-fold').length")))
        check("版本号移入页脚",
              str(ui.js("(document.querySelector('.foot-version')||{}).textContent") or "").startswith("v"))
        ui.js("document.querySelector('.sheet [data-close]').click()")
        time.sleep(0.4)
        check("设置面板可关闭", ui.js("!document.querySelector('.sheet')"))

        # ------------------------------------------------- motion & a11y invariants
        # Every pressable surface must carry a transition (press feedback) and a
        # keyboard ring; keyboard view switches must not animate.
        # :focus-visible only matches after keyboard interaction, so drive a
        # real Tab instead of calling .focus() (which stays in pointer modality).
        ui.js("document.querySelector('#btn-theme').blur(); document.body.focus(); 'ok'")
        ui.key("Tab", "Tab", 9)
        time.sleep(0.2)
        focus_ring = ui.js("""(() => {
          const el = document.activeElement;
          if (!el || el === document.body) return {ok: false, why: 'no active element'};
          const cs = getComputedStyle(el);
          return {ok: cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) >= 1,
                  why: (el.id || el.className) + ' ' + cs.outlineStyle + ' ' + cs.outlineWidth,
                  visible: el.matches(':focus-visible')};
        })()""")
        check("键盘焦点环可见", bool(focus_ring and focus_ring.get("ok")),
              str(focus_ring))
        check("切页动画可关闭（instant）",
              ui.js("""(() => {
                const v = document.querySelector('#view-compress');
                return getComputedStyle(v).animationName !== 'none'
                  || v.classList.contains('is-instant');
              })()""") is not False)
        check("空任务页有明确动作",
              ui.js("!!document.querySelector('#q-empty-go')") is True,
              "empty state needs one clear action")
        check("按压过渡覆盖 transform",
              ui.js("""(() => {
                const cs = getComputedStyle(document.querySelector('#btn-theme'));
                return cs.transitionProperty.includes('transform')
                  && parseFloat(cs.transitionDuration) > 0;
              })()""") is True)
    finally:
        try:
            api(port, token, "/settings", {"patch": {"appearance": orig_theme,
                                                     "checkEngineUpdates": orig_check,
                                                     "checkAppUpdates": orig_appcheck}})
        except Exception:
            pass
        ws.close()
        proc.terminate()

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
