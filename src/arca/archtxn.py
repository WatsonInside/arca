"""覆盖压缩的可恢复替换（T06）。

范围刻意很小：同卷内的归档、分卷与 PAR2 sidecar 的"覆盖"提交。不建设通用
事务框架，只处理一种流程：

    staging（新集合写在目标旁边的隐藏临时名下）
      → switching（旧集合 rename 到备份名，新集合 rename 到正式名）
      → cleanup（删除旧备份、生成 PAR2，删除 manifest）

关键约定：
* 切换前不删旧集合；压缩/校验/取消失败时旧集合原样保留。
* manifest 在切换开始前落盘（state/archive-txns/<id>.json）；进程在切换过程中
  崩溃，启动时 recover_pending() 按 manifest 幂等"向前滚"：把 staging 补成正式
  名、删除旧备份、清理 manifest。manifest 仍处于 staging 阶段则说明压缩尚未
  完成，直接丢弃 staging（旧集合没有被碰过）。
* PAR2 默认 best-effort：归档切换成功即算成功；PAR2 失败保留新归档、不恢复
  旧 PAR2（避免新旧混用），结果带 par2Error。
* 不宣称多文件集合的"单次原子"：能保证的是任一点崩溃后可以确定地前滚或
  丢弃 staging，不丢旧集合。
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid

from . import util

SCHEMA = 1
_STATE_DIR = "archive-txns"


class TxnError(Exception):
    pass


class SimulatedCrash(Exception):
    """测试用：在指定切换点中断，验证恢复路径。"""


def _hit(point: str) -> None:
    if os.environ.get("ARCA_TXN_FAIL_AT") == point:
        if os.environ.get("ARCA_TXN_CRASH") == "1":
            # 真实崩溃模拟：进程直接退出（用于验证重启后的前滚恢复）。
            os._exit(70)
        raise SimulatedCrash(point)


def _root() -> str:
    path = util.state_path(_STATE_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def _manifest_path(txn_id: str) -> str:
    return os.path.join(_root(), txn_id + ".json")


def _save(manifest: dict) -> None:
    path = _manifest_path(manifest["id"])
    util.write_json(path, manifest)


def _remove(path: str) -> None:
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.lexists(path):
            os.remove(path)
    except OSError:
        pass


def _glob_owned(base: str) -> list[str]:
    """staging/backup 文件：base 本身、base.NNN 分卷、以及备份加的 sidecar 后缀。"""
    found = []
    for cand in glob.glob(glob.escape(base) + "*"):
        found.append(cand)
    if os.path.lexists(base):
        found.append(base)
    return sorted(set(found))


class ArchiveTxn:
    def __init__(self, manifest: dict):
        self.manifest = manifest
        self.id = manifest["id"]
        self.target = manifest["target"]
        self.staging = manifest["staging"]

    def _backup(self, path: str) -> str:
        return self.manifest["backups"][path]


def _par2_files(path: str) -> list[str]:
    try:
        from . import par2 as par2_mod
        return par2_mod.recovery_files(path)
    except Exception:
        return []


def begin(job_id: str, target: str, *, par2_percent: int = 0,
          par2_single: bool = False) -> ArchiveTxn:
    """在目标同卷创建 staging 名并落 manifest（phase=staging）。"""
    target = os.path.abspath(target)
    folder = os.path.dirname(target)
    base = os.path.basename(target)
    txn_id = "%s-%s" % (job_id, uuid.uuid4().hex[:8])
    staging = os.path.join(folder, ".%s.arca-txn-%s" % (base, txn_id))
    old = [target] + _glob_parts(target) + _par2_files(target)
    backups = {}
    for path in old:
        if not os.path.exists(path):
            continue
        suffix = ""
        if re.search(r"\.\d{3}$", path) and path != target:
            suffix = path[-4:]
        elif path.endswith(".par2") or path.endswith(".PAR2"):
            suffix = "-par2-%d.par2" % len(backups)
        backups[path] = os.path.join(
            folder, ".%s.arca-backup-%s%s" % (base, txn_id, suffix))
    manifest = {
        "schema": SCHEMA, "id": txn_id, "job": job_id,
        "created": time.time(), "phase": "staging",
        "target": target, "staging": staging,
        "backups": backups,
        "par2": {"percent": int(par2_percent or 0),
                 "single": bool(par2_single) and par2_percent > 0},
    }
    _save(manifest)
    return ArchiveTxn(manifest)


def _glob_parts(entry: str) -> list[str]:
    base = entry
    if re.search(r"\.\d{3}$", base):
        base = base[:-4]
    return sorted(glob.glob(glob.escape(base) + ".[0-9][0-9][0-9]"))


def _staging_parts(txn: ArchiveTxn) -> list[str]:
    return _glob_parts(txn.staging)


def verify(engine, files: list[str], password: str = "", fmt: str = "") -> None:
    """切换前的回读校验（7zz t / zstd -t）；失败抛 TxnError，旧集合不会被碰。"""
    files = [f for f in files if os.path.exists(f)]
    if not files:
        raise TxnError("新归档不存在")
    entry = files[0]
    zz = getattr(engine, "zz", "")
    zstd = getattr(engine, "zstd", "")
    if (fmt or "").lower() == "tar.zst" or entry.lower().endswith(".tar.zst"):
        if not zstd:
            raise TxnError("缺少 zstd 组件，无法回读校验新归档")
        cmd = [zstd, "-t", "-q", entry]
    else:
        if not zz:
            raise TxnError("缺少 7-Zip 组件，无法回读校验新归档")
        cmd = [zz, "t", entry, "-bso0", "-bsp0", "-bse0"]
        if password:
            cmd.insert(1, "-p" + password)
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TxnError("新归档回读校验失败：%s" % (exc,))
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise TxnError("新归档回读校验失败：%s"
                       % (detail[-300:] if detail else "返回码 %d" % proc.returncode))


def abort(txn: ArchiveTxn) -> None:
    """切换前失败/取消：只清理 staging 与 manifest，旧集合不动。"""
    for path in _glob_owned(txn.staging):
        _remove(path)
    _remove(_manifest_path(txn.id))


def _map_moves(txn: ArchiveTxn, new_files: list[str]) -> list[dict]:
    moves = []
    staging = txn.staging
    for path in new_files:
        if path == staging:
            suffix = ""
        elif path.startswith(staging + ".") and re.fullmatch(r"\.\d{3}", path[len(staging):]):
            suffix = path[len(staging):]
        else:
            continue
        moves.append({"src": path, "dst": txn.target + suffix})
    if not moves:
        raise TxnError("新归档文件不可识别")
    return moves


def commit(txn: ArchiveTxn, engine, new_files: list[str], *,
           cancel=None) -> dict:
    """把 staging 切换成正式名；返回 {"files": [...], "par2": {...}|None,
    "par2Error": str}。调用前应已完成回读校验。"""
    if cancel is not None and cancel.is_set():
        raise RuntimeError("已取消")
    manifest = dict(txn.manifest)
    moves = _map_moves(txn, new_files)
    manifest["phase"] = "switching"
    manifest["moves"] = moves
    manifest["old"] = [p for p in manifest["backups"] if os.path.exists(p)]
    _save(manifest)
    _hit("switch-manifest")
    try:
        for path in manifest["old"]:
            _hit("switch-old")
            os.replace(path, manifest["backups"][path])
        _hit("switch-new")
        for move in moves:
            if os.path.exists(move["src"]):
                if os.path.exists(move["dst"]):
                    _remove(move["src"])
                else:
                    os.replace(move["src"], move["dst"])
    except SimulatedCrash:
        raise
    except Exception as exc:
        # 切换失败尽量回滚已改名的旧文件，保证启动后仍能恢复。
        try:
            for path in manifest["old"]:
                backup = manifest["backups"][path]
                if os.path.exists(backup) and not os.path.exists(path):
                    os.replace(backup, path)
        except OSError:
            pass
        raise TxnError("切换新归档失败：%s" % exc)

    manifest["phase"] = "cleanup"
    _save(manifest)
    _hit("switch-cleanup")
    for backup in manifest["backups"].values():
        _remove(backup)
    result = {"files": [m["dst"] for m in moves], "par2": None,
              "par2Error": ""}
    par2_conf = manifest.get("par2") or {}
    percent = int(par2_conf.get("percent") or 0)
    if percent > 0 and engine is not None and getattr(engine, "par2", None):
        try:
            from . import par2 as par2_mod
            rec = par2_mod.create(
                engine.par2, result["files"], percent,
                single=bool(par2_conf.get("single")),
                on_progress=None, cancel=cancel)
            result["par2"] = {
                "files": rec["files"], "made": rec["made"],
                "bytes": rec["bytes"],
                "percent": percent,
                "sizeText": util.human_size(rec["bytes"]),
                "single": bool(par2_conf.get("single")),
                "index": rec["made"][0] if rec["made"] else "",
            }
        except Exception as exc:  # 归档本身已经切换成功：PAR2 只是尽力而为
            result["par2Error"] = str(exc)
    _remove(_manifest_path(txn.id))
    return result


# --------------------------------------------------------------- recovery
def _load_pending() -> list[dict]:
    pending = []
    for path in sorted(glob.glob(os.path.join(_root(), "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if data.get("schema") != SCHEMA or not data.get("id"):
            continue
        if os.path.basename(path) != data["id"] + ".json":
            continue
        pending.append(data)
    return pending


def recover_pending(engine=None) -> list[str]:
    """启动时（或测试里）恢复未完成的覆盖压缩，返回处理过的 txn id。"""
    handled = []
    for manifest in _load_pending():
        txn = ArchiveTxn(manifest)
        phase = manifest.get("phase")
        if phase == "staging":
            # 压缩还没跑完：staging 可能残缺，旧集合从未被触碰。
            for path in _glob_owned(txn.staging):
                _remove(path)
        else:
            # switching/cleanup：幂等前滚。先把尚未改名的旧集合补到备份名
            # （switch-manifest / switch-old 处崩溃时旧名可能还在），再落新名。
            for path, backup in (manifest.get("backups") or {}).items():
                if os.path.exists(path) and not os.path.exists(backup):
                    try:
                        os.replace(path, backup)
                    except OSError:
                        pass
            moves = manifest.get("moves") or []
            for move in moves:
                src, dst = move.get("src"), move.get("dst")
                if not src or not dst:
                    continue
                if os.path.exists(dst):
                    _remove(src)
                elif os.path.exists(src):
                    os.replace(src, dst)
            for backup in (manifest.get("backups") or {}).values():
                _remove(backup)
            par2_conf = manifest.get("par2") or {}
            percent = int(par2_conf.get("percent") or 0)
            if percent > 0 and engine is not None and getattr(engine, "par2", None):
                files = [m["dst"] for m in moves if os.path.exists(m["dst"])]
                try:
                    from . import par2 as par2_mod
                    par2_mod.create(engine.par2, files, percent,
                                    single=bool(par2_conf.get("single")),
                                    on_progress=None, cancel=None)
                except Exception as exc:  # noqa: BLE001
                    print("[arca] 恢复覆盖压缩时生成 PAR2 失败: %r" % (exc,),
                          file=sys.stderr)
        _remove(_manifest_path(txn.id))
        handled.append(txn.id)
    return handled
