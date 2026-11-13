"""发布前自动化：登录硬校验 → 上传视频/填文案 → 停在发布前（绝不点发布）。"""
from __future__ import annotations

import base64
import json
import shutil
import time
from pathlib import Path
from typing import Any

from .accounts import ApiError
from .config import Config
from . import cdp as cdp_module, chrome, login, videos

UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
DEFAULT_SELECTORS = {
    "file_input": "input[type=file]",
    "title": "input[placeholder*='填写作品标题']",
    "desc": "div[contenteditable='true']",
    "tags": "div[contenteditable='true']",
}


def load_selectors(config: Config) -> dict[str, str]:
    selectors = dict(DEFAULT_SELECTORS)
    path = config.paths.data / "upload_selectors.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                selectors.update({str(k): str(v) for k, v in data.items()})
        except (OSError, ValueError):
            pass
    return selectors


def preflight(config: Config, account: dict[str, Any], package: dict[str, Any],
              video_info: dict[str, Any], simulate: bool = False) -> list[dict[str, Any]]:
    """上传前预检测：账号、视频、内容、磁盘、资质提醒。"""
    checks: list[dict[str, Any]] = []

    def add(cid: str, name: str, ok: bool, detail: str) -> None:
        checks.append({"id": cid, "name": name, "ok": bool(ok), "detail": detail})

    account_ok = account.get("account_status") == "READY" or simulate
    add("account", "账号已登录 READY", account_ok,
        "模拟模式跳过" if simulate else str(account.get("account_status")))
    try:
        verified = videos.verify_video(config, video_info.get("path", ""), video_info.get("sha256", ""))
        add("video", "视频存在 / sha256 一致", True, str(verified.get("size_bytes")) + " 字节")
    except ApiError as exc:
        add("video", "视频存在 / sha256 一致", False, exc.message)
    content = package.get("content") or {}
    content_ok = bool(content.get("title")) and bool(content.get("desc")) and bool(content.get("tags"))
    add("content", "内容包字段完整", content_ok,
        "title/desc/tags 齐全" if content_ok else "缺少标题/简介/话题")
    try:
        try:
            config.paths.temp.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        usage = shutil.disk_usage(str(config.paths.root))
        free_gb = usage.free / (1024 ** 3)
        add("disk", "磁盘剩余 ≥ 2GB", free_gb >= 2.0, str(round(free_gb, 1)) + " GB")
    except OSError as exc:
        add("disk", "磁盘剩余 ≥ 2GB", False, str(exc))
    add("qualification", "带货资质（保证金/商品分享）", True,
        "无法自动检测；若商品链接被拒，先确认已缴保证金/开通商品分享")
    return checks


UPLOAD_START_SIGNALS = ("上传中", "重新上传", "编辑封面", "上传完成")


def upload_wait_budget(size_bytes: int) -> int:
    """按 10 秒/MB + 120 秒预算估算，夹在 300–1800 秒。参考 15 号项目实测。"""
    size_mb = max(0.0, float(size_bytes or 0) / (1024 * 1024))
    return int(max(300, min(1800, 120 + size_mb * 10)))


def has_upload_started(text: str, has_progress: bool) -> bool:
    return bool(has_progress) or any(sig in str(text or "") for sig in UPLOAD_START_SIGNALS)


def has_upload_completed(text: str, stem: str = "") -> bool:
    value = str(text or "")
    if "重新上传" in value or "编辑封面" in value:
        return True
    return bool(stem and stem in value)


def _js(client: Any, script: str) -> Any:
    return cdp_module.evaluate(client, script, await_promise=False)


def _fill_selector(client: Any, selector: str, value: str) -> bool:
    script = (
        "(function(){var el=document.querySelector(" + json.dumps(selector) + ");"
        "if(!el)return false;el.focus();"
        "if(el.isContentEditable){el.innerText=" + json.dumps(value) + ";}else{el.value=" + json.dumps(value) + ";}"
        "el.dispatchEvent(new Event('input',{bubbles:true}));"
        "el.dispatchEvent(new Event('change',{bubbles:true}));return true;})()"
    )
    try:
        return bool(_js(client, script))
    except Exception:
        return False


