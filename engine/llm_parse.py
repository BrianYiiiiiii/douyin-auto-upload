"""解析免费 LLM 粘贴结果，并按 ContentSpec 校验（不静默截断）。"""
from __future__ import annotations

import json
import re
from typing import Any

from .accounts import ApiError
from . import content_spec as spec_module
from .copy_rules import scan_text

FENCE = chr(96) * 3


def _repair_json(text: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", text)


def extract_json(text: str) -> Any:
    value = str(text or "").strip()
    if not value:
        raise ApiError(1013, "粘贴内容为空", 400)
    if "~~~" in value:
        value = max(value.split("~~~"), key=len).strip()
    if FENCE in value:
        value = max(value.split(FENCE), key=len).strip()
        if value.lower().startswith("json"):
            value = value[4:].strip()
    start = value.find("{")
    if start < 0:
        raise ApiError(1013, "粘贴内容里没有 JSON 对象", 400)
    for i in range(start, len(value)):
        if value[i] != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for j in range(i, len(value)):
            ch = value[j]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        snippet = value[i:j + 1]
                        for attempt in (snippet, _repair_json(snippet)):
                            try:
                                return json.loads(attempt)
                            except ValueError:
                                continue
                        break
    raise ApiError(1013, "无法从粘贴内容中解析出 JSON（可检查是否被截断）", 400)


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"[\s，。；、,.!！?？:：\-—\"'“”‘’（）()\[\]【】/]+", "", text or "")
    return {cleaned[i:i + 2] for i in range(max(0, len(cleaned) - 1))}


def _supported(text: str, fact_bigrams: set[str]) -> bool:
    grams = _bigrams(text)
    return bool(grams & fact_bigrams)


def validate_candidates(candidates: Any, spec: dict[str, Any], facts: list[str] | None = None,
                        bans: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = spec_module.normalize(spec)
    errors: list[str] = []
    warnings: list[str] = []
    items = candidates if isinstance(candidates, list) else []
    if not items:
        errors.append("没有候选：candidates 必须是非空数组")
        return {"ok": False, "errors": errors, "warnings": warnings, "candidates": [], "spec": normalized}
    if len(items) != normalized["candidates"]:
        warnings.append(f"候选数量 {len(items)} 与 ContentSpec 要求 {normalized['candidates']} 不一致（不自动截断）")
    fact_bigrams: set[str] = set()
    for fact in (facts or []):
        fact_bigrams |= _bigrams(str(fact))
    if bans is None:
        bans = {"categories": {}}
    cleaned: list[dict[str, Any]] = []
    for index, raw in enumerate(items, 1):
        if not isinstance(raw, dict):
            errors.append(f"候选 {index} 不是对象")
            continue
        title = _text(raw.get("title"))
        desc = _text(raw.get("desc"))
        short_title = _text(raw.get("short_title"))
        tags_raw = raw.get("tags")
        tags = [str(t).strip() for t in tags_raw if str(t).strip()] if isinstance(tags_raw, list) else []
        selling_raw = raw.get("selling_points")
        selling = [str(t).strip() for t in selling_raw if str(t).strip()] if isinstance(selling_raw, list) else []
        if not title:
            errors.append(f"候选 {index}: title 不能为空")
        elif len(title) > normalized["title_max"]:
            errors.append(f"候选 {index}: 标题 {len(title)} 字，超过上限 {normalized['title_max']}")
        if not desc:
            errors.append(f"候选 {index}: desc 不能为空")
        elif len(desc) > normalized["desc_max"]:
            errors.append(f"候选 {index}: 简介 {len(desc)} 字，超过上限 {normalized['desc_max']}")
        if not tags:
            errors.append(f"候选 {index}: 至少需要 1 个话题")
        elif len(tags) > normalized["tags_per_candidate"]:
            errors.append(f"候选 {index}: 话题 {len(tags)} 个，超过上限 {normalized['tags_per_candidate']}")
        if not short_title:
            errors.append(f"候选 {index}: 短标题不能为空")
        elif len(short_title) > normalized["short_title_max"]:
            errors.append(f"候选 {index}: 短标题 {len(short_title)} 字，超过上限 {normalized['short_title_max']}")
        for field, text in (("title", title), ("desc", desc), ("short_title", short_title),
                            ("tags", " ".join(tags)), ("selling_points", " ".join(selling))):
            for hit in scan_text(text, normalized, bans):
                label = f"候选 {index}: {field} 命中{'硬禁词' if hit['severity'] == 'error' else '需谨慎词'}"
                message = f"{label}「{hit['word']}」（{hit['category']}）"
                (errors if hit["severity"] == "error" else warnings).append(message)
        if fact_bigrams:
            for point in selling:
                if not _supported(point, fact_bigrams):
                    warnings.append(f"候选 {index}: 卖点「{point[:24]}」在事实里找不到依据（疑似编造）")
            if desc and not _supported(desc, fact_bigrams):
                warnings.append(f"候选 {index}: 简介与事实缺少重合词（请人工确认是否有依据）")
        cleaned.append({"title": title, "desc": desc, "tags": tags,
                        "short_title": short_title, "selling_points": selling})
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "candidates": cleaned, "spec": normalized}


def parse_and_validate(raw: str, spec: dict[str, Any] | None, facts: list[str] | None = None,
                       bans: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = spec_module.normalize(spec)
    payload = extract_json(raw)
    if isinstance(payload, dict):
        candidates = payload.get("candidates")
        if candidates is None and "title" in payload:
            candidates = [payload]
    elif isinstance(payload, list):
        candidates = payload
    else:
        candidates = None
    if candidates is None:
        raise ApiError(1013, "JSON 里没有 candidates 字段", 400)
    result = validate_candidates(candidates, normalized, facts, bans)
    result["prompt_hash"] = spec_module.prompt_hash(normalized)
    result["bans_version"] = str((bans or {}).get("version") or "")
    return result
