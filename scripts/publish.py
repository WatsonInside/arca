"""Prepare local GitHub release assets; never upload or publish.

Also rewrites the version strings in README.md so the badge and the
example filenames never drift from the build.
"""
from __future__ import annotations
import argparse
import hashlib
import re
from pathlib import Path
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from arca.appupdate import bundle_version



VERSION_RE = re.compile(r'v\d+\.\d+\.\d+')
FILE_RE = re.compile(r'Arca-\d+\.\d+\.\d+(?=-mac\.tar\.xz|\.dmg|\.pkg)')


def sync_readme(version: str, readme: Path) -> list[str]:
    """Keep the badge and the example filenames on the version we just built.

    Only those two patterns are touched: prose that mentions an older release on
    purpose (changelogs, notes) must survive untouched.
    """
    text = readme.read_text(encoding='utf-8')
    updated = FILE_RE.sub(f'Arca-{version}', text)
    updated = VERSION_RE.sub(f'v{version}', updated)
    if updated == text:
        return []
    readme.write_text(updated, encoding='utf-8')
    return [line for line in updated.splitlines()
            if f'v{version}' in line or f'Arca-{version}' in line][:4]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=ROOT / 'release/Arca.app')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    parser.add_argument('--readme-only', action='store_true',
                        help='only rewrite README version strings, build nothing')
    args = parser.parse_args()
    app = args.app.resolve()
    if not app.is_dir():
        parser.error(f'找不到应用包：{app}（先用 scripts/bundle.py 组装，或用 --app 指定）')
    version = bundle_version(app)
    if args.readme_only:
        changed = sync_readme(version, ROOT / 'README.md')
        print(f'README.md -> {version}: ' + ('updated' if changed else 'already current'))
        for line in changed:
            print('  ' + line.strip())
        return 0
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / f'Arca-{version}-mac.tar.xz'
    sidecar = archive.with_name(archive.name + '.sha256')
    notes = args.output / f'Arca-{version}-release-notes.txt'
    if any(p.exists() for p in (archive, sidecar, notes)):
        parser.error('release assets already exist; refusing to overwrite')
    with tarfile.open(archive, 'x:xz', dereference=False) as output:
        output.add(app, arcname='Arca.app')
    digest = hashlib.sha256()
    with archive.open('rb') as source:
        for chunk in iter(lambda: source.read(256 * 1024), b''):
            digest.update(chunk)
    sidecar.write_text(f'{digest.hexdigest()}  {archive.name}\n')
    notes.write_text(f'Arca {version}\n\n变更：\n- 待填写\n\n手动上传 tar.xz 与 .sha256 到同一 GitHub Release。\n本轮尚未签名公证，请勿宣称已完成签名验证。\n')
    changed = sync_readme(version, ROOT / 'README.md')
    print(f'{archive}\n{sidecar}\n{notes}')
    print(f'README.md -> {version}: ' + ('updated' if changed else 'already current'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
