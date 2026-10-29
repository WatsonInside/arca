from __future__ import annotations
import hashlib
import io
import json
from pathlib import Path
import plistlib
import sys
import tarfile
import threading
import time
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / 'src'))
from arca import appupdate as a, settings, server


class Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT / 'build')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.bundle = self.make_bundle(self.root / 'Arca.app', '1.0.0')
        for target, name, value in [(a, 'bundle_path', lambda: self.bundle),
                                    (a.util, 'state_path', lambda name: str(self.root / name))]:
            mock = patch.object(target, name, side_effect=value)
            mock.start()
            self.addCleanup(mock.stop)
        self.settings = settings.Settings()
        self.settings.patch({'appUpdateRepo': 'owner/repo'})
        self.updater = a.Updater(self.settings)
        self.name = 'Arca-1.0.1-mac.tar.xz'
        self.url = a.asset_url('owner/repo', 'v1.0.1', self.name)
        self.asset = {'name': self.name, 'browser_download_url': self.url, 'digest': 'sha256:' + 'a' * 64}
        self.release = {'tag_name': 'v1.0.1', 'assets': [self.asset]}

    def make_bundle(self, path, version):
        (path / 'Contents/MacOS').mkdir(parents=True)
        (path / 'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleShortVersionString': version, 'CFBundleIdentifier': a.util.BUNDLE_ID}))
        executable = path / 'Contents/MacOS/Arca'
        executable.write_text('#!/bin/sh\nexit 0\n')
        executable.chmod(0o755)
        return path

    def archive(self):
        new = self.make_bundle(self.root / 'source/Arca.app', '1.0.1')
        target = self.root / 'update.tar.xz'
        with tarfile.open(target, 'w:xz', dereference=False) as output:
            output.add(new, arcname='Arca.app')
        return target.read_bytes()

    def install(self, marker_error=False):
        payload = self.archive()
        info = dict(latest='1.0.1', url=self.url, digest=hashlib.sha256(payload).hexdigest())
        inst = a.Installer(self.updater, info)
        response = io.BytesIO(payload)
        response.headers = {'Content-Length': str(len(payload))}
        with patch.object(a, '_open', return_value=response):
            if marker_error:
                with patch.object(a, '_write_marker', side_effect=OSError('marker failed')):
                    inst._run()
            else:
                inst._run()
        return inst

    def test_patch_version(self):
        self.assertTrue(a.is_newer('1.0.1', '1.0.0'))
        self.assertFalse(a.is_newer('1.0.0', '1.0.0'))
        self.assertFalse(a.is_newer('0.9.9', '1.0.0'))

    def test_bad_versions(self):
        for value in ('1.0', 'junk1.0.1', '1.0.1-beta', '', None):
            with self.assertRaises(a.UpdateError):
                a.is_newer(value, '1.0.0')

    def test_disabled(self):
        self.settings.patch({'appUpdateRepo': ''})
        with patch.object(a, '_read', side_effect=AssertionError('network')):
            self.assertFalse(self.updater.check()['enabled'])

    def test_repo_validation(self):
        for value in ('https://github.com/a/b', '../repo', 'a/b/c', 'a/b?x', 'a/..', 1):
            with self.assertRaises(ValueError):
                self.settings.patch({'appUpdateRepo': value})

    def test_check_cache(self):
        with patch.object(a, '_read', return_value=json.dumps(self.release).encode()) as read:
            self.assertTrue(self.updater.check()['available'])
            self.updater.check()
            self.assertEqual(read.call_count, 1)

    def test_check_rejects_asset_url(self):
        self.asset['browser_download_url'] = 'https://evil.invalid/a'
        with patch.object(a, '_read', return_value=json.dumps(self.release).encode()):
            self.assertFalse(self.updater.check()['ok'])
        self.assertIsNone(self.updater.cached)

    def test_sidecar_priority_windows_case(self):
        side = {'browser_download_url': self.url + '.sha256'}
        with patch.object(a, '_read', return_value=('B' * 64 + '  C:\\release\\' + self.name.upper()).encode()):
            self.assertEqual(a.fetch_digest(self.asset, side, 'owner/repo', 'v1.0.1', self.name), 'b' * 64)

    def test_digest_fallback_missing(self):
        self.assertEqual(a.fetch_digest(self.asset, None, 'owner/repo', 'v1.0.1', self.name), 'a' * 64)
        self.asset.pop('digest')
        with self.assertRaises(a.UpdateError):
            a.fetch_digest(self.asset, None, 'owner/repo', 'v1.0.1', self.name)

    def test_invalid_sidecar_no_fallback(self):
        for text in ('a' * 64 + ' wrong.tar.xz', 'z' * 64 + ' ' + self.name):
            with patch.object(a, '_read', return_value=text.encode()), self.assertRaises(a.UpdateError):
                a.fetch_digest(self.asset, {'browser_download_url': self.url + '.sha256'}, 'owner/repo', 'v1.0.1', self.name)

    def test_apply_requires_cache_and_matching_repo(self):
        with self.assertRaises(a.UpdateError):
            self.updater.apply('1.0.1')
        with patch.object(a, '_read', return_value=json.dumps(self.release).encode()):
            self.updater.check()
        self.settings.patch({'appUpdateRepo': 'other/repo'})
        with self.assertRaises(a.UpdateError):
            self.updater.apply('1.0.1')

    def test_dismiss_internal_persists(self):
        self.settings.patch({'appSkipVersion': '9.9.9'})
        self.assertEqual(self.settings.get('appSkipVersion'), '')
        self.settings.dismiss_app_version('1.0.1')
        self.assertEqual(settings.Settings().get('appSkipVersion'), '1.0.1')

    def test_bundle_validation(self):
        (self.bundle / 'Contents/MacOS/Arca').unlink()
        with self.assertRaises(a.UpdateError):
            a.bundle_version(self.bundle)

    def test_install_and_startup_success(self):
        inst = self.install()
        self.assertEqual(inst.snapshot()['phase'], 'done', inst.snapshot())
        self.assertEqual(a.bundle_version(self.bundle), '1.0.1')
        data = json.loads(self.updater.marker.read_text())
        self.assertTrue(Path(data['old']).is_dir())
        boot = a.Updater(self.settings)
        boot.startup(ready=True)
        self.assertFalse(Path(data['old']).exists())
        self.assertFalse(boot.marker.exists())

    def test_startup_not_ready_preserves_backup(self):
        self.install()
        data = json.loads(self.updater.marker.read_text())
        boot = a.Updater(self.settings)
        boot.startup(ready=False)
        self.assertTrue(Path(data['old']).exists())
        self.assertIn('手动', boot.recovery)

    def test_old_running_version_cannot_clean(self):
        self.install()
        self.updater.startup(ready=True)
        self.assertTrue(self.updater.marker.exists())

    def test_marker_failure_rolls_back(self):
        inst = self.install(marker_error=True)
        self.assertEqual(inst.snapshot()['phase'], 'error')
        self.assertEqual(a.bundle_version(self.bundle), '1.0.0')
        self.assertFalse(self.updater.marker.exists())

    def test_checksum_failure_never_replaces(self):
        payload = self.archive()
        response = io.BytesIO(payload)
        response.headers = {}
        inst = a.Installer(self.updater, dict(latest='1.0.1', url=self.url, digest='0' * 64))
        with patch.object(a, '_open', return_value=response):
            inst._run()
        self.assertIn('校验不符', inst.snapshot()['error'])
        self.assertEqual(a.bundle_version(self.bundle), '1.0.0')

    def test_tar_rejects_paths_and_links(self):
        for name, link in [('/escape', ''), ('Arca.app/../escape', ''), ('Arca.app/link', '../../../escape')]:
            target = self.root / 'bad.tar.xz'
            with tarfile.open(target, 'w:xz') as output:
                member = tarfile.TarInfo(name)
                if link:
                    member.type = tarfile.SYMTYPE
                    member.linkname = link
                output.addfile(member, io.BytesIO())
            with self.assertRaises(a.UpdateError):
                a.unpack(target, self.root / 'unpacked')

    def test_forged_marker_does_not_delete_user_directory(self):
        user = self.make_bundle(self.root / 'User.app', '1.0.0')
        self.updater.marker.write_text(json.dumps(dict(schema=1, current=str(self.bundle), old=str(user), version='1.0.0')))
        self.updater.startup(ready=True)
        self.assertTrue(user.is_dir())

    def test_replace_order(self):
        original = a.os.replace
        calls = []
        def replace(source, destination):
            calls.append((Path(source), Path(destination)))
            original(source, destination)
        with patch.object(a.os, 'replace', side_effect=replace):
            self.assertEqual(self.install().snapshot()['phase'], 'done')
        self.assertEqual(calls[0][0], self.bundle)
        self.assertTrue(calls[0][1].name.startswith('.Arca-old-'))
        self.assertEqual(calls[1][1], self.bundle)
        self.assertEqual(calls[2][1], self.updater.marker)

    def test_tar_normal_framework_symlink(self):
        source = self.make_bundle(self.root / 'framework/Arca.app', '1.0.1')
        framework = source / 'Contents/Frameworks/Test.framework/Versions'
        (framework / 'A').mkdir(parents=True)
        (framework / 'A/library').write_text('fixture')
        (framework / 'Current').symlink_to('A')
        target = self.root / 'framework.tar.xz'
        with tarfile.open(target, 'w:xz', dereference=False) as output:
            output.add(source, arcname='Arca.app')
        output = a.unpack(target, self.root / 'safe')
        self.assertEqual((output / 'Contents/Frameworks/Test.framework/Versions/Current/library').read_text(), 'fixture')

    def test_version_mismatch_never_installs(self):
        payload = self.archive()
        response = io.BytesIO(payload)
        response.headers = {}
        inst = a.Installer(self.updater, dict(latest='1.0.2', url=self.url, digest=hashlib.sha256(payload).hexdigest()))
        with patch.object(a, '_open', return_value=response):
            inst._run()
        self.assertEqual(inst.snapshot()['phase'], 'error')
        self.assertEqual(a.bundle_version(self.bundle), '1.0.0')

    def test_http_server_cached_apply_end_to_end(self):
        import urllib.request
        payload = self.archive()
        self.asset['digest'] = 'sha256:' + hashlib.sha256(payload).hexdigest()
        class Response(io.BytesIO):
            headers = {}
        def upstream(url):
            if url.startswith('https://api.github.com/'):
                return Response(json.dumps(self.release).encode())
            self.assertEqual(url, self.url)
            return Response(payload)
        ctx = SimpleNamespace(app_updater=self.updater, token='fixture-token', settings=self.settings,
                              saved_passwords=SimpleNamespace(summary=lambda: {}),
                              headless=False, ui_ready=threading.Event(),
                              ui_ready_lock=threading.Lock(), ui_ready_checks={})
        service = server.Server(ctx)
        service.start()
        try:
            def request(path, body=None):
                data = json.dumps(body).encode() if body is not None else None
                req = urllib.request.Request(f'http://127.0.0.1:{service.port}/api/' + path,
                    data=data, headers={'X-Arca-Token': ctx.token, 'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=5) as response:
                    return json.load(response)
            with patch.object(a, '_open', side_effect=upstream):
                self.assertTrue(request('appupdate')['update']['available'])
                with patch.object(a.Installer, 'start', lambda inst: inst._run()):
                    result = request('appupdate/apply', {'version': '1.0.1', 'url': 'http://127.0.0.1:1/evil', 'repo': 'evil/repo'})
                self.assertEqual(result['install']['phase'], 'done', result)
                self.assertEqual(request('appupdate')['update']['install']['phase'], 'done')
            boot = a.Updater(self.settings)
            ctx.app_updater = boot
            restarted = server.Server(ctx)
            try:
                restarted.start()
                # T05：只有页面真正上报四项自检通过后才清理旧包（异步）。
                request('ui-ready', {"bridge": True, "dom": True,
                                     "meta": True, "engine": True})
                deadline = time.monotonic() + 5
                while boot.marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(boot.marker.exists())
            finally:
                restarted.stop()
                restarted.httpd.server_close()
                restarted.thread.join()
        finally:
            service.stop()
            service.httpd.server_close()
            service.thread.join()

    def test_http_apply_ignores_body_urls(self):
        with patch.object(a, '_read', return_value=json.dumps(self.release).encode()):
            self.updater.check()
        handler = object.__new__(server.Handler)
        handler.ctx = SimpleNamespace(app_updater=self.updater)
        handler._json = lambda data: data
        with patch.object(a.Installer, 'start'):
            result = handler._api_post('/api/appupdate/apply', {
                'version': '1.0.1', 'url': 'file:///bad', 'repo': 'evil/repo'})
        self.assertTrue(result['ok'])
        self.assertEqual(self.updater.installer.info['url'], self.url)

    def test_stale_confirmation_rejected(self):
        with patch.object(a, '_read', return_value=json.dumps(self.release).encode()):
            self.updater.check()
        self.updater.cached['latest'] = '1.0.2'
        for confirmed in ('1.0.1', None, '', 102):
            with self.assertRaises(a.UpdateError):
                self.updater.apply(confirmed)
            with self.assertRaises(a.UpdateError):
                self.updater.dismiss(confirmed)
        self.assertIsNone(self.updater.installer)
        self.assertEqual(self.settings.get('appSkipVersion'), '')
        self.updater.dismiss('1.0.2')
        self.assertTrue(self.updater.check()['skipped'])

    def test_offline_install_snapshots_do_not_refresh_expired_feed(self):
        with patch.object(a, '_read', return_value=json.dumps(self.release).encode()):
            self.updater.check()
        self.updater.checked_at = 0
        for phase in ('download', 'install', 'done'):
            self.updater.installer = SimpleNamespace(snapshot=lambda: {'phase': phase, 'version': '1.0.1'})
            with patch.object(a, '_read', side_effect=OSError('offline')) as read:
                info = self.updater.check()
                self.assertEqual(info['install']['phase'], phase)
                self.assertTrue(info['ok'])
                read.assert_not_called()


    def test_relaunch_script_retries_then_starts_the_binary(self):
        """A failed `open` right after the swap must not leave the app closed."""
        bundle = self.root / 'Arca With Space.app'
        log = self.root / 'relaunch.log'
        script = a.relaunch_script(4242, bundle, log)
        self.assertIn('while kill -0 4242', script)
        self.assertIn('for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15', script)
        self.assertIn('pgrep -f', script)                                  # liveness check
        self.assertIn('sleep 1; if alive', script)                         # checked twice
        self.assertIn('/usr/bin/open -b com.arca.app', script)             # bundle-id fallback
        self.assertIn("/usr/bin/open -n", script)
        self.assertIn("'%s'" % str(bundle), script)          # quoted for spaces
        self.assertIn('exec ', script)
        self.assertIn(str(bundle / 'Contents' / 'MacOS' / 'Arca'), script)
        self.assertIn(str(log), script)

    def test_relaunch_script_really_restarts_a_bundle(self):
        import subprocess
        import time
        bundle = self.root / 'Fake Bundle.app'
        (bundle / 'Contents' / 'MacOS').mkdir(parents=True)
        exe = bundle / 'Contents' / 'MacOS' / 'Arca'
        marker = self.root / 'launched.txt'
        exe.write_text('#!/bin/sh\necho started > "%s"\n' % marker)
        exe.chmod(0o755)
        log = self.root / 'relaunch.log'
        victim = subprocess.Popen(['/bin/sleep', '1'])
        # Reap it in the background: a zombie still answers kill -0, and the
        # script (correctly) waits for the pid to disappear.  In production the
        # old app's parent is launchd, which reaps immediately.
        import threading
        threading.Thread(target=victim.wait, daemon=True).start()
        script = a.relaunch_script(victim.pid, bundle, log)
        subprocess.Popen(['/bin/sh', '-c', script], start_new_session=True)
        deadline = time.time() + 20
        while time.time() < deadline and not marker.exists():
            time.sleep(.2)
        self.assertTrue(marker.exists(), log.read_text() if log.exists() else 'no log')
        self.assertIn('oldpid=', log.read_text())

    def test_orphan_backup_is_swept_only_when_stale(self):
        import os
        import time
        parent = self.bundle.parent
        stale = parent / ('.Arca-old-1700000000-%s.app' % ('a' * 32))
        fresh = parent / ('.Arca-old-%d-%s.app' % (int(time.time()), 'b' * 32))
        foreign = parent / '.Arca-old-not-ours.app'
        for path in (stale, fresh, foreign):
            (path / 'Contents').mkdir(parents=True)
        old = time.time() - 7200
        os.utime(stale, (old, old))
        self.updater.startup(ready=True)          # no marker -> sweep
        self.assertFalse(stale.exists(), 'stale backup should be removed')
        self.assertTrue(fresh.exists(), 'fresh backup must survive')
        self.assertTrue(foreign.exists(), 'only our own pattern is touched')


class ConfirmUpdateTests(unittest.TestCase):
    """T05：页面就绪确认的策略（server.confirm_app_update）。"""

    class FakeUpdater:
        def __init__(self, has_marker):
            self.marker = SimpleNamespace(exists=lambda: has_marker)
            self.calls = []

        def startup(self, ready=False):
            self.calls.append(ready)

    class FakeCtx:
        def __init__(self, headless=False):
            self.headless = headless
            self.ui_ready = threading.Event()
            self.ui_ready_checks = {}

    def test_headless_never_cleans_up(self):
        ctx = self.FakeCtx(headless=True)
        ctx.app_updater = self.FakeUpdater(True)
        server.confirm_app_update(ctx, timeout=0.05)
        self.assertEqual(ctx.app_updater.calls, [False])

    def test_timeout_keeps_old_bundle(self):
        ctx = self.FakeCtx()
        ctx.app_updater = self.FakeUpdater(True)
        started = time.monotonic()
        server.confirm_app_update(ctx, timeout=0.05)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(ctx.app_updater.calls, [False])

    def test_ready_signal_confirms(self):
        ctx = self.FakeCtx()
        ctx.app_updater = self.FakeUpdater(True)
        ctx.ui_ready_checks = {"bridge": True, "dom": True,
                               "meta": True, "engine": True}
        ctx.ui_ready.set()
        server.confirm_app_update(ctx, timeout=0.5)
        self.assertEqual(ctx.app_updater.calls, [True])

    def test_no_marker_does_nothing(self):
        ctx = self.FakeCtx()
        ctx.app_updater = self.FakeUpdater(False)
        server.confirm_app_update(ctx, timeout=0.05)
        self.assertEqual(ctx.app_updater.calls, [])

    def test_missing_updater_does_nothing(self):
        ctx = self.FakeCtx()
        ctx.app_updater = None
        server.confirm_app_update(ctx, timeout=0.05)


if __name__ == '__main__':
    unittest.main(verbosity=2)
