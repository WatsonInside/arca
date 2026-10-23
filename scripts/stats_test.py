"""Usage stats: client state machine and log aggregation.

Two things must never break: the client sends at most one install and one
ping per day (and nothing when disabled/headless), and the aggregator's
day bucketing follows Asia/Shanghai regardless of the log timestamp's
timezone.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from arca import stats as appstats  # noqa: E402
import stats as tool  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok    " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))


# ---------------------------------------------------------------- client

class _Recorder:
    def __init__(self):
        self.sent = []


class _FakeThread:
    """Run the ping synchronously so assertions are deterministic."""

    def __init__(self, *, target, args, daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _settings(enabled=True):
    values = {"anonymousStats": enabled}

    class S:
        def get(self, key, default=None):
            return values.get(key, default)

    return S()


def client_tests(tmp: str) -> None:
    os.environ["HOME"] = tmp
    state_path = os.path.join(tmp, "Library", "Application Support", "Arca", "stats.json")
    recorder = _Recorder()
    appstats._send = lambda event, iid: recorder.sent.append(event)
    appstats.threading = types.SimpleNamespace(Thread=_FakeThread)

    appstats.maybe_ping(_settings(), headless=True)
    check("headless 不上报", recorder.sent == [] and not os.path.exists(state_path))

    appstats.maybe_ping(_settings(enabled=False))
    check("关闭后不上报", recorder.sent == [] and not os.path.exists(state_path))

    appstats.maybe_ping(_settings())
    state = json.load(open(state_path, encoding="utf-8"))
    check("首次启动发 install", recorder.sent == ["install"], str(recorder.sent))
    check("状态含 iid 与日期", len(state.get("iid", "")) == 32
          and state.get("installedAt") == state.get("lastPing"))

    appstats.maybe_ping(_settings())
    check("同日第二次启动不重发", recorder.sent == ["install"], str(recorder.sent))

    # 直接改状态文件模拟“过了一天”。
    state["lastPing"] = "2000-01-01"
    json.dump(state, open(state_path, "w", encoding="utf-8"))
    appstats.maybe_ping(_settings())
    check("次日首启动发 ping", recorder.sent == ["install", "ping"], str(recorder.sent))

    url = appstats._url("install", "a" * 32)
    check("URL 形状", url == appstats.ENDPOINT + "/install/" + appstats.util.APP_VERSION
          + "/" + appstats._platform() + "/" + "a" * 32, url)
    check("路径段消毒", appstats._safe("../../etc p@ss") == "..-..-etc-p-ss",
          appstats._safe("../../etc p@ss"))


# ------------------------------------------------------------ aggregation

AGG_LINE = ('[23/Oct/2026:17:00:00 +0000] GET /ping/v1/install/1.0.7/mac15-arm64/{iid} '
            '"-" 404 - 12 "-" "Arca-Stats" -')


def aggregation_tests(tmp: str) -> None:
    os.makedirs(tmp, exist_ok=True)
    log = os.path.join(tmp, "u.log")
    a, b = "a" * 32, "b" * 32
    with open(log, "w", encoding="utf-8") as fh:
        fh.write("header line without pings\n")
        fh.write(AGG_LINE.format(iid=a) + "\n")                 # UTC 17点 → 东八区次日
        fh.write(AGG_LINE.replace("install", "ping").format(iid=a) + "\n")  # 同日同设备 → 活跃去重
        fh.write(AGG_LINE.replace("17:00:00", "01:00:00").format(iid=b) + "\n")
        fh.write(AGG_LINE.replace("17:00:00", "02:00:00").replace("install", "ping").format(iid=b) + "\n")

    records = tool.parse_logs([log])
    dates = sorted({r[0] for r in records})
    check("UTC 17 点落到东八区次日", dates == ["2026-10-23", "2026-10-24"], str(dates))

    summary = tool.aggregate(records)
    totals = summary["totals"]
    check("schema 版本", summary["schema"] == "arca-stats/1")
    check("去重安装 2 台", totals["installs"] == 2, str(totals))
    check("24 日活跃 1", summary["days"][-1]["active"] == 1)
    check("日序列零填充且无空洞",
          [d["date"] for d in summary["days"]] == ["2026-10-23", "2026-10-24"])
    check("版本分布", summary["versions"] == [
        {"id": "1.0.7", "installs": 2, "active30d": 2}], str(summary["versions"]))
    check("平台分布", summary["platforms"] == [
        {"id": "mac15-arm64", "installs": 2, "active30d": 2}], str(summary["platforms"]))

    empty = tool.aggregate([])
    check("空数据也是合法 JSON 结构",
          empty["days"] == [] and empty["totals"]["installs"] == 0
          and empty["window"]["first"] == "")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        client_tests(os.path.join(tmp, "home"))
        aggregation_tests(os.path.join(tmp, "logs"))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
