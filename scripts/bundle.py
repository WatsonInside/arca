"""Assemble Arca.app: native launcher + embedded CPython + app sources.

Why not PyInstaller?  A PyInstaller bundle rewrites the bootloader's code
signature, which needs `codesign` to replace an existing signature.  Building
the bundle by hand keeps every binary's original signature intact and only
compiles one brand-new file (the launcher), which the linker signs ad-hoc on
Apple silicon.  The result is a genuine double-clickable .app with no external
runtime dependency.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from _paths import build_dir  # noqa: E402
LOCAL_TMP = os.path.join(ROOT, ".tmp")
os.makedirs(LOCAL_TMP, exist_ok=True)
os.environ["TMPDIR"] = LOCAL_TMP
tempfile.tempdir = LOCAL_TMP

sys.path.insert(0, os.path.join(ROOT, "src"))
from arca import util  # noqa: E402

APP = "Arca"
DIST = os.path.join(ROOT, "dist")
BUNDLE = os.environ.get("ARCA_OUT") or os.path.join(DIST, f"{APP}.app")
PY_SRC = os.environ.get("ARCA_PYTHON") or "/Users/watsoninside/.workbuddy/binaries/python/versions/3.13.12"


def _site_packages() -> str:
    for cand in (os.environ.get("ARCA_SITE"),
                 str(build_dir() / "venv" / "lib" / "python3.13" / "site-packages"),
                 os.path.join(PY_SRC, "lib", "python3.13", "site-packages")):
        if cand and os.path.isdir(os.path.join(cand, "objc")):
            return cand
    raise SystemExit("找不到含 pyobjc 的 site-packages，请先创建 build/venv")


SITE = _site_packages()

SKIP_DIRS = {
    "__pycache__", "test", "tests", "idlelib", "tkinter", "ensurepip",
    "lib2to3", "pydoc_data", "turtledemo", "site-packages", "dist-packages",
    "tcl8", "tcl8.6", "tk8.6", "tcl9.0", "tk9.0",
}
SKIP_FILES = {"turtle.py", "turtledemo.py", "this.py"}

# only what the app actually imports from site-packages
SITE_WHITELIST = {
    "objc", "AppKit", "Cocoa", "Foundation", "CoreFoundation", "WebKit",
    "JavaScriptCore", "UniformTypeIdentifiers", "PyObjCTools", "Security", "LocalAuthentication",
}


def log(msg):
    print(msg, flush=True)


def copy_tree_filtered(src, dst, skip_dirs=(), skip_files=()):
    os.makedirs(dst, exist_ok=True)
    count = 0
    for entry in os.scandir(src):
        name = entry.name
        target = os.path.join(dst, name)
        if entry.is_dir(follow_symlinks=False):
            if name in skip_dirs:
                continue
            count += copy_tree_filtered(entry.path, target, skip_dirs, skip_files)
        else:
            if name in skip_files or name.endswith((".pyc", ".pyo")):
                continue
            if entry.is_symlink():
                link = os.readlink(entry.path)
                if not os.path.lexists(target):
                    os.symlink(link, target)
            else:
                shutil.copy2(entry.path, target)
            count += 1
    return count


def build_runtime(contents):
    py_home = os.path.join(contents, "Resources", "python")
    log("· embedding CPython runtime")
    os.makedirs(os.path.join(py_home, "bin"), exist_ok=True)
    os.makedirs(os.path.join(py_home, "lib"), exist_ok=True)
    shutil.copy2(os.path.join(PY_SRC, "bin", "python3.13"),
                 os.path.join(py_home, "bin", "python3.13"))
    for lib in ("libpython3.13.dylib",):
        src = os.path.join(PY_SRC, "lib", lib)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(py_home, "lib", lib))
    stdlib = os.path.join(py_home, "lib", "python3.13")
    n = copy_tree_filtered(os.path.join(PY_SRC, "lib", "python3.13"), stdlib,
                           SKIP_DIRS, SKIP_FILES)
    log(f"  stdlib files: {n}")

    site_dst = os.path.join(stdlib, "site-packages")
    os.makedirs(site_dst, exist_ok=True)
    for pkg in sorted(os.listdir(SITE)):
        if pkg.split("-")[0] not in SITE_WHITELIST and pkg not in SITE_WHITELIST:
            continue
        src = os.path.join(SITE, pkg)
        dst = os.path.join(site_dst, pkg)
        if os.path.isdir(src):
            copy_tree_filtered(src, dst, SKIP_DIRS, SKIP_FILES)
        else:
            shutil.copy2(src, dst)
    log("  pyobjc frameworks copied")
    return py_home


def build_app_sources(contents):
    app_dir = os.path.join(contents, "Resources", "app")
    os.makedirs(app_dir, exist_ok=True)
    shutil.copy2(os.path.join(ROOT, "src", "main.py"), os.path.join(app_dir, "main.py"))
    pkg_dst = os.path.join(app_dir, "arca")
    os.makedirs(pkg_dst, exist_ok=True)
    for name in os.listdir(os.path.join(ROOT, "src", "arca")):
        src = os.path.join(ROOT, "src", "arca", name)
        if os.path.isdir(src):
            # Development runs leave __pycache__ behind; shipping an empty cache
            # dir inside the bundle is noise, and a stray .pyc could shadow a
            # source file the next build writes.
            copy_tree_filtered(src, os.path.join(pkg_dst, name), SKIP_DIRS)
        elif name.endswith(".py"):
            shutil.copy2(src, os.path.join(pkg_dst, name))
    vendor = os.path.join(app_dir, "vendor")
    os.makedirs(vendor, exist_ok=True)
    # Every executable the app shells out to must travel with the bundle,
    # otherwise tar.zst / PAR2 silently degrade to "missing component".
    for binary in ("7zz", "zstd", "par2"):
        src = os.path.join(ROOT, "vendor", binary)
        if not os.path.exists(src):
            raise SystemExit(f"缺少 vendor/{binary}，请先运行 scripts/build_thirdparty.py")
        dst = os.path.join(vendor, binary)
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
    for name in ("7-Zip-License.txt", "zstd-LICENSE.txt", "GPL-2.0.txt",
                 "THIRD-PARTY.md"):
        src = os.path.join(ROOT, "vendor", name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(vendor, name))
    sizes = " · ".join(
        f"{b} {os.path.getsize(os.path.join(vendor, b)) // 1024}K"
        for b in ("7zz", "zstd", "par2"))
    log(f"· app sources + engines ({sizes})")


def build_launcher(contents):
    os.makedirs(os.path.join(contents, "MacOS"), exist_ok=True)
    out = os.path.join(contents, "MacOS", APP)
    src = os.path.join(ROOT, "packaging", "launcher.c")
    cmd = ["clang", "-O2", "-arch", "arm64", "-mmacosx-version-min=12.0",
           "-o", out, src]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        raise SystemExit("clang failed:\n" + r.stdout.decode("utf-8", "replace"))
    os.chmod(out, 0o755)
    log("· compiled native launcher")


def build_plist(contents):
    os.makedirs(os.path.join(contents, "Resources"), exist_ok=True)
    icon_src = os.path.join(ROOT, "assets", "Arca.icns")
    if os.path.exists(icon_src):
        shutil.copy2(icon_src, os.path.join(contents, "Resources", "Arca.icns"))
    data = {
        "CFBundleName": APP,
        "CFBundleDisplayName": APP,
        "CFBundleExecutable": APP,
        "CFBundleIdentifier": util.BUNDLE_ID,
        "CFBundleIconFile": "Arca.icns",
        "CFBundlePackageType": "APPL",
        "CFBundleSignature": "????",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleShortVersionString": util.APP_VERSION,
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "12.0",
        "LSApplicationCategoryType": "public.app-category.utilities",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        "NSSupportsAutomaticGraphicsSwitching": True,
        "NSPrincipalClass": "NSApplication",
        "NSHumanReadableCopyright": "Arca 1.0 · 内置 7-Zip (LGPL) · 全部处理均在本机完成",
        "LSMultipleInstancesProhibited": True,
        # 快速操作（Finder 右键）不再走 Info.plist 的 NSServices：macOS 27 的
        # pbs 对 ad-hoc 签名 app 的服务「不显示菜单也不投递」（对照实验：Apple
        # 平台签名与 Developer ID 签名都正常，ad-hoc/自签一律拒绝）。改为由
        # ~/Library/Services 下的 Quick Action workflow 承担——执行者是系统
        # 签名的 WorkflowServiceRunner，不受该策略影响。安装入口在设置里
        # （integration.install_quick_actions，或 /api/integration/quickactions）。
        # 注意 app bundle 的服务声明必须缺席：同名条目会让 pbs 把请求路由给
        # 这个投不进去的 app，workflow 反而被架空。
        # .rar and .zst have no system-declared UTI, so LaunchServices gives
        # them a *dynamic* identifier (dyn.age81e2pw / dyn.age81y65y).
        # LSSetDefaultRoleHandlerForContentType rejects dynamic identifiers with
        # -50 (paramErr) -- that is why "设为默认" silently left RAR and ZST on
        # 系统默认 while the other seven formats switched.  Exporting our own
        # UTI for those two extensions makes the mapping stable and settable.
        "UTExportedTypeDeclarations": [
            {
                "UTTypeIdentifier": "com.arca.rar-archive",
                "UTTypeDescription": "RAR 压缩包",
                "UTTypeConformsTo": ["public.data", "public.archive"],
                "UTTypeTagSpecification": {
                    "public.filename-extension": ["rar"],
                    "public.mime-type": ["application/vnd.rar", "application/x-rar-compressed"],
                },
            },
            {
                "UTTypeIdentifier": "com.arca.zstd-stream",
                "UTTypeDescription": "Zstandard 压缩流",
                "UTTypeConformsTo": ["public.data", "public.archive"],
                "UTTypeTagSpecification": {
                    "public.filename-extension": ["zst", "zstd"],
                    "public.mime-type": ["application/zstd"],
                },
            },
        ],
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeName": "压缩归档",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": [
                    "public.zip-archive",
                    "org.7-zip.7-zip-archive",
                    "com.arca.rar-archive",
                    "public.tar-archive",
                    "org.gnu.gnu-zip-archive",
                    "org.gnu.gnu-zip-tar-archive",
                    "public.bzip2-archive",
                    "org.tukaani.xz-archive",
                    "com.arca.zstd-stream",
                    "public.archive",
                ],
                # Extension tags stay as a fallback for machines that map these
                # extensions before our exported types are indexed.
                "CFBundleTypeExtensions": ["zip", "7z", "rar", "tar", "gz", "tgz",
                                           "bz2", "xz", "zst"],
            }
        ],
    }
    with open(os.path.join(contents, "Info.plist"), "wb") as fh:
        plistlib.dump(data, fh, sort_keys=False)
    with open(os.path.join(contents, "PkgInfo"), "w") as fh:
        fh.write("APPL????")
    log("· Info.plist")


MACHO_MAGIC = (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",   # Mach-O BE (32/64)
               b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",   # Mach-O LE (32/64)
               b"\xca\xfe\xba\xbe")                        # fat / universal


def _needs_exec(path):
    # 精确到文件本质而不是扩展名猜测：Mach-O（可执行、dylib、.so）与
    # shebang 脚本才要执行位。python 运行时来源全是 -rwx------，若原样
    # 保留执行位，几百个 .py/.pyc/.txt 都会带着 +x 装进用户机器。
    with open(path, "rb") as fh:
        head = fh.read(4)
    return head in MACHO_MAGIC or head[:2] == b"#!"


def normalize_modes():
    # pkg 安装会把属主换成 root 并原样保留文件权限：bundle 里任何 owner-only
    # 文件装完就成了「用户读不了、app 打不开」（dmg 拖装没这问题，Finder 把
    # 属主交给用户）。内嵌 python 运行时进 bundle 时就是 -rwx------，统一摊平
    # 为全局可读：目录 755，确属可执行的 755，其余 644。
    for root, dirs, files in os.walk(BUNDLE):
        for name in dirs:
            os.chmod(os.path.join(root, name), 0o755)
        for name in files:
            p = os.path.join(root, name)
            if os.path.islink(p):
                continue
            os.chmod(p, 0o755 if _needs_exec(p) else 0o644)
    os.chmod(BUNDLE, 0o755)


def smoke_test():
    exe = os.path.join(BUNDLE, "Contents", "MacOS", APP)
    py = os.path.join(BUNDLE, "Contents", "Resources", "python", "bin", "python3.13")
    env = dict(os.environ)
    env["PYTHONHOME"] = os.path.join(BUNDLE, "Contents", "Resources", "python")
    env["PYTHONPATH"] = os.path.join(BUNDLE, "Contents", "Resources", "app")
    env.pop("PYTHONSTARTUP", None)
    r = subprocess.run([py, "-c", "import objc, AppKit, WebKit, Security, LocalAuthentication, sys;print(sys.version.split()[0], 'ok')"],
                       env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log("  runtime: " + r.stdout.decode("utf-8", "replace").strip())
    if r.returncode != 0:
        raise SystemExit("embedded runtime cannot import pyobjc")
    return exe


def main():
    contents = os.path.join(BUNDLE, "Contents")
    os.makedirs(contents, exist_ok=True)
    build_plist(contents)
    build_runtime(contents)
    build_app_sources(contents)
    build_launcher(contents)
    normalize_modes()
    exe = smoke_test()
    size = subprocess.run(["du", "-sh", BUNDLE], stdout=subprocess.PIPE).stdout.decode().split()[0]
    n = sum(len(f) for _r, _d, f in os.walk(BUNDLE))
    log(f"\nbundle : {BUNDLE}  ({size}, {n} files)")
    log(f"exec   : {exe}")


if __name__ == "__main__":
    main()
