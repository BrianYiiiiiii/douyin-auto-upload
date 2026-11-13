"""ContentSpec：默认值、规范化、canonical JSON、prompt_hash 与提示词生成。"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .accounts import ApiError

DEFAULT_CONTENT_SPEC: dict[str, Any] = {
    "candidates": 5,
    "title_max": 30,
    "desc_max": 30,
    "tags_per_candidate": 5,
    "short_title_max": 10,
    "allow_material": False,
    "allow_price": False,
    "allow_absolute": False,
    "allow_medical": False,
    "allow_competitor": False,
}

INT_RANGES: dict[str, tuple[int, int]] = {
    "candidates": (1, 10),
    "title_max": (5, 30),
    "desc_max": (10, 60),
    "tags_per_candidate": (1, 5),
    "short_title_max": (4, 10),
}

BOOL_FIELDS = ("allow_material", "allow_price", "allow_absolute", "allow_medical", "allow_competitor")

PROMPT_TEMPLATE = """你是抖音短视频带货文案助手。请只基于下面给出的商品事实写文案，不要编造事实。

【商品】{product_name}
【商品链接】{product_link}

【商品事实（用户提供，必须以此为准）】
{facts}

【生成要求】
1. 生成 {candidates} 条候选，每条候选独立不同。
2. 每条候选单独生成 {tags_per_candidate} 个话题标签（不是总数）。
3. 标题不超过 {title_max} 字；简介只有一个字段，不超过 {desc_max} 字；短标题不超过 {short_title_max} 字。
4. {ban_instruction}
5. 所有卖点必须能在上面的事实中找到依据；找不到依据的不要写。
6. 不改写、不夸大、不使用绝对化词。

【输出格式】
只输出一个 JSON 对象，不要任何解释、Markdown 或代码块围栏。字段固定为：
{{"candidates":[{{"title":"","desc":"","tags":["",""],"short_title":"","selling_points":["",""]}}]}}
""".strip()


def normalize(spec: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_CONTENT_SPEC)
    source = spec or {}
    if not isinstance(source, dict):
        raise ApiError(1013, "content_spec 必须是对象", 400)
    for key in DEFAULT_CONTENT_SPEC:
        if key in source and source[key] is not None:
            merged[key] = source[key]
    for key in BOOL_FIELDS:
        value = merged[key]
        if isinstance(value, str):
            merged[key] = value.strip().lower() in ("1", "true", "yes", "on")
        else:
            merged[key] = bool(value)
    for key, (low, high) in INT_RANGES.items():
        try:
            value = int(merged[key])
        except (TypeError, ValueError) as exc:
            raise ApiError(1013, f"{key} 必须是整数", 400) from exc
        if not low <= value <= high:
            raise ApiError(1013, f"{key} 必须在 {low}–{high} 之间", 400)
        merged[key] = value
    return merged


def canonical_json(spec: dict[str, Any] | None) -> str:
    return json.dumps(normalize(spec), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def prompt_hash(spec: dict[str, Any] | None) -> str:
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


def _ban_instruction(spec: dict[str, Any]) -> str:
    allowed: list[str] = []
    banned: list[str] = []
    labels = {
        "material": ("allow_material", "具体材质承诺（如真皮/纯羊毛/100%棉）"),
        "price": ("allow_price", "价格表述（原价/划线价/全网最低等）"),
        "absolute": ("allow_absolute", "绝对化极限词（最/第一/顶级/100%等）"),
        "medical": ("allow_medical", "医疗/功效表述（治疗/根治/药效等）"),
        "competitor": ("allow_competitor", "竞品对比/点名竞品"),
    }
    for key, (flag, label) in labels.items():
        if spec.get(flag):
            allowed.append(label + "（仅限事实有依据时使用）")
        else:
            banned.append(label)
    parts = []
    if banned:
        parts.append("禁止：" + "；".join(banned))
    if allowed:
        parts.append("若使用以下内容，必须有事实依据：" + "；".join(allowed))
    parts.append("系统硬禁：违法、虚假宣传、医疗功效、绝对化极限词一律不得出现。")
    return " ".join(parts)


def build_prompt(spec: dict[str, Any] | None, product: dict[str, Any], facts: list[str]) -> str:
    normalized = normalize(spec)
    name = str((product or {}).get("name") or "").strip()
    link = str((product or {}).get("link") or "").strip()
    if not name:
        raise ApiError(1013, "商品名称不能为空", 400)
    fact_lines = [str(item).strip() for item in (facts or []) if str(item).strip()]
    if len(fact_lines) < 3:
        raise ApiError(1013, f"商品事实至少 3 条（当前 {len(fact_lines)} 条）", 400)
    if len(fact_lines) > 5:
        raise ApiError(1013, f"商品事实最多 5 条（当前 {len(fact_lines)} 条）", 400)
    facts_text = "\n".join(f"- {line}" for line in fact_lines)
    return PROMPT_TEMPLATE.format(
        product_name=name,
        product_link=link or "（未提供）",
        facts=facts_text,
        candidates=normalized["candidates"],
        tags_per_candidate=normalized["tags_per_candidate"],
        title_max=normalized["title_max"],
        desc_max=normalized["desc_max"],
        short_title_max=normalized["short_title_max"],
        ban_instruction=_ban_instruction(normalized),
    )