def _set_input_value(client: Any, selector: str, value: str) -> dict[str, Any]:
    """用原生 value setter + input/change 事件写 React 输入框，并读回校验。"""
    script = (
        "(function(){var el=document.querySelector(" + json.dumps(selector) + ");"
        "if(!el)return {ok:false,reason:'not_found'};el.focus();"
        "var proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;"
        "try{Object.getOwnPropertyDescriptor(proto,'value').set.call(el," + json.dumps(value) + ");}"
        "catch(e){el.value=" + json.dumps(value) + ";}"
        "el.dispatchEvent(new Event('input',{bubbles:true}));"
        "el.dispatchEvent(new Event('change',{bubbles:true}));"
        "return {ok:true,value:el.value};})()"
    )
    try:
        result = _js(client, script) or {}
        return {"ok": bool(result.get("ok")) and str(result.get("value") or "") == str(value),
                "value": str(result.get("value") or ""), "reason": result.get("reason") or ""}
    except Exception as exc:
        return {"ok": False, "value": "", "reason": str(exc)}


def _focus_editor_end(client: Any, selector: str) -> bool:
    script = (
        "(function(){var el=document.querySelector(" + json.dumps(selector) + ");"
        "if(!el)return false;el.focus();"
        "var r=document.createRange();r.selectNodeContents(el);r.collapse(false);"
        "var s=window.getSelection();s.removeAllRanges();s.addRange(r);return true;})()"
    )
    try:
        return bool(_js(client, script))
    except Exception:
        return False


def _insert_text(client: Any, text: str) -> bool:
    try:
        client.call("Input.insertText", {"text": str(text)})
        return True
    except Exception:
        try:
            return bool(_js(client, "(function(){return document.execCommand('insertText',false," + json.dumps(str(text)) + ");})()"))
        except Exception:
            return False


def _press_space(client: Any) -> None:
    client.call("Input.dispatchKeyEvent", {"type": "keyDown", "windowsVirtualKeyCode": 32,
                                           "nativeVirtualKeyCode": 32, "key": " ", "code": "Space"})
    client.call("Input.dispatchKeyEvent", {"type": "char", "text": " ", "key": " "})
    client.call("Input.dispatchKeyEvent", {"type": "keyUp", "windowsVirtualKeyCode": 32,
                                           "nativeVirtualKeyCode": 32, "key": " ", "code": "Space"})


def _clear_editor(client: Any, selector: str) -> bool:
    script = (
        "(function(){var el=document.querySelector(" + json.dumps(selector) + ");"
        "if(!el)return false;el.focus();"
        "var r=document.createRange();r.selectNodeContents(el);"
        "var s=window.getSelection();s.removeAllRanges();s.addRange(r);"
        "try{document.execCommand('delete');}catch(e){}return true;})()"
    )
    try:
        return bool(_js(client, script))
    except Exception:
        return False


def _editor_text(client: Any, selector: str) -> str:
    script = (
        "(function(){var el=document.querySelector(" + json.dumps(selector) + ");"
        "return el?(el.innerText||''):'';})()"
    )
    try:
        return str(_js(client, script) or "")
    except Exception:
        return ""


def _fill_editor(client: Any, selector: str, text: str) -> bool:
    """清空 contenteditable 后插入正文，避免编辑器保留 \u200b/空行导致简介从第二行开始。"""
    if not _clear_editor(client, selector):
        return False
    time.sleep(0.25)
    if not _focus_editor_end(client, selector):
        return False
    return _insert_text(client, str(text or "").strip())


def _click_selector(client: Any, selector: str) -> bool:
    script = (
        "(function(){var el=document.querySelector(" + json.dumps(selector) + ");"
        "if(!el)return false;el.scrollIntoView({block:'center'});"
        "var r=el.getBoundingClientRect();"
        "for(var i=0;i<1;i++){"
        "el.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}));"
        "el.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,cancelable:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}));"
        "el.click();}"
        "return true;})()"
    )
    try:
        return bool(_js(client, script))
    except Exception:
        return False


