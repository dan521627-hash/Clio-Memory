"""Small, fixed memory taxonomy used by every write surface."""

from __future__ import annotations

from collections.abc import Iterable


CATEGORIES = (
    "核心与世界观",
    "关系与亲密",
    "日常生活",
    "健康与照护",
    "计划与事务",
    "技术与创作",
    "社交与社区",
)

_CATEGORY_SET = set(CATEGORIES)
_KEYWORDS = {
    "核心与世界观": (
        "存在论", "世界观", "身份", "锚点", "我是谁", "本人", "主体性",
        "底色", "开机口令", "相处方式", "人格", "性格",
    ),
    "关系与亲密": (
        "恋爱", "爱她", "爱你", "亲密", "私密", "伴侣", "调情", "吃醋",
        "占有欲", "欲望", "亲吻", "拥抱", "伴侣", "亲密互动",
    ),
    "日常生活": (
        "日常", "生活", "今天", "昨晚", "吃饭", "饮食", "穿搭", "购物",
        "搬家", "居家", "一天", "偏好", "口味", "睡醒",
    ),
    "健康与照护": (
        "健康", "照护", "睡眠", "药", "复诊", "医院", "疼",
        "胃", "身体", "经期", "体重", "饮食控制",
    ),
    "计划与事务": (
        "待办", "计划", "提醒", "续费", "工作", "学习", "考试", "备考",
        "任务", "日期", "安排", "预算", "账", "截止",
    ),
    "技术与创作": (
        "docker", "mcp", "cloudflare", "api", "编程", "部署", "技术", "硬件",
        "软件", "代码", "服务器", "域名", "向量", "模型", "创作", "写作",
    ),
    "社交与社区": (
        "论坛", "花园", "社区", "发帖", "回帖", "朋友", "群", "社交",
        "codex", "AI 助手", "网友", "社交平台",
    ),
}


def _strings(values: Iterable[object] | object | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = values.split(",")
    return [str(value).strip() for value in values if str(value).strip()]


def parse_category(values: Iterable[object] | object | None) -> str:
    """Return one explicitly selected category, rejecting free-form labels."""
    selected = _strings(values)
    if not selected:
        return ""
    unique = list(dict.fromkeys(selected))
    if len(unique) != 1 or unique[0] not in _CATEGORY_SET:
        allowed = "、".join(CATEGORIES)
        raise ValueError(f"分类只能选择一个系统分类：{allowed}")
    return unique[0]


def classify_category(
    content: str,
    proposed_domains: Iterable[object] | object | None = None,
    proposed_tags: Iterable[object] | object | None = None,
) -> str:
    """Map content and model suggestions to exactly one stable category."""
    proposed = _strings(proposed_domains) + _strings(proposed_tags)
    explicit = [value for value in proposed if value in _CATEGORY_SET]
    if explicit:
        return explicit[0]

    haystack = " ".join([str(content or ""), *proposed]).lower()
    scores = {
        category: sum(haystack.count(keyword.lower()) for keyword in keywords)
        for category, keywords in _KEYWORDS.items()
    }
    best = max(CATEGORIES, key=lambda category: scores[category])
    return best if scores[best] > 0 else "日常生活"


def normalize_analysis(content: str, analysis: dict | None) -> dict:
    """Keep emotion/name analysis, while replacing free tags with one category."""
    normalized = dict(analysis or {})
    category = classify_category(
        content,
        normalized.get("domain", []),
        normalized.get("tags", []),
    )
    normalized["domain"] = [category]
    normalized["tags"] = [category]
    normalized["category"] = category
    return normalized


def category_from_metadata(metadata: dict | None) -> str:
    metadata = metadata or {}
    for value in _strings(metadata.get("domain", [])) + _strings(metadata.get("tags", [])):
        if value in _CATEGORY_SET:
            return value
    return ""
