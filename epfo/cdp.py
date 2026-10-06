"""Minimal Chrome DevTools Protocol client (stdlib only, no websocket dep).

Used for two things the portal makes impossible otherwise:

* reading the JavaScript the page actually loads, including the post-login
  bundle, so the endpoint surface can be enumerated honestly;
* driving a real browser session when the portal's token/OTP dance needs one.

Deliberately narrow: it speaks just enough of CDP (attach, enable domains,
collect network events, evaluate, screenshot, navigate) to be auditable.
"""

from __future__ import annotations

import base64
import itertools
import json
import socket
import struct
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.request import urlopen


class CDPError(RuntimeError):
    """Raised when the browser or protocol rejects an operation."""


@dataclass
class CDPClient:
    """A CDP connection over a raw socket to one page target."""

    endpoint: str
    target_id: str | None = None
    _sock: socket.socket | None = field(default=None, init=False, repr=False)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1),
                                 init=False, repr=False)
    _events: list[dict] = field(default_factory=list, init=False, repr=False)
    _text: str = field(default="", init=False, repr=False)

    # -- lifecycle --------------------------------------------------------

    def __enter__(self) -> "CDPClient":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _target_ws_url(self) -> str:
        base = self.endpoint.rstrip("/")
        if not base.startswith("http"):
            base = f"http://{base}"
        with urlopen(f"{base}/json/list", timeout=20) as response:
            targets = json.load(response)
        if self.target_id:
            for target in targets:
                if target.get("id") == self.target_id:
                    return target["webSocketDebuggerUrl"]
            raise CDPError(f"no target with id {self.target_id!r}")
        for target in targets:
            if target.get("type") == "page":
                return target["webSocketDebuggerUrl"]
        raise CDPError("no page target found")

    def connect(self) -> None:
        parsed = urlparse(self._target_ws_url())
        sock = socket.create_connection((parsed.hostname, parsed.port or 80),
                                        timeout=30)
        key = base64.b64encode(b"hermes-epfo-cdp").decode()
        handshake = (
            f"GET {parsed.path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(handshake.encode())
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = sock.recv(4096)
            if not chunk:
                raise CDPError("websocket handshake closed early")
            header += chunk
        if b"101" not in header.split(b"\r\n")[0]:
            raise CDPError(f"websocket handshake failed: {header[:200]!r}")
        self._sock = sock

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None

    # -- framing ----------------------------------------------------------

    def _send_frame(self, payload: str) -> None:
        data = payload.encode()
        header = bytearray([0x81])
        length = len(data)
        if length < 126:
            header.append(length | 0x80)
        elif length < 65536:
            header.append(126 | 0x80)
            header += struct.pack(">H", length)
        else:
            header.append(127 | 0x80)
            header += struct.pack(">Q", length)
        mask = b"epfo"
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self._sock.sendall(bytes(header) + masked)

    def _read_exact(self, count: int) -> bytes:
        buffer = b""
        while len(buffer) < count:
            chunk = self._sock.recv(count - len(buffer))
            if not chunk:
                raise CDPError("websocket closed while reading")
            buffer += chunk
        return buffer

    def _recv_frame(self) -> str:
        first, second = self._read_exact(2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        payload = self._read_exact(length)
        if first & 0x0F == 0x8:  # close
            raise CDPError("browser closed the CDP connection")
        return payload.decode("utf-8", errors="replace")

    def call(self, method: str, params: dict | None = None,
             timeout: float = 30.0) -> dict:
        """Issue a CDP command and return its result, collecting events."""
        message_id = next(self._ids)
        self._send_frame(json.dumps(
            {"id": message_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._sock.settimeout(max(deadline - time.time(), 0.1))
            try:
                message = json.loads(self._recv_frame())
            except socket.timeout:
                continue
            if "id" in message and message["id"] == message_id:
                if "error" in message:
                    raise CDPError(f"{method} failed: {message['error']}")
                return message.get("result", {})
            self._events.append(message)
        raise CDPError(f"{method} timed out after {timeout}s")

    def drain(self, seconds: float) -> list[dict]:
        """Collect events for a fixed window (network traffic, console, ...)."""
        deadline = time.time() + seconds
        self._sock.settimeout(0.25)
        while time.time() < deadline:
            try:
                self._events.append(json.loads(self._recv_frame()))
            except (socket.timeout, OSError):
                continue
        return self._events

    # -- convenience ------------------------------------------------------

    def enable(self, domains: list[str]) -> None:
        for domain in domains:
            self.call(f"{domain}.enable")

    def script_urls(self, origins: list[str] | None = None) -> list[str]:
        """Every script URL the page has loaded (network + document scripts)."""
        urls: set[str] = set()
        for event in self.drain(1.5):
            if event.get("method") == "Network.responseReceived":
                url = event["params"]["response"].get("url", "")
                mime = event["params"]["response"].get("mimeType", "")
                if "javascript" in mime or url.endswith(".js"):
                    urls.add(url)
        tree = self.call("Page.getResourceTree").get("frameTree", {})
        stack = [tree]
        while stack:
            node = stack.pop()
            for resource in node.get("resources", []):
                if resource.get("type") == "Script":
                    urls.add(resource["url"])
            stack.extend(node.get("childFrames", []))
        if origins:
            urls = {u for u in urls if any(u.startswith(o) for o in origins)}
        return sorted(urls)

    def fetch_text(self, url: str) -> str:
        result = self.call("Network.getResponseBody", {"url": url}, timeout=20)
        if result.get("base64Encoded"):
            return base64.b64decode(result["body"]).decode("utf-8", "replace")
        return result["body"]

    def evaluate(self, expression: str, timeout: float = 30.0):
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        }, timeout=timeout)
        return result.get("result", {}).get("value")

    def navigate(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})

    def content(self) -> str:
        return self.evaluate("document.documentElement.outerHTML") or ""

    def screenshot(self, path: str) -> str:
        result = self.call("Page.captureScreenshot", {"format": "png"})
        with open(path, "wb") as handle:
            handle.write(base64.b64decode(result["data"]))
        return path