def _click_text(client: Any, text: str, tag: str = "") -> bool:
    script = (
        "(function(){var t=" + json.dumps(str(text or "")) + ";var tag=" + json.dumps(str(tag or "")) + ";"
        "var els=Array.from(document.querySelectorAll(tag||'*'));"
        "var cand=els.filter(function(e){var s=(e.innerText||'').trim();"
        "var r=e.getBoundingClientRect();return s===t && r.width>0 && r.height>0;});"
        "if(!cand.length)return false;var el=cand[cand.length-1];"
        "el.scrollIntoView({block:'center'});var r=el.getBoundingClientRect();"
        "el.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}));"
        "el.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,cancelable:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}));"
        "el.click();return true;})()"
    )
    try:
        return bool(_js(client, script))
    except Exception:
        return False


def _wait_body_text(client: Any, needles: list[str], timeout: float = 12.0) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            last = str(cdp_module.evaluate(client, "() => document.body ? document.body.innerText : ''") or "")
        except Exception:
            last = ""
        if any(n in last for n in needles):
            return next((n for n in needles if n in last), "")
        time.sleep(0.5)
    return ""


def _set_save_permission_disallow(client: Any) -> bool:
    script = (
        "(function(){var labels=Array.from(document.querySelectorAll('label'));"
        "for(var i=0;i<labels.length;i++){var el=labels[i];"
        "if((el.innerText||'').trim()!=='不允许')continue;"
        "var near=false,n=el;for(var k=0;k<8&&n;k++){if((n.innerText||'').includes('保存权限')){near=true;break;}n=n.parentElement;}"
        "if(!near)continue;if(el.getAttribute('data-checked')==='true')return {ok:true,skipped:true};"
        "el.click();return {ok:true,skipped:false};}"
        "return {ok:false};})()"
    )
    try:
        res = _js(client, script) or {}
        time.sleep(0.6)
        after = _js(client, """(function(){var labels=Array.from(document.querySelectorAll('label'));for(var i=0;i<labels.length;i++){var el=labels[i];if((el.innerText||'').trim()!=='不允许')continue;var near=false,n=el;for(var k=0;k<8&&n;k++){if((n.innerText||'').includes('保存权限')){near=true;break;}n=n.parentElement;}if(!near)continue;var inp=el.querySelector('input');return {dataChecked:el.getAttribute('data-checked'),inputChecked:inp?!!inp.checked:null};}return null;})()""") or {}
        return bool(after.get("dataChecked") == "true" or after.get("inputChecked") is True)
    except Exception:
        return False


def _select_recommended_cover(client: Any) -> bool:
    """优先选平台 AI 推荐的第一张封面；必要时点确认。"""
    try:
        clicked = bool(_js(client, """(function(){var els=Array.from(document.querySelectorAll('[class^="recommendCover-"]'));if(!els.length)return false;var el=els[0];el.scrollIntoView({block:'center'});var r=el.getBoundingClientRect();el.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}));el.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,cancelable:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}));el.click();return true;})()"""))
        time.sleep(1.2)
        if clicked and "是否确认应用此封面" in (cdp_module.evaluate(client, "() => document.body ? document.body.innerText : ''") or ""):
            _click_text(client, "确定", "button")
            _click_text(client, "确定", "span")
            time.sleep(1.0)
        return clicked
    except Exception:
        return False


