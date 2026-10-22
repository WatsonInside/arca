"""Minimal CDP client (no third-party deps) used to drive Chrome for UI shots."""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import time
import urllib.request

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class WS:
    def __init__(self, url: str):
        # ws://host:port/path
        rest = url.split("://", 1)[1]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("handshake closed")
            buf += chunk
        if b"101" not in buf.split(b"\r\n")[0]:
            raise RuntimeError("handshake failed: " + buf[:200].decode("latin1"))
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self.next_id = 0

    def _recv_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("socket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, payload: bytes) -> None:
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send(self, method: str, params=None, await_id=None):
        self.next_id += 1
        msg = {"id": self.next_id, "method": method, "params": params or {}}
        self._frame(json.dumps(msg).encode())
        return self.next_id

    def recv(self):
        data = b""
        opcode = 1
        while True:
            b1, b2 = self._recv_exact(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._recv_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._recv_exact(8))[0]
            data += self._recv_exact(ln)
            if fin:
                break
        if opcode == 8:
            raise RuntimeError("websocket closed")
        return json.loads(data.decode("utf-8", "replace"))

    def call(self, method: str, params=None, timeout=60):
        mid = self.send(method, params)
        end = time.time() + timeout
        while time.time() < end:
            msg = self.recv()
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(method)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def launch(port: int, profile: str, size=(1120, 764), scale=2):
    os.makedirs(profile, exist_ok=True)
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
         f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
         f"--window-size={size[0]},{size[1]}",
         f"--force-device-scale-factor={scale}",
         "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
         "--disable-crash-reporter", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws_url = None
    for _ in range(80):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as r:
                tabs = json.loads(r.read().decode())
            for t in tabs:
                if t.get("type") == "page":
                    ws_url = t["webSocketDebuggerUrl"]
                    break
        except Exception:
            pass
        if ws_url:
            break
        time.sleep(0.25)
    if not ws_url:
        proc.kill()
        raise RuntimeError("chrome devtools not reachable")
    return proc, WS(ws_url)


def shoot(ws: WS, path: str, full=False):
    kwargs = {"format": "png", "captureBeyondViewport": False}
    if full:
        kwargs["captureBeyondViewport"] = True
    res = ws.call("Page.captureScreenshot", kwargs)
    with open(path, "wb") as fh:
        fh.write(base64.b64decode(res["data"]))
    return path
