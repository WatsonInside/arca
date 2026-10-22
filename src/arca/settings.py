"""Persisted preferences."""
from __future__ import annotations

import threading

from . import util

DEFAULTS = {
    "format": "7z",
    "level": 3,
    "method": "auto",
    "splitId": "none",
    "splitBytes": 0,
    "excludeMeta": True,
    "overwrite": "rename",
    "encryptNames": True,
    "appearance": "system",
    "suppressSound": False,
    "passwordAuth": "off",
    "rarPath": "",
    "threads": 0,
    "autoFolder": True,
    "appUpdateRepo": "WatsonInside/arca",
    "appSkipVersion": "",
    # Engine updates: check quietly on launch, and remember a version the user
    # asked not to be nagged about.
    "checkEngineUpdates": True,
    "checkAppUpdates": True,
    "engineSkipVersion": "",
    # "稍后" is a snooze, not a decision: remember until when (epoch seconds).
    "engineSnoozeVersion": "",
    "engineSnoozeUntil": 0,
    # Recovery records are opt-in: 0 means off, otherwise the redundancy
    # percentage used by par2.
    "par2Percent": 0,
    # Finder 右键服务：空字符串表示跟随上面的默认格式；autoStart=False 时
    # 只把文件放进队列，等用户按开始。
    "serviceFormat": "",
    # Finder 右键压缩默认进压缩设置页（文件已就位，由用户确认格式后再开始）；
    # 想一键直压的用户在设置里打开这个开关。
    "serviceAutoStart": False,
}

ALLOWED = set(DEFAULTS) - {"appSkipVersion"}


class Settings:
    def __init__(self):
        self._path = util.state_path("settings.json")
        self._lock = threading.RLock()
        raw = util.read_json(self._path, {})
        self._data = dict(DEFAULTS)
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k in DEFAULTS and (k != "passwordAuth" or v in ("off", "touchid")):
                    self._data[k] = v
        from .appupdate import valid_repo
        if not valid_repo(self._data["appUpdateRepo"]):
            self._data["appUpdateRepo"] = ""
        self._data["rarPath"] = self._data.get("rarPath") or ""

    def all(self) -> dict:
        with self._lock:
            return dict(self._data)

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key, default))

    def dismiss_app_version(self, value):
        from .appupdate import version
        version(value)
        with self._lock:
            self._data["appSkipVersion"] = value
            util.write_json(self._path, self._data)

    def patch(self, values: dict) -> dict:
        from .appupdate import valid_repo
        if "appUpdateRepo" in (values or {}) and not valid_repo(values["appUpdateRepo"]):
            raise ValueError("GitHub 仓库格式应为 owner/repo")
        with self._lock:
            for k, v in (values or {}).items():
                if k in ALLOWED and (k != "passwordAuth" or v in ("off", "touchid")):
                    self._data[k] = v
            data = dict(self._data)
            util.write_json(self._path, data)
        return data