def _wait_cover_ready(client: Any, timeout: float = 25.0) -> bool:
    """等待竖/横封面生成完成，避免任务停在“横封面缺失/生成中”。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            body = cdp_module.evaluate(client, "() => document.body ? document.body.innerText : ''") or ""
        except Exception:
            body = ""
        if "横封面缺失" not in body and "生成中" not in body:
            return True
        time.sleep(1.0)
    return False


def _close_cart_quota_modal(client: Any) -> bool:
    try:
        body = cdp_module.evaluate(client, "() => document.body ? document.body.innerText : ''") or ""
        if "无法添加购物车" in body or "购物车视频数量已经达到限额" in body:
            _click_text(client, "取消", "button")
            _click_text(client, "取消", "span")
            time.sleep(0.5)
            return True
    except Exception:
        pass
    return False


def _add_product_link(client: Any, link: str, short_title: str) -> dict[str, Any]:
    """添加标签→购物车→粘贴链接→添加链接→商品短标题→完成编辑。"""
    if not link:
        return {"ok": False, "reason": "商品链接为空"}
    try:
        # 打开“添加标签”下拉（当前值通常是“位置”）
        opened = bool(_js(client, """(function(){var all=Array.from(document.querySelectorAll('*'));var t=all.find(function(e){return (e.innerText||'').trim()==='添加标签';});if(!t)return false;var p=t;for(var i=0;i<3&&p;i++)p=p.parentElement;if(!p)return false;var sel=p.querySelector('.semi-select');if(!sel)return false;sel.click();return true;})()"""))
        if not opened:
            return {"ok": False, "reason": "未找到添加标签下拉"}
        time.sleep(0.8)
        if not _click_text(client, "购物车", '[role="option"]'):
            if not _js(client, """(function(){var o=Array.from(document.querySelectorAll('[role="option"]')).find(function(e){return (e.innerText||'').includes('购物车');});if(!o)return false;o.click();return true;})()"""):
                return {"ok": False, "reason": "下拉里没有购物车选项"}
        time.sleep(0.8)
        if not _js(client, """(function(){return !!document.querySelector('input[placeholder="粘贴商品链接"]');})()"""):
            return {"ok": False, "reason": "未出现商品链接输入框"}
        set_link = _set_input_value(client, 'input[placeholder="粘贴商品链接"]', link)
        if not set_link.get("ok"):
            return {"ok": False, "reason": "商品链接写入失败：" + str(set_link.get("reason") or "")}
        if not _click_selector(client, 'span[class*="cart-mybtn"]'):
            return {"ok": False, "reason": "未找到添加链接按钮"}
        deadline = time.time() + 12
        while time.time() < deadline:
            body = cdp_module.evaluate(client, "() => document.body ? document.body.innerText : ''") or ""
            if "购物车视频数量已经达到限额" in body or "无法添加购物车" in body:
                _close_cart_quota_modal(client)
                return {"ok": False, "reason": "购物车视频数量已经达到限额（账号限额，非链接无效）"}
            if "未搜索到对应商品" in body:
                _click_text(client, "确定", "button")
                return {"ok": False, "reason": "未搜索到对应商品（链接无效/商品不在橱窗）"}
            short_input = _js(client, """(function(){var el=document.querySelector('input[placeholder="请输入商品短标题"]');return !!el;})()""")
            if short_input:
                short = (str(short_title or "").strip() or "好物")[:10]
                set_short = _set_input_value(client, 'input[placeholder="请输入商品短标题"]', short)
                if not set_short.get("ok"):
                    return {"ok": False, "reason": "商品短标题写入失败"}
                time.sleep(0.5)
                _click_text(client, "完成编辑", "button")
                _click_text(client, "完成编辑", "span")
                time.sleep(1.0)
                return {"ok": True, "reason": "", "short_title": short}
            if "已添加商品" in body:
                return {"ok": True, "reason": ""}
            time.sleep(0.5)
        return {"ok": False, "reason": "添加商品超时（没有出现商品短标题弹窗/已添加商品）"}
    except Exception as exc:
        return {"ok": False, "reason": "添加商品异常：" + str(exc)}


def _append_tags(client: Any, selector: str, tags: list[str]) -> int:
    """在描述编辑器末尾输入 #话题 + 空格，返回成功写入数量。"""
    if not _focus_editor_end(client, selector):
        return 0
    written = 0
    for tag in tags:
        tag = str(tag or "").strip().lstrip("#")
        if not tag:
            continue
        if not _insert_text(client, " #" + tag):
            break
        _press_space(client)
        written += 1
        time.sleep(0.35)
    return written


