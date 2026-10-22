"""Prepare local GitHub release assets; never upload or publish."""
from __future__ import annotations
import argparse
import hashlib
from pathlib import Path
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from arca.appupdate import bundle_version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=ROOT / 'dist/Arca.app')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    app = args.app.resolve()
    version = bundle_version(app)
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
    print(f'{archive}\n{sidecar}\n{notes}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
