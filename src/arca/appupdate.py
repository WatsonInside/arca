"""Conservative, same-volume application updates from a configured GitHub release."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
import uuid

from . import updates, util

MAX_DOWNLOAD = 1024 * 1024 * 1024
MAX_UNPACK = 4 * MAX_DOWNLOAD


class UpdateError(Exception):
    pass


def valid_repo(value):
    return isinstance(value, str) and (not value or bool(re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", value))
        and value.split('/')[1] not in ('.', '..'))


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise UpdateError("应用版本格式无效")
    return tuple(int(n) for n in value.split('.'))


def is_newer(candidate, current):
    a, b = version(candidate), version(current)
    return updates.is_newer(candidate, current) or (
        updates.parse_version(candidate) == updates.parse_version(current) and a[2] > b[2])


def bundle_path():
    for parent in Path(__file__).absolute().parents:
        if parent.suffix == '.app' and (parent / 'Contents/Info.plist').is_file():
            return parent
    return None


def bundle_version(bundle):
    if bundle is None:
        raise UpdateError("应用更新仅在 Arca.app 中可用")
    with open(Path(bundle) / 'Contents/Info.plist', 'rb') as stream:
        info = plistlib.load(stream)
    value = info.get('CFBundleShortVersionString')
    version(value)
    if info.get('CFBundleIdentifier') != util.BUNDLE_ID:
        raise UpdateError("应用标识不匹配")
    executable = Path(bundle) / 'Contents/MacOS/Arca'
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise UpdateError("应用可执行文件缺失")
    return value


class _GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        from urllib.parse import urlsplit
        parsed = urlsplit(newurl)
        host = parsed.hostname or ''
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.port not in (None, 443)
                or not (host in ('github.com', 'api.github.com')
                        or host.endswith('.githubusercontent.com'))):
            raise UpdateError("更新下载重定向地址不可信")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def _open(url):
    return urllib.request.build_opener(_GitHubRedirect()).open(urllib.request.Request(url, headers={
        'User-Agent': 'Arca-AppUpdate', 'Accept': 'application/vnd.github+json'}), timeout=30)


_LAST_META = {'etag': '', 'last_modified': ''}


def _read(url, limit):
    with _open(url) as response:
        try:
            _LAST_META['etag'] = response.headers.get('ETag') or ''
            _LAST_META['last_modified'] = response.headers.get('Last-Modified') or ''
        except (AttributeError, OSError):
            pass
        data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("更新响应过大")
    return data


def _open_conditional(url, etag, last_modified):
    """条件请求；304 时返回 None（调用方复用本地缓存）。"""
    headers = {'User-Agent': 'Arca-AppUpdate', 'Accept': 'application/vnd.github+json'}
    if etag:
        headers['If-None-Match'] = etag
    if last_modified:
        headers['If-Modified-Since'] = last_modified
    opener = urllib.request.build_opener(_GitHubRedirect())
    try:
        return opener.open(urllib.request.Request(url, headers=headers), timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None
        raise


def asset_url(repo, tag, name):
    from urllib.parse import quote
    return 'https://github.com/' + repo + '/releases/download/' + quote(tag, safe='') + '/' + name


def fetch_digest(asset, sidecar, repo, tag, name):
    if sidecar is not None:
        if sidecar.get('browser_download_url') != asset_url(repo, tag, name + '.sha256'):
            raise UpdateError("摘要地址不匹配")
        text = _read(sidecar['browser_download_url'], 65536).decode('utf-8')
        for line in text.splitlines():
            match = re.fullmatch(r'\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*', line)
            if match and match[2].replace('\\', '/').split('/')[-1].lower() == name.lower():
                return match[1].lower()
        raise UpdateError("摘要文件无效或文件名不匹配")
    digest = asset.get('digest', '')
    if isinstance(digest, str) and re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest):
        return digest[7:].lower()
    raise UpdateError("缺少可信 SHA256 摘要，已放弃更新")


def verify_signature(bundle):
    # A3: replace this slot with codesign verification after signing is deployed.
    return True


def unpack(archive, directory):
    root = Path(directory)
    with tarfile.open(archive, 'r:xz') as tar:
        members = tar.getmembers()
        if len(members) > 100000 or sum(m.size for m in members) > MAX_UNPACK:
            raise UpdateError("更新包解包大小超限")
        paths = set()
        links = []
        for member in members:
            p = PurePosixPath(member.name)
            if (p.is_absolute() or '..' in p.parts or '\\' in member.name
                    or not p.parts or p.parts[0] != 'Arca.app' or p in paths):
                raise UpdateError("更新包包含不安全路径")
            paths.add(p)
            if member.issym():
                links.append(member)
            elif not (member.isdir() or member.isfile()):
                raise UpdateError("更新包包含不安全文件类型")
        link_paths = {PurePosixPath(m.name) for m in links}
        for member in members:
            p = PurePosixPath(member.name)
            if any(parent in link_paths for parent in p.parents):
                raise UpdateError("更新包试图通过符号链接写入")
            if member.issym():
                target = member.linkname
                if not target or PurePosixPath(target).is_absolute() or '\\' in target:
                    raise UpdateError("更新包符号链接不安全")
                resolved = (root / member.name).parent.joinpath(target).resolve()
                if not resolved.is_relative_to(root / 'Arca.app'):
                    raise UpdateError("更新包符号链接逃逸")
                continue
            destination = root / member.name
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, open(destination, 'xb') as output:
                    shutil.copyfileobj(source, output)
                destination.chmod(member.mode & 0o777)
        for member in links:
            os.symlink(member.linkname, root / member.name)
        for member in links:
            if not (root / member.name).resolve().is_relative_to(root / 'Arca.app'):
                raise UpdateError("更新包符号链接链逃逸")
    return root / 'Arca.app'


def _write_marker(path, data):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    try:
        with open(temporary, 'x', encoding='utf-8') as output:
            json.dump(data, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)



def relaunch_script(pid: int, bundle, log) -> str:
    """Shell program that re-opens ``bundle`` once ``pid`` is gone.

    `open` exiting 0 is not proof the app is up: right after a bundle swap
    LaunchServices can accept the request and still fail to launch, so each
    attempt is followed by a liveness check, then a bundle-id attempt, then a
    direct exec of the binary as the last resort.  Everything is appended to
    ``log`` because this runs after the previous process is gone.
    """
    q = shlex.quote
    path = Path(bundle)
    exe = path / 'Contents' / 'MacOS' / 'Arca'
    return (
        f'exec >>{q(str(log))} 2>&1; '
        f'echo "--- $(date "+%Y-%m-%d %H:%M:%S") relaunch bundle={q(str(path))} oldpid={pid}"; '
        # Bounded wait: a zombie still answers kill -0.
        f'n=0; while kill -0 {pid} 2>/dev/null && [ $n -lt 30 ]; do sleep 0.2; n=$((n+1)); done; '
        f'alive() {{ /usr/bin/pgrep -f {q(str(exe))} >/dev/null 2>&1; }}; '
        'for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do '
        f'  if /usr/bin/open -n {q(str(path))}; then '
        '    sleep 1; '
        # Twice: a crash-on-launch process is briefly visible and would
        # otherwise be mistaken for a successful restart.
        '    if alive; then sleep 1; if alive; then echo "open ok (try $i)"; exit 0; fi; '
        '      echo "opened then died (try $i)"; '
        '    else echo "open returned 0 but nothing is running (try $i)"; fi; '
        '  else '
        '    echo "open failed (try $i)"; '
        '  fi; '
        '  sleep 0.5; '
        'done; '
        'echo "trying bundle id com.arca.app"; '
        'if /usr/bin/open -b com.arca.app; then sleep 1; if alive; then sleep 1; if alive; then echo "open -b ok"; exit 0; fi; fi; fi; '
        f'echo "starting the binary directly: {q(str(exe))}"; '
        f'exec {q(str(exe))}'
    )


APPUPDATE_CACHE_TTL = 3600
# 明确新鲜度策略：安装前若检查结果早于该窗口，必须重新在线校验。
APPUPDATE_APPLY_MAX_AGE = 600


def _app_cache_file():
    return Path(util.state_path('appupdate-cache.json'))


def _load_repo_cache(repo):
    raw = util.read_json(str(_app_cache_file()), None)
    if not isinstance(raw, dict) or raw.get('schema') != 1:
        return None
    repos = raw.get('repos')
    if not isinstance(repos, dict):
        return None
    entry = repos.get(repo)
    if not isinstance(entry, dict) or not isinstance(entry.get('data'), dict):
        return None
    return {'ts': float(entry.get('ts') or 0),
            'etag': str(entry.get('etag') or ''),
            'last_modified': str(entry.get('last_modified') or ''),
            'data': entry['data']}


def _save_repo_cache(repo, ts, etag, last_modified, data):
    raw = util.read_json(str(_app_cache_file()), None)
    if not isinstance(raw, dict) or raw.get('schema') != 1 or not isinstance(raw.get('repos'), dict):
        raw = {'schema': 1, 'repos': {}}
    raw['repos'][repo] = {'ts': float(ts), 'etag': etag or '',
                          'last_modified': last_modified or '', 'data': data}
    util.write_json(str(_app_cache_file()), raw)


class Updater:
    def __init__(self, settings):
        self.settings = settings
        self.bundle = bundle_path()
        self.current = ''
        self.error = ''
        try:
            self.current = bundle_version(self.bundle)
        except Exception as exc:
            self.error = str(exc)
        self.marker = Path(util.state_path('appupdate-recovery.json'))
        self.lock = threading.RLock()
        self.cached = None
        self.checked_at = 0
        self.installer = None
        self.recovery = ''
        self.stale = False

    def check(self, force=False):
        with self.lock:
            repo = self.settings.get('appUpdateRepo') or ''
            base = {'ok': True, 'enabled': bool(repo), 'current': self.current,
                    'available': False, 'repo': repo}
            self.stale = False
            if not repo:
                self.cached = None
                return self._snapshot(base)
            if not force and self.installer and self.installer.snapshot()['phase'] != 'error':
                return self._snapshot(dict(self.cached) if self.cached and self.cached.get('repo') == repo else base)
            now = time.time()
            if (not force and self.cached and self.checked_at and self.cached.get('repo') == repo
                    and now - self.checked_at < APPUPDATE_CACHE_TTL):
                return self._snapshot(dict(self.cached))
            disk = _load_repo_cache(repo)
            if not force and disk and now - disk['ts'] < APPUPDATE_CACHE_TTL:
                self.cached = dict(disk['data'])
                self.checked_at = disk['ts']
                return self._snapshot(self._fresh_snapshot(base, disk['data']))
            self.cached = None
            try:
                if not valid_repo(repo):
                    raise UpdateError("GitHub 仓库格式应为 owner/repo")
                if self.error:
                    raise UpdateError(self.error)
                data, etag, last_modified = self._fetch_release(repo, disk)
                if data is None and disk:          # 304：条件请求命中本地缓存
                    data = dict(disk['data'])
                    _save_repo_cache(repo, now, etag, last_modified, data)
                    self.cached = data
                    self.checked_at = now
                    return self._snapshot(self._fresh_snapshot(base, data))
                data['repo'] = repo
                _save_repo_cache(repo, now, etag, last_modified, data)
                self.cached = data
                self.checked_at = now
                return self._snapshot(self._fresh_snapshot(base, data))
            except Exception as exc:  # noqa: BLE001
                if disk:
                    # 离线/403/429：只返回明确标记的 stale 数据供展示，禁止安装。
                    self.cached = dict(disk['data'])
                    self.checked_at = disk['ts']
                    self.stale = True
                    snapshot = self._fresh_snapshot(base, self.cached)
                    snapshot['error'] = "%s（展示的是 %s 的缓存）" % (
                        exc, time.strftime('%m-%d %H:%M', time.localtime(disk['ts'])))
                    return self._snapshot(snapshot)
                base.update(ok=False, error=str(exc))
            return self._snapshot(base)

    def _fresh_snapshot(self, base, data):
        merged = dict(base)
        for key in ('latest', 'available', 'url', 'urls', 'digest', 'highlights',
                    'released', 'page', 'historyUrl', 'versions', 'securityFixes'):
            if key in data:
                merged[key] = data[key]
        merged['ok'] = True
        merged['available'] = bool(merged.get('latest')) and is_newer(merged['latest'], self.current)
        return merged

    def _fetch_release(self, repo, disk):
        url = 'https://api.github.com/repos/' + repo + '/releases/latest'
        limit = 2 * 1024 * 1024
        if disk and (disk.get('etag') or disk.get('last_modified')):
            response = _open_conditional(url, disk['etag'], disk['last_modified'])
            if response is None:
                return None, disk['etag'], disk['last_modified']
            try:
                etag = response.headers.get('ETag') or disk['etag']
                last_modified = response.headers.get('Last-Modified') or disk['last_modified']
                raw = response.read(limit + 1)
            finally:
                response.close()
            if len(raw) > limit:
                raise UpdateError("更新响应过大")
        else:
            raw = _read(url, limit)
            etag, last_modified = _LAST_META['etag'], _LAST_META['last_modified']
        try:
            release = json.loads(raw)
        except ValueError as exc:
            raise UpdateError("更新响应不是有效 JSON") from exc
        return self._parse_release(repo, release), etag, last_modified

    def _parse_release(self, repo, release):
        if not isinstance(release, dict):
            raise UpdateError("更新响应格式异常")
        tag = str(release.get('tag_name') or '')
        latest = tag[1:] if tag.startswith('v') else tag
        version(latest)
        if release.get('draft') or release.get('prerelease'):
            raise UpdateError("不安装预发布版本")
        name = f'Arca-{latest}-mac.tar.xz'
        assets = release.get('assets', [])
        if not isinstance(assets, list):
            raise UpdateError("更新资产格式异常")
        matching = [a for a in assets if isinstance(a, dict) and a.get('name') == name]
        sidecars = [a for a in assets if isinstance(a, dict) and a.get('name') == name + '.sha256']
        if len(matching) != 1 or len(sidecars) > 1:
            raise UpdateError("缺少唯一的应用更新资产")
        asset = matching[0]
        url = asset_url(repo, tag, name)
        if asset.get('browser_download_url') != url:
            raise UpdateError("应用更新地址不匹配")
        digest = fetch_digest(asset, sidecars[0] if sidecars else None, repo, tag, name)
        return {'ok': True, 'latest': latest, 'available': is_newer(latest, self.current),
                'url': url, 'digest': digest,
                'highlights': str(release.get('body') or '')[:10000],
                'released': str(release.get('published_at') or '')[:10]}

    def _snapshot(self, info):
        info['skipped'] = info.get('latest') == self.settings.get('appSkipVersion')
        info['install'] = self.installer.snapshot() if self.installer else None
        info['recovery'] = self.recovery
        info['stale'] = bool(self.stale)
        return info

    def _confirm(self, confirmed_version):
        if (not self.cached or self.cached.get('repo') != self.settings.get('appUpdateRepo')
                or confirmed_version != self.cached.get('latest')):
            raise UpdateError("应用更新信息已变化，请重新检查后确认")

    def apply(self, confirmed_version):
        with self.lock:
            repo = self.settings.get('appUpdateRepo')
            info = self.cached
            if not info or info.get('repo') != repo or not info.get('available'):
                raise UpdateError("请先检查应用更新")
            if self.installer:
                if self.installer.snapshot()['phase'] != 'error':
                    return self.installer.snapshot()
            # 明确新鲜度策略：stale 数据只展示；过期缓存必须重新在线校验
            # 版本、资产 URL 与 digest 之后才允许安装。
            if self.stale or time.time() - self.checked_at > APPUPDATE_APPLY_MAX_AGE:
                fresh = self.check(force=True)
                if not fresh.get('ok') or fresh.get('stale'):
                    raise UpdateError("更新信息已过期，请联网后重新检查应用更新")
            self._confirm(confirmed_version)
            if (not self.cached or self.cached.get('repo') != repo
                    or not self.cached.get('available')):
                raise UpdateError("请先检查应用更新")
            if self.marker.exists():
                raise UpdateError("上次应用更新尚待确认，请重启应用或检查恢复标记")
            self.installer = Installer(self, dict(self.cached))
            self.installer.start()
            return self.installer.snapshot()

    def dismiss(self, confirmed_version):
        with self.lock:
            self._confirm(confirmed_version)
            self.settings.dismiss_app_version(self.cached['latest'])


    def _sweep_old_bundles(self):
        """Delete `.Arca-old-*.app` left behind when a restart never happened.

        A relaunch that fails leaves both the marker-less backup and the new
        bundle in /Applications.  Only names in our own pattern are touched, and
        only once they are clearly stale: a fresh backup may belong to a swap
        that is still settling.
        """
        if self.bundle is None:
            return
        cutoff = time.time() - 3600
        try:
            candidates = list(self.bundle.parent.glob('.Arca-old-*.app'))
        except OSError:
            return
        for path in candidates:
            try:
                if not re.fullmatch(r'\.Arca-old-\d+-[0-9a-f]{32}\.app', path.name):
                    continue
                if path.is_symlink() or path.stat().st_mtime > cutoff:
                    continue
                shutil.rmtree(path)
            except OSError:
                continue

    def relaunch(self):
        """Restart into the freshly installed bundle.

        A detached shell waits for this process to exit, then opens the bundle
        from disk.  Waiting matters: launching while the old process is still
        alive races the same bundle path and can re-open the executable we are
        replacing.

        The first ``open`` right after a swap is the fragile part: LaunchServices
        may still be updating its database, so the call fails with no visible
        error.  Retry it, then fall back to starting the binary directly, and
        keep a log — otherwise a failed restart is invisible after the process
        is gone.
        """
        if not self.marker.exists() or self.bundle is None:
            raise UpdateError("还没有待生效的应用更新")
        if not (self.installer and self.installer.snapshot().get('phase') == 'done'):
            raise UpdateError("应用更新尚未安装完成")
        script = relaunch_script(os.getpid(), self.bundle,
                                 Path(util.state_path('relaunch.log')))
        subprocess.Popen(["/bin/sh", "-c", script], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        threading.Timer(0.5, lambda: os._exit(0)).start()
        return {"ok": True, "restarting": True}

    def startup(self, ready=False):
        if not self.marker.exists():
            self._sweep_old_bundles()
            return
        try:
            data = json.loads(self.marker.read_text())
            current = self.bundle
            old = Path(data['old'])
            if (current is None or current.is_symlink() or current.resolve() != current
                    or data.get('schema') != 1 or data.get('current') != str(current)
                    or not re.fullmatch(r'\.Arca-old-\d+-[0-9a-f]{32}\.app', old.name)
                    or old.parent != current.parent or old.is_symlink()
                    or old.resolve() != old or not old.is_dir()):
                raise UpdateError("恢复标记路径不可信")
            st = old.stat()
            if [st.st_dev, st.st_ino] != data.get('identity'):
                raise UpdateError("恢复旧包身份已变化")
            if not ready or self.current != data['version'] or bundle_version(current) != self.current:
                raise UpdateError("新版本尚未确认启动成功")
            bundle_version(old)
            shutil.rmtree(old)
            self.marker.unlink()
        except Exception as exc:
            self.recovery = f'应用更新恢复需要手动处理：{exc}；请查看 {self.marker}'


class Installer:
    def __init__(self, updater, info):
        self.updater, self.info = updater, info
        self.lock = threading.Lock()
        self.state = dict(phase='check', percent=0, received=0, total=0,
                          message='准备应用更新', version=info['latest'], error='')

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def _set(self, **values):
        with self.lock:
            self.state.update(values)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        temporary = None
        try:
            updater = self.updater
            current = updater.bundle
            if current is None or current.is_symlink() or current.resolve() != current:
                raise UpdateError("应用安装路径不安全")
            if bundle_version(current) != updater.current:
                raise UpdateError("当前应用版本已变化")
            temporary = Path(tempfile.mkdtemp(prefix='.Arca-update-', dir=current.parent))
            archive = temporary / 'download.tar.xz'
            digest = hashlib.sha256()
            received = 0
            self._set(phase='download', message='正在下载应用更新')
            with _open(self.info['url']) as response, open(archive, 'xb') as output:
                total = int(response.headers.get('Content-Length') or 0)
                if total > MAX_DOWNLOAD:
                    raise UpdateError("应用更新包过大")
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > MAX_DOWNLOAD:
                        raise UpdateError("应用更新包过大")
                    output.write(chunk)
                    digest.update(chunk)
                    self._set(received=received, total=total,
                              percent=min(95, int(received * 100 / total)) if total else 0)
            self._set(phase='verify', message='正在校验应用更新')
            if digest.hexdigest() != self.info['digest']:
                raise UpdateError("校验不符，已放弃更新")
            new = unpack(archive, temporary)
            if bundle_version(new) != self.info['latest'] or not verify_signature(new):
                raise UpdateError("应用更新包自校验失败")
            self._set(phase='install', message='正在安装应用更新')
            old = current.with_name(f'.Arca-old-{int(time.time())}-{uuid.uuid4().hex}.app')
            os.replace(current, old)
            try:
                os.replace(new, current)
                st = old.stat()
                _write_marker(updater.marker, dict(schema=1, current=str(current), old=str(old),
                              version=self.info['latest'], identity=[st.st_dev, st.st_ino]))
            except Exception:
                try:
                    if current.exists():
                        os.replace(current, new)
                    os.replace(old, current)
                    updater.marker.unlink(missing_ok=True)
                except Exception as rollback:
                    temporary = None
                    raise UpdateError(f'自动回滚失败，请手动从 {old} 恢复：{rollback}') from rollback
                raise
            self._set(phase='done', percent=100, message='更新已就绪，重启应用生效')
        except Exception as exc:
            self._set(phase='error', error=str(exc), message=str(exc))
        finally:
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)
