"""内容包：保存/读取/列表（Phase 2 保存草稿，创建任务时补 job_id）。"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any

from .accounts import ApiError, now_iso
from .config import Config
from . import content_spec as spec_module
from . import copy_rules, llm_parse


def _packages_dir(config: Config):
    path = config.paths.packages
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write(path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def validate_content(content: dict[str, Any], spec: dict[str, Any] | None,
                     facts: list[str] | None = None, bans: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(content, dict):
        raise ApiError(1013, "content 必须是对象", 400)
    normalized = spec_module.normalize(spec)
    return llm_parse.validate_candidates([content], normalized, facts, bans)


def save(config: Config, payload: dict[str, Any]) -> dict[str, Any]:
    product = payload.get("product") or {}
    content = payload.get("content") or {}
    facts = [str(x).strip() for x in (payload.get("facts") or product.get("facts") or []) if str(x).strip()]
    if not str(product.get("name") or "").strip():
        raise ApiError(1013, "商品名称不能为空", 400)
    spec = spec_module.normalize(payload.get("content_spec"))
    bans = copy_rules.load_bans(config)
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raw_candidates = [content]
    validated_candidates: list[dict[str, Any]] = []
    all_errors: list[str] = []
    for index, candidate in enumerate(raw_candidates):
        validation = validate_content(candidate, spec, facts, bans)
        if not validation["ok"]:
            all_errors.extend(["候选" + str(index + 1) + "：" + item for item in validation["errors"][:2]])
        else:
            validated_candidates.append(validation["candidates"][0])
    if all_errors:
        raise ApiError(1013, "内容校验失败：" + "；".join(all_errors[:3]), 400)
    try:
        selected_index = int(payload.get("selected_index") or 0)
    except (TypeError, ValueError):
        selected_index = 0
    selected_index = max(0, min(selected_index, len(validated_candidates) - 1))
    selected_content = validated_candidates[selected_index]
    base_id = str(payload.get("package_id") or "").strip()
    previous = load(config, base_id) if base_id else None
    if previous:
        family_id = str(previous.get("family_id") or previous.get("package_id") or base_id)
        version = int(previous.get("version") or 1) + 1
        previous_package_id = str(previous.get("package_id") or base_id)
        change_type = "update"
        package_id = uuid.uuid4().hex
    else:
        package_id = uuid.uuid4().hex
        family_id = package_id
        version = 1
        previous_package_id = ""
        change_type = "new"
    source = dict(payload.get("source") or {})
    source.setdefault("type", "llm_pasteback")
    source.setdefault("raw_result_path", str(payload.get("raw_result_path") or ""))
    source.setdefault("prompt_hash", spec_module.prompt_hash(spec))
    source.setdefault("approved_by", "user")
    source.setdefault("approved_at", now_iso())
    package_name = str(payload.get("name") or "").strip()[:80]
    package = {
        "package_id": package_id,
        "name": package_name,
        "family_id": family_id,
        "version": version,
        "previous_package_id": previous_package_id,
        "change_type": change_type,
        "job_id": str(payload.get("job_id") or ""),
        "account_slug": str(payload.get("account_slug") or ""),
        "product": {
            "name": str(product.get("name") or "").strip(),
            "link": str(product.get("link") or "").strip(),
            "short_title": str(product.get("short_title") or "").strip(),
            "facts": facts,
        },
        "content": selected_content,
        "candidates": validated_candidates,
        "selected_index": selected_index,
        "content_spec": spec,
        "source": source,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "status": "confirmed",
    }
    path = _packages_dir(config) / f"{package_id}.json"
    _atomic_write(path, package)
    return package


def load(config: Config, package_id: str) -> dict[str, Any] | None:
    safe_id = "".join(ch for ch in str(package_id or "") if ch.isalnum() or ch in ("-", "_"))
    path = _packages_dir(config) / f"{safe_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def rename(config: Config, package_id: str, name: str) -> dict[str, Any]:
    """仅修改内容包显示名，不产生新版本、不改内容。"""
    package = load(config, package_id)
    if package is None:
        raise ApiError(1014, "内容包不存在：" + str(package_id), 404)
    package["name"] = str(name or "").strip()[:80]
    package["updated_at"] = now_iso()
    path = _packages_dir(config) / (str(package.get("package_id")) + ".json")
    _atomic_write(path, package)
    return package


def list_packages(config: Config) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(_packages_dir(config).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows.append({
            "package_id": data.get("package_id"),
            "family_id": data.get("family_id") or data.get("package_id"),
            "version": int(data.get("version") or 1),
            "previous_package_id": data.get("previous_package_id") or "",
            "change_type": data.get("change_type") or "new",
            "account_slug": data.get("account_slug"),
            "name": str(data.get("name") or ""),
            "title": (data.get("content") or {}).get("title"),
            "candidate_count": len(data.get("candidates") or [data.get("content") or {}]),
            "selected_index": int(data.get("selected_index") or 0),
            "created_at": data.get("created_at"),
            "job_id": data.get("job_id"),
        })
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows
