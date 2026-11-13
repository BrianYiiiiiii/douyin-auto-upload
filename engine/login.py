"""登录检测与账号态抓取（CDP）。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from . import cdp as cdp_module
from .accounts import ApiError, now_iso

CREATOR_HOME = "https://creator.douyin.com/creator-micro/home"
LOGGED_OUT_MARKERS = ("扫码登录", "验证码登录", "密码登录")
LOGGED_IN_MARKERS = ("发布视频", "作品管理", "数据中心")

LOCAL_STORAGE_JS = (
    "() => { const o={}; for(let i=0;i<localStorage.length;i++){"
    "const k=localStorage.key(i); if(k!==null)o[k]=localStorage.getItem(k)||'';} return o; }"
)
SESSION_STORAGE_JS = (
    "() => { const o={}; for(let i=0;i<sessionStorage.length;i++){"
    "const k=sessionStorage.key(i); if(k!==null)o[k]=sessionStorage.getItem(k)||'';} return o; }"
)
IDB_JS = r"""async () => {
  const out=[];
  try {
    const dbs = await indexedDB.databases();
    for (const info of dbs) {
      const db = await new Promise((res,rej)=>{const r=indexedDB.open(info.name);r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);});
      const stores=[];
      for (const sn of Array.from(db.objectStoreNames)) {
        const tx = db.transaction(sn,'readonly');
        const st = tx.objectStore(sn);
        const count = await new Promise((res,rej)=>{const r=st.count();r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);});
        const records = await new Promise((res,rej)=>{const r=st.getAll();r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);});
        const ser = (v) => {
          if (v === undefined) return null;
          if (v === null || typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') return v;
          if (typeof Blob !== 'undefined' && v instanceof Blob) return {__blob:true,size:v.size,type:v.type};
          if (typeof ArrayBuffer !== 'undefined' && v instanceof ArrayBuffer) return {__arraybuffer:true,bytes:Array.from(new Uint8Array(v)).slice(0,2048)};
          if (Array.isArray(v)) return v.slice(0,1000).map(ser);
          if (v instanceof Date) return {__date:v.toISOString()};
          const o={}; for (const k of Object.keys(v).slice(0,200)) o[k]=ser(v[k]); return o;
        };
        stores.push({name:sn,count,records:records.slice(0,500).map(ser)});
      }
      out.push({name:info.name,version:info.version,stores}); db.close();
    }
  } catch (e) { return {__error: String(e)}; }
  return out;
}"""


def fingerprint(cookies: list[dict[str, Any]]) -> str:
    parts = sorted(
        f"{c.get('domain','')}|{c.get('path','')}|{c.get('name','')}|{c.get('value','')}"
        for c in cookies
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def parse_account_text(text: str) -> tuple[str, str]:
    """从创作者中心页面文本解析 昵称 + 抖音号。"""
    text = text or ""
    douyin_id = ""
    name = ""
    m = re.search(r"抖音号\s*[：:]?\s*([A-Za-z0-9_]+)", text)
    if m:
        douyin_id = m.group(1)
        prefix = text[:m.start()]
        lines = [ln for ln in prefix.splitlines() if ln.strip()]
        if lines:
            parts = [x.strip() for x in lines[-1].split("|") if x.strip()]
            if parts:
                name = parts[-1]
    return name, douyin_id


def inspect(port: int, navigate_home: bool = False) -> dict[str, Any]:
    """打开/复用创作者中心页面，判断登录状态。

    navigate_home=True 时会确保停在 creator-micro/home，便于解析昵称/抖音号；
    上传前检测使用默认 False，避免离开用户正在编辑的草稿页。
    """
    page = cdp_module.ensure_page(port, CREATOR_HOME)
    client = cdp_module.CDP(page["webSocketDebuggerUrl"], auto_accept_beforeunload=True)
    try:
        try:
            client.call("Page.enable")
        except Exception:
            pass
        url = cdp_module.evaluate(client, "() => location.href") or ""
        # 有未发布草稿时绝不允许用导航触发 beforeunload；直接在当前页做登录检测
        has_draft = ("content/post" in url) or ("content/upload" in url)
        need_home = ("creator.douyin.com" not in url) or (
            navigate_home and not has_draft and "creator-micro/home" not in url)
        if need_home:
            cdp_module.navigate(client, CREATOR_HOME)
        cdp_module.wait_ready(client, timeout=90)
        deadline = time.time() + 60
        text = ""
        title = ""
        url = ""
        while time.time() < deadline:
            text = cdp_module.evaluate(client, "() => document.body ? document.body.innerText : ''") or ""
            title = cdp_module.evaluate(client, "() => document.title") or ""
            url = cdp_module.evaluate(client, "() => location.href") or ""
            if text.strip() and (
                any(m in text for m in LOGGED_OUT_MARKERS)
                or any(m in text for m in LOGGED_IN_MARKERS)
                or "抖音号" in text
            ):
                break
            time.sleep(0.6)
    finally:
        client.close()
    cookies: list[dict[str, Any]] | None = None
    try:
        cookies = cdp_module.get_all_cookies(port)
    except Exception:
        cookies = None
    cookie_list = cookies or []
    markers = [m for m in LOGGED_OUT_MARKERS if m in text]
    name, douyin_id = parse_account_text(text)
    cookie_names = {str(c.get("name") or "") for c in cookie_list}
    has_session = bool(cookie_names & {"sessionid", "sessionid_ss", "uid_tt", "sid_tt", "sid_guard"})
    has_login_marker = any(m in text for m in LOGGED_IN_MARKERS)
    login_url = "/login" in (url or "").lower() or "passport" in (url or "").lower()
    if markers or login_url:
        status = "LOGGED_OUT"
    elif cookies is None:
        status = "UNKNOWN"
    elif not has_session:
        status = "LOGGED_OUT"
    elif has_login_marker or douyin_id:
        status = "READY"
    else:
        status = "UNKNOWN"
    return {
        "account_status": status,
        "display_name": name,
        "douyin_id": douyin_id,
        "url": url,
        "title": title,
        "markers": markers,
        "cookie_count": len(cookie_list),
        "douyin_cookie_count": len([c for c in cookie_list if "douyin" in str(c.get("domain") or "")]),
        "fingerprint": fingerprint(cookie_list),
        "checked_at": now_iso(),
    }


def needs_capture(record: dict[str, Any], info: dict[str, Any]) -> bool:
    """登录检测后是否需要自动抓取账号态（未登录→READY / 首次 / 指纹变化）。"""
    if not info or info.get("account_status") != "READY":
        return False
    if str(record.get("account_status") or "") != "READY":
        return True
    if not record.get("last_capture_at"):
        return True
    stored = str(record.get("cookie_fingerprint") or "")
    current = str(info.get("fingerprint") or "")
    return bool(stored and current and stored != current)


def capture(port: int, slug: str, profile: str, expected_douyin_id: str,
            account_state_path: Path, cookie_snapshot_path: Path,
            display_name_hint: str = "") -> dict[str, Any]:
    """登录后抓取完整账号态（cookies + localStorage + sessionStorage + IndexedDB）。"""
    info = inspect(port)
    if info["account_status"] != "READY":
        raise ApiError(1008, "账号未登录（NOT_LOGGED_IN）", 409)
    expected = (expected_douyin_id or "").strip()
    if expected and info["douyin_id"] and expected != info["douyin_id"]:
        raise ApiError(1009, f"账号不匹配：期望 {expected}，实际 {info['douyin_id']}", 409)
    page = cdp_module.ensure_page(port, CREATOR_HOME)
    client = cdp_module.CDP(page["webSocketDebuggerUrl"])
    try:
        origin = cdp_module.evaluate(client, "() => location.origin") or ""
        url = cdp_module.evaluate(client, "() => location.href") or ""
        local_storage = cdp_module.evaluate(client, LOCAL_STORAGE_JS) or {}
        session_storage = cdp_module.evaluate(client, SESSION_STORAGE_JS) or {}
        try:
            indexed_db = cdp_module.evaluate(client, IDB_JS)
        except Exception as exc:  # IndexedDB 失败不阻断账号态主体
            indexed_db = {"__error": str(exc)}
    finally:
        client.close()
    cookies = cdp_module.get_all_cookies(port)
    fp = fingerprint(cookies)
    account_name = info["display_name"] or display_name_hint
    account_state = {
        "account": account_name,
        "douyin_id": info["douyin_id"] or expected,
        "account_slug": slug,
        "profile": profile,
        "port": port,
        "captured_at": now_iso(),
        "url": url,
        "origin": origin,
        "cookies": cookies,
        "origins": [{
            "origin": origin,
            "localStorage": [{"name": k, "value": v} for k, v in local_storage.items()],
            "sessionStorage": [{"name": k, "value": v} for k, v in session_storage.items()],
        }],
        "indexedDB": indexed_db,
    }
    storage_state = {"cookies": cookies, "origins": account_state["origins"]}
    for path, payload in ((account_state_path, account_state), (cookie_snapshot_path, storage_state)):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
        os.replace(tmp, path)
    idb_stores = 0
    if isinstance(indexed_db, list):
        idb_stores = sum(len(db.get("stores", [])) for db in indexed_db)
    return {
        "ok": True,
        "account": account_name,
        "douyin_id": info["douyin_id"] or expected,
        "fingerprint": fp,
        "cookie_count": len(cookies),
        "douyin_cookie_count": len([c for c in cookies if "douyin" in str(c.get("domain") or "")]),
        "localStorage_keys": len(local_storage),
        "sessionStorage_keys": len(session_storage),
        "indexedDB_stores": idb_stores,
        "captured_at": now_iso(),
        "url": url,
        "cookie_path": str(cookie_snapshot_path),
        "account_state": str(account_state_path),
    }



COOKIE_RESTORE_FIELDS = ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "expires")


def restore(port: int, cookie_snapshot_path: Path) -> dict[str, Any]:
    """用已抓取的 cookie 快照尝试恢复登录（profile 失效时的兜底）。

    只恢复 cookie；localStorage 在创作者域内尽力补写。恢复后重新 inspect，
    成功返回 READY；失败不抛异常，交给用户重新扫码。
    """
    path = Path(cookie_snapshot_path)
    if not path.is_file():
        return {"ok": False, "reason": "cookie 快照不存在"}
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "reason": "cookie 快照无法解析：" + str(exc)}
    cookies = snapshot.get("cookies") if isinstance(snapshot, dict) else None
    if not isinstance(cookies, list) or not cookies:
        return {"ok": False, "reason": "cookie 快照为空"}
    payload = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        item = {key: cookie[key] for key in COOKIE_RESTORE_FIELDS
                if key in cookie and cookie[key] is not None}
        if item.get("name") and item.get("domain") and item.get("value") is not None:
            payload.append(item)
    if not payload:
        return {"ok": False, "reason": "cookie 快照没有可用 cookie"}
    page = cdp_module.ensure_page(port, CREATOR_HOME)
    client = cdp_module.CDP(page["webSocketDebuggerUrl"], auto_accept_beforeunload=True)
    try:
        try:
            client.call("Page.enable")
        except Exception:
            pass
        client.call("Network.enable")
        client.call("Network.setCookies", {"cookies": payload})
        cdp_module.navigate(client, CREATOR_HOME)
        cdp_module.wait_ready(client, timeout=30)
        origin = cdp_module.evaluate(client, "() => location.origin") or ""
        for entry in snapshot.get("origins") or []:
            if not isinstance(entry, dict) or str(entry.get("origin") or "") != origin:
                continue
            for key, value in entry.get("localStorage") or []:
                try:
                    cdp_module.evaluate(
                        client,
                        "() => { try { localStorage.setItem(" + json.dumps(str(key)) + "," +
                        json.dumps(str(value)) + "); } catch(e) {} }",
                    )
                except Exception:
                    pass
            for key, value in entry.get("sessionStorage") or []:
                try:
                    cdp_module.evaluate(
                        client,
                        "() => { try { sessionStorage.setItem(" + json.dumps(str(key)) + "," +
                        json.dumps(str(value)) + "); } catch(e) {} }",
                    )
                except Exception:
                    pass
        # localStorage 写完后重新加载，让应用读到
        cdp_module.navigate(client, CREATOR_HOME)
        cdp_module.wait_ready(client, timeout=20)
    except Exception as exc:
        return {"ok": False, "reason": "恢复 cookie 失败：" + str(exc)}
    finally:
        client.close()
    info = inspect(port)
    return {"ok": info.get("account_status") == "READY",
            "restored_cookies": len(payload),
            "info": info}

