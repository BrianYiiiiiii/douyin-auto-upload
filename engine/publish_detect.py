"""发布状态检测（只读）：L1 URL 跳转 / L2 成功文案 / L3 作品列表匹配。"""
from __future__ import annotations

import time
from typing import Any

from . import cdp as cdp_module

MANAGE_URL_HINTS = ("content/manage",)
SUCCESS_TEXT_HINTS = ("发布成功", "作品发布成功")
WORK_STATE_HINTS = [
    ("审核中", "reviewing"),
    ("审核不通过", "rejected"),
    ("未通过", "rejected"),
    ("已发布", "published"),
    ("仅自己可见", "private"),
]
CHECK_URL = "https://creator.douyin.com/creator-micro/content/manage"
PUBLISH_PAGE_HINTS = ("content/post/video", "content/upload")


def is_publish_page(url: str) -> bool:
    return any(hint in str(url or "") for hint in PUBLISH_PAGE_HINTS)


def read_page(port: int) -> dict[str, str]:
    page = cdp_module.ensure_page(port, CHECK_URL)
    client = cdp_module.CDP(page["webSocketDebuggerUrl"])
    try:
        url = cdp_module.evaluate(client, "() => location.href") or ""
        title = cdp_module.evaluate(client, "() => document.title") or ""
        text = cdp_module.evaluate(client, "() => document.body ? document.body.innerText.slice(0, 8000) : ''") or ""
        return {"url": url, "title": title, "text": text}
    finally:
        client.close()


def work_status_from_text(text: str) -> str:
    for hint, status in WORK_STATE_HINTS:
        if hint in text:
            return status
    return ""


def detect(port: int, title: str = "") -> dict[str, Any]:
    """只读检测发布结果；不刷新、不点击。"""
    checked_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        page = read_page(port)
    except Exception as exc:
        return {"matched": False, "error": str(exc), "checked_at": checked_at, "work_status": ""}
    matched_by = ""
    publish_page = is_publish_page(page["url"])
    if not publish_page:
        if any(hint in page["url"] for hint in MANAGE_URL_HINTS):
            matched_by = "url_jump"
        elif any(hint in page["text"] for hint in SUCCESS_TEXT_HINTS):
            matched_by = "success_text"
        elif title and title[:6] in page["text"]:
            if work_status_from_text(page["text"]):
                matched_by = "work_list"
    # 发布页本身也包含“作品发布 / 仅自己可见 / 标题”等文案；在发布页上不能判定已发布。
    work_status = "" if publish_page else work_status_from_text(page["text"])
    evidence = {
        "matched": bool(matched_by),
        "matched_by": matched_by,
        "checked_at": checked_at,
        "url": page["url"],
        "title": page["title"],
        "work_status": work_status,
        "text_fragment": page["text"][:300],
    }
    return evidence
