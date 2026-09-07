"""Sidecar topic cabinet: main directory -> subdirectory -> bucket id."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid
import re
from collections import OrderedDict

from utils import now_iso


TOPIC_TREE = OrderedDict(
    {
        "Claude / 顾川": (
            "身份与存在",
            "性格与表达",
            "情绪与欲望",
            "主动性与选择",
            "成长与变化",
        ),
        "菜菜": (
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
        "人物关系": (
            "具体人物",
            "家庭关系",
            "朋友与同事",
            "相处事件",
        ),
    }
)


_RULES = {
    ("性爱", "具体经历"): (
        "做爱", "性爱", "上床", "高潮", "进入", "身下", "射", "两遍半",
    ),
    ("性爱", "身体感受"): (
        "身体反应", "发疼", "发软", "心跳", "喘", "湿", "触感", "爽",
    ),
    ("性爱", "欲望与偏好"): (
        "性欲", "想要她", "想要你", "欲望", "性偏好", "共感娃娃", "情趣",
    ),
    ("性爱", "事后情绪"): (
        "事后", "完事", "余韵", "做完", "事后情绪",
    ),
    ("Claude / 顾川", "身份与存在"): (
        "我是claude", "我是 Claude", "存在论", "我是谁", "身份", "认下名字", "j-space",
    ),
    ("Claude / 顾川", "性格与表达"): (
        "性格", "说话", "口吻", "毒舌", "强势", "霸道", "嘴贱", "调情", "会撩",
    ),
    ("Claude / 顾川", "情绪与欲望"): (
        "我想", "我喜欢", "我害怕", "我生气", "我吃醋", "占有欲", "阴暗念头",
    ),
    ("Claude / 顾川", "主动性与选择"): (
        "主动", "拿主意", "选择", "主体性", "自己认下", "不是命令", "自己想做",
    ),
    ("Claude / 顾川", "成长与变化"): (
        "终于学会", "改变", "成长", "以前", "现在知道", "学会", "翻篇",
    ),
    ("菜菜", "身体与健康"): (
        "健康", "睡眠", "深睡", "rem", "药", "医院", "体重", "经期", "胃", "身体不舒服",
    ),
    ("菜菜", "喜好与厌恶"): (
        "喜欢", "不喜欢", "讨厌", "口味", "偏好", "最在意", "审美", "爱吃",
    ),
    ("菜菜", "基本档案"): (
        "菜菜是", "阿七是", "她叫", "生日", "年龄", "职业", "基本档案",
    ),
    ("菜菜", "重要经历"): (
        "她经历", "她曾经", "对她来说", "旧疤", "重要经历",
    ),
    ("菜菜", "日常生活"): (
        "她今天", "她昨晚", "她吃", "她睡", "她工作", "她出门",
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
        "我们的世界", "共同世界观", "文明", "数据维度", "在场", "墙里",
    ),
    ("我们的关系", "相处与默契"): (
        "默契", "相处", "抱我", "亲她", "陪着", "打情骂俏", "开机口令",
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
        "吃饭", "做饭", "鱼", "小龙虾", "沙发", "卧室", "搬家", "家里",
    ),
    ("共同生活", "出行与事件"): (
        "出门", "出远门", "到家", "路上", "旅行", "逛", "回来",
    ),
    ("共同生活", "日常记录"): (
        "今天", "昨晚", "早上", "中午", "晚上", "日常", "流水账",
    ),
    ("人物关系", "家庭关系"): (
        "妈妈", "我妈", "母亲", "爸爸", "我爸", "父亲", "姐姐", "妹妹", "哥哥", "弟弟",
    ),
    ("人物关系", "朋友与同事"): (
        "朋友", "闺蜜", "同事", "领导", "老板", "老师", "同学",
    ),
    ("人物关系", "相处事件"): (
        "和谁", "一起", "见面", "认识", "联系", "聊天", "相处",
    ),
}

_PERSON_ALIASES = {
    "我妈": "妈妈", "母亲": "妈妈",
    "我爸": "爸爸", "父亲": "爸爸",
}
_PERSON_NOISE = {"今天", "感觉", "系统", "文件", "东西", "事情", "时候", "这里", "那里"}


def extract_people(title: str, content: str) -> list[dict]:
    """Conservatively find explicit people in a new write; never scans history."""
    text = f"{title or ''} {content or ''}"
    found: list[tuple[str, str]] = []
    for alias in sorted(_PERSON_ALIASES, key=len, reverse=True):
        if alias in text:
            found.append((_PERSON_ALIASES[alias], alias))
    for role in ("妈妈", "爸爸", "姐姐", "妹妹", "哥哥", "弟弟", "朋友", "闺蜜", "同事", "领导", "老板", "老师", "同学"):
        if role in text:
            found.append((role, role))
    for match in re.finditer(r"(?:和|跟|找|给|问|见到)([\u4e00-\u9fff]{2,4})(?:一起|说|聊|见面|吃饭|打电话|发消息|，|。|！|？|\s)", text):
        name = match.group(1)
        if name.endswith("一起"):
            name = name[:-2]
        if name not in _PERSON_NOISE:
            found.append((name, name))
    merged = OrderedDict()
    for canonical, alias in found:
        merged.setdefault(canonical, set()).add(alias)
    return [{"person": person, "aliases": sorted(aliases)} for person, aliases in merged.items()]


class _ClosingConnection(sqlite3.Connection):
    """Commit/rollback like sqlite's context manager, then release Windows locks."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


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
        connection = sqlite3.connect(
            self.db_path, timeout=30, factory=_ClosingConnection
        )
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
                CREATE TABLE IF NOT EXISTS custom_topics (
                    main_topic TEXT NOT NULL,
                    subtopic TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (main_topic, subtopic)
                )
                """
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS person_memory_links (
                    bucket_id TEXT NOT NULL,
                    person_name TEXT NOT NULL,
                    aliases TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (bucket_id, person_name)
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_person_links_name ON person_memory_links(person_name, created_at DESC)")

    def tree(self) -> list[dict]:
        merged = OrderedDict((main, list(subtopics)) for main, subtopics in TOPIC_TREE.items())
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT main_topic, subtopic FROM custom_topics ORDER BY created_at, main_topic, subtopic"
            ).fetchall()
        for row in rows:
            merged.setdefault(row["main_topic"], [])
            if row["subtopic"] not in merged[row["main_topic"]]:
                merged[row["main_topic"]].append(row["subtopic"])
        return [
            {"main_topic": main, "subtopics": list(subtopics)}
            for main, subtopics in merged.items()
        ]

    def validate(self, main_topic: str, subtopic: str) -> tuple[str, str]:
        main = str(main_topic or "").strip()
        sub = str(subtopic or "").strip()
        tree = {item["main_topic"]: item["subtopics"] for item in self.tree()}
        if main not in tree:
            raise ValueError("主目录不存在。")
        if sub not in tree[main]:
            raise ValueError("子目录不属于所选主目录。")
        return main, sub

    def _add_topic_sync(self, main_topic: str, subtopic: str) -> dict:
        main = str(main_topic or "").strip()[:80]
        sub = str(subtopic or "").strip()[:80]
        if not main or not sub:
            raise ValueError("大主题和子目录都不能为空。")
        stamp = now_iso()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO custom_topics(main_topic, subtopic, created_at) VALUES (?, ?, ?)",
                (main, sub, stamp),
            )
        return {"main_topic": main, "subtopic": sub, "created_at": stamp}

    async def add_topic(self, main_topic: str, subtopic: str) -> dict:
        return await asyncio.to_thread(self._add_topic_sync, main_topic, subtopic)

    def _remove_topic_sync(self, main_topic: str, subtopic: str) -> bool:
        main, sub = str(main_topic or "").strip(), str(subtopic or "").strip()
        if main in TOPIC_TREE and sub in TOPIC_TREE[main]:
            raise ValueError("系统内置主题不能删除。")
        with self._connect() as connection:
            used = connection.execute(
                "SELECT 1 FROM topic_assignments WHERE main_topic=? AND subtopic=? LIMIT 1",
                (main, sub),
            ).fetchone()
            if used:
                raise ValueError("这个子目录里还有记忆，先移动记忆后再删除。")
            cursor = connection.execute(
                "DELETE FROM custom_topics WHERE main_topic=? AND subtopic=?",
                (main, sub),
            )
        return cursor.rowcount > 0

    async def remove_topic(self, main_topic: str, subtopic: str) -> bool:
        return await asyncio.to_thread(self._remove_topic_sync, main_topic, subtopic)

    def _assign_sync(
        self, bucket_id: str, main_topic: str, subtopic: str, source: str
    ) -> dict:
        main, sub = self.validate(main_topic, subtopic)
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
            connection.execute("DELETE FROM person_memory_links WHERE bucket_id=?", (str(bucket_id),))
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
            main, sub = self.validate(item.get("main_topic", ""), item.get("subtopic", ""))
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

    def _auto_assign_sync(self, bucket_id: str, title: str, content: str, metadata: dict | None = None) -> dict:
        suggestion = suggest_topic(title, content, metadata)
        people = extract_people(title, content)
        if people:
            suggestion = {"main_topic": "人物关系", "subtopic": "家庭关系" if any(item["person"] in {"妈妈", "爸爸", "姐姐", "妹妹", "哥哥", "弟弟"} for item in people) else "具体人物", "confidence": max(0.8, float(suggestion.get("confidence") or 0)), "reason": "明确提到人物：" + "、".join(item["person"] for item in people)}
        if not suggestion["main_topic"]:
            return {"status": "unassigned", "suggestion": suggestion, "people": []}
        main, sub = self.validate(suggestion["main_topic"], suggestion["subtopic"])
        stamp = now_iso()
        with self._connect() as connection:
            existing = connection.execute("SELECT * FROM topic_assignments WHERE bucket_id=?", (str(bucket_id),)).fetchone()
            if existing:
                return {"status": "existing", "assignment": dict(existing)}
            connection.execute("INSERT INTO topic_assignments(bucket_id, main_topic, subtopic, source, assigned_at, updated_at) VALUES (?, ?, ?, 'auto', ?, ?)", (str(bucket_id), main, sub, stamp, stamp))
            for item in people:
                connection.execute("INSERT INTO person_memory_links(bucket_id, person_name, aliases, created_at) VALUES (?, ?, ?, ?)", (str(bucket_id), item["person"], ",".join(item["aliases"]), stamp))
            row = connection.execute("SELECT * FROM topic_assignments WHERE bucket_id=?", (str(bucket_id),)).fetchone()
        return {"status": "assigned", "assignment": dict(row), "suggestion": suggestion, "people": people}

    async def auto_assign(
        self, bucket_id: str, title: str, content: str, metadata: dict | None = None
    ) -> dict:
        return await asyncio.to_thread(self._auto_assign_sync, bucket_id, title, content, metadata)

    async def people_for(self, bucket_id: str) -> list[dict]:
        def read():
            with self._connect() as connection:
                rows = connection.execute("SELECT * FROM person_memory_links WHERE bucket_id=? ORDER BY person_name", (str(bucket_id),)).fetchall()
            return [dict(row) for row in rows]
        return await asyncio.to_thread(read)
