from pathlib import Path
import hashlib
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/split"


def generate(zz):
    payloads = {
        "文档.txt": ("真实分卷归档\n" * 80).encode(),
        "数据.bin": bytes(range(256)) * 8,
        "子目录/备注.md": b"# split fixture\n" * 40,
    }
    FIXTURES.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="arca-split-") as tmp:
        source = Path(tmp) / "素材"
        for name, data in payloads.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for fmt in ("zip", "7z"):
            archive = FIXTURES / f"sample.{fmt}"
            for old in FIXTURES.glob(f"sample.{fmt}.*"):
                old.unlink()
            cmd = [zz, "a", f"-t{fmt}", "-mx=0", "-v1k", "-pSECRET123",
                   str(archive), "素材"]
            subprocess.run(cmd, cwd=tmp, check=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=30)
    return {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}


if __name__ == "__main__":
    import sys
    print(generate(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "vendor/7zz")))
    for path in sorted(FIXTURES.glob("sample.*")):
        print(path.relative_to(ROOT), path.stat().st_size)
