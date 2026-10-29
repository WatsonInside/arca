"""构建目录解析（T07b：所有脚本共用的唯一入口）。

默认仍是仓库里 tracked 的 `build/` 符号链接；设置 ARCA_BUILD_DIR 后所有
构建/测试产物（venv、runtime、测试 app、日志、profile、port-file 等）都落到
该目录。这里只做解析，不创建、不删除、不改写 build 符号链接。
"""
from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def build_dir() -> pathlib.Path:
    raw = os.environ.get("ARCA_BUILD_DIR") or (ROOT / "build")
    return pathlib.Path(os.path.expanduser(str(raw))).resolve()


def ensure() -> pathlib.Path:
    path = build_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path
