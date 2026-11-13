#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""douyin-auto-upload skill CLI (v1.0).

Platforms: Windows / macOS. No FastAPI/Web UI/service dependency.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import accounts, chrome, login, packages, publish_detect, uploader, videos  # noqa: E402
from engine.config import APP_VERSION, Config, SKILL_ROOT, detect_chrome, default_root  # noqa: E402

STATE_NAME = "current.json"
POINTER = SKILL_ROOT / "config.local.json"
CODE_RE = re.compile(r"(?:__?)(\d{5,6})(?=[_\s.])")


def emit(data: Any, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif isinstance(data, str):
        print(data)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


def resolve_root(args: argparse.Namespace) -> Path:
    raw = str(getattr(args, "data_root", "") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    env = str(os.environ.get("DOUYIN_UPLOAD_DATA_ROOT", "") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    pointer = POINTER
    if pointer.is_file():
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
            root = str((data or {}).get("data_root") or "").strip()
            if root:
                return Path(root).expanduser().resolve()
        except Exception:
            pass
    raise SystemExit('skill 尚未初始化：请先运行 init --data-root "<数据根>"')


def cfg(args: argparse.Namespace) -> Config:
    return Config(resolve_root(args))


def state_path(config: Config) -> Path:
    path = config.paths.runtime / "batches" / STATE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_state(config: Config) -> dict[str, Any] | None:
    path = state_path(config)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_state(config: Config, data: dict[str, Any]) -> None:
    path = state_path(config)
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def products_path(config: Config) -> Path:
    return config.paths.data / "products.json"


def load_products(config: Config) -> dict[str, Any]:
    path = products_path(config)
    if not path.is_file():
        return {"version": 1, "products": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("products", {})
            return data
    except Exception:
        pass
    return {"version": 1, "products": {}}


def save_products(config: Config, data: dict[str, Any]) -> None:
    path = products_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def style_code(filename: str) -> str:
    m = CODE_RE.search(str(filename or ""))
    return m.group(1) if m else ""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hashes_path(config: Config) -> Path:
    return config.paths.data / "uploaded_hashes.json"


def load_hashes(config: Config) -> dict[str, Any]:
    path = hashes_path(config)
    if not path.is_file():
        return {"version": 1, "hashes": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("hashes", {})
            return data
    except Exception:
        pass
    return {"version": 1, "hashes": {}}


def mark_uploaded(config: Config, sha: str, path: Path) -> None:
    if not sha:
        return
    data = load_hashes(config)
    data.setdefault("hashes", {})[sha] = {"file": str(path), "marked_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    path_obj = hashes_path(config)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    path_obj.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def video_files(directory: Path, recursive: bool = False) -> list[Path]:
    if not directory.is_dir():
        raise SystemExit("目录不存在: " + str(directory))
    it = directory.rglob("*") if recursive else directory.glob("*")
    return sorted([p for p in it if p.is_file() and p.suffix.lower() in (".mp4", ".mov")],
                  key=lambda p: p.name)


def package_for_code(config: Config, code: str) -> dict[str, Any] | None:
    rows = packages.list_packages(config)
    if not code:
        return None
    for row in rows:
        if code in str(row.get("name") or "") or code in str(row.get("title") or ""):
            return packages.load(config, row.get("package_id") or "")
    return None


def product_for_code(config: Config, code: str) -> dict[str, Any] | None:
    for key, value in (load_products(config).get("products") or {}).items():
        if code and (code in str(value.get("style_code") or "") or code in str(value.get("name") or "")):
            out = dict(value)
            out["_key"] = key
            return out
    return None


def account_for_slug(config: Config, slug: str) -> dict[str, Any]:
    return accounts.Registry(config).require(slug)


def capture_ready(config: Config, rec: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
    slug = rec["slug"]
    known_id = str(info.get("douyin_id") or rec.get("expected_douyin_id") or "").strip()
    captured = login.capture(int(rec["port"]), slug, rec["profile"], known_id,
                             config.paths.account_state(slug), config.paths.cookie_snapshot(slug),
                             display_name_hint=rec.get("display_name") or "")
    captured_id = str(captured.get("douyin_id") or info.get("douyin_id") or known_id or "").strip()
    if captured_id and not captured.get("douyin_id"):
        captured["douyin_id"] = captured_id
    accounts.Registry(config).set_status(
        slug, "READY",
        expected_douyin_id=captured_id or None,
        last_capture_at=captured.get("captured_at") or "",
        cookie_fingerprint=str(info.get("fingerprint") or ""),
    )
    rec = account_for_slug(config, slug)
    rec["account_status"] = "READY"
    rec["last_capture"] = captured
    return rec


def login_ready(config: Config, slug: str, wait_seconds: float = 0.0,
                restore: bool = True) -> dict[str, Any]:
    """确保账号浏览器打开、尝试快照恢复、按需等待用户扫码，最后抓取登录态。"""
    rec = account_for_slug(config, slug)
    port = int(rec["port"])
    chrome.ensure_single_instance(config, slug, rec["profile"], port, login.CREATOR_HOME,
                                  headless=False, display_name=rec.get("display_name") or slug)
    waiting_for_scan = float(wait_seconds or 0) > 0
    # 等待扫码时绝不导航，避免把用户正在扫的二维码刷新掉；正常检查才回 home 解析昵称/抖音号
    info = login.inspect(port, navigate_home=not waiting_for_scan)
    current_url = str(info.get("url") or "")
    has_draft = ("content/post" in current_url) or ("content/upload" in current_url)
    if info.get("account_status") != "READY" and has_draft:
        raise SystemExit("当前浏览器停留在未发布草稿页，不能安全刷新/恢复登录；"
                         "请先点发布或关闭草稿页，再运行 account check")
    restored = None
    if (not waiting_for_scan and info.get("account_status") != "READY" and restore
            and rec.get("cookie_snapshot")):
        restored = login.restore(port, Path(rec["cookie_snapshot"]))
        info = restored.get("info") or login.inspect(port)
    deadline = time.time() + max(0.0, float(wait_seconds))
    while info.get("account_status") != "READY" and time.time() < deadline:
        time.sleep(3)
        info = login.inspect(port)
    if info.get("account_status") != "READY":
        accounts.Registry(config).set_status(slug, "LOGIN_REQUIRED",
                                             cookie_fingerprint=str(info.get("fingerprint") or ""))
        suffix = "（已尝试 cookie 快照恢复）" if restored is not None else ""
        raise SystemExit("账号 " + slug + " 未登录/未 READY" + suffix +
                         "，请让用户扫码，然后运行 account check --slug " + slug + " --wait-login 300")
    if login.needs_capture(rec, info) or not rec.get("last_capture_at"):
        rec = capture_ready(config, rec, info)
    rec["account_status"] = "READY"
    rec["restored"] = bool(restored and restored.get("ok"))
    return rec


def account_ready(config: Config, slug: str, wait_seconds: float = 0.0) -> dict[str, Any]:
    return login_ready(config, slug, wait_seconds=wait_seconds, restore=True)


def cmd_init(args: argparse.Namespace) -> int:
    root = resolve_root(args)
    config = Config(root)
    config.paths.ensure()
    config.save()
    POINTER.write_text(json.dumps({"data_root": str(config.paths.root)}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    emit({"ok": True, "data_root": str(config.paths.root), "pointer": str(POINTER)}, args.json)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = cfg(args)
    data = {
        "ok": True,
        "version": APP_VERSION,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "data_root": str(config.paths.root),
        "data_root_exists": config.paths.root.is_dir(),
        "chrome": detect_chrome() or str(config.get("chrome_path") or ""),
        "websocket": False,
        "accounts": 0,
        "packages": 0,
    }
    try:
        import websocket  # noqa: F401
        data["websocket"] = True
    except Exception:
        data["ok"] = False
    data["accounts"] = len(accounts.Registry(config).list())
    data["packages"] = len(packages.list_packages(config))
    if not data["data_root_exists"] or not data["chrome"]:
        data["ok"] = False
    emit(data, args.json)
    return 0 if data["ok"] else 1


def cmd_account_add(args: argparse.Namespace) -> int:
    config = cfg(args)
    registry = accounts.Registry(config)
    profile = str(args.profile_path or "").strip()
    if args.profile_mode == "existing":
        if not profile:
            raise SystemExit("--profile-path 必填")
        rec = registry.create_imported(args.display, args.slug, profile, int(args.port or 0),
                                       expected_douyin_id=args.expected_id or "")
    else:
        rec = registry.create(args.display, args.slug or None)
        profile = str(rec["profile"])
    if args.open:
        chrome.ensure_single_instance(config, rec["slug"], profile, int(rec["port"]),
                                      "https://creator.douyin.com/creator-micro/home",
                                      headless=False, display_name=rec.get("display_name") or "")
    if float(getattr(args, "wait_login", 0) or 0) > 0:
        rec = login_ready(config, rec["slug"], wait_seconds=float(args.wait_login), restore=False)
        emit({"ok": True, "account": rec, "capture": rec.get("last_capture")}, args.json)
        return 0
    emit({"ok": True, "account": rec, "next": "手机扫码登录后运行 account check --slug " + rec["slug"] +
          " --wait-login 300；或直接重新运行 account add --wait-login 300"}, args.json)
    return 0


def cmd_account_list(args: argparse.Namespace) -> int:
    config = cfg(args)
    rows = accounts.Registry(config).list()
    emit({"ok": True, "accounts": rows}, args.json)
    return 0


def restart_browser(config: Config, rec: dict[str, Any]) -> None:
    """页面无响应（beforeunload/渲染器卡死）时关闭并重新拉起账号浏览器。"""
    try:
        chrome.close(rec["profile"], int(rec["port"]))
    except Exception:
        pass
    time.sleep(1.0)
    chrome.ensure_single_instance(config, rec["slug"], rec["profile"], int(rec["port"]),
                                  login.CREATOR_HOME, headless=False,
                                  display_name=rec.get("display_name") or "")


def cmd_account_check(args: argparse.Namespace) -> int:
    config = cfg(args)
    rec = None
    recovered = False
    try:
        rec = login_ready(config, args.slug, wait_seconds=float(getattr(args, "wait_login", 0) or 0), restore=True)
    except SystemExit as exc:
        emit({"ok": False, "error": str(exc)}, args.json)
        return 1
    except Exception as exc:
        if not any(token in str(exc).lower() for token in ("timeout", "timed out", "connection")):
            raise
        rec = accounts.Registry(config).require(args.slug)
        restart_browser(config, rec)
        recovered = True
        rec = login_ready(config, args.slug, wait_seconds=float(getattr(args, "wait_login", 0) or 0), restore=True)
    emit({"ok": True, "account": rec, "capture": rec.get("last_capture"),
          "restored": rec.get("restored", False), "recovered_browser": recovered}, args.json)
    return 0


def cmd_package_add(args: argparse.Namespace) -> int:
    config = cfg(args)
    tags = [x.strip().lstrip("#") for x in str(args.tags or "").split(",") if x.strip()]
    content = {
        "title": args.title, "desc": args.desc, "tags": tags,
        "short_title": args.short_title, "selling_points": [],
    }
    product_name = args.product_name or ("普通视频" if args.plain else "")
    product = {"name": product_name, "link": args.product_link or "",
               "short_title": args.short_title or "", "facts": []}
    spec = {"candidates": 1, "title_max": 30, "desc_max": 60, "tags_per_candidate": 5,
            "short_title_max": 10, "allow_material": True}
    payload = {"name": args.name or (("[" + args.style_code + "] ") if args.style_code else "") + (args.title or ""),
               "account_slug": args.account or "", "product": product,
               "content": content, "candidates": [content], "selected_index": 0,
               "facts": [], "content_spec": spec,
               "source": {"type": "skill_package", "style_code": args.style_code or "",
                          "plain": bool(args.plain)}}
    try:
        package = packages.save(config, payload)
    except Exception as exc:
        raise SystemExit("创建内容包失败：" + str(exc))
    if args.style_code:
        products = load_products(config)
        key = "plain-" + args.style_code if args.plain else "product-" + args.style_code
        products.setdefault("products", {})[key] = {
            "name": product_name, "product_link": args.product_link or "",
            "product_id": args.product_id or "", "style_code": args.style_code,
            "short_title_default": args.short_title or "",
            "account_scope": [args.account] if args.account else [],
            "status": "ready", "plain": bool(args.plain),
        }
        save_products(config, products)
    emit({"ok": True, "package": package}, args.json)
    return 0


def cmd_package_list(args: argparse.Namespace) -> int:
    config = cfg(args)
    emit({"ok": True, "packages": packages.list_packages(config)}, args.json)
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    config = cfg(args)
    directory = Path(args.dir).expanduser().resolve()
    rows = []
    for path in video_files(directory, args.recursive):
        size = path.stat().st_size
        code = style_code(path.name)
        package = package_for_code(config, code)
        product = product_for_code(config, code)
        sha = sha256_file(path)
        row = {
            "file": path.name,
            "path": str(path),
            "size": size,
            "sha256": sha,
            "already_uploaded": sha in (load_hashes(config).get("hashes") or {}),
            "style_code": code,
            "package_id": (package or {}).get("package_id", ""),
            "package_name": (package or {}).get("name", ""),
            "mode": "linked" if (package and (package.get("product") or {}).get("link")) else "plain_candidate",
            "product": (product or {}).get("name", ""),
            "product_link": (product or {}).get("product_link", ""),
            "account": (package or {}).get("account_slug", ""),
        }
        rows.append(row)
    emit({"ok": True, "dir": str(directory), "count": len(rows), "videos": rows}, args.json)
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    config = cfg(args)
    directory = Path(args.dir).expanduser().resolve()
    rows = []
    for path in video_files(directory, args.recursive):
        code = style_code(path.name)
        package = package_for_code(config, code)
        row = {"file": path.name, "path": str(path), "style_code": code,
               "package_id": (package or {}).get("package_id", ""),
               "package_name": (package or {}).get("name", ""),
               "account": (package or {}).get("account_slug", ""),
               "mode": "linked" if package and (package.get("product") or {}).get("link") else "plain_candidate"}
        rows.append(row)
    plan = {"ok": True, "dir": str(directory), "count": len(rows), "items": rows}
    if args.write:
        plan_path = config.paths.runtime / "plans" / ("plan.json")
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        plan["plan_path"] = str(plan_path)
    emit(plan, args.json)
    return 0


def wait_publish(config: Config, job: dict[str, Any], timeout_min: float) -> bool:
    port = int(job["account_port"])
    title = str((job.get("package") or {}).get("content", {}).get("title") or "")
    deadline = time.time() + max(1.0, timeout_min * 60)
    while time.time() < deadline:
        try:
            state = load_state(config)
            if state and state.get("paused"):
                return False
            evidence = publish_detect.detect(port, title)
            if evidence.get("matched"):
                return True
        except Exception:
            pass
        time.sleep(5)
    return False


def upload_one(config: Config, path: Path, package: dict[str, Any], account: dict[str, Any],
               plain: bool, batch_id: str, index: int) -> dict[str, Any]:
    video_info = videos.verify_video(config, str(path), "")
    checks = uploader.preflight(config, account, package, video_info)
    failed = [c for c in checks if not c["ok"]]
    if failed:
        return {"ok": False, "error": "前置校验失败：" + "；".join(c["name"] + "=" + c["detail"] for c in failed)}
    job = {"job_id": batch_id + "-" + str(index + 1), "account_slug": account["slug"],
           "package_id": package.get("package_id") or "", "content": package.get("content") or {},
           "video": video_info}
    result = uploader.run_prepublish(config, job, account, package, video_info, plain=plain)
    guard = result.get("tab_guard")
    try:
        return {"ok": True, "url": result.get("url"), "checkpoints": result.get("checkpoints"),
                "plain": plain}
    finally:
        if guard is not None:
            try:
                guard.stop()
            except Exception:
                pass


def require_account_for_item(config: Config, slug: str, args: argparse.Namespace,
                            state: dict[str, Any], path: Path) -> dict[str, Any] | None:
    try:
        return account_ready(config, slug, wait_seconds=float(getattr(args, "wait_login", 0) or 0))
    except SystemExit as exc:
        state["status"] = "login_required"
        state["error"] = str(exc)
        state["current"] = {"path": str(path), "file": path.name, "account": slug, "mode": "login"}
        save_state(config, state)
        emit({"ok": False, "error": str(exc), "file": path.name, "next": "扫码后运行 continue"},
             bool(getattr(args, "json", False)))
        return None
    except Exception as exc:
        state["status"] = "browser_error"
        state["error"] = str(exc)
        state["current"] = {"path": str(path), "file": path.name, "account": slug, "mode": "login"}
        save_state(config, state)
        emit({"ok": False, "error": str(exc), "file": path.name,
              "next": "运行 account check --slug " + slug + " 恢复浏览器后 continue"},
             bool(getattr(args, "json", False)))
        return None


def run_batch(args: argparse.Namespace, config: Config, state: dict[str, Any] | None = None) -> int:
    recursive = bool(getattr(args, "recursive", False))
    directory = Path((state or {}).get("dir") or getattr(args, "dir", "")).expanduser().resolve()
    files = video_files(directory, recursive)
    if state is None:
        state = {"id": "batch-" + time.strftime("%Y%m%d_%H%M%S"), "dir": str(directory),
                 "mode": getattr(args, "mode", "auto"),
                 "continue_mode": getattr(args, "continue_mode", "semi_auto"),
                 "timeout_min": float(getattr(args, "timeout_min", 10.0)),
                 "index": 0, "paused": False,
                 "items": [{"path": str(p), "file": p.name} for p in files], "status": "running",
                 "current": None}
        save_state(config, state)
    mode_default = str(state.get("mode") or getattr(args, "mode", "auto"))
    continue_mode = str(state.get("continue_mode") or getattr(args, "continue_mode", "semi_auto"))
    timeout_min = float(state.get("timeout_min") or getattr(args, "timeout_min", 10.0))
    start = int(state.get("index") or 0)
    for i in range(start, len(files)):
        state = load_state(config) or state
        if state.get("paused"):
            state["status"] = "paused"
            save_state(config, state)
            emit({"ok": False, "paused": True, "state": state}, args.json)
            return 2
        path = files[i]
        code = style_code(path.name)
        package = package_for_code(config, code)
        mode = mode_default
        if mode == "auto":
            product = (package or {}).get("product") or {}
            source = (package or {}).get("source") or {}
            if package and product.get("link"):
                mode = "linked"
            elif package and (source.get("plain") or str(product.get("name") or "") == "普通视频"):
                mode = "plain"
            elif package:
                state["status"] = "needs_content"
                state["error"] = "内容包缺少商品链接，不能当作带链接视频；请补链接，或明确用 --mode plain"
                save_state(config, state)
                emit({"ok": False, "error": state["error"], "file": path.name}, args.json)
                return 1
            else:
                mode = "plain"
        if mode == "linked":
            if not package:
                state["status"] = "needs_content"
                state["error"] = "缺少款号 " + code + " 的内容包，请先 package add"
                save_state(config, state)
                emit({"ok": False, "error": state["error"], "file": path.name}, args.json)
                return 1
            account_slug = package.get("account_slug") or getattr(args, "account", "")
            if not account_slug:
                state["status"] = "needs_account"
                state["error"] = "内容包没有账号，且未指定 --account"
                save_state(config, state)
                emit({"ok": False, "error": state["error"]}, args.json)
                return 1
            account = require_account_for_item(config, account_slug, args, state, path)
            if account is None:
                return 1
        else:
            if not package:
                state["status"] = "needs_content"
                state["error"] = "普通视频缺少内容包（标题/简介/话题），请先 package add --plain"
                save_state(config, state)
                emit({"ok": False, "error": state["error"], "file": path.name}, args.json)
                return 1
            account_slug = getattr(args, "account", "") or package.get("account_slug")
            if not account_slug:
                state["status"] = "needs_account"
                state["error"] = "普通视频未指定账号"
                save_state(config, state)
                emit({"ok": False, "error": state["error"]}, args.json)
                return 1
            account = require_account_for_item(config, account_slug, args, state, path)
            if account is None:
                return 1
        sha = sha256_file(path)
        if not getattr(args, "allow_duplicate", False) and sha in (load_hashes(config).get("hashes") or {}):
            state["status"] = "duplicate"
            state["error"] = "视频已上传过：" + path.name
            save_state(config, state)
            emit({"ok": False, "error": state["error"], "sha256": sha}, args.json)
            return 1
        state["status"] = "uploading"
        state["index"] = i
        state["current"] = {"path": str(path), "file": path.name, "mode": mode,
                            "package_id": package.get("package_id"), "account": account_slug,
                            "account_port": int(account["port"]), "sha256": sha}
        save_state(config, state)
        emit({"ok": True, "uploading": path.name, "mode": mode, "account": account_slug}, args.json)
        try:
            result = upload_one(config, path, package, account, mode == "plain", state["id"], i)
        except Exception as exc:
            state = load_state(config) or state
            state["status"] = "failed"
            state["error"] = str(exc)
            save_state(config, state)
            emit({"ok": False, "file": path.name, "error": str(exc)}, args.json)
            return 1
        state = load_state(config) or state
        state["last_result"] = result
        if not result.get("ok"):
            state["status"] = "failed"
            state["error"] = result.get("error")
            save_state(config, state)
            emit({"ok": False, "file": path.name, "error": state["error"]}, args.json)
            return 1
        state["status"] = "prepublish"
        state["current"]["checkpoints"] = result.get("checkpoints") or []
        save_state(config, state)
        emit({"ok": True, "prepublish": True, "file": path.name, "url": result.get("url"),
              "checkpoints": result.get("checkpoints")}, args.json)
        if continue_mode == "manual":
            state["status"] = "waiting_publish"
            save_state(config, state)
            emit({"ok": True, "waiting_publish": True, "next": "用户发布后运行 continue"}, args.json)
            return 0
        wait_min = timeout_min if continue_mode == "semi_auto" else 60.0
        matched = wait_publish(config, state["current"], wait_min)
        if not matched:
            state["status"] = "paused_waiting_publish"
            save_state(config, state)
            emit({"ok": False, "paused": True, "reason": "发布等待超时/暂停", "file": path.name}, args.json)
            return 2
        mark_uploaded(config, str(state.get("current", {}).get("sha256") or ""), Path(state["current"]["path"]))
        state["index"] = i + 1
        state["status"] = "running"
        save_state(config, state)
    state["status"] = "done"
    state["index"] = len(files)
    save_state(config, state)
    emit({"ok": True, "done": True, "state": state}, args.json)
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    config = cfg(args)
    config.paths.ensure()
    if not (config.paths.data / "accounts.json").is_file():
        raise SystemExit("还没有账号，请先 account add")
    return run_batch(args, config)


def cmd_status(args: argparse.Namespace) -> int:
    config = cfg(args)
    state = load_state(config)
    emit({"ok": bool(state), "state": state}, args.json)
    return 0 if state else 1


def cmd_continue(args: argparse.Namespace) -> int:
    config = cfg(args)
    state = load_state(config)
    if not state:
        raise SystemExit("没有批次状态")
    if state.get("status") == "waiting_publish":
        current = state.get("current") or {}
        port = int(current.get("account_port") or 0)
        title = ""
        pkg = packages.load(config, current.get("package_id") or "")
        if pkg:
            title = str((pkg.get("content") or {}).get("title") or "")
        if not port or not publish_detect.detect(port, title).get("matched"):
            raise SystemExit("当前作品还没有检测到发布，请先在浏览器点发布")
        mark_uploaded(config, str(current.get("sha256") or ""), Path(current.get("path") or ""))
        state["index"] = int(state.get("index") or 0) + 1
        state["status"] = "running"
        save_state(config, state)
    return run_batch(args, config, state=state)


def cmd_pause(args: argparse.Namespace) -> int:
    config = cfg(args)
    state = load_state(config)
    if not state:
        raise SystemExit("没有批次状态")
    state["paused"] = True
    save_state(config, state)
    emit({"ok": True, "paused": True}, args.json)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    config = cfg(args)
    state = load_state(config)
    if not state:
        raise SystemExit("没有批次状态")
    state["paused"] = False
    if state.get("status") == "paused_waiting_publish":
        state["status"] = "waiting_publish"
    save_state(config, state)
    return cmd_continue(args)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="douyin-auto-upload")
    ap.add_argument("--data-root", default="", help="数据根目录；首次使用先 init")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("account")
    asub = p.add_subparsers(dest="account_cmd", required=True)
    pa = asub.add_parser("add")
    pa.add_argument("--display", required=True)
    pa.add_argument("--slug", default="")
    pa.add_argument("--profile-mode", choices=("new", "existing"), default="new")
    pa.add_argument("--profile-path", default="")
    pa.add_argument("--port", type=int, default=0)
    pa.add_argument("--expected-id", default="")
    pa.add_argument("--wait-login", type=float, default=0)
    pa.add_argument("--open", action="store_true", default=True)
    pa.set_defaults(func=cmd_account_add)
    pl = asub.add_parser("list")
    pl.set_defaults(func=cmd_account_list)
    pc = asub.add_parser("check")
    pc.add_argument("--slug", required=True)
    pc.add_argument("--wait-login", type=float, default=0)
    pc.set_defaults(func=cmd_account_check)

    p = sub.add_parser("package")
    psub = p.add_subparsers(dest="package_cmd", required=True)
    pa = psub.add_parser("add")
    pa.add_argument("--name", default="")
    pa.add_argument("--style-code", default="")
    pa.add_argument("--product-name", default="")
    pa.add_argument("--product-link", default="")
    pa.add_argument("--product-id", default="")
    pa.add_argument("--short-title", required=True)
    pa.add_argument("--title", required=True)
    pa.add_argument("--desc", required=True)
    pa.add_argument("--tags", required=True, help="逗号分隔")
    pa.add_argument("--account", default="")
    pa.add_argument("--plain", action="store_true")
    pa.set_defaults(func=cmd_package_add)
    pl = psub.add_parser("list")
    pl.set_defaults(func=cmd_package_list)

    p = sub.add_parser("scan")
    p.add_argument("--dir", required=True)
    p.add_argument("--recursive", action="store_true")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("plan")
    p.add_argument("--dir", required=True)
    p.add_argument("--recursive", action="store_true")
    p.add_argument("--write", action="store_true")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("upload")
    p.add_argument("--dir", required=True)
    p.add_argument("--allow-duplicate", action="store_true")
    p.add_argument("--recursive", action="store_true")
    p.add_argument("--mode", choices=("auto", "linked", "plain"), default="auto")
    p.add_argument("--account", default="")
    p.add_argument("--continue-mode", choices=("manual", "auto", "semi_auto"), default="semi_auto")
    p.add_argument("--timeout-min", type=float, default=10.0)
    p.add_argument("--wait-login", type=float, default=0.0)
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("status")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("continue")
    p.set_defaults(func=cmd_continue)
    p = sub.add_parser("pause")
    p.set_defaults(func=cmd_pause)
    p = sub.add_parser("resume")
    p.set_defaults(func=cmd_resume)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        config = cfg(args)
        config.paths.ensure()
        return int(args.func(args))
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
