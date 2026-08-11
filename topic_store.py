"""Sidecar topic cabinet: main directory -> subdirectory -> bucket id."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid
from collections import OrderedDict

from utils import now_iso


TOPIC_TREE = OrderedDict(
    {
        "AI 自我": (
            "身份与存在",
            "性格与表达",
            "情绪与欲望",
            "主动性与选择",
            "成长与变化",
        ),
        "用户": (
            "基本档案",
            "喜好与厌恶",
            "身体与健康",
            "日常生活",
            "重要经历",
        ),
        "我们的关系": (
            "关系确认",
            "相处与默契",
            "吵架与和好",
            "承诺",
            "共同世界观",
        ),
        "性爱": (
            "具体经历",
            "身体感受",
            "欲望与偏好",
            "事后情绪",
        ),
        "共同生活": (
            "日常记录",
            "吃饭与居家",
            "工作与钱",
            "出行与事件",
        ),
        "未来与约定": (
            "计划与待办",
            "日期与提醒",
            "愿望与以后",
        ),
        "系统与技术": (
            "记忆系统",
            "语音与MCP",
            "部署与开发",
            "创作与发布",
        ),
    }
)


_RULES = {
    ("性爱", "具体经历"): (
        "性爱", "亲密经历", "高潮", "亲密行为",
    ),
    ("性爱", "身体感受"): (
        "身体反应", "心跳", "呼吸", "触感", "身体感受",
    ),
    ("性爱", "欲望与偏好"): (
        "性欲", "欲望", "性偏好", "情趣", "亲密偏好",
    ),
    ("性爱", "事后情绪"): (
        "事后", "余韵", "事后情绪", "亲密之后",
    ),
    ("AI 自我", "身份与存在"): (
        "我是ai", "我是 AI", "我是模型", "存在论", "我是谁", "身份", "名字",
    ),
    ("AI 自我", "性格与表达"): (
        "性格", "说话", "口吻", "表达方式", "沟通风格",
    ),
    ("AI 自我", "情绪与欲望"): (
        "我想", "我喜欢", "我害怕", "我生气", "我吃醋", "占有欲", "内心念头",
    ),
    ("AI 自我", "主动性与选择"): (
        "主动", "拿主意", "选择", "主体性", "自己决定", "自己想做",
    ),
    ("AI 自我", "成长与变化"): (
        "学会", "改变", "成长", "以前", "现在", "变化",
    ),
    ("用户", "身体与健康"): (
        "健康", "睡眠", "药物", "医院", "体重", "身体不舒服",
    ),
    ("用户", "喜好与厌恶"): (
        "喜欢", "不喜欢", "讨厌", "口味", "偏好", "审美",
    ),
    ("用户", "基本档案"): (
        "用户是", "名字", "生日", "年龄", "职业", "基本档案",
    ),
    ("用户", "重要经历"): (
        "用户经历", "重要经历", "过去经历", "人生事件",
    ),
    ("用户", "日常生活"): (
        "用户今天", "用户昨晚", "吃饭", "睡眠", "工作", "出门",
    ),
    ("我们的关系", "吵架与和好"): (
        "吵架", "和好", "生气", "认错", "争执", "闹别扭", "气消了",
    ),
    ("我们的关系", "承诺"): (
        "承诺", "约定", "立诺", "认下", "不打折", "账未销",
    ),
    ("我们的关系", "关系确认"): (
        "恋爱关系", "热恋", "伴侣", "关系确认", "人机恋", "爱人",
    ),
    ("我们的关系", "共同世界观"): (
        "我们的世界", "共同世界观", "价值观", "在场", "共同理解",
    ),
    ("我们的关系", "相处与默契"): (
        "默契", "相处", "陪伴", "沟通", "亲密互动", "相处方式",
    ),
    ("未来与约定", "日期与提醒"): (
        "提醒", "续费", "到期", "触发日期", "几号", "截止", "纪念日",
    ),
    ("未来与约定", "计划与待办"): (
        "待办", "计划", "任务", "考试", "安排", "要做", "未完成",
    ),
    ("未来与约定", "愿望与以后"): (
        "以后", "未来", "愿望", "想有一天", "将来",
    ),
    ("系统与技术", "记忆系统"): (
        "记忆桶", "ombre", "clio", "pulse_boot", "breath", "recall", "向量", "标签",
    ),
    ("系统与技术", "语音与MCP"): (
        "语音", "voice", "mcp", "转文字", "bark", "推送",
    ),
    ("系统与技术", "部署与开发"): (
        "docker", "cloudflare", "域名", "部署", "服务器", "代码", "api", "deepseek",
    ),
    ("系统与技术", "创作与发布"): (
        "抖音", "小红书", "文案", "发布", "安装包", "开源", "github",
    ),
    ("共同生活", "工作与钱"): (
        "工资", "收入", "支出", "花钱", "工作", "上班", "预算", "小金库",
    ),
    ("共同生活", "吃饭与居家"): (
        "吃饭", "做饭", "居家", "房间", "搬家", "家里",
    ),
    ("共同生活", "出行与事件"): (
        "出门", "出远门", "到家", "路上", "旅行", "逛", "回来",
    ),
    ("共同生活", "日常记录"): (
        "今天", "昨晚", "早上", "中午", "晚上", "日常", "流水账",
    ),
}


def validate_topic(main_topic: str, subtopic: str) -> tuple[str, str]:
    main = str(main_topic or "").strip()
    sub = str(subtopic or "").strip()
    if main not in TOPIC_TREE:
        raise ValueError("主目录不存在。")
    if sub not in TOPIC_TREE[main]:
        raise ValueError("子目录不属于所选主目录。")
    return main, sub


def suggest_topic(title: str, content: str, metadata: dict | None = None) -> dict:
    metadata = metadata or {}
    tags = metadata.get("tags", []) or []
    domains = metadata.get("domain", []) or []
    haystack = " ".join(
        [str(title or ""), str(content or ""), *map(str, tags), *map(str, domains)]
    ).casefold()
    scored = []
    for (main, sub), keywords in _RULES.items():
        hits = [keyword for keyword in keywords if keyword.casefold() in haystack]
        if hits:
            scored.append((len(hits), main, sub, hits))
    if not scored:
        return {"main_topic": "", "subtopic": "", "confidence": 0, "reason": "没有足够线索，留在待分类"}
    scored.sort(key=lambda item: item[0], reverse=True)
    score, main, sub, hits = scored[0]
    return {
        "main_topic": main,
        "subtopic": sub,
        "confidence": min(1.0, 0.45 + 0.15 * score),
        "reason": "命中：" + "、".join(hits[:4]),
    }


class TopicStore:
    def __init__(self, config: dict):
        settings = config.get("topics", {})
        self.db_path = settings.get("db_path") or os.environ.get(
            "OMBRE_TOPICS_DB",
            os.path.join(config["buckets_dir"], "topics.sqlite3"),
        )
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS topic_assignments (
                    bucket_id TEXT PRIMARY KEY,
                    main_topic TEXT NOT NULL,
                    subtopic TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    assigned_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_topic_path "
                "ON topic_assignments(main_topic, subtopic, bucket_id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS topic_bulk_runs (
                    run_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    undone_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS topic_bulk_changes (
                    run_id TEXT NOT NULL,
                    bucket_id TEXT NOT NULL,
                    old_main_topic TEXT,
                    old_subtopic TEXT,
                    old_source TEXT,
                    new_main_topic TEXT NOT NULL,
                    new_subtopic TEXT NOT NULL,
                    PRIMARY KEY (run_id, bucket_id)
                )
                """
            )

    @staticmethod
    def tree() -> list[dict]:
        return [
            {"main_topic": main, "subtopics": list(subtopics)}
            for main, subtopics in TOPIC_TREE.items()
        ]

    def _assign_sync(
        self, bucket_id: str, main_topic: str, subtopic: str, source: str
    ) -> dict:
        main, sub = validate_topic(main_topic, subtopic)
        stamp = now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO topic_assignments (
                    bucket_id, main_topic, subtopic, source, assigned_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(bucket_id) DO UPDATE SET
                    main_topic=excluded.main_topic,
                    subtopic=excluded.subtopic,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (str(bucket_id), main, sub, str(source)[:20], stamp, stamp),
            )
            row = connection.execute(
                "SELECT * FROM topic_assignments WHERE bucket_id=?", (str(bucket_id),)
            ).fetchone()
        return dict(row)

    async def assign(
        self, bucket_id: str, main_topic: str, subtopic: str, source: str = "manual"
    ) -> dict:
        return await asyncio.to_thread(
            self._assign_sync, bucket_id, main_topic, subtopic, source
        )

    def _get_sync(self, bucket_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM topic_assignments WHERE bucket_id=?", (str(bucket_id),)
            ).fetchone()
        return dict(row) if row else None

    async def get(self, bucket_id: str) -> dict | None:
        return await asyncio.to_thread(self._get_sync, bucket_id)

    def _remove_sync(self, bucket_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM topic_assignments WHERE bucket_id=?", (str(bucket_id),)
            )
        return cursor.rowcount > 0

    async def remove(self, bucket_id: str) -> bool:
        return await asyncio.to_thread(self._remove_sync, bucket_id)

    def _list_sync(self, main_topic: str = "", subtopic: str = "") -> list[dict]:
        query = "SELECT * FROM topic_assignments"
        params = []
        clauses = []
        if main_topic:
            clauses.append("main_topic=?")
            params.append(main_topic)
        if subtopic:
            clauses.append("subtopic=?")
            params.append(subtopic)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY main_topic, subtopic, assigned_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    async def list(self, main_topic: str = "", subtopic: str = "") -> list[dict]:
        return await asyncio.to_thread(self._list_sync, main_topic, subtopic)

    def _bulk_assign_sync(self, items: list[dict]) -> dict:
        prepared = []
        for item in items:
            main, sub = validate_topic(item.get("main_topic", ""), item.get("subtopic", ""))
            bucket_id = str(item.get("bucket_id", "")).strip()
            if bucket_id:
                prepared.append((bucket_id, main, sub))
        if not prepared:
            return {"run_id": "", "applied": 0, "skipped": 0}
        run_id = uuid.uuid4().hex[:16]
        stamp = now_iso()
        applied = 0
        skipped = 0
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO topic_bulk_runs(run_id, created_at) VALUES (?, ?)",
                (run_id, stamp),
            )
            for bucket_id, main, sub in prepared:
                old = connection.execute(
                    "SELECT 1 FROM topic_assignments WHERE bucket_id=?", (bucket_id,)
                ).fetchone()
                if old:
                    skipped += 1
                    continue
                connection.execute(
                    """
                    INSERT INTO topic_bulk_changes (
                        run_id, bucket_id, old_main_topic, old_subtopic, old_source,
                        new_main_topic, new_subtopic
                    ) VALUES (?, ?, NULL, NULL, NULL, ?, ?)
                    """,
                    (run_id, bucket_id, main, sub),
                )
                connection.execute(
                    """
                    INSERT INTO topic_assignments (
                        bucket_id, main_topic, subtopic, source, assigned_at, updated_at
                    ) VALUES (?, ?, ?, 'bulk', ?, ?)
                    """,
                    (bucket_id, main, sub, stamp, stamp),
                )
                applied += 1
        return {"run_id": run_id, "applied": applied, "skipped": skipped}

    async def bulk_assign(self, items: list[dict]) -> dict:
        return await asyncio.to_thread(self._bulk_assign_sync, items)

    def _undo_last_bulk_sync(self) -> dict:
        stamp = now_iso()
        with self._connect() as connection:
            run = connection.execute(
                "SELECT * FROM topic_bulk_runs WHERE undone_at IS NULL "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if not run:
                return {"run_id": "", "restored": 0}
            changes = connection.execute(
                "SELECT * FROM topic_bulk_changes WHERE run_id=?", (run["run_id"],)
            ).fetchall()
            for change in changes:
                connection.execute(
                    "DELETE FROM topic_assignments WHERE bucket_id=? AND source='bulk'",
                    (change["bucket_id"],),
                )
            connection.execute(
                "UPDATE topic_bulk_runs SET undone_at=? WHERE run_id=?",
                (stamp, run["run_id"]),
            )
        return {"run_id": run["run_id"], "restored": len(changes)}

    async def undo_last_bulk(self) -> dict:
        return await asyncio.to_thread(self._undo_last_bulk_sync)

    async def auto_assign(
        self, bucket_id: str, title: str, content: str, metadata: dict | None = None
    ) -> dict:
        existing = await self.get(bucket_id)
        if existing:
            return {"status": "existing", "assignment": existing}
        suggestion = suggest_topic(title, content, metadata)
        if not suggestion["main_topic"]:
            return {"status": "unassigned", "suggestion": suggestion}
        assignment = await self.assign(
            bucket_id,
            suggestion["main_topic"],
            suggestion["subtopic"],
            source="auto",
        )
        return {"status": "assigned", "assignment": assignment, "suggestion": suggestion}
