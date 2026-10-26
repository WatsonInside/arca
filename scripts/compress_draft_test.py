"""Compress-draft lifecycle + cancelled-task state, through the real UI.

Runs against a real headless Arca instance (--port-file, same convention as
menu_test) and a real Chrome DevTools session, with isolated HOME and temp
fixtures.  Every scenario goes UI -> HTTP -> job -> disk; nothing is asserted
from state alone.

Covered regressions:
  * accepted /api/compress clears the submitted draft (no old "待压缩" rows);
  * failed submit keeps the current draft and explicit choices;
  * cancel -> cancelled, cancel card collapses, recent list includes it;
  * after cancelling A, dropping B never mixes A inputs or A directory;
  * add/remove inputs recompute automatic name and destination;
  * an explicit destination survives edits and receives the product;
  * "再次压缩这些文件" is a fresh draft without an unrelated E draft;
  * two Finder inbox requests stay independent;
  * edits made while a /api/compress response is delayed are not wiped.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cdp  # noqa: E402
import jobwait  # noqa: E402

TERMINAL = ("done", "error", "cancelled")
ZSTD_LEVEL = 5

NATIVE_SHIM = r"""
window.__nextDest = '';
window.webkit = {messageHandlers: {arca: {postMessage(raw) {
  const message = JSON.parse(raw);
  let result = {};
  if (message.method === 'pick') {
    result = {paths: message.params.kind === 'dest' && window.__nextDest
      ? [window.__nextDest] : []};
  }
  setTimeout(() => window.__arcaReply(message.id, result), 0);
}}}};
'native shim ready'
"""

HELPERS = r"""
window.__dropPaths = (paths) => {
  const dt = new DataTransfer();
  dt.setData('text/uri-list', paths.map(p => 'file://' + encodeURI(p)).join('\r\n'));
  dt.setData('text/plain', paths.map(p => 'file://' + encodeURI(p)).join('\r\n'));
  window.dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true,
                                               dataTransfer: dt}));
  return true;
};
window.__compressCalls = [];
window.__preflightCalls = [];
window.__originalFetch = window.fetch;
window.__compressGate = null;
window.__preflightGate = null;
window.fetch = async function(input, init) {
  const url = typeof input === 'string' ? input : input.url;
  const kind = url.includes('/api/compress') ? 'compress'
    : url.includes('/api/preflight') ? 'preflight' : '';
  if (!kind) return window.__originalFetch.apply(this, arguments);
  const request = JSON.parse(init.body);
  const calls = kind === 'compress' ? window.__compressCalls : window.__preflightCalls;
  const record = {request, status: null, response: null};
  calls.push(record);
  const gate = kind === 'compress' ? window.__compressGate : window.__preflightGate;
  if (gate) {
    if (kind === 'compress') window.__compressGate = null;
    else window.__preflightGate = null;
    await gate;
  }
  const response = await window.__originalFetch.apply(this, arguments);
  try { record.response = await response.clone().json(); } catch (_) {}
  record.status = response.status;
  return response;
};
window.__draft = () => {
  const card = document.querySelector('#c-files');
  return {
    view: (document.querySelector('.view.is-active') || {}).id,
    viewTitle: document.querySelector('#view-title').textContent,
    cardHidden: card.classList.contains('hidden'),
    cardTitle: card.querySelector('.card-head .t').textContent,
    rows: [...document.querySelectorAll('#c-list .file-row')].map(row => ({
      name: row.querySelector('b').title,
      dir: row.querySelector('small').textContent,
    })),
    meta: document.querySelector('#c-meta').textContent,
    dest: document.querySelector('#c-dest-text').textContent,
    name: document.querySelector('#c-name').value,
    startDisabled: document.querySelector('#c-start').disabled,
    dropHidden: document.querySelector('#drop-c').classList.contains('hidden'),
    outPath: document.querySelector('#c-outpath').textContent,
  };
};
window.__jobCard = (id) => {
  const card = document.querySelector(`[data-job="${id}"]`);
  if (!card) return null;
  const bar = card.querySelector('.bar');
  return {
    className: card.className,
    barHidden: bar.classList.contains('hidden'),
    cancel: !!card.querySelector(`[data-cancel="${id}"]`),
    remove: !!card.querySelector(`[data-remove="${id}"]`),
    actions: card.querySelector('.job-actions').textContent,
  };
};
window.__recentIds = () => [...document.querySelectorAll('#rail-recent [data-recent]')]
  .map(el => el.dataset.recent);
