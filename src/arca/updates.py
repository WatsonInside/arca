"""7-Zip engine updates.

The app ships a 7-Zip build, so it works offline and identically on Intel and
Apple silicon.  That build ages: 7-Zip ships several releases a year, and a
good part of them are security fixes for archive parsers that run on whatever
the user was sent.  This module lets the app notice a newer release, explain
what changed in Chinese, and install it on demand.

Two deliberate constraints:

* The signed ``.app`` is never touched.  A downloaded engine lives in
  ``~/Library/Application Support/Arca/engines/`` and the app prefers it over
  the bundled one.  Rewriting a binary inside an arm64 bundle invalidates its
  signature (the kernel then kills the process on launch), and it would be
  undone by the next app install anyway.
* Nothing is installed without a click.  ``check()`` only reads two public
  text files; the download starts from ``install()``, which the UI calls from
  the user's confirmation button.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import unquote, urlsplit
import lzma

from . import util

PAGE_URL = "https://www.7-zip.org/download.html"
HISTORY_URL = "https://www.7-zip.org/history.txt"
# T14：官网页面是主源，GitHub Releases API 是兜底；只接受可信 HTTPS 主机
# 与预期资产名，校验值缺失时 fail closed。
API_LATEST = "https://api.github.com/repos/ip7z/7zip/releases/latest"
TRUSTED_HOSTS = {"www.7-zip.org", "7-zip.org", "github.com",
                 "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com"}
UA = "Arca/%s (macOS) engine-updater" % util.APP_VERSION

CACHE_TTL = 6 * 3600          # a launch-time check reuses today's answer
DOWNLOAD_LIMIT = 80 * 1024 * 1024
CHUNK = 96 * 1024

_VERSION_IN_FILE = re.compile(r"7z(\d{2})(\d{2})-mac\.tar\.xz")
_LATEST_HEAD = re.compile(r"Download\s+7-Zip\s+(\d+\.\d+)\s+\((\d{4}-\d{2}-\d{2})\)")
_HISTORY_HEAD = re.compile(r"^(\d+\.\d+)\s+(\d{4}-\d{2}-\d{2})\s*$")
_RUNNING = re.compile(r"7-Zip \(z\) ([0-9.]+)")

# Chinese notes for the releases a user is likely to jump across.  Kept as
# data, not as prose in the UI, so the "what changed" list stays factual and
# can be extended as 7-Zip ships.  Tags drive the highlight summary:
# security > speed > format > feature > fix.
#
# For a version missing here, check() falls back to the official notes (English)
# and says so in the dialog rather than inventing a summary.
NOTES_ZH = {
    "26.05": [("format", "改进 Z 归档与 Mach-O 文件的解包代码。"),
              ("security", "修复若干缺陷与安全漏洞（官方未逐条列出 CVE 编号）。")],
    "26.04": [("fix", "修复若干缺陷与安全漏洞。")],
    "26.03": [("format", "改进 Joliet ISO 与复合文档（Compound）格式支持。"),
              ("security", "修复 CVE-2026-58052：解压特制压缩包时未保留「来自网络」标记。"),
              ("fix", "修复若干缺陷与安全漏洞。")],
    "26.02": [("security", "修复 CVE-2026-14266：XZ 解压堆缓冲区溢出，可被远程执行代码。"),
              ("fix", "修复若干缺陷与安全漏洞。")],
    "26.01": [("feature", "新增 -spo 开关，可指定解压输出目录的命名方式。"),
              ("security", "修复 NTFS / UDF / WIM / SquashFS / AR / UEFI 等格式的多处越界读取"
                           "（共 7 个 CVE）。")],
    "26.00": [("format", "改进 ZIP / CPIO / RAR / QCOW / 复合文档的处理。"),
              ("fix", "修复含稀疏文件的 TAR 归档解压不正确的问题。")],
    "25.01": [("security", "符号链接处理改为更严格的安全检查（CVE-2025-55188），"
                           "解压来源不明的归档时更安全。")],
    "25.00": [("speed", "bzip2 压缩提速 15–40%，deflate（ZIP/gz）提速 1–3%。"),
              ("format", "改进 ZIP / CPIO / FAT 支持。"),
              ("security", "修复符号链接处理漏洞（CVE-2025-11001 / 11002）。")],
    "24.09": [("speed", "LZMA/LZMA2 默认字典变大：同为「标准」级别，768 MB 内存档位下"
                        "字典从 16 MB 提到 32 MB，压缩率更高。"),
              ("feature", "新增 SHA-512、SHA-384、SHA3-256、MD5 校验值计算。"),
              ("format", "改进 APM 与 HFS 支持。")],
    "24.08": [("fix", "修复 24.00–24.07 创建 ZIP 时在归档末尾写入多余零字节的问题"
                      "（旧版解压会提示「归档末尾有数据」）。")],
    "24.07": [("security", "修复畸形 ZSTD 归档导致崩溃的问题（CVE-2024-11477）。")],
    "24.06": [("fix", "修复部分 ZSTD 归档无法解压的问题。")],
    "24.05": [("feature", "新增 -myv / -myfa / -myfd 开关，可控制 7z 归档使用的压缩方法与兼容版本。")],
    "24.04": [("fix", "这一版主要是 Windows 图形界面的改动，命令行引擎没有行为变化。")],
    "24.03": [("feature", "新增 -smemx 开关，可限制解压大字典 RAR 时的内存占用；"
                         "遇到字典超过 4 GB 的 RAR 会先征求许可。"),
              ("format", "新增 RISCV 过滤器，对 RISC-V 可执行文件压缩率更高。"),
              ("fix", "支持使用反斜杠路径的 .sha256 校验文件。")],
    "24.01": [("format", "新增解压 ZSTD 归档（.zst），以及使用 ZSTD 压缩的 ZIP / SquashFS / RPM。"),
              ("format", "支持解压 WinRAR 7.00 创建的大字典（>4 GB）RAR、用 XZ 压缩的 DMG，"
                         "以及 64 KB 以上簇大小的 NTFS 镜像。"),
              ("speed", "rar / cab / wim / zip / gz 解压提速，CRC-32 / CRC-64 / BLAKE2sp 校验提速。")],
}

TAG_TEXT = {
    "security": "安全",
    "speed": "速度",
    "format": "格式",
    "feature": "功能",
    "fix": "修复",
}


class UpdateError(Exception):
    pass


# --------------------------------------------------------------------- versions
def parse_version(text) -> tuple:
    """'26.04' -> (26, 4).  Anything unparsable sorts lowest."""
    m = re.search(r"(\d+)\s*\.\s*(\d+)", str(text or ""))
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)))


def is_newer(latest, current) -> bool:
    return parse_version(latest) > parse_version(current)


# ------------------------------------------------------------------ http helpers
def _get(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.read()
    except urllib.error.HTTPError as exc:
        raise UpdateError("更新服务器返回 HTTP %s" % exc.code) from exc
    except urllib.error.URLError as exc:
        raise UpdateError("无法连接更新服务器：%s" % (exc.reason or exc)) from exc
    except TimeoutError as exc:
        raise UpdateError("连接更新服务器超时") from exc


def _trusted_asset_url(url: str, archive: str) -> bool:
    """只接受可信 HTTPS 主机上、文件名恰为预期资产的 URL。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if (parts.scheme != "https" or parts.username or parts.password
            or parts.port not in (None, 443)):
        return False
    host = (parts.hostname or "").lower()
    if host not in TRUSTED_HOSTS:
        return False
    return unquote(parts.path.rsplit("/", 1)[-1]) == archive


