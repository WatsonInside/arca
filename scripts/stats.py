"""Aggregate anonymous usage pings from COS access logs into stats.json.

The app reports one path-encoded GET per install and one per active day
(see src/arca/stats.py); COS access logging is the only storage involved.
This script pulls those logs, aggregates them, and uploads a versioned JSON
(documented in docs/STATS-API.md) that the website renders.

Usage:
  build/venv/bin/python -S -E scripts/stats.py              # fetch + upload
  ... scripts/stats.py --no-upload                          # local only
  ... scripts/stats.py --from-dir build/coslogs             # parse existing files
  ... scripts/stats.py --sample                             # fixture for testing

Credentials live in build/cos.json (gitignored):
  {"region": "ap-hongkong", "bucket": "name-125xxxxxx",
   "secretId": "...", "secretKey": "...",
   "logPrefix": "cos-access-log/", "logBucket": "",
   "apiKey": "events/api/stats.json",
   "uploadBuckets": [{"region": "...", "bucket": "..."}]}
"bucket" is the bucket receiving /ping/v1 requests with logging enabled;
"logBucket" defaults to it; "apiKey" is where stats.json is uploaded and must
sit next to the website's stats page (it fetches "api/stats.json" relative
to /events/stats.html); "uploadBuckets" (optional) uploads to mirrors too.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
LOGS_DIR = os.path.join(BUILD, "coslogs")
OUT_PATH = os.path.join(BUILD, "stats", "stats.json")
CONFIG_PATH = os.path.join(BUILD, "cos.json")
STATE_PATH = os.path.join(LOGS_DIR, ".downloaded.json")
DAYS_CAP = 400
TZ = dt.timezone(dt.timedelta(hours=8))

MONTHS = {m: i + 1 for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"))}

PING_RE = re.compile(r"/ping/v1/(install|ping)/([0-9A-Za-z._-]+)/([0-9A-Za-z._-]+)/([0-9a-f]{32})")
TIME_RES = (
    # [23/Oct/2026:15:26:28 +0000]  (Apache/COS style)
    (re.compile(r"\[(\d{2})/([A-Z][a-z]{2})/(\d{4}):(\d{2}):(\d{2}):(\d{2})\s+([+-]\d{4})\]"), 0),
    # 2026-10-23T15:26:28Z / +00:00
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:Z|([+-]\d{2}):?(\d{2}))"), 1),
    # bare date, no time: assume UTC midnight
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), 2),
)


def _read_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as exc:
        raise SystemExit(f"缺少 build/cos.json（{exc}）——按文件头注释里的格式创建后重试，"
                         "或用 --from-dir / --sample 离线跑")
    for key in ("region", "bucket", "secretId", "secretKey"):
        if not cfg.get(key):
            raise SystemExit(f"build/cos.json 缺少 {key}")
    return cfg


# ---------------------------------------------------------------- COS client
# XML API 签名（stdlib-only，与项目脚本约定一致）：q-sign-algorithm=sha1 方案，
# header 不参与签名（q-header-list 留空），只签 query 参数。

def _cos_sign(secret_id: str, secret_key: str, key_time: str, method: str,
              path: str, params: dict) -> dict:
    def canonical(d: dict) -> str:
        parts = []
        for k in sorted(d):
            parts.append(urllib.parse.quote(k, safe="-_.~").lower() + "="
                         + urllib.parse.quote(str(d[k]), safe="-_.~"))
        return "&".join(parts)

    sign_key = hmac.new(secret_key.encode(), key_time.encode(), hashlib.sha1).hexdigest()
    # COS 的 HttpString：方法名小写，四个字段各自以 \n 结尾（空 headers 也占一行，
    # 已用服务端返回的 FormatString 哈希逐字对齐验证）。
    http_string = "".join(part + "\n" for part in
                          [method.lower(), path, canonical(params), ""])
    string_to_sign = "\n".join(["sha1", key_time,
                                hashlib.sha1(http_string.encode()).hexdigest(), ""])
    signature = hmac.new(sign_key.encode(), string_to_sign.encode(), hashlib.sha1).hexdigest()
    return {
        "q-sign-algorithm": "sha1", "q-ak": secret_id,
        "q-sign-time": key_time, "q-key-time": key_time,
        "q-header-list": "", "q-url-param-list": ";".join(sorted(k.lower() for k in params)),
        "q-signature": signature,
    }


def _cos_request(cfg: dict, bucket: str, method: str, key: str, params: dict,
                 body: bytes = b"", headers: dict | None = None) -> bytes:
    host = f"{bucket}.cos.{cfg['region']}.myqcloud.com"
    # 签名里的 URI 是完整路径：桶级操作是 "/"，对象操作是 "/<key>"。
    path = "/" + (key or "").lstrip("/")
    now = int(time.time())
    key_time = f"{now - 60};{now + 600}"
    auth = _cos_sign(cfg["secretId"], cfg["secretKey"], key_time, method, path, params)
    query = dict(params, **auth)
    url = f"https://{host}/{key.lstrip('/')}" + ("?" + urllib.parse.urlencode(query) if query else "")
    request = urllib.request.Request(url, data=body if method == "PUT" else None, method=method)
    request.add_header("User-Agent", "Arca-Stats-Tool")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def list_log_keys(cfg: dict, prefix: str) -> list[str]:
    keys, marker = [], ""
    bucket = cfg.get("logBucket") or cfg["bucket"]
    while True:
        params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if marker:
            params["marker"] = marker
        xml = _cos_request(cfg, bucket, "GET", "", params).decode("utf-8", "replace")
        batch = re.findall(r"<Key>([^<]+)</Key>", xml)
        keys.extend(batch)
        if "<IsTruncated>true</IsTruncated>" not in xml or not batch:
            return keys
        marker = batch[-1]


def download_logs(cfg: dict) -> int:
    os.makedirs(LOGS_DIR, exist_ok=True)
    state = {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception:
        pass
    keys = list_log_keys(cfg, cfg.get("logPrefix") or "")
    fetched = 0
    for key in keys:
        if state.get(key):
            continue
        bucket = cfg.get("logBucket") or cfg["bucket"]
        data = _cos_request(cfg, bucket, "GET", key, {})
        # 文件名仅用于本地缓存：转成安全形式，gzip 的加后缀防二次解压判断错乱。
        dest = re.sub(r"[^0-9A-Za-z._-]", "_", key.rsplit("/", 1)[-1] or "root")
        if data[:2] == b"\x1f\x8b":
            dest += ".gz"
        with open(os.path.join(LOGS_DIR, dest), "wb") as fh:
            fh.write(data)
        state[key] = dest
        fetched += 1
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    return fetched


def upload_stats(cfg: dict, data: bytes) -> None:
    # 默认键与统计页 /events/stats.html 同目录：页面用相对路径 fetch("api/stats.json")。
    # 网站是「上海部署源 → 香港源站（Cloudflare 回源）」双桶镜像，stats.json 两边都写，
    # 避免被镜像同步当成漂移删掉。
    key = cfg.get("apiKey") or "events/api/stats.json"
    targets = cfg.get("uploadBuckets") or [{"region": cfg["region"], "bucket": cfg["bucket"]}]
    for target in targets:
        scoped = dict(cfg, region=target["region"], bucket=target["bucket"])
        _cos_request(scoped, target["bucket"], "PUT", key, {}, body=data, headers={
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "public, max-age=300",
        })
        print(f"已上传 → https://{target['bucket']}.cos.{target['region']}.myqcloud.com/{key}")


# ------------------------------------------------------------------ parsing

def _line_datetime(line: str):
    for pattern, kind in TIME_RES:
        match = pattern.search(line)
        if not match:
            continue
        try:
            if kind == 0:
                day, mon, year, hh, mm, ss, zone = match.groups()
                offset = dt.timedelta(hours=int(zone[:3]), minutes=int(zone[0] + zone[3:]))
                when = dt.datetime(int(year), MONTHS.get(mon, 1), int(day),
                                   int(hh), int(mm), int(ss), tzinfo=dt.timezone(offset))
            else:
                parts = match.groups()
                year, month, day = parts[0], parts[1], parts[2]
                if kind == 1:
                    hh, mm, ss = parts[3], parts[4], parts[5]
                    zh = parts[6] if len(parts) > 6 else None
                    zm = parts[7] if len(parts) > 7 else None
                else:
                    hh = mm = ss = "0"
                    zh = zm = None
                hours = int(zh) if zh else 0
                minutes = int(zm) if zm else 0
                if hours < 0:
                    minutes = -minutes
                offset = dt.timezone(dt.timedelta(hours=hours, minutes=minutes))
                when = dt.datetime(int(year), int(month), int(day),
                                   int(hh), int(mm), int(ss), tzinfo=offset)
            return when.astimezone(TZ).date().isoformat()
        except ValueError:
            continue
    return None


def parse_logs(paths) -> list[tuple]:
    """Return [(date, event, version, platform, iid)] from raw log files.

    Column-agnostic on purpose: COS log layout has changed between docs and
    regions, so fields are located by the ping path itself and a tolerant
    timestamp scan instead of fixed indexes.
    """
    records = []
    for path in paths:
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            text = raw.decode("utf-8", "replace")
        except Exception as exc:
            print(f"  跳过 {os.path.basename(path)}: {exc}")
            continue
        for line in text.splitlines():
            match = PING_RE.search(line)
            if not match:
                continue
            date = _line_datetime(line)
            if date:
                records.append((date,) + match.groups())
    return records


# -------------------------------------------------------------- aggregation

def aggregate(records) -> dict:
    installs_by_day: dict[str, set] = {}
    active_by_day: dict[str, set] = {}
    pings_by_day: dict[str, int] = {}
    install_events: dict[str, int] = {}
    platform_devices: dict[str, set] = {}
    last_seen: dict[str, tuple] = {}  # iid -> (date, version, platform)

    for date, event, version, platform, iid in records:
        active_by_day.setdefault(date, set()).add(iid)
        pings_by_day[date] = pings_by_day.get(date, 0) + 1
        if event == "install":
            installs_by_day.setdefault(date, set()).add(iid)
            install_events[version] = install_events.get(version, 0) + 1
        prev = last_seen.get(iid)
        if prev is None or date >= prev[0]:
            last_seen[iid] = (date, version, platform)
        platform_devices.setdefault(platform, set()).add(iid)

    dates = sorted(set(active_by_day) | set(installs_by_day))
    if not dates:
        return _summary([], {}, {}, {}, {}, last_seen, dates)

    start = dt.date.fromisoformat(dates[0])
    end = dt.date.fromisoformat(dates[-1])
    days = []
    day = start
    while day <= end:
        key = day.isoformat()
        days.append({
            "date": key,
            "installs": len(installs_by_day.get(key, ())),
            "active": len(active_by_day.get(key, ())),
            "pings": pings_by_day.get(key, 0),
        })
        day += dt.timedelta(days=1)

    cutoff = (end - dt.timedelta(days=29)).isoformat()
    version_active: dict[str, set] = {}
    platform_active: dict[str, set] = {}
    for iid, (date, version, platform) in last_seen.items():
        if date >= cutoff:
            version_active.setdefault(version, set()).add(iid)
            platform_active.setdefault(platform, set()).add(iid)

    return _summary(days, install_events, platform_devices, version_active,
                    platform_active, last_seen, dates)


def _summary(days, install_events, platform_devices, version_active,
             platform_active, last_seen, dates) -> dict:
    def dist(counts: dict, active: dict) -> list[dict]:
        rows = []
        for key, installs in counts.items():
            rows.append({"id": key, "installs": installs,
                         "active30d": len(active.get(key, ()))})
        rows.sort(key=lambda r: (-r["installs"], r["id"]))
        return rows

    def count(days_list, field, offset):
        if not days_list:
            return 0
        cutoff = (dt.date.fromisoformat(days_list[-1]["date"]) - dt.timedelta(days=offset)).isoformat()
        return sum(day[field] for day in days_list if day["date"] >= cutoff)

    return {
        "schema": "arca-stats/1",
        "generatedAt": dt.datetime.now(TZ).isoformat(timespec="seconds"),
        "window": {"first": dates[0] if dates else "", "last": dates[-1] if dates else "",
                   "timezone": "Asia/Shanghai"},
        "totals": {
            "installs": len(last_seen),
            "activeToday": days[-1]["active"] if days else 0,
            "activeYesterday": days[-2]["active"] if len(days) > 1 else 0,
            "active7d": count(days, "active", 6),
            "active30d": count(days, "active", 29),
            "pings": sum(day["pings"] for day in days),
        },
        # 完整日序列（含零填充天），最多保留最近 DAYS_CAP 天。
        "days": days[-DAYS_CAP:],
        "versions": dist(install_events, version_active),
        "platforms": dist({p: len(iids) for p, iids in platform_devices.items()},
                          platform_active),
    }


# --------------------------------------------------------------------- main

def write_sample() -> None:
    os.makedirs(LOGS_DIR, exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc)
    lines = []
    for i in range(7):
        when = today - dt.timedelta(days=i)
        stamp = when.strftime("[%d/%b/%Y:%H:%M:%S +0000]")
        for n in range(3 + i):
            event = "install" if i == 6 or n == 0 else "ping"
            version = "1.0.7" if n % 2 else "1.0.6"
            platform = "mac15-arm64" if n % 3 else "mac14-x86_64"
            iid = hashlib.sha1(f"{i}-{n}".encode()).hexdigest()[:32]
            lines.append(f'{stamp} GET /ping/v1/{event}/{version}/{platform}/{iid} '
                         f'"-" 404 - 12 "-" "Arca-Stats" -')
    path = os.path.join(LOGS_DIR, "sample.log")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"已生成样例日志 {path}（{len(lines)} 条）")


def print_summary(summary: dict) -> None:
    totals = summary["totals"]
    window = summary["window"]
    print(f"窗口 {window['first']} → {window['last']}（东八区）")
    print(f"  总安装 {totals['installs']} · 今日活跃 {totals['activeToday']} · "
          f"昨日 {totals['activeYesterday']} · 7 天 {totals['active7d']} · "
          f"30 天 {totals['active30d']} · 心跳 {totals['pings']}")
    for title, key in (("版本", "versions"), ("平台", "platforms")):
        for row in summary[key][:5]:
            print(f"  {title} {row['id']}: 安装 {row['installs']} · 30 天活跃 {row['active30d']}")
    days = summary["days"][-14:]
    if days:
        chart = " ".join(f"{day['active']:>2}" for day in days)
        print(f"  近 14 天活跃：{chart}")


def _log_files(directory: str) -> list[str]:
    # sample.log 只属于 --sample 的离线验证，不能混进真实聚合。
    return [os.path.join(directory, name) for name in sorted(os.listdir(directory))
            if not name.startswith(".") and name != "sample.log"]


def main(argv) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    upload = "--no-upload" not in argv
    if "--sample" in argv:
        write_sample()
        return 0

    if "--from-dir" in argv:
        directory = args[0] if args else LOGS_DIR
        paths = _log_files(directory) or [os.path.join(directory, "sample.log")]
    else:
        cfg = _read_config()
        fetched = download_logs(cfg)
        print(f"拉取日志：{fetched} 个新文件")
        paths = _log_files(LOGS_DIR)

    records = parse_logs(paths)
    summary = aggregate(records)
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    payload = json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8")
    with open(OUT_PATH, "wb") as fh:
        fh.write(payload)
    print_summary(summary)
    print(f"已写出 {OUT_PATH}（{len(records)} 条记录）")

    if upload and "--from-dir" not in argv:
        upload_stats(cfg, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