window.__queueIds = () => [...document.querySelectorAll('#q-list [data-job]')]
  .map(el => el.dataset.job);
'helpers ready'
"""


def check(name, condition, detail=""):
    print(("  ok    " if condition else "  FAIL  ") + name
          + (f"   [{detail}]" if detail else ""), flush=True)
    if not condition:
        check.failed = True


check.failed = False


class Harness:
    def __init__(self, port_file):
        ports = json.loads(Path(port_file).read_text())
        self.port = int(ports["port"])
        self.token = ports["token"]
        self.base = f"http://127.0.0.1:{self.port}/api"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.ws = None
        self.proc = None

    # ------------------------------------------------------------------ HTTP
    def api(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"X-Arca-Token": self.token,
                     "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=120) as res:
                return res.status, json.loads(res.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def jobs(self):
        return self.api("/jobs")[1].get("jobs", [])

    def job(self, job_id):
        return next((j for j in self.jobs() if j["id"] == job_id), None)

    def wait_job(self, job_id, terminal=True, timeout=180):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.job(job_id)
            if last and terminal and last["status"] in TERMINAL:
                return last
            if last and not terminal and last["status"] == "running":
                return last
            time.sleep(0.06)
        raise TimeoutError(f"job {job_id} did not reach the expected state: {last}")

    # ------------------------------------------------------------------- JS
    def js(self, expression):
        out = self.ws.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
            "awaitPromise": True})
        if out.get("exceptionDetails"):
            raise RuntimeError(out["exceptionDetails"].get("text")
                               or json.dumps(out["exceptionDetails"])[:400])
        return out.get("result", {}).get("value")

    def wait_js(self, expression, timeout=20):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.js(expression)
            if last:
                return last
            time.sleep(0.05)
        raise TimeoutError(f"JS condition not ready: {expression}; last={last!r}")

    def click(self, selector):
        ok = self.js(f"""(() => {{
          const el = document.querySelector({json.dumps(selector)});
          if (!el) return false;
          el.click();
          return true;
        }})()""")
        if not ok:
            raise AssertionError(f"missing click target {selector}")

    def drop(self, paths):
        self.js(f"window.__dropPaths({json.dumps([str(p) for p in paths])})")

    def set_name(self, value):
        self.js(f"""(() => {{
          const el = document.querySelector('#c-name');
          el.value = {json.dumps(value)};
          el.dispatchEvent(new Event('input', {{bubbles: true}}));
          return true;
        }})()""")

    def choose_dest(self, path):
        self.js(f"window.__nextDest = {json.dumps(str(path))}; 'ok'")
        self.click("#c-dest")
        self.wait_js(f"document.querySelector('#c-dest-text').textContent"
                     f".includes({json.dumps(path.name)})")

    def frames(self):
        self.js("new Promise(r => requestAnimationFrame("
                "() => requestAnimationFrame(() => r(true))))")

    def draft(self):
        return self.js("window.__draft()")

    def page(self, token):
        self.ws.call("Page.navigate", {
            "url": f"http://127.0.0.1:{self.port}/index.html?t={token}"})
        self.wait_js("document.readyState === 'complete'")
        self.wait_js("document.querySelector('#c-format').children.length > 1")
        self.js(HELPERS)

    def _await_call(self, index):
        self.wait_js(f"window.__compressCalls.length > {index}"
                     f" && window.__compressCalls[{index}].status !== null")
        return self.js(f"window.__compressCalls[{index}]")

    def start(self, _index=None):
        index = self.js("window.__compressCalls.length")
        self.click("#c-start")
        call = self._await_call(index)
        if not (call.get("response") or {}).get("job"):
            raise AssertionError(f"compress rejected: {call}")
        return call, call["response"]["job"]["id"]

    def start_expect_failure(self, _index=None):
        index = self.js("window.__compressCalls.length")
        self.click("#c-start")
        call = self._await_call(index)
        if call.get("status") == 200:
            raise AssertionError(f"compress unexpectedly succeeded: {call}")
        return call

    def navigate(self, name):
        self.click(f'[data-nav="{name}"]')
        self.wait_js(f"document.querySelector('#view-{name}').classList.contains('is-active')")
        time.sleep(0.25)

    def archive_names(self, target):
        status, body = self.api("/inspect", {"path": str(target)})
        if status != 200 or not body.get("ok"):
            return None
        return {e["path"] for e in body["info"].get("entries", []) if not e.get("isDir")}


def build_fixtures(root: Path):
    dirs = {}
    for name in ("A_dir", "B_dir", "C_dir", "C2_dir", "D_dir", "E_dir",
                 "F_dir", "G_dir", "Old_dir", "New_dir", "OldDest_dir",
                 "NewDest_dir", "OldPre_dir", "NewPre_dir"):
        path = root / name
        path.mkdir(parents=True)
        dirs[name] = path
    files = {}
    files["A"] = dirs["A_dir"] / "first.bin"
    files["A"].write_bytes(os.urandom(160 * 1024 * 1024))
    files["B"] = dirs["B_dir"] / "second.txt"
    files["B"].write_text("second input only\n", encoding="utf-8")
    files["C"] = dirs["C_dir"] / "alpha.txt"
    files["C"].write_text("alpha\n", encoding="utf-8")
    files["C2"] = dirs["C2_dir"] / "beta.txt"
    files["C2"].write_text("beta\n", encoding="utf-8")
    files["E"] = dirs["E_dir"] / "draft.txt"
    files["E"].write_text("unrelated draft\n", encoding="utf-8")
    files["F"] = dirs["F_dir"] / "inbox-one.txt"
    files["F"].write_text("inbox one\n", encoding="utf-8")
    files["G"] = dirs["G_dir"] / "inbox-two.txt"
    files["G"].write_text("inbox two\n", encoding="utf-8")
    for key, sub, text in (("Old", "Old_dir", "old"), ("New", "New_dir", "new"),
                           ("OldPre", "OldPre_dir", "old-pre"),
                           ("NewPre", "NewPre_dir", "new-pre")):
        files[key] = dirs[sub] / f"{text}.txt"
        files[key].write_text(text + " payload\n", encoding="utf-8")
    return dirs, files


def run(port_file):
    root = Path(tempfile.mkdtemp(prefix="compress-draft-", dir=ROOT / "build"))
    profile = root / "chrome-profile"
    dirs, files = build_fixtures(root)
    h = Harness(port_file)
    try:
        h.api("/settings", {"patch": {
            "checkEngineUpdates": False, "checkAppUpdates": False,
            "suppressSound": True, "statsPrompted": True,
            "serviceAutoStart": False, "serviceFormat": "7z"}})
        h.api("/jobs/clear", {})
        h.proc, h.ws = cdp.launch(9474, str(profile))
        h.ws.call("Page.enable")
        h.ws.call("Runtime.enable")
        h.ws.call("Emulation.setDeviceMetricsOverride", {
            "width": 1120, "height": 764, "deviceScaleFactor": 2,
            "mobile": False})
        h.ws.call("Page.addScriptToEvaluateOnNewDocument",
                  {"source": NATIVE_SHIM})
        h.page(h.token)

        # ----------------------------------------------- 1. submit clears draft
        h.drop([files["A"]])
        h.wait_js("document.querySelectorAll('#c-list .file-row').length === 1")
        h.js("""(() => {
          const el = document.querySelector('#c-level');
          el.value = String(%d);
          el.dispatchEvent(new Event('input', {bubbles: true}));
          return el.value;
        })()""" % ZSTD_LEVEL)
        call_a, job_a = h.start(0)
        check("A 请求 inputs/dest 正确",
              call_a["request"]["inputs"] == [str(files["A"])]
              and call_a["request"]["destDir"] == str(dirs["A_dir"]),
              str(call_a["request"]))
        running = h.wait_job(job_a, terminal=False, timeout=60)
        progress = time.monotonic() + 45
        while time.monotonic() < progress:
            running = h.job(job_a)
            if running and running["status"] == "running" and running["percent"] > 0:
                break
            time.sleep(0.05)
        check("A 出现真实非零进度",
              running and running["status"] == "running" and running["percent"] > 0,
              f"status={running and running['status']} pct={running and running['percent']}")
        h.navigate("compress")
        drafted = h.draft()
        check("提交后压缩页不再保留 A 的待压缩草稿",
              drafted["cardHidden"] and not drafted["rows"]
              and drafted["name"] == "" and drafted["startDisabled"],
              str(drafted))

        # ---------------------------------- 2. cancel via real queue control
        h.navigate("queue")
        h.wait_js(f"!!document.querySelector('[data-cancel=\"{job_a}\"]')")
        h.click(f'[data-cancel="{job_a}"]')
        cancelled_a = h.wait_job(job_a)
        check("A 后端终态为 cancelled", cancelled_a["status"] == "cancelled",
              cancelled_a["status"])
        h.wait_js(f"document.querySelector('[data-job=\"{job_a}\"]')?.className.includes('cancelled')")
        card_a = h.js(f"window.__jobCard({json.dumps(job_a)})")
        check("取消卡片：已取消、进度条收起、取消按钮消失、移除可用",
              card_a and "cancelled" in card_a["className"]
              and card_a["barHidden"] and not card_a["cancel"] and card_a["remove"]
              and "已取消" in card_a["actions"], str(card_a))
        h.wait_js(f"window.__recentIds().includes({json.dumps(job_a)})")
        check("侧栏最近任务包含取消任务",
              job_a in (h.js("window.__recentIds()") or []))

        # ------------------- 3. drop B after cancel, no manual clear; B only
        h.drop([files["B"]])
        h.wait_js("document.querySelectorAll('#c-list .file-row').length === 1")
        mixed = h.draft()
        check("取消 A 后拖入 B：草稿只有 B",
              [r["name"] for r in mixed["rows"]] == ["second.txt"]
              and dirs["B_dir"].name in mixed["dest"]
              and mixed["name"] == "second", str(mixed))
        call_b, job_b = h.start(1)
        check("B 请求不混入 A、目录为 B",
              call_b["request"]["inputs"] == [str(files["B"])]
              and call_b["request"]["destDir"] == str(dirs["B_dir"]),
              str(call_b["request"]))
        done_b = h.wait_job(job_b)
        target_b = Path(call_b["response"]["target"])
        check("B 真实完成且落盘在 B 目录",
              done_b["status"] == "done" and target_b.is_file()
              and target_b.parent == dirs["B_dir"]
              and (done_b.get("outputs") or [None])[0] == str(target_b),
              f"status={done_b['status']} target={target_b}")
        names_b = h.archive_names(target_b)
        check("B 归档内容只有 B，不含 A",
              names_b == {"second.txt"}, str(names_b))

        # queue order: cancelled before done, recent includes both
        h.wait_js(f"window.__queueIds().includes({json.dumps(job_b)})")
        h.wait_js(f"window.__recentIds().includes({json.dumps(job_b)})")
        queue = h.js("window.__queueIds()") or []
        check("任务页排序：取消任务在完成之前",
              job_a in queue and job_b in queue
              and queue.index(job_a) < queue.index(job_b), str(queue))
        recent = h.js("window.__recentIds()") or []
        check("侧栏最近任务同时包含取消与完成",
              job_a in recent and job_b in recent, str(recent))

        # -------------------------- 4. add/remove recompute auto name & dest
        h.navigate("compress")
        h.drop([files["C"], files["C2"]])
        h.wait_js("document.querySelectorAll('#c-list .file-row').length === 2")
        auto_two = h.draft()
        check("两个目录的自动名称/目录：归档 + 与源文件相同",
              auto_two["name"] == "归档"
              and auto_two["dest"] == "与源文件相同", str(auto_two))
        h.click('#c-list .file-row[data-idx="0"] .file-x')
        h.wait_js("document.querySelectorAll('#c-list .file-row').length === 1")
        auto_one = h.draft()
        h.wait_js(f"document.querySelector('#c-outpath').textContent.includes("
                  f"{json.dumps(dirs['C2_dir'].name)})")
        check("删除首个输入后自动名称/目录跟随剩余输入",
              [r["name"] for r in auto_one["rows"]] == ["beta.txt"]
              and auto_one["name"] == "beta"
              and dirs["C2_dir"].name in auto_one["dest"], str(auto_one))
        h.click('#c-list .file-row[data-idx="0"] .file-x')
        h.wait_js("document.querySelectorAll('#c-list .file-row').length === 0")
        empty = h.draft()
        check("清空后回到空草稿，不残留旧目录/名称",
              empty["cardHidden"] and empty["dropHidden"] is False
              and empty["name"] == "" and empty["dest"] == "与源文件相同"
              and empty["startDisabled"], str(empty))

        # --------------------------------- 5. explicit dest survives edits
        h.drop([files["C"]])
        h.wait_js("document.querySelectorAll('#c-list .file-row').length === 1")
        h.js(f"window.__nextDest = {json.dumps(str(dirs['D_dir']))}; 'ok'")
        h.click("#c-dest")
        h.wait_js(f"document.querySelector('#c-dest-text').textContent.includes("
                  f"{json.dumps(dirs['D_dir'].name)})")
        h.drop([files["C2"]])
        h.wait_js("document.querySelectorAll('#c-list .file-row').length === 2")
        explicit = h.draft()
        check("显式选择 D 后添加输入仍使用 D",
              dirs["D_dir"].name in explicit["dest"], str(explicit))
        h.click('#c-list .file-row[data-idx="0"] .file-x')
        h.wait_js("document.querySelectorAll('#c-list .file-row').length === 1")
        after_remove = h.draft()
        check("显式选择 D 后删除输入仍使用 D",
              dirs["D_dir"].name in after_remove["dest"], str(after_remove))
        call_d, job_d = h.start(2)
        check("显式 D 请求 destDir 是 D",
              call_d["request"]["destDir"] == str(dirs["D_dir"]),
              str(call_d["request"]))
        done_d = h.wait_job(job_d)
        target_d = Path(call_d["response"]["target"])
        check("显式 D 产物真实落在 D",
              done_d["status"] == "done" and target_d.is_file()
              and target_d.parent == dirs["D_dir"], str(target_d))

        # ----------------------- 6. recompress ignores unrelated E draft
        h.navigate("compress")
        h.drop([files["E"]])
        h.wait_js("document.querySelector('#c-list .file-row b')?.title === 'draft.txt'")
        h.navigate("queue")
        h.wait_js(f"!!document.querySelector('[data-job=\"{job_b}\"]')")
        h.js(f"""(() => {{
          const card = document.querySelector('[data-job="{job_b}"]');
          const r = card.getBoundingClientRect();
          card.dispatchEvent(new MouseEvent('contextmenu', {{bubbles: true,
            cancelable: true, clientX: r.left + 30, clientY: r.top + 20}}));
          return true;
        }})()""")
        h.wait_js("[...document.querySelectorAll('.menu .menu-item')]"
                  ".some(x => x.textContent.includes('再次压缩这些文件'))")
        h.js("[...document.querySelectorAll('.menu .menu-item')]"
             ".find(x => x.textContent.includes('再次压缩这些文件')).click()")
        h.wait_js("document.querySelector('#view-compress').classList.contains('is-active')"
                  " && document.querySelector('#c-list .file-row b')?.title === 'second.txt'")
        redo = h.draft()
        check("再次压缩：只采用原任务 inputs，不继承 E",
              [r["name"] for r in redo["rows"]] == ["second.txt"]
              and redo["name"] == "second"
              and dirs["B_dir"].name in redo["dest"]
              and dirs["E_dir"].name not in redo["dest"], str(redo))
        call_r, job_r = h.start(3)
        check("再次压缩请求不携带 E 目录/无关输入",
              call_r["request"]["inputs"] == [str(files["B"])]
              and call_r["request"]["destDir"] == str(dirs["B_dir"]),
              str(call_r["request"]))
        done_r = h.wait_job(job_r)
        target_r = Path(call_r["response"]["target"])
        check("再次压缩产物落在 B 目录",
              done_r["status"] == "done" and target_r.is_file()
              and target_r.parent == dirs["B_dir"], str(target_r))

        # -------------------------- 7. two Finder inbox requests stay apart
        h.api("/jobs/clear", {})
        h.api("/settings", {"patch": {"serviceAutoStart": True}})
        h.page(h.token)
        h.api("/inbox", {"action": "compress", "paths": [str(files["F"])]})
        h.api("/inbox", {"action": "compress", "paths": [str(files["G"])]})
        h.wait_js("window.__compressCalls.length >= 2"
                  " && window.__compressCalls.every(c => c.status !== null)",
                  timeout=30)
        inbox_calls = h.js("window.__compressCalls") or []
        first, second = inbox_calls[0], inbox_calls[1]
        check("inbox 第一个请求 inputs/dest 正确",
              first["request"]["inputs"] == [str(files["F"])]
              and first["request"]["destDir"] == str(dirs["F_dir"]),
              str(first["request"]))
        check("inbox 第二个请求不混入第一个输入/目录",
              second["request"]["inputs"] == [str(files["G"])]
              and second["request"]["destDir"] == str(dirs["G_dir"]),
              str(second["request"]))
        inbox_jobs = [first["response"]["job"]["id"], second["response"]["job"]["id"]]
        for jid in inbox_jobs:
            h.wait_job(jid)
        check("inbox 两次任务均完成且产物在各自目录",
              Path(first["response"]["target"]).is_file()
              and Path(first["response"]["target"]).parent == dirs["F_dir"]
              and Path(second["response"]["target"]).is_file()
              and Path(second["response"]["target"]).parent == dirs["G_dir"])
        h.api("/settings", {"patch": {"serviceAutoStart": False}})

        # ------------------- 8. failed submit keeps draft and explicit choice
        h.page(h.token)
        h.navigate("compress")
        h.drop([files["C"]])
        h.wait_js("document.querySelectorAll('#c-list .file-row').length === 1")
        h.js("""(() => {
          const el = document.querySelector('#c-name');
          el.value = 'keep-me';
          el.dispatchEvent(new Event('input', {bubbles: true}));
          return true;
        })()""")
        missing = root / "missing-destination"
        h.js(f"window.__nextDest = {json.dumps(str(missing))}; 'ok'")
        h.click("#c-dest")
        h.wait_js(f"document.querySelector('#c-dest-text').textContent.includes('missing-destination')")
        failed_call = h.start_expect_failure(0)
        failed_draft = h.draft()
        check("提交失败保留输入、名称和显式目录",
              [r["name"] for r in failed_draft["rows"]] == ["alpha.txt"]
              and failed_draft["name"] == "keep-me"
              and "missing-destination" in failed_draft["dest"]
              and not failed_draft["startDisabled"], str(failed_draft))
        h.click("#c-clear")
        h.wait_js("document.querySelectorAll('#c-list .file-row').length === 0")
        time.sleep(0.9)   # ingest de-dupes an identical drop within 800ms

        # ---------------- 9. edits during delayed response are not wiped
        h.drop([files["C"]])
        h.wait_js("document.querySelectorAll('#c-list .file-row').length === 1")
        h.js("window.__compressGate = new Promise(r => { window.__releaseCompress = r; }); 'ok'")
        delayed_index = h.js("window.__compressCalls.length")
        h.click("#c-start")
        h.wait_js(f"window.__compressCalls.length > {delayed_index}")
        h.drop([files["C2"]])
        h.wait_js("document.querySelectorAll('#c-list .file-row').length === 2")
        h.js("window.__releaseCompress(); 'ok'")
        delayed_call = h._await_call(delayed_index)
        delayed_job = delayed_call["response"]["job"]["id"]
        h.wait_job(delayed_job)
        h.wait_js("document.querySelectorAll('#c-list .file-row').length === 1")
        delayed_draft = h.draft()
        h.wait_js(f"document.querySelector('#c-outpath').textContent.includes("
                  f"{json.dumps(dirs['C2_dir'].name)})")
        check("延迟响应期间的新编辑不被旧请求清理",
              [r["name"] for r in delayed_draft["rows"]] == ["beta.txt"]
              and delayed_draft["name"] == "beta"
              and dirs["C2_dir"].name in delayed_draft["dest"], str(delayed_draft))
        check("延迟请求本身只提交旧输入",
              delayed_call["request"]["inputs"] == [str(files["C"])]
              and delayed_call["request"]["destDir"] == str(dirs["C_dir"]),
              str(delayed_call["request"]))
        h.click("#c-clear")

        # ----------------- 10. ABA revisions: old compress response vs edits
        # Deterministic ordering: the old /api/compress is held by a gate through
        # clear + new draft + new manual name/destination, then released.
        h.page(h.token)
        h.drop([files["Old"]])
        h.wait_js("document.querySelector('#c-list .file-row b')?.title === 'old.txt'")
        h.set_name("old-manual")
        h.choose_dest(dirs["OldDest_dir"])
        h.js("window.__compressGate = new Promise(r => { window.__releaseCompress = r; }); 'ok'")
        old_index = h.js("window.__compressCalls.length")
        h.click("#c-start")
        h.wait_js(f"window.__compressCalls.length > {old_index}")
        h.click("#c-clear")
        h.wait_js("document.querySelectorAll('#c-list .file-row').length === 0")
        h.drop([files["New"]])
        h.wait_js("document.querySelector('#c-list .file-row b')?.title === 'new.txt'")
        h.set_name("new-manual")
        h.choose_dest(dirs["NewDest_dir"])
        h.js("window.__releaseCompress(); 'ok'")
        old_call = h._await_call(old_index)
        h.wait_job(old_call["response"]["job"]["id"])
        h.navigate("compress")
        aba = h.draft()
        check("延迟旧压缩响应：新输入保留",
              [r["name"] for r in aba["rows"]] == ["new.txt"], str(aba))
        check("延迟旧压缩响应：new-manual 保留",
              aba["name"] == "new-manual", aba["name"])
        check("延迟旧压缩响应：NewDest 保留",
              dirs["NewDest_dir"].name in aba["dest"], aba["dest"])
        h.click("#c-clear")
        h.wait_js("document.querySelectorAll('#c-list .file-row').length === 0")

        # --------------------- 11. stale /api/preflight (new draft wins)
        h.page(h.token)
        h.js("window.__preflightGate = new Promise(r => { window.__releasePreflight = r; }); 'ok'")
        pre_base = h.js("window.__preflightCalls.length")
        h.drop([files["OldPre"]])
        h.wait_js(f"window.__preflightCalls.length > {pre_base}")
        h.click("#c-clear")
        h.wait_js("document.querySelectorAll('#c-list .file-row').length === 0")
        h.drop([files["NewPre"]])
        h.wait_js("document.querySelector('#c-list .file-row b')?.title === 'new-pre.txt'")
        h.wait_js(f"window.__preflightCalls.length > {pre_base + 1}"
                  f" && window.__preflightCalls[{pre_base + 1}].status !== null")
        h.wait_js(f"document.querySelector('#c-outpath').textContent"
                  f".includes({json.dumps(dirs['NewPre_dir'].name)})")
        new_pre_path = h.js("document.querySelector('#c-outpath').textContent")
        h.js("window.__releasePreflight(); 'ok'")
        h.wait_js(f"window.__preflightCalls[{pre_base}].status !== null")
        h.frames()
        after_pre = h.draft()
        check("旧 preflight 不覆盖新草稿输出路径",
              dirs["NewPre_dir"].name in after_pre["outPath"]
              and dirs["OldPre_dir"].name not in after_pre["outPath"],
              f"before={new_pre_path} after={after_pre['outPath']}")

        # -------- 12. same draft, format-only switch: older preflight ignored
        h.page(h.token)
        h.drop([files["NewPre"]])
        h.wait_js("document.querySelector('#c-list .file-row b')?.title === 'new-pre.txt'")
        h.wait_js("window.__preflightCalls.length >= 1"
                  " && window.__preflightCalls[0].status !== null")
        h.wait_js("document.querySelector('#c-outpath').textContent.endsWith('.7z')")
        fmt_base = h.js("window.__preflightCalls.length")
        h.js("window.__preflightGate = new Promise(r => { window.__releasePreflight = r; }); 'ok'")
        h.click('#c-format button[data-id="zip"]')
        h.wait_js(f"window.__preflightCalls.length > {fmt_base}")
        h.click('#c-format button[data-id="7z"]')
        h.wait_js(f"window.__preflightCalls.length > {fmt_base + 1}"
                  f" && window.__preflightCalls[{fmt_base + 1}].status !== null")
        h.wait_js("document.querySelector('#c-outpath').textContent.endsWith('.7z')")
        fmt_before = h.js("document.querySelector('#c-outpath').textContent")
        h.js("window.__releasePreflight(); 'ok'")
        h.wait_js(f"window.__preflightCalls[{fmt_base}].status !== null")
        h.frames()
        fmt_after = h.js("document.querySelector('#c-outpath').textContent")
        check("同代仅切换 format 时旧 preflight 也不能覆盖新结果",
              fmt_after == fmt_before and fmt_after.endswith(".7z"),
              f"before={fmt_before} after={fmt_after}")
        h.click("#c-clear")
        h.wait_js("document.querySelectorAll('#c-list .file-row').length === 0")

        return not check.failed
    finally:
        try:
            for job in h.jobs():
                if job["status"] in ("queued", "running"):
                    h.api("/job/cancel", {"id": job["id"]})
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if not [j for j in h.jobs() if j["status"] in ("queued", "running")]:
                    break
                time.sleep(0.05)
            h.api("/jobs/clear", {})
            h.api("/settings", {"patch": {"serviceAutoStart": False}})
        except Exception:
            pass
        try:
            if h.ws is not None:
                h.ws.close()
        except Exception:
            pass
        try:
            if h.proc is not None:
                h.proc.terminate()
                try:
                    h.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    h.proc.kill()
                    h.proc.wait(timeout=10)
        except Exception:
            pass
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port-file", required=True)
    args = parser.parse_args()
    started = time.monotonic()
    try:
        ok = run(args.port_file)
    except Exception:
        traceback.print_exc()
        ok = False
    print(f"compress-draft: {time.monotonic() - started:.1f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