def _set_file_input(client: Any, selector: str, path: str) -> bool:
    document = client.call("DOM.getDocument", {"depth": -1, "pierce": True})
    root = (document or {}).get("root") or {}
    found = client.call("DOM.querySelector", {"nodeId": root.get("nodeId"), "selector": selector})
    node_id = (found or {}).get("nodeId")
    if not node_id:
        return False
    client.call("DOM.setFileInputFiles", {"nodeId": node_id, "files": [str(path)]})
    return True


def _press_enter(client: Any) -> None:
    client.call("Input.dispatchKeyEvent", {"type": "keyDown", "windowsVirtualKeyCode": 13,
                                           "nativeVirtualKeyCode": 13, "key": "Enter", "code": "Enter"})
    client.call("Input.dispatchKeyEvent", {"type": "keyUp", "windowsVirtualKeyCode": 13,
                                           "nativeVirtualKeyCode": 13, "key": "Enter", "code": "Enter"})


def _screenshot(config: Config, job_id: str, client: Any, name: str) -> str:
    try:
        result = client.call("Page.captureScreenshot", {"format": "png"})
        raw = base64.b64decode(result.get("data") or "")
        folder = config.paths.jobs / str(job_id) / "checkpoints"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (name + ".png")
        path.write_bytes(raw)
        return str(path)
    except Exception:
        return ""