def _fetch_latest_page(timeout: float = 15.0) -> dict:
    """主源：7-Zip 官网下载页（历史行为保持不变）。"""
    html = _get(PAGE_URL, timeout).decode("utf-8", "replace")
    head = _LATEST_HEAD.search(html)
    if not head:
        raise UpdateError("无法从 7-Zip 官网解析版本号")
    version, released = head.group(1), head.group(2)
    if not re.fullmatch(r"\d+\.\d+", version):
        raise UpdateError("7-Zip 官网版本号异常")

    mmnn = version.replace(".", "").zfill(4)
    archive = "7z%s-mac.tar.xz" % mmnn
    urls = []
    canonical = "https://www.7-zip.org/a/%s" % archive
    if _trusted_asset_url(canonical, archive):
        urls.append(canonical)
    for m in re.finditer(r'href="([^"]*?7z(\d{4})-mac\.tar\.xz)"', html):
        if m.group(2) != mmnn:
            continue
        url = m.group(1)
        if not url.startswith("http"):
            url = "https://www.7-zip.org/" + url.lstrip("/")
        if _trusted_asset_url(url, archive) and url not in urls:
            urls.append(url)
    if not urls:
        raise UpdateError("7-Zip 官网没有可信的 macOS 资产链接")
    return {"version": version, "released": released, "url": urls[0],
            "urls": urls, "archive": archive, "page": PAGE_URL,
            "history": HISTORY_URL, "source": "page"}


