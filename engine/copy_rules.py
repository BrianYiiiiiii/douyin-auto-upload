"""硬禁/软禁词与合规检查（默认词表，可被 data/hard_bans.json 覆盖）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Config

DEFAULT_BANS: dict[str, Any] = {
    "version": 1,
    "categories": {
        "absolute": ["国家级", "最高级", "最佳", "第一", "顶级", "绝无仅有", "独一无二", "史上最低", "全网最低", "100%"],
        "medical": ["治疗", "治愈", "根治", "药效", "疗效", "永久", "防癌", "抗癌", "包治", "100% 有效"],
        "price": ["原价", "划线价", "仅售", "秒杀价", "到手价", "全网最低价"],
        "material": ["真皮", "纯羊毛", "羊绒", "真丝", "100%棉", "纯棉", "白鸭绒", "兔毛", "狐狸毛", "水貂"],
        "competitor": ["比某", "吊打", "秒杀某", "竞品"],
        "false": ["假一赔十", "包治百病", "无效退款"],
    },
    "note": "absolute/medical/false 为系统硬禁（勾选也不放行）；material/price/competitor 默认禁，勾选后仅提示有事实依据。",
}


def load_bans(config: Config) -> dict[str, Any]:
    path = config.paths.hard_bans
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("categories"), dict):
                return data
        except (OSError, ValueError):
            pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_BANS, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return dict(DEFAULT_BANS)


def _hit(category: str, word: str, severity: str) -> dict[str, str]:
    return {"category": category, "word": word, "severity": severity}


def scan_text(text: str, spec: dict[str, Any], bans: dict[str, Any]) -> list[dict[str, str]]:
    """返回命中列表：severity=error（硬禁/未允许类）或 warning（已允许类）。"""
    value = str(text or "")
    hits: list[dict[str, str]] = []
    if not value:
        return hits
    flags = {
        "material": "allow_material",
        "price": "allow_price",
        "absolute": "allow_absolute",
        "medical": "allow_medical",
        "competitor": "allow_competitor",
    }
    for category, words in (bans.get("categories") or {}).items():
        for word in words or []:
            if word and word in value:
                allowed = bool(spec.get(flags.get(category, ""), False))
                hard = category in ("absolute", "medical", "false")
                severity = "error" if (hard or not allowed) else "warning"
                hits.append(_hit(category, word, severity))
    return hits
