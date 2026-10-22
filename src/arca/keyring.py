"""Optional macOS Keychain storage; secrets never enter the local index."""
from __future__ import annotations

import json
import hashlib
import os
import subprocess
import threading

from . import util

SECURITY = "/usr/bin/security"
SERVICE = "Arca"
LIMIT = 50


def account(path: str) -> str:
    return hashlib.sha256(os.path.realpath(os.path.expanduser(path)).encode("utf-8")).hexdigest()[:16]


def _run(args):
    try:
        return subprocess.run([SECURITY, *args], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _security():
    import Security
    return Security


def _query(api, path):
    return {api.kSecClass: api.kSecClassGenericPassword,
            api.kSecAttrService: SERVICE, api.kSecAttrAccount: account(path)}


def _protected_remember(path, password):
    try:
        api = _security()
        acl, error = api.SecAccessControlCreateWithFlags(
            None, api.kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
            api.kSecAccessControlUserPresence, None)
        if acl is None or error is not None:
            return False
        query = _query(api, path)
        attrs = {api.kSecAttrAccessControl: acl,
                 api.kSecValueData: password.encode("utf-8")}
        status, _ = api.SecItemAdd({**query, **attrs}, None)
        if status == api.errSecDuplicateItem:
            status = api.SecItemUpdate(query, attrs)
        return status == api.errSecSuccess
    except Exception:
        return False


def lookup(path: str, *, auth="off") -> str | None:
    if auth == "touchid":
        try:
            api = _security()
            status, data = api.SecItemCopyMatching(
                {**_query(api, path), api.kSecReturnData: True,
                 api.kSecMatchLimit: api.kSecMatchLimitOne}, None)
            if status == api.errSecSuccess:
                return bytes(data).decode("utf-8") or None
            if status != api.errSecItemNotFound:
                return None
        except Exception:
            return None
    elif auth != "off":
        return None
    result = _run(["find-generic-password", "-s", SERVICE, "-a", account(path), "-w"])
    if result is None or result.returncode:
        return None
    try:
        value = result.stdout.decode("utf-8").removesuffix("\n")
        return value or None
    except UnicodeError:
        return None


def remember(path: str, password: str, *, opt_in: bool = False, auth="off") -> bool:
    if opt_in is not True or not isinstance(password, str) or not password:
        return False
    if auth == "touchid":
        return _protected_remember(path, password)
    if auth != "off":
        return False
    result = _run(["add-generic-password", "-U", "-s", SERVICE,
                   "-a", account(path), "-w", password])
    return result is not None and result.returncode == 0


def _delete(key: str) -> bool:
    result = _run(["delete-generic-password", "-s", SERVICE, "-a", key])
    return result is not None and result.returncode in (0, 44)


def forget(path: str) -> bool:
    return _delete(account(path))


def redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {redact(k, secrets): redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v, secrets) for v in value)
    return value


class SavedPasswords:
    def __init__(self):
        self._path = util.state_path("saved-passwords.json")
        self._lock = threading.RLock()
        raw = util.read_json(self._path, [])
        self._items = []
        if isinstance(raw, list):
            for row in raw:
                if (isinstance(row, dict) and isinstance(row.get("account"), str)
                        and len(row["account"]) == 16
                        and all(c in "0123456789abcdef" for c in row["account"])
                        and isinstance(row.get("name"), str)
                        and row["account"] not in {r["account"] for r in self._items}):
                    item = {"account": row["account"], "name": os.path.basename(row["name"])}
                    if row.get("cleanup") is True:
                        item["cleanup"] = True
                    if len(self._items) < LIMIT or item.get("cleanup"):
                        self._items.append(item)

    def summary(self):
        with self._lock:
            return {"count": len(self._items), "files": [r["name"] for r in self._items]}

    def _save(self):
        try:
            with open(self._path + ".tmp", "w", encoding="utf-8") as stream:
                json.dump(self._items, stream, ensure_ascii=False)
            os.replace(self._path + ".tmp", self._path)
            return True
        except (OSError, ValueError):
            return False

    def remember(self, path, password, *, opt_in=False, auth="off"):
        if opt_in is not True or not isinstance(password, str) or not password:
            return False
        with self._lock:
            key = account(path)
            existing = any(r["account"] == key for r in self._items)
            if auth == "touchid" and not existing and len(self._items) >= LIMIT:
                pending = {"account": key, "name": os.path.basename(path), "cleanup": True}
                self._items.append(pending)
                if not self._save():
                    self._items.pop()
                    return False
                if not remember(path, password, opt_in=opt_in, auth=auth):
                    self._items.remove(pending)
                    self._save()
                    return False
                if not _delete(self._items[0]["account"]):
                    if _delete(key):
                        self._items.remove(pending)
                        self._save()
                    return False
                self._items.pop(0)
                pending.pop("cleanup")
                return self._save()
            if auth == "touchid" and not remember(path, password, opt_in=opt_in, auth=auth):
                return False
            if not existing and len(self._items) >= LIMIT:
                if not _delete(self._items[0]["account"]):
                    return False
                self._items.pop(0)
                self._save()
            if auth != "touchid" and not remember(path, password, opt_in=opt_in, auth=auth):
                return False
            if not existing:
                self._items.append({"account": key, "name": os.path.basename(path)})
            if not self._save():
                _delete(key)
                self._items = [r for r in self._items if r["account"] != key]
                return False
            return True

    def forget(self, path):
        with self._lock:
            if not forget(path):
                return False
            self._items = [r for r in self._items if r["account"] != account(path)]
            return self._save()

    def clear_all(self):
        with self._lock:
            self._items = [r for r in self._items if not _delete(r["account"])]
            saved = self._save()
            return saved and not self._items


def clear_all() -> bool:
    return SavedPasswords().clear_all()