def _fetch_latest_github(timeout: float = 15.0) -> dict:
    """兜底：GitHub Releases API，并做 tag/资产一致性校验。"""
    try:
        payload = json.loads(_get(API_LATEST, timeout).decode("utf-8", "replace"))
    except ValueError as exc:
        raise UpdateError("Releases API 响应不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise UpdateError("Releases API 响应格式异常")
    if payload.get("draft") or payload.get("prerelease"):
        raise UpdateError("Releases API 返回的是预发布版本")
    tag = str(payload.get("tag_name") or "").lstrip("vV")
    if not re.fullmatch(r"\d+\.\d+", tag):
        raise UpdateError("Releases API 版本号无法解析")
    mmnn = tag.replace(".", "").zfill(4)
    archive = "7z%s-mac.tar.xz" % mmnn
    urls = []
    for asset in payload.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        if os.path.basename(str(asset.get("name") or "")) != archive:
            continue
        url = str(asset.get("browser_download_url") or "")
        if _trusted_asset_url(url, archive) and url not in urls:
            urls.append(url)
    if not urls:
        raise UpdateError("Releases API 缺少预期的 macOS 资产")
    released = str(payload.get("published_at") or "")[:10]
    return {"version": tag, "released": released, "url": urls[0], "urls": urls,
            "archive": archive, "page": PAGE_URL, "history": HISTORY_URL,
            "source": "github"}


def fetch_latest(timeout: float = 15.0) -> dict:
    """主源失败才回退 Releases API；一致性/可信主机校验两侧都做。"""
    primary_error = None
    try:
        return _fetch_latest_page(timeout)
    except UpdateError as exc:
        primary_error = exc
    try:
        return _fetch_latest_github(timeout)
    except UpdateError as exc:
        raise UpdateError("无法获取 7-Zip 更新信息（官网：%s；Releases API：%s）"
                          % (primary_error, exc)) from exc


def _checksum_urls(urls: list[str]) -> list[str]:
    out = []
    for url in urls:
        candidate = url + ".sha256"
        if candidate not in out:
            out.append(candidate)
    return out


def _checksum_from_text(text: bytes, filename: str) -> str | None:
    target = os.path.basename(filename).lower()
    for line in text.decode("utf-8", "replace").splitlines():
        match = re.match(r"^\s*([0-9a-fA-F]{64})\s+(?:[*]\s*)?(.+?)\s*$", line)
        if not match:
            continue
        name = os.path.basename(match.group(2).replace("\\", "/")).lower()
        if name == target:
            return match.group(1).lower()
    return None


def _official_checksum(urls: list[str], filename: str, timeout: float = 15.0,
                       release_api: str | None = None) -> str:
    for url in _checksum_urls(urls):
        try:
            digest = _checksum_from_text(_get(url, timeout), filename)
        except UpdateError:
            continue
        if digest:
            return digest
    match = re.search(r"7z(\d{4})-", filename)
    if release_api is None and match:
        release_api = "https://api.github.com/repos/ip7z/7zip/releases/tags/%s.%s" % (
            match.group(1)[:2], match.group(1)[2:])
    if release_api:
        try:
            payload = json.loads(_get(release_api, timeout).decode("utf-8", "replace"))
            for asset in payload.get("assets", []):
                name = os.path.basename(asset.get("name", "").replace("\\", "/"))
                if name.lower() == filename.lower():
                    digest = str(asset.get("digest") or "")
                    matched = re.fullmatch(r"sha256:([0-9a-f]{64})", digest, re.I)
                    if matched:
                        return matched.group(1).lower()
        except (UpdateError, ValueError, TypeError, AttributeError):
            pass
    raise UpdateError("无法获取校验值")


def verify_download(path: str, urls: list[str], filename: str,
                    timeout: float = 15.0, release_api: str | None = None) -> None:
    expected = _official_checksum(urls, filename, timeout, release_api=release_api)
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    if digest.hexdigest().lower() != expected.lower():
        raise UpdateError("校验不符，已放弃安装")


def fetch_history(timeout: float = 15.0) -> dict:
    """Parse history.txt into {version: {"date": str, "notes": [str]}}."""
    text = _get(HISTORY_URL, timeout).decode("utf-8", "replace")
    out, current = {}, None
    for line in text.splitlines():
        head = _HISTORY_HEAD.match(line.strip())
        if head:
            current = {"date": head.group(2), "notes": []}
            out[head.group(1)] = current
            continue
        if current is None:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if set(stripped) <= {"-"}:
            continue
        if stripped.startswith("- "):
            current["notes"].append(stripped[2:].strip())
        elif current["notes"]:
            # wrapped continuation of the previous bullet
            current["notes"][-1] += " " + stripped
    return out


# ----------------------------------------------------------------- curate notes
def _security_count(history: dict, after: str, upto: str) -> int:
    n = 0
    for ver, item in history.items():
        if after and parse_version(ver) <= parse_version(after):
            continue
        if parse_version(ver) > parse_version(upto):
            continue
        for note in item.get("notes", []):
            if "CVE-" in note:
                n += 1
    return n


def _versions_between(current: str, latest: str, history: dict) -> list[dict]:
    """Newest first, only versions the user is actually moving across."""
    def key(v):
        return parse_version(v)

    picked = [v for v in history if key(current) < key(v) <= key(latest)]
    picked.sort(key=key, reverse=True)
    rows = []
    for ver in picked:
        info = history[ver]
        zh = NOTES_ZH.get(ver) or []
        rows.append({
            "version": ver,
            "date": info.get("date", ""),
            "notes": [{"tag": t, "label": TAG_TEXT.get(t, "变更"), "text": s}
                      for t, s in zh],
            "official": info.get("notes", []),
            "translated": bool(zh),
        })
    return rows


def _highlights(rows: list[dict]) -> list[dict]:
    """One line per kind of change, newest first, most important kind first.

    Exactly one entry per tag: five security bullets in a row would bury the
    speed and format improvements the user actually notices.
    """
    order = ["security", "speed", "format", "feature", "fix"]
    seen, out = set(), []
    for tag in order:
        for row in rows:
            note = next((n for n in row["notes"]
                         if n["tag"] == tag and n["text"] not in seen), None)
            if note is None:
                continue
            seen.add(note["text"])
            out.append(dict(note, version=row["version"]))
            break
    return out[:5]


# ------------------------------------------------------------------------ cache
def _cache_path() -> str:
    return util.state_path("engine-update.json")


def cached() -> dict | None:
    raw = util.read_json(_cache_path(), None)
    if not isinstance(raw, dict) or "checkedAt" not in raw:
        return None
    if time.time() - float(raw["checkedAt"]) > CACHE_TTL:
        return None
    return raw


def check(engine, force: bool = False, timeout: float = 15.0) -> dict:
    """Is there a newer 7-Zip?  Never installs anything."""
    current = getattr(engine, "version", "?") or "?"
    base = {
        "checkedAt": time.time(),
        "current": current,
        "source": "user" if getattr(engine, "zz", "").startswith(util.app_support_dir())
                  else "bundled",
        "available": False,
        "skipped": False,
    }
    if not force:
        hit = cached()
        if hit and hit.get("latest"):
            hit = dict(hit)
            hit["cached"] = True
            if parse_version(hit.get("current")) != parse_version(current):
                hit["current"] = current
            hit["available"] = is_newer(hit["latest"], current)
            return hit

    try:
        latest = fetch_latest(timeout)
        history = fetch_history(timeout)
    except UpdateError as exc:
        return dict(base, ok=False, error=str(exc))

    rows = _versions_between(current, latest["version"], history)
    if not rows and is_newer(latest["version"], current):
        # history.txt not published for this release yet
        rows = [{"version": latest["version"], "date": latest["released"],
                 "notes": [], "official": [], "translated": False}]
    result = dict(base, ok=True, latest=latest["version"], released=latest["released"],
                  url=latest["url"], urls=latest.get("urls") or [latest["url"]],
                  page=latest["page"], historyUrl=latest["history"],
                  versions=rows, highlights=_highlights(rows),
                  securityFixes=_security_count(history, current, latest["version"]),
                  available=is_newer(latest["version"], current))
    util.write_json(_cache_path(), result)
    return result


# --------------------------------------------------------------- installed engine
def engine_record_path() -> str:
    return util.state_path("engine.json")


def installed() -> dict | None:
    """The user-installed engine, if it is still on disk and still runs."""
    rec = util.read_json(engine_record_path(), None)
    if not isinstance(rec, dict):
        return None
    path = rec.get("path") or ""
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return None
    return rec


def engines_dir() -> str:
    path = util.state_path("engines")
    os.makedirs(path, exist_ok=True)
    return path


def forget() -> None:
    """Drop the pointer only — the binary stays on disk for a manual retry."""
    try:
        os.remove(engine_record_path())
    except FileNotFoundError:
        pass


def _run_version(binary: str, timeout: float = 12.0) -> str:
    out = subprocess.run([binary], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, timeout=timeout)
    text = out.stdout.decode("utf-8", "replace")
    m = _RUNNING.search(text)
    return m.group(1) if m else ""


# ------------------------------------------------------------------------ install
class Installer:
    """Download → unpack → verify → switch, with a state the UI can poll."""

    def __init__(self, engine, url, version: str):
        self.engine = engine
        self.url = url if isinstance(url, list) else [url]
        self.urls = list(self.url)
        self.version = version
        self.archive_name = "7z%s-mac.tar.xz" % version.replace(".", "").zfill(4)
        self.lock = threading.Lock()
        self.state = {"phase": "idle", "percent": 0, "received": 0, "total": 0,
                      "message": "", "version": version, "error": "",
                      "url": self.urls[0] if self.urls else ""}
        self._thread = None

    # -- state ---------------------------------------------------------------
    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    def start(self) -> dict:
        with self.lock:
            if self.state["phase"] in ("download", "unpack", "verify"):
                return dict(self.state)
            self.state.update({"phase": "download", "percent": 0, "received": 0,
                               "total": 0, "error": "", "message": "正在连接…"})
        self._thread = threading.Thread(target=self._run, name="arca-engine-update",
                                        daemon=True)
        self._thread.start()
        return self.snapshot()

    # -- work ----------------------------------------------------------------
    def _run(self):
        work = os.path.join(engines_dir(), ".download-%d" % int(time.time()))
        os.makedirs(work, exist_ok=True)
        archive = os.path.join(work, "7z-mac.tar.xz")
        try:
            self._download(archive)
            self._set(phase="verify", message="正在校验下载文件…")
            verify_download(archive, self.urls, self.archive_name, timeout=25)
            self._set(phase="unpack", percent=0, message="正在解包…")
            binary = self._unpack(archive, work)
            self._set(phase="verify", message="正在校验新引擎…")
            got = _run_version(binary)
            if not got:
                raise UpdateError("下载的引擎无法运行，已放弃安装")
            if parse_version(got) < parse_version(self.version):
                raise UpdateError("下载的引擎版本为 %s，低于预期的 %s" % (got, self.version))
            self._set(phase="install", message="正在启用…")
            final = os.path.join(engines_dir(), "7zz-%s" % got)
            tmp = final + ".new"
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass
            shutil.copyfile(binary, tmp)
            os.chmod(tmp, 0o755)
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp, final)
            util.write_json(engine_record_path(), {
                "version": got, "path": final, "installedAt": time.time(),
                "source": self.state.get("url") or (self.urls[0] if self.urls else ""),
                "bytes": os.path.getsize(final),
            })
            # Switch the running process over: the next archive operation uses
            # the new binary, no restart needed.
            self.engine.zz = final
            self.engine.version = got
            self.engine.source = "user"
            self._set(phase="done", percent=100, message="已更新到 %s" % got,
                      version=got)
        except UpdateError as exc:
            self._set(phase="error", error=str(exc), message=str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced to the dialog
            self._set(phase="error", error="更新失败：%s" % exc, message=str(exc))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _download(self, dest: str):
        """Stream the archive, trying each mirror until one actually delivers.

        A read timeout with zero bytes means the host is blocked, not that the
        file is missing — so move on to the next URL instead of failing.
        """
        last = None
        for url in self.urls:
            host = url.split("/")[2] if "://" in url else url
            for attempt in (1, 2):
                self._set(url=url, message="正在连接 %s…" % host, received=0, percent=0)
                try:
                    self._fetch(url, dest)
                    return
                except UpdateError as exc:
                    last = exc
                    got = self.state.get("received") or 0
                    if got > 0 or attempt == 2:
                        break          # partial progress: try the next mirror
        raise last or UpdateError("下载失败")

    def _fetch(self, url: str, dest: str):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=25) as res:
                total = int(res.headers.get("Content-Length") or 0)
                if total > DOWNLOAD_LIMIT:
                    raise UpdateError("下载文件异常大（%s），已中止" % util.human_size(total))
                self._set(total=total)
                done = 0
                t0 = time.time()
                with open(dest, "wb") as fh:
                    while True:
                        chunk = res.read(CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        if done > DOWNLOAD_LIMIT:
                            raise UpdateError("下载体积超出预期，已中止")
                        pct = int(done * 100 / total) if total else 0
                        speed = done / max(0.2, time.time() - t0)
                        self._set(percent=pct, received=done,
                                  message="正在下载 %s / %s · %s/s" % (
                                      util.human_size(done),
                                      util.human_size(total) or "?",
                                      util.human_size(speed)))
                if total and done < total:
                    raise UpdateError("下载不完整（%s / %s）" % (
                        util.human_size(done), util.human_size(total)))
        except urllib.error.HTTPError as exc:
            raise UpdateError("下载失败：HTTP %s" % exc.code) from exc
        except urllib.error.URLError as exc:
            raise UpdateError("下载失败：%s" % (exc.reason or exc)) from exc
        except (TimeoutError, OSError) as exc:
            raise UpdateError("下载超时：%s" % (exc or "网络无响应")) from exc

    def _unpack(self, archive: str, work: str) -> str:
        out = os.path.join(work, "x")
        os.makedirs(out, exist_ok=True)
        # tarfile handles .tar.xz without needing the engine; fall back to 7zz
        # if Python was built without lzma.
        unsafe = 0
        try:
            with tarfile.open(archive, "r:xz") as tf:
                for member in tf.getmembers():
                    name = member.name.lstrip("./")
                    if not name or name.startswith("..") or name.startswith("/"):
                        unsafe += 1
                        continue
                    if member.isreg() or member.isdir():
                        tf.extract(member, out, filter="tar")
            if unsafe:
                # Nothing outside the temp dir is ever written; say so plainly
                # instead of failing later with "no engine found".
                raise UpdateError("下载包包含不安全的路径（%d 项），已放弃安装" % unsafe)
        except (tarfile.TarError, lzma.LZMAError, EOFError):
            self._set(message="正在用内置引擎解包…")
            p = subprocess.run([self.engine.zz, "x", "-y", "-bso0", "-bsp0",
                                "-o" + out, archive],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=180)
            if p.returncode != 0:
                raise UpdateError("解包下载文件失败")

        found = None
        for root, _dirs, files in os.walk(out):
            for name in files:
                if name in ("7zz", "7z"):
                    cand = os.path.join(root, name)
                    if os.path.getsize(cand) > 1024 * 1024:
                        found = cand
                        break
            if found:
                break
        if not found:
            raise UpdateError("下载包中没有找到 7zz 引擎")
        os.chmod(found, 0o755)
        return found


def make_installer(engine, url, version: str) -> Installer:
    return Installer(engine, url, version)
