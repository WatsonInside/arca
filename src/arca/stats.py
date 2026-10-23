"""Anonymous usage pings: one GET per install, one per active day.

The request path carries all the data (event/version/platform/install-id) so
the receiving object store's access log doubles as the database — nothing is
stored client-side beyond a random id and two dates. The state file is written
before the request is attempted (at-most-once): a dropped ping costs one day
of precision, while retrying would cost the user's battery and patience.
"""
from __future__ import annotations

import os
import platform
import re
import threading
import time
import urllib.request
import uuid

from . import util

ENDPOINT = "https://event.hushemeng.group/ping/v1"
_TIMEOUT = 5


def _state_path() -> str:
    return util.state_path("stats.json")


def _safe(segment: str) -> str:
    segment = re.sub(r"[^0-9A-Za-z._-]", "-", segment or "x")
    return segment[:64] or "x"


def _platform() -> str:
    major = (platform.mac_ver()[0] or "0").split(".")[0]
    return _safe("mac" + major + "-" + os.uname().machine)


def _url(event: str, iid: str) -> str:
    return "/".join((ENDPOINT, _safe(event), _safe(util.APP_VERSION), _platform(), iid))


def _send(event: str, iid: str) -> None:
    try:
        urllib.request.urlopen(urllib.request.Request(
            _url(event, iid), headers={"User-Agent": "Arca-Stats"}), timeout=_TIMEOUT).read(1)
    except Exception:
        pass


def maybe_ping(settings, headless: bool = False) -> None:
    """Fire today's ping in a background thread; returns immediately.

    Headless instances are the test fleet (isolated HOME, fresh install ids on
    every run) — letting them report would drown the real numbers, so they
    never do.
    """
    if headless or settings.get("anonymousStats") is False:
        return
    # First launch shows an allow/deny notice; until a choice is made nothing
    # leaves the machine ("refuse" then means zero requests, ever).
    if not settings.get("statsPrompted"):
        return
    today = time.strftime("%Y-%m-%d")
    state = util.read_json(_state_path(), {})
    first = not state.get("installedAt")
    if not first and state.get("lastPing") == today:
        return
    iid = state.get("iid") or uuid.uuid4().hex
    state["iid"] = iid
    state["installedAt"] = state.get("installedAt") or today
    state["lastPing"] = today
    util.write_json(_state_path(), state)
    threading.Thread(target=_send, args=("install" if first else "ping", iid), daemon=True).start()
