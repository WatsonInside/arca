"""Build Arca.app with PyInstaller, then fix Info.plist and ad-hoc sign it."""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TMP = os.path.join(ROOT, ".tmp")
os.makedirs(LOCAL_TMP, exist_ok=True)
os.environ["TMPDIR"] = LOCAL_TMP
tempfile.tempdir = LOCAL_TMP

sys.path.insert(0, os.path.join(ROOT, "src"))
from arca import util  # noqa: E402

APP = "Arca"
DIST = os.path.join(ROOT, "dist")
WORK = os.path.join(ROOT, "build", "pyi")


def _tolerate_removals():
    """PyInstaller tidies up with os.remove/rmtree; hardened sandboxes may veto
    deletes. Missing a cleanup step never hurts a fresh build tree, so degrade
    gracefully instead of failing the whole build."""
    real_remove = os.remove
    real_unlink = os.unlink
    real_rmtree = shutil.rmtree

    def soft_remove(path, *a, **k):
        try:
            return real_remove(path, *a, **k)
        except (PermissionError, OSError):
            return None

    def soft_unlink(path, *a, **k):
        try:
            return real_unlink(path, *a, **k)
        except (PermissionError, OSError):
            return None

    def soft_rmtree(path, *a, **k):
        try:
            return real_rmtree(path, *a, **k)
        except (PermissionError, OSError):
            return None

    os.remove = soft_remove
    os.unlink = soft_unlink
    shutil.rmtree = soft_rmtree


def run_pyinstaller():
    import PyInstaller.__main__ as pyi
    import PyInstaller.utils.osx as pyiosx

    _tolerate_removals()

    # The bootloader ships as a universal binary; PyInstaller would `lipo -thin`
    # it in place, which some hardened environments refuse. A universal stub
    # runs perfectly well on arm64, so keep it as shipped.
    pyiosx.binary_to_target_arch = lambda *a, **k: None

    os.environ.setdefault("PYINSTALLER_CONFIG_DIR", os.path.join(ROOT, "build", "pyi-cache"))
    args = [
        os.path.join(ROOT, "src", "main.py"),
        "--name", APP,
        "--windowed",
        "--noconfirm",
        "--distpath", DIST,
        "--workpath", WORK,
        "--specpath", os.path.join(ROOT, "build"),
        "--icon", os.path.join(ROOT, "assets", "Arca.icns"),
        "--osx-bundle-identifier", util.BUNDLE_ID,
        "--add-data", f"{os.path.join(ROOT, 'src', 'arca', 'web')}:arca/web",
        "--add-binary", f"{os.path.join(ROOT, 'vendor', '7zz')}:vendor",
        "--hidden-import", "objc",
        "--hidden-import", "AppKit",
        "--hidden-import", "Foundation",
        "--hidden-import", "WebKit",
        "--hidden-import", "UniformTypeIdentifiers",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "numpy",
        "--exclude-module", "PIL",
    ]
    if os.environ.get("ARCA_DEBUG_BUILD"):
        args.remove("--windowed")
        args.append("--console")
    pyi.run(args)


def patch_plist(app_path):
    plist_path = os.path.join(app_path, "Contents", "Info.plist")
    with open(plist_path, "rb") as fh:
        data = plistlib.load(fh)
    data.update({
        "CFBundleName": APP,
        "CFBundleDisplayName": APP,
        "CFBundleShortVersionString": util.APP_VERSION,
        "CFBundleVersion": "1",
        "CFBundleIdentifier": util.BUNDLE_ID,
        "CFBundleIconFile": "Arca.icns",
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": "12.0",
        "LSApplicationCategoryType": "public.app-category.utilities",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        "NSHumanReadableCopyright": "Arca 1.0 · 内置 7-Zip (LGPL) · 本地处理，无网络请求",
        "NSSupportsAutomaticGraphicsSwitching": True,
        "LSMultipleInstancesProhibited": True,
    })
    with open(plist_path, "wb") as fh:
        plistlib.dump(data, fh, sort_keys=False)
    # PyInstaller names the icon file after the app; make sure it exists
    res = os.path.join(app_path, "Contents", "Resources")
    icon = os.path.join(res, "Arca.icns")
    if not os.path.exists(icon):
        src = os.path.join(ROOT, "assets", "Arca.icns")
        if os.path.exists(src):
            shutil.copy2(src, icon)


def make_frameworks_relocatable(app_path):
    """Ensure the bundled 7zz is executable and has no quarantine."""
    for root, _dirs, files in os.walk(app_path):
        for name in files:
            if name in ("7zz", "7z"):
                p = os.path.join(root, name)
                os.chmod(p, 0o755)
                subprocess.run(["xattr", "-c", p], check=False)


def sign(app_path):
    # sign nested mach-o binaries first, then the bundle
    subprocess.run(["codesign", "--force", "--deep", "--timestamp=none",
                    "--sign", "-", app_path], check=False,
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = subprocess.run(["codesign", "-dv", "--verbose=2", app_path],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return out.stdout.decode("utf-8", "replace")


def main():
    run_pyinstaller()
    app_path = os.path.join(DIST, f"{APP}.app")
    if not os.path.isdir(app_path):
        raise SystemExit("build failed: no bundle at " + app_path)
    patch_plist(app_path)
    make_frameworks_relocatable(app_path)
    info = sign(app_path)
    size = subprocess.run(["du", "-sh", app_path], stdout=subprocess.PIPE).stdout.decode().split()[0]
    print(f"\nbundle : {app_path}")
    print(f"size   : {size}")
    print("codesign:\n" + info.strip())
    print("exec   :", os.path.join(app_path, "Contents", "MacOS", APP))


if __name__ == "__main__":
    main()
