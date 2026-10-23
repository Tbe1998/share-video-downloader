#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简 Chrome DevTools Protocol 客户端（纯标准库 WebSocket，无依赖）。
只实现本项目需要的：连页面 target、发命令、取返回值。
"""
import base64
import hashlib
import json
import os
import socket
from urllib.parse import urlparse


class WSError(RuntimeError):
    pass


class WebSocket:
    def __init__(self, url, timeout=30.0):
        u = urlparse(url)
        host = u.hostname
        port = u.port or 80
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n") % (path, host, port, key)
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("WebSocket 握手失败：连接被关闭")
            head += chunk
        header, _, rest = head.partition(b"\r\n\r\n")
        first = header.split(b"\r\n")[0].decode("latin1")
        if "101" not in first:
            raise WSError("WebSocket 握手失败：%s" % first)
        self._buf = rest

    # ---------- 底层收发 ----------
    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise WSError("连接已断开")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, payload, opcode=1):
        mask = os.urandom(4)
        n = len(payload)
        header = bytes([0x80 | opcode])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            header += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _recv_message(self):
        data = b""
        while True:
            b0, b1 = self._read(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            ln = b1 & 0x7F
            if ln == 126:
                ln = int.from_bytes(self._read(2), "big")
            elif ln == 127:
                ln = int.from_bytes(self._read(8), "big")
            mkey = self._read(4) if masked else b""
            payload = self._read(ln) if ln else b""
            if masked:
                payload = bytes(c ^ mkey[i % 4] for i, c in enumerate(payload))
            if opcode == 0x8:
                raise WSError("对端关闭连接")
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0xA:
                continue
            data += payload
            if fin:
                return data

    def send_text(self, text):
        self._send_frame(text.encode("utf-8"), opcode=1)

    def recv_json(self):
        return json.loads(self._recv_message().decode("utf-8", "replace"))

    def close(self):
        try:
            self._send_frame(b"", opcode=0x8)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class CDPSession:
    """连到一个页面 target，提供 call(method, params) -> result"""

    def __init__(self, ws_url, timeout=30.0):
        self.ws = WebSocket(ws_url, timeout=timeout)
        self._id = 0
        self.events = []

    def call(self, method, params=None, timeout=30.0):
        self._id += 1
        mid = self._id
        self.ws.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = self.ws.recv_json()
            if msg.get("id") == mid:
                if "error" in msg:
                    raise WSError("%s 失败: %s" % (method, msg["error"]))
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)

    def evaluate(self, expression, await_promise=False, timeout=60.0):
        res = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": True,
        }, timeout=timeout)
        if res.get("exceptionDetails"):
            d = res["exceptionDetails"]
            raise WSError("页面脚本出错: %s" % (d.get("text") or d))
        return (res.get("result") or {}).get("value")

    def close(self):
        self.ws.close()
