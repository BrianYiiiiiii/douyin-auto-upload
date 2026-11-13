"""最小 Chrome DevTools Protocol 客户端（基于 websocket-client）。"""
from __future__ import annotations

import json
import time
from typing import Any

import websocket

from . import chrome


class CDPError(Exception):
    pass


class CDP:
    def __init__(self, ws_url: str, timeout: float = 20.0,
                 auto_accept_beforeunload: bool = False):
        self.ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        self._id = 0
        self.auto_accept_beforeunload = bool(auto_accept_beforeunload)
        self.dialogs_handled = 0

    def _send_no_wait(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))

    def _handle_event(self, msg: dict[str, Any]) -> None:
        if not self.auto_accept_beforeunload:
            return
        if msg.get("method") != "Page.javascriptDialogOpening":
            return
        params = msg.get("params") or {}
        # 只自动确认“离开网页”这类 beforeunload；不替用户确认其他弹窗
        if str(params.get("type") or "") != "beforeunload":
            return
        self._send_no_wait("Page.handleJavaScriptDialog", {"accept": True})
        self.dialogs_handled += 1

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 25.0) -> dict[str, Any]:
        self.ws.settimeout(timeout)
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            raw = self.ws.recv()
            msg = json.loads(raw)
            if msg.get("id") != mid:
                self._handle_event(msg)
                continue
            if "error" in msg:
                raise CDPError(f"{method}: {msg['error']}")
            return msg.get("result") or {}

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def browser_ws(port: int) -> str:
    info = chrome.http_json(f"http://127.0.0.1:{port}/json/version", timeout=3.0)
    ws = (info or {}).get("webSocketDebuggerUrl")
    if not ws:
        raise CDPError("no browser webSocketDebuggerUrl")
    return ws


def ensure_page(port: int, url: str) -> dict[str, Any]:
    pages = [t for t in chrome.list_targets(port) if t.get("type") == "page"]
    page = next((t for t in pages if "creator.douyin.com" in (t.get("url") or "")), None)
    if page is None and pages:
        page = pages[0]
    if page is None:
        page = chrome.new_target(port, url)
        if page is None:
            raise CDPError("无法创建页面")
        time.sleep(0.8)
    return page


def fresh_page(port: int, url: str) -> dict[str, Any]:
    """复用当前页面做“新文档”，不再新开/关闭标签，避免上传时标签闪现。

    上传组件跨视频复用会僵死，因此调用方需要对返回页面做 about:blank → UPLOAD_URL 的硬重置。
    只有浏览器一个 page 都没有时才创建 1 个 target；其余多余 page（含会话恢复/站点弹窗）关闭。
    """
    pages = [t for t in chrome.list_targets(port) if t.get("type") == "page"]
    page = next((t for t in pages if "creator.douyin.com" in (t.get("url") or "")), None)
    if page is None and pages:
        page = pages[0]
    if page is None:
        page = chrome.new_target(port, url)
        if page is None:
            raise CDPError("无法创建页面")
        time.sleep(0.8)
        return page
    page_id = str(page.get("id") or "")
    for target in pages:
        target_id = str(target.get("id") or "")
        if not target_id or target_id == page_id:
            continue
        chrome.close_target(port, target_id)
    return page