def run_prepublish(config: Config, job: dict[str, Any], account: dict[str, Any],
                   package: dict[str, Any], video_info: dict[str, Any],
                   simulate: bool = False, plain: bool = False) -> dict[str, Any]:
    if simulate:
        time.sleep(1.0)
        return {"ok": True, "simulated": True, "url": "simulate://prepublish", "checkpoints": []}
    port = int(account["port"])
    # 已有未发布草稿页时禁止再次导航/上传：避免 beforeunload「确定离开网页」卡住或覆盖草稿
    try:
        for target in chrome.list_targets(port):
            if target.get("type") != "page":
                continue
            page_url = str(target.get("url") or "")
            if ("content/post" in page_url) or ("content/upload" in page_url):
                raise ApiError(1015, "检测到未发布的上传草稿页，已阻止上传下一条。"
                                     "请先点发布或关闭/放弃草稿，再重新上传；"
                                     "skill 不会自动确认「离开网页」以免丢草稿。", 409)
    except ApiError:
        raise
    except Exception:
        pass
    chrome.ensure_single_instance(config, account["slug"], account["profile"], port, UPLOAD_URL,
                                  headless=bool(config.get("browser_headless", False)),
                                  display_name=str(account.get("display_name") or ""))
    info = login.inspect(port)
    if info.get("account_status") != "READY":
        raise ApiError(1008, "上传前登录硬校验未通过（NOT_LOGGED_IN）", 409)
    expected = str(account.get("expected_douyin_id") or "").strip()
    actual = str(info.get("douyin_id") or "").strip()
    if expected and actual and expected != actual:
        raise ApiError(1009, "账号不匹配：期望 " + expected + "，实际 " + actual, 409)
    selectors = load_selectors(config)
    fresh_mode = bool(config.get("fresh_upload_page", True))
    if fresh_mode:
        page = cdp_module.fresh_page(port, UPLOAD_URL)
    else:
        page = cdp_module.ensure_page(port, UPLOAD_URL)
    client = cdp_module.CDP(page["webSocketDebuggerUrl"])
    try:
        client.call("Page.bringToFront")
    except Exception:
        pass
    tab_guard = None
    if bool(config.get("tab_guard", True)):
        try:
            guard_log = config.paths.logs / "tabs" / (str(job.get("job_id")) + ".jsonl")
            tab_guard = cdp_module.TabGuard(port, guard_log, allow_ids={str(page.get("id") or "")})
            tab_guard.start()
        except Exception:
            tab_guard = None
    keep_guard = False
    checkpoints: list[str] = []
    try:
        if fresh_mode:
            # 同一标签内硬重置文档，替代“新开标签再关旧标签”的闪 tab 做法
            try:
                cdp_module.navigate(client, "about:blank")
            except Exception:
                pass
            time.sleep(0.5)
        url = cdp_module.evaluate(client, "() => location.href") or ""
        if "content/upload" not in url:
            cdp_module.navigate(client, UPLOAD_URL)
        cdp_module.wait_ready(client, timeout=90)
        time.sleep(2)
        checkpoints.append(_screenshot(config, job["job_id"], client, "01_upload_page"))
        if not _set_file_input(client, selectors["file_input"], str(video_info["path"])):
            raise ApiError(1015, "未找到视频文件选择框（需校准 upload_selectors.json 的 file_input）", 500)
        filename = Path(str(video_info["path"])).name
        stem = Path(filename).stem[:12]
        # 上传启动看门狗（20s）：文件已设置但页面没有进度/重新上传/编辑封面信号 → 立刻失败，
        # 不再干等 240s。参考 15 号项目 2026-11-08 UPLOAD_START_STALL 修复。
        started = False
        stall_deadline = time.time() + 20
        while time.time() < stall_deadline:
            state = cdp_module.evaluate(
                client,
                "() => ({text: document.body ? document.body.innerText.slice(0,6000) : '', "
                "progress: !!document.querySelector('div.progress-div, [class*=\"progress\"]')})",
            ) or {}
            text = str(state.get("text") or "")
            if "上传失败" in text:
                raise ApiError(1015, "视频上传失败（页面提示）", 500)
            if has_upload_started(text, bool(state.get("progress"))):
                started = True
                break
            time.sleep(1.5)
        if not started:
            shot = _screenshot(config, job["job_id"], client, "02_upload_stall")
            raise ApiError(1015, "UPLOAD_START_STALL：文件已设置，但 20 秒内没有上传进度/重新上传/编辑封面信号；"
                                "已停止等待（建议重建上传页面或浏览器）。截图：" + shot, 500)
        checkpoints.append(_screenshot(config, job["job_id"], client, "02_upload_started"))
        size_bytes = int(video_info.get("size_bytes") or 0)
        budget = upload_wait_budget(size_bytes)
        deadline = time.time() + budget
        uploaded = False
        while time.time() < deadline:
            text = cdp_module.evaluate(client, "() => document.body ? document.body.innerText.slice(0,6000) : ''") or ""
            if "上传失败" in text:
                raise ApiError(1015, "视频上传失败（页面提示）", 500)
            if has_upload_completed(text, stem):
                uploaded = True
                break
            time.sleep(2)
        if not uploaded:
            raise ApiError(1015, "等待视频上传超时（" + str(budget) + " 秒，未检测到重新上传/编辑封面）", 500)
        checkpoints.append(_screenshot(config, job["job_id"], client, "02_video_uploaded"))
        content = package.get("content") or {}
        title = str(content.get("title") or "").strip()
        desc = str(content.get("desc") or "").strip()
        tags = [str(t).strip().lstrip("#") for t in (content.get("tags") or []) if str(t).strip()]
        title_fill = _set_input_value(client, selectors["title"], title)
        if not title_fill.get("ok"):
            # 兼容旧版页面：回退到通用填充
            _fill_selector(client, selectors["title"], title)
        if not _fill_editor(client, selectors["desc"], desc):
            raise ApiError(1015, "未找到简介输入框（需校准 desc 选择器）", 500)
        desc_state = _editor_text(client, selectors["desc"]).replace("​", "").strip()
        if not desc_state.startswith(desc):
            # 编辑器偶发保留首行占位，清空重填一次
            _fill_editor(client, selectors["desc"], desc)
            desc_state = _editor_text(client, selectors["desc"]).replace("​", "").strip()
        if not desc_state.startswith(desc):
            raise ApiError(1015, "简介填充失败：编辑器开头异常（可能残留空行/占位符）", 500)
        written = _append_tags(client, selectors["desc"], tags)
        time.sleep(1.2)
        # 标题可能被后续编辑器的 React 重渲染清掉，最后再校验/补一次
        title_state = _js(client, "(function(){var el=document.querySelector(" + json.dumps(selectors["title"]) + ");return el?el.value:'';})()")
        if str(title_state or "") != title:
            _set_input_value(client, selectors["title"], title)
            time.sleep(0.5)
            title_state = _js(client, "(function(){var el=document.querySelector(" + json.dumps(selectors["title"]) + ");return el?el.value:'';})()")
        if str(title_state or "") != title:
            raise ApiError(1015, "标题填充失败：输入框读回为空/不一致（需校准 title 选择器）", 500)
        body_text = cdp_module.evaluate(client, "() => document.body ? document.body.innerText : ''") or ""
        missing_tags = [tag for tag in tags if ("#" + tag) not in body_text]
        if tags and (written < len(tags) or missing_tags):
            raise ApiError(1015, "话题填充失败：写入 " + str(written) + "/" + str(len(tags)) +
                                "，页面缺少 " + "、".join(missing_tags[:3]), 500)
        checkpoints.append(_screenshot(config, job["job_id"], client, "03_content_filled"))

        if plain:
            # 普通视频：只自动选封面，不改保存权限、不加商品/短标题、不碰扩展信息
            if not _select_recommended_cover(client):
                raise ApiError(1015, "封面选择失败：没有找到可用的推荐封面", 500)
            if not _wait_cover_ready(client):
                raise ApiError(1015, "封面生成未完成：竖/横封面仍缺失或生成中", 500)
            checkpoints.append(_screenshot(config, job["job_id"], client, "03_cover_ready_plain"))
            final_url = cdp_module.evaluate(client, "() => location.href") or ""
            keep_guard = True
            return {"ok": True, "simulated": False, "url": final_url,
                    "checkpoints": [p for p in checkpoints if p], "selectors": selectors,
                    "plain": True, "tab_guard": tab_guard}
        if not _set_save_permission_disallow(client):
            raise ApiError(1015, "保存权限设置失败：未选中「不允许」", 500)
        checkpoints.append(_screenshot(config, job["job_id"], client, "03_save_permission"))

        if not _select_recommended_cover(client):
            raise ApiError(1015, "封面选择失败：没有找到可用的推荐封面", 500)
        if not _wait_cover_ready(client):
            raise ApiError(1015, "封面生成未完成：竖/横封面仍缺失或生成中", 500)
        checkpoints.append(_screenshot(config, job["job_id"], client, "03_cover_ready"))

        product = package.get("product") or {}
        product_link = str(product.get("link") or "").strip()
        product_short = str(content.get("short_title") or product.get("short_title") or "").strip()
        if not product_link:
            raise ApiError(1015, "内容包缺少商品链接，无法添加商品卡", 500)
        product_result = _add_product_link(client, product_link, product_short)
        if not product_result.get("ok"):
            reason = str(product_result.get("reason") or "添加商品失败")
            if "限额" in reason:
                raise ApiError(2003, reason, 409)
            if "未搜索到对应商品" in reason or "链接无效" in reason:
                raise ApiError(2002, reason, 409)
            raise ApiError(1015, reason, 500)
        if not _wait_cover_ready(client, timeout=10):
            raise ApiError(1015, "添加商品后封面状态异常（横封面缺失/生成中）", 500)
        checkpoints.append(_screenshot(config, job["job_id"], client, "04_product_linked"))
        final_url = cdp_module.evaluate(client, "() => location.href") or ""
        keep_guard = True
        return {"ok": True, "simulated": False, "url": final_url,
                "checkpoints": [p for p in checkpoints if p], "selectors": selectors,
                "tab_guard": tab_guard}
    finally:
        client.close()
        if tab_guard is not None and not keep_guard:
            try:
                tab_guard.stop()
            except Exception:
                pass
