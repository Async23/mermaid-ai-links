"""Minimal Chrome DevTools Protocol adapter scoped to one target tab.

Unlike Playwright's ``connect_over_cdp``, this adapter never auto-attaches to
every page in the browser.  It discovers targets over Chrome's JSON endpoint,
then opens the WebSocket belonging only to the selected target.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import websocket


class CdpError(RuntimeError):
    """Chrome CDP could not complete a target-scoped operation."""


@dataclass(frozen=True)
class TargetInfo:
    target_id: str
    type: str
    url: str
    title: str
    websocket_url: str


def _read_json(url: str, timeout_seconds: float) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise CdpError(f"无法读取 Chrome CDP endpoint {url}: {exc}") from exc


class CdpConnection:
    """Synchronous request/response connection to one CDP WebSocket."""

    def __init__(self, websocket_url: str, timeout_seconds: float) -> None:
        self._websocket_url = websocket_url
        self._timeout_seconds = timeout_seconds
        self._next_id = 1
        try:
            self._socket = websocket.create_connection(
                websocket_url,
                timeout=timeout_seconds,
                suppress_origin=True,
            )
        except (OSError, websocket.WebSocketException) as exc:
            raise CdpError(f"无法连接目标标签 CDP WebSocket: {exc}") from exc

    def __enter__(self) -> CdpConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._socket.close()
        except Exception:
            pass

    def call(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        request: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            request["params"] = dict(params)
        try:
            self._socket.send(json.dumps(request, ensure_ascii=False, separators=(",", ":")))
            while True:
                raw = self._socket.recv()
                message = json.loads(raw)
                if message.get("id") != request_id:
                    continue
                error = message.get("error")
                if error:
                    detail = error.get("message") if isinstance(error, dict) else str(error)
                    raise CdpError(f"CDP {method} 失败: {detail}")
                result = message.get("result", {})
                return result if isinstance(result, dict) else {}
        except CdpError:
            raise
        except (
            socket.timeout,
            websocket.WebSocketTimeoutException,
            websocket.WebSocketConnectionClosedException,
            websocket.WebSocketException,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            raise CdpError(f"CDP {method} 通信失败: {exc}") from exc

    def evaluate(self, expression: str) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            if isinstance(details, dict):
                exception = details.get("exception", {})
                description = exception.get("description") if isinstance(exception, dict) else None
                text = description or details.get("text", "JavaScript evaluation failed")
            else:
                text = str(details)
            raise CdpError(f"目标标签脚本执行失败: {text}")
        remote = result.get("result", {})
        if isinstance(remote, dict):
            if remote.get("subtype") == "error":
                raise CdpError(f"目标标签脚本执行失败: {remote.get('description', 'JavaScript Error')}")
            return remote.get("value")
        return None


class ChromeCdp:
    """Discover Chrome targets and connect only to a selected target."""

    def __init__(self, cdp_url: str, timeout_ms: int) -> None:
        self.cdp_url = cdp_url.rstrip("/")
        self.timeout_seconds = max(timeout_ms, 1) / 1000

    def targets(self) -> tuple[TargetInfo, ...]:
        value = _read_json(f"{self.cdp_url}/json/list", min(self.timeout_seconds, 5.0))
        if not isinstance(value, list):
            raise CdpError("Chrome CDP /json/list 没有返回 target 数组")
        targets: list[TargetInfo] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            target_id = item.get("id")
            websocket_url = item.get("webSocketDebuggerUrl")
            if not isinstance(target_id, str) or not isinstance(websocket_url, str):
                continue
            targets.append(
                TargetInfo(
                    target_id=target_id,
                    type=str(item.get("type", "")),
                    url=str(item.get("url", "")),
                    title=str(item.get("title", "")),
                    websocket_url=websocket_url,
                )
            )
        return tuple(targets)

    def find_target(self, predicate: Callable[[TargetInfo], bool]) -> TargetInfo | None:
        return next((target for target in self.targets() if target.type == "page" and predicate(target)), None)

    def target_by_id(self, target_id: str) -> TargetInfo | None:
        return next((target for target in self.targets() if target.target_id == target_id), None)

    def connect(self, target: TargetInfo) -> CdpConnection:
        return CdpConnection(target.websocket_url, self.timeout_seconds)

    def create_background_target(self, url: str) -> str:
        version = _read_json(f"{self.cdp_url}/json/version", min(self.timeout_seconds, 5.0))
        websocket_url = version.get("webSocketDebuggerUrl") if isinstance(version, dict) else None
        if not isinstance(websocket_url, str):
            raise CdpError("Chrome CDP /json/version 缺少 webSocketDebuggerUrl")
        with CdpConnection(websocket_url, self.timeout_seconds) as browser:
            result = browser.call("Target.createTarget", {"url": url, "background": True})
        target_id = result.get("targetId")
        if not isinstance(target_id, str):
            raise CdpError("Target.createTarget 没有返回 targetId")
        return target_id

    def probe(self) -> int:
        """Verify a real browser-level CDP round trip and return page count."""
        version = _read_json(f"{self.cdp_url}/json/version", min(self.timeout_seconds, 3.0))
        websocket_url = version.get("webSocketDebuggerUrl") if isinstance(version, dict) else None
        if not isinstance(websocket_url, str):
            raise CdpError("Chrome CDP /json/version 缺少 webSocketDebuggerUrl")
        with CdpConnection(websocket_url, min(self.timeout_seconds, 3.0)) as browser:
            browser.call("Browser.getVersion")
        return sum(target.type == "page" for target in self.targets())