class TabGuard:
    """后台监听浏览器 page target：记录标签事件，并关闭站点弹出的非 creator 页面。

    使用浏览器级 CDP 事件，不阻塞上传主流程。日志写入 runtime/logs/tabs/<job>.jsonl。
    """

    def __init__(self, port: int, log_path: Any, allow_ids: set[str] | None = None) -> None:
        self.port = int(port)
        self.log_path = log_path
        self._allowed = {str(x) for x in (allow_ids or set()) if x}
        self._seen: dict[str, float] = {}
        self._stop = False
        self._thread = None

    def _is_allowed(self, target_id: str, page_url: str) -> bool:
        return (target_id in self._allowed) or ("creator.douyin.com" in str(page_url or ""))

    def _sweep(self) -> None:
        try:
            pages = [t for t in chrome.list_targets(self.port) if t.get("type") == "page"]
        except Exception:
            return
        now = time.time()
        ids = set()
        for target in pages:
            target_id = str(target.get("id") or "")
            page_url = str(target.get("url") or "")
            ids.add(target_id)
            if not target_id or self._is_allowed(target_id, page_url):
                continue
            self._seen.setdefault(target_id, now)
            age = now - self._seen.get(target_id, now)
            if page_url and "creator.douyin.com" in page_url:
                continue
            if (not page_url) or page_url.startswith("about:") or page_url.startswith("data:"):
                if age < 2.0:
                    continue
            if chrome.close_target(self.port, target_id):
                self._log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "event": "closed_extra_tab", "target_id": target_id,
                           "url": page_url, "age_s": round(age, 1)})
        for tid in list(self._seen):
            if tid not in ids:
                self._seen.pop(tid, None)
        self._seen = {tid: ts for tid, ts in self._seen.items() if tid in ids}

    def _log(self, item: dict[str, Any]) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def start(self) -> None:
        import threading
        self._thread = threading.Thread(target=self._run, name="tab-guard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        try:
            if self._thread is not None:
                self._thread.join(timeout=2.0)
        except Exception:
            pass

    def _run(self) -> None:
        while not self._stop:
            if not chrome.cdp_alive(self.port):
                time.sleep(1.0)
                continue
            ws = None
            try:
                ws = websocket.create_connection(browser_ws(self.port), timeout=5.0, suppress_origin=True)
                ws.settimeout(1.0)
                ws.send(json.dumps({"id": 1, "method": "Target.setDiscoverTargets",
                                    "params": {"discover": True}}))
                while not self._stop:
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        self._sweep()
                        continue
                    except Exception:
                        break
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    method = str(msg.get("method") or "")
                    params = msg.get("params") or {}
                    info = params.get("targetInfo") or {}
                    if method in ("Target.targetCreated", "Target.targetInfoChanged"):
                        if str(info.get("type") or "") != "page":
                            continue
                        target_id = str(info.get("targetId") or "")
                        page_url = str(info.get("url") or "")
                        self._log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                   "event": method.replace("Target.", ""),
                                   "target_id": target_id, "url": page_url})
                        if self._is_allowed(target_id, page_url):
                            continue
                        self._seen.setdefault(target_id, time.time())
                        if page_url and "creator.douyin.com" not in page_url and not page_url.startswith("about:") and not page_url.startswith("data:"):
                            # 站点 popup / 会话恢复的普通页面；关闭并记录（creator/主页面保留）
                            if chrome.close_target(self.port, target_id):
                                self._log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                           "event": "closed_non_creator", "target_id": target_id,
                                           "url": page_url})
                    elif method == "Target.targetDestroyed":
                        self._log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                   "event": "targetDestroyed",
                                   "target_id": str(params.get("targetId") or "")})
            except Exception as exc:
                self._log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "event": "tab_guard_reconnect", "error": str(exc)[:200]})
                time.sleep(1.0)
            finally:
                try:
                    if ws is not None:
                        ws.close()
                except Exception:
                    pass


def evaluate(client: CDP, expression: str, await_promise: bool = True) -> Any:
    expr = (expression or "").strip()
    if expr.startswith("() =>") or expr.startswith("async () =>") or expr.startswith("async("):
        expr = f"({expr})()"
    result = client.call("Runtime.evaluate", {
        "expression": expr,
        "returnByValue": True,
        "awaitPromise": await_promise,
    })
    if "exceptionDetails" in result:
        raise CDPError(f"JS 异常：{result['exceptionDetails']}")
    value = (result.get("result") or {}).get("value")
    return value


def navigate(client: CDP, url: str) -> None:
    client.call("Page.navigate", {"url": url})


def wait_ready(client: CDP, timeout: float = 90.0, interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            state = evaluate(client, "() => document.readyState", await_promise=False)
            if state in ("interactive", "complete"):
                return True
        except CDPError:
            pass
        time.sleep(interval)
    return False


def get_all_cookies(port: int) -> list[dict[str, Any]]:
    client = CDP(browser_ws(port))
    try:
        try:
            result = client.call("Storage.getCookies")
        except CDPError:
            result = client.call("Network.getAllCookies")
        return list(result.get("cookies") or [])
    finally:
        client.close()
