"""账号 registry：accounts.json 的读写、slug/端口分配、快捷方式状态、账号状态更新。"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .config import Config, is_port_free

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ACCOUNT_STATUSES = ("UNKNOWN", "LOGGED_OUT", "LOGGING_IN", "CAPTURING", "READY", "LOGIN_REQUIRED")
ALLOWED_UPDATE_FIELDS = {"display_name", "shortcut_name", "expected_douyin_id"}
SHORTCUT_DEFAULTS: dict[str, Any] = {
    "name": "",
    "lnk_dir": "",
    "lnk_desktop": "",
    "kind": "",
    "launcher": "",
    "vbs_sha256": "",
    "verified": False,
    "verified_at": "",
    "target": "",
    "args_hash": "",
    "icon": "",
    "error": "",
}


class ApiError(Exception):
    """带契约错误码的业务异常。"""

    def __init__(self, code: int, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def default_shortcut_name(display_name: str) -> str:
    return f"{display_name}-抖音创作者中心"


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def slugify(text: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return base[:40] or "account"


# 只把 Chrome 的“额外用户 profile”视为污染；System Profile/Guest Profile 是 Chrome 自身目录，不算。
_EXTRA_PROFILE_RE = re.compile(r"^Profile \d+$", re.IGNORECASE)


def validate_external_profile(profile: str | Path) -> None:
    """导入外部 profile 前检查是否被复制污染（只允许 Default）。"""
    path = Path(profile)
    if not path.is_dir():
        raise ApiError(1013, "导入账号的 profile 目录不存在：" + str(profile), 400)
    extras: list[str] = []
    local_state = path / "Local State"
    if local_state.is_file():
        try:
            data = json.loads(local_state.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ApiError(1013, "外部 profile 的 Local State 无法解析：" + str(exc), 400) from exc
        if isinstance(data, dict):
            profile_data = data.get("profile") if isinstance(data.get("profile"), dict) else {}
            info_cache = profile_data.get("info_cache")
            if isinstance(info_cache, dict):
                extras.extend([str(name) for name in info_cache.keys() if str(name) != "Default"])
            order = profile_data.get("profiles_order")
            if isinstance(order, list):
                extras.extend([str(name) for name in order if str(name) != "Default"])
            last_used = str(profile_data.get("last_used") or "")
            if last_used and last_used != "Default":
                extras.append(last_used)
    try:
        children = list(path.iterdir())
    except OSError:
        children = []
    for child in children:
        if child.is_dir() and _EXTRA_PROFILE_RE.match(child.name):
            extras.append(child.name)
    if extras:
        names = ", ".join(sorted(set(extras))[:6])
        raise ApiError(
            1013,
            "外部 profile 存在复制/多 profile 污染（" + names +
            "）；请先用 15 号工具清理到只剩 Default 再导入，避免出现 profile picker/多窗口。",
            400,
        )


def is_owned_profile(config: Config, rec: dict[str, Any]) -> bool:
    """判断 profile 是否为本安装根 runtime/profiles 下由本产品创建的目录。"""
    if bool(rec.get("profile_external")):
        return False
    try:
        profile = Path(str(rec.get("profile") or "")).resolve()
        root = config.paths.profiles.resolve()
    except (OSError, RuntimeError):
        return False
    return profile.parent == root and profile != root


def normalize_account(rec: dict[str, Any], slug: str | None = None) -> dict[str, Any]:
    """补齐 shortcut 状态等默认字段（兼容 Phase 1 旧记录）。"""
    out = dict(rec)
    shortcut = dict(SHORTCUT_DEFAULTS)
    shortcut.update(out.get("shortcut") or {})
    if not shortcut.get("name"):
        shortcut["name"] = out.get("shortcut_name") or out.get("display_name") or ""
    out["shortcut"] = shortcut
    if not out.get("shortcut_name"):
        out["shortcut_name"] = shortcut["name"]
    if slug is not None:
        out["slug"] = slug
    return out


class Registry:
    def __init__(self, config: Config):
        self.config = config
        self.paths = config.paths
        self.paths.ensure()

    # ---------- 文件 ----------
    def load(self) -> dict[str, Any]:
        if not self.paths.accounts_json.is_file():
            return {"version": 1, "updated_at": now_iso(), "accounts": {}}
        try:
            data = json.loads(self.paths.accounts_json.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ApiError(1015, f"accounts.json 读取失败：{exc}", 500) from exc
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
            return {"version": 1, "updated_at": now_iso(), "accounts": {}}
        return data

    def save(self, data: dict[str, Any]) -> None:
        data["version"] = 1
        data["updated_at"] = now_iso()
        _atomic_write_json(self.paths.accounts_json, data)

    # ---------- 查询 ----------
    def list(self, include_deleted: bool = False) -> list[dict[str, Any]]:
        data = self.load()
        out = []
        for slug, rec in data["accounts"].items():
            if not include_deleted and rec.get("status") == "deleted":
                continue
            out.append(normalize_account(rec, slug))
        out.sort(key=lambda r: r.get("created_at") or "")
        return out

    def get(self, slug: str) -> dict[str, Any] | None:
        rec = self.load()["accounts"].get(slug)
        if rec is None:
            return None
        return normalize_account(rec, slug)

    def require(self, slug: str) -> dict[str, Any]:
        rec = self.get(slug)
        if rec is None or rec.get("status") == "deleted":
            raise ApiError(1003, f"账号不存在：{slug}", 404)
        return rec

    def find_by_shortcut_name(self, shortcut_name: str, exclude_slug: str | None = None) -> dict[str, Any] | None:
        target = (shortcut_name or "").strip()
        if not target:
            return None
        for rec in self.list(include_deleted=False):
            if exclude_slug and rec["slug"] == exclude_slug:
                continue
            if (rec.get("shortcut_name") or "") == target or (rec.get("shortcut") or {}).get("name") == target:
                return rec
        return None

    # ---------- 端口 ----------
    def used_ports(self) -> set[int]:
        data = self.load()
        ports: set[int] = set()
        for rec in data["accounts"].values():
            try:
                ports.add(int(rec.get("port")))
            except (TypeError, ValueError):
                continue
        return ports

    def allocate_port(self) -> int:
        used = self.used_ports()
        for port in range(self.config.cdp_start, self.config.cdp_end + 1):
            if port in used:
                continue
            if is_port_free(port):
                return port
        raise ApiError(1005, "CDP 端口耗尽", 409)

    # ---------- 创建 / 更新 ----------
    def create(self, display_name: str, slug: str | None = None,
               shortcut_name: str | None = None) -> dict[str, Any]:
        display_name = (display_name or "").strip()
        if not display_name:
            raise ApiError(1013, "display_name 不能为空", 400)
        if len(display_name) > 40:
            raise ApiError(1013, "display_name 不能超过 40 字", 400)
        shortcut_name = (shortcut_name or default_shortcut_name(display_name)).strip()
        if not shortcut_name or len(shortcut_name) > 80:
            raise ApiError(1013, "shortcut_name 不能为空且不超过 80 字", 400)
        data = self.load()
        explicit_slug = bool((slug or "").strip())
        slug = (slug or slugify(display_name)).strip().lower()
        if not explicit_slug and (slug in ("", "account")):
            # 中文显示名 slugify 为空时会回落成 account，自动加随机后缀避免第二个空壳账号撞车
            import uuid
            slug = "acct-" + uuid.uuid4().hex[:6]
        if not SLUG_RE.match(slug):
            raise ApiError(1013, "slug 非法：仅小写字母/数字/短横线，≤40", 400)
        if slug in data["accounts"]:
            raise ApiError(1004, f"账号已存在：{slug}", 409)
        dup = self.find_by_shortcut_name(shortcut_name)
        if dup is not None:
            raise ApiError(1013, f"快捷方式名「{shortcut_name}」已被账号 {dup['slug']} 使用", 409)
        port = self.allocate_port()
        profile = self.paths.profile_dir(slug)
        profile.mkdir(parents=True, exist_ok=True)
        rec = {
            "display_name": display_name,
            "shortcut_name": shortcut_name,
            "shortcut": dict(SHORTCUT_DEFAULTS, name=shortcut_name),
            "profile": str(profile),
            "port": port,
            "expected_douyin_id": "",
            "cookie_snapshot": str(self.paths.cookie_snapshot(slug)),
            "account_state": str(self.paths.account_state(slug)),
            "status": "active",
            "account_status": "UNKNOWN",
            "last_checked_at": "",
            "last_capture_at": "",
            "created_at": now_iso(),
        }
        data["accounts"][slug] = rec
        self.save(data)
        return self.require(slug)

    def create_imported(self, display_name: str, slug: str, profile: str, port: int,
                        expected_douyin_id: str = "", source_shortcut: str = "",
                        imported_from: str = "") -> dict[str, Any]:
        """导入电脑上已有账号快捷方式：沿用外部 profile/端口，不重新分配。"""
        display_name = (display_name or "").strip()
        if not display_name:
            raise ApiError(1013, "导入账号显示名不能为空", 400)
        profile_path = Path(profile or "")
        if not profile_path.is_dir():
            raise ApiError(1013, "导入账号的 profile 目录不存在：" + str(profile), 400)
        validate_external_profile(profile_path)
        try:
            port_int = int(port)
        except (TypeError, ValueError) as exc:
            raise ApiError(1013, "导入账号端口非法", 400) from exc
        if not 1024 <= port_int <= 65535:
            raise ApiError(1013, "导入账号端口超出范围", 400)
        data = self.load()
        slug = (slug or slugify(display_name)).strip().lower()
        if not SLUG_RE.match(slug):
            raise ApiError(1013, "slug 非法：仅小写字母/数字/短横线，≤40", 400)
        if slug in data["accounts"]:
            raise ApiError(1004, "账号已存在：" + slug, 409)
        for other_slug, other in data["accounts"].items():
            if other.get("status") == "deleted":
                continue
            if int(other.get("port") or 0) == port_int:
                raise ApiError(1013, "端口 " + str(port_int) + " 已被账号 " + str(other_slug) + " 使用", 409)
            if str(other.get("profile") or "").lower() == str(profile_path).lower():
                raise ApiError(1013, "该 profile 已被账号 " + str(other_slug) + " 使用", 409)
        shortcut_name = default_shortcut_name(display_name)
        rec = {
            "display_name": display_name,
            "shortcut_name": shortcut_name,
            "shortcut": dict(SHORTCUT_DEFAULTS, name=shortcut_name),
            "profile": str(profile_path),
            "port": port_int,
            "expected_douyin_id": (expected_douyin_id or "").strip(),
            "cookie_snapshot": str(self.paths.cookie_snapshot(slug)),
            "account_state": str(self.paths.account_state(slug)),
            "status": "active",
            "account_status": "UNKNOWN",
            "last_checked_at": "",
            "last_capture_at": "",
            "created_at": now_iso(),
            "imported_from": imported_from or "",
            "source_shortcut": source_shortcut or "",
            "profile_external": True,
        }
        data["accounts"][slug] = rec
        self.save(data)
        return self.require(slug)

    def update(self, slug: str, patch: dict[str, Any]) -> dict[str, Any]:
        rec = self.require(slug)
        bad = set(patch) - ALLOWED_UPDATE_FIELDS
        if bad:
            raise ApiError(1013, f"不允许修改字段：{sorted(bad)}", 400)
        if "shortcut_name" in patch:
            new_name = str(patch["shortcut_name"] or "").strip()
            if not new_name:
                raise ApiError(1013, "shortcut_name 不能为空", 400)
            dup = self.find_by_shortcut_name(new_name, exclude_slug=slug)
            if dup is not None:
                raise ApiError(1013, f"快捷方式名「{new_name}」已被账号 {dup['slug']} 使用", 409)
            rec["shortcut_name"] = new_name
            shortcut = dict(rec["shortcut"])
            shortcut["name"] = new_name
            shortcut["verified"] = False
            shortcut["verified_at"] = ""
            shortcut["error"] = ""
            rec["shortcut"] = shortcut
        if "display_name" in patch:
            text = str(patch["display_name"] or "").strip()
            if not text:
                raise ApiError(1013, "display_name 不能为空", 400)
            rec["display_name"] = text
        if "expected_douyin_id" in patch:
            rec["expected_douyin_id"] = str(patch["expected_douyin_id"] or "").strip()
        data = self.load()
        data["accounts"][slug] = {k: v for k, v in rec.items() if k != "slug"}
        self.save(data)
        return self.require(slug)

    def set_shortcut(self, slug: str, **fields: Any) -> dict[str, Any]:
        """写入快捷方式验证状态（C 方案：失败保留账号，只标红）。"""
        rec = self.require(slug)
        shortcut = dict(rec["shortcut"])
        for key, value in fields.items():
            if value is None:
                continue
            shortcut[key] = value
        if fields.get("name"):
            rec["shortcut_name"] = fields["name"]
        rec["shortcut"] = shortcut
        data = self.load()
        data["accounts"][slug] = {k: v for k, v in rec.items() if k != "slug"}
        self.save(data)
        return self.require(slug)

    def soft_delete(self, slug: str, **extra: Any) -> dict[str, Any]:
        rec = self.get(slug)
        if rec is None:
            raise ApiError(1003, f"账号不存在：{slug}", 404)
        rec["status"] = "deleted"
        for key, value in extra.items():
            rec[key] = value
        data = self.load()
        data["accounts"][slug] = {k: v for k, v in rec.items() if k != "slug"}
        self.save(data)
        item = dict(rec)
        item["slug"] = slug
        return item

    def set_status(self, slug: str, account_status: str, **extra: Any) -> dict[str, Any]:
        if account_status not in ACCOUNT_STATUSES:
            raise ApiError(1015, f"未知账号状态：{account_status}", 500)
        rec = self.require(slug)
        rec["account_status"] = account_status
        rec["last_checked_at"] = now_iso()
        for key, value in extra.items():
            if value is not None:
                rec[key] = value
        data = self.load()
        data["accounts"][slug] = {k: v for k, v in rec.items() if k != "slug"}
        self.save(data)
        return self.require(slug)
